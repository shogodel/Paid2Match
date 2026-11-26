"""Audit 2026-11-25 (correctness workstream) regression tests.

1. Unsecured paid needs must not leak through notifications: the
   feeds/search/digest/sitemap all hide them until escrow, but the
   space-member and @mention notifications fired at creation time,
   handing the need's text to strangers. The audience hears about
   the need when the payment is secured (webhook), not before.
2. Compose-by-email lookup is case-insensitive; the user picker
   never returns deleted/deactivated accounts.
3. A thread URL whose bounty and pitch belong to different needs
   is a 404, not a thread filed under a nonsense context.
4. The message notification titles are wrapped for translation
   (FR/ES catalogs carry them).
"""
from unittest.mock import patch

from extensions import db
from models.bounty import Bounty
from models.notification import Notification
from models.pitch import Pitch
from models.post import Post
from models.space import Space, SpaceMembership
from models.user import User

JSON_HEADERS = {'X-Requested-With': 'XMLHttpRequest'}


def _space_with_member(app, member_id):
    """A space with member_id on notification level 'all'. Returns
    the space id (the row is detached once the context closes)."""
    with app.app_context():
        space = Space(
            slug='tech', name='Tech & Software', description='Seeded space.')
        db.session.add(space)
        db.session.flush()
        db.session.add(SpaceMembership(
            user_id=member_id, space_id=space.id,
            notification_level='all'))
        db.session.commit()
        return space.id


def _need_post_data(space_id, body):
    return {
        'post_type': 'need_paid',
        'title': 'Senior Python Engineer Needed',
        'body': body,
        'reward_amount': '2000',
        'space_id': space_id,
    }


class TestUnsecuredNeedNotifications:
    def test_unsecured_paid_need_notifies_nobody(
            self, app, client, logged_in_user, sample_user, second_user):
        """A paid need posted in a space, @mentioning a user, sends no
        space_post or mention notification while the payment is
        unsecured — the need is not public yet."""
        space_id = _space_with_member(app, second_user.id)
        resp = client.post(
            '/social/posts/new',
            data=_need_post_data(
                space_id, 'Shout-out to @Second — need a Python dev!'),
            headers=JSON_HEADERS)
        assert resp.status_code == 200
        with app.app_context():
            post = Post.query.filter_by(
                author_id=sample_user.id, post_type='need_paid').first()
            assert post is not None
            assert post.bounty is not None
            assert post.bounty.payment_status == 'unsecured'
            assert Notification.query.filter_by(
                recipient_id=second_user.id, type='space_post').first() is None
            assert Notification.query.filter_by(
                recipient_id=second_user.id, type='mention').first() is None

    def test_free_need_notifies_space_members(
            self, app, client, logged_in_user, sample_user, second_user):
        """The gate is about publicity, not about needs: a free need
        is public at once, so its space members are notified."""
        space_id = _space_with_member(app, second_user.id)
        resp = client.post(
            '/social/posts/new',
            data={
                'post_type': 'need_free',
                'title': 'Community referral for a friend',
                'body': 'Free community referral, come help out.',
                'space_id': space_id,
            },
            headers=JSON_HEADERS)
        assert resp.status_code == 200
        with app.app_context():
            assert Notification.query.filter_by(
                recipient_id=second_user.id, type='space_post').first() is not None

    def test_comment_mention_on_unsecured_need_notifies_nobody(
            self, app, client, logged_in_user, sample_user, second_user):
        """The author can comment on their own unsecured need post;
        an @mention in that comment must not reach the mentioned
        user before escrow."""
        space_id = _space_with_member(app, second_user.id)
        resp = client.post(
            '/social/posts/new',
            data=_need_post_data(space_id, 'Need a Python dev, ASAP.'),
            headers=JSON_HEADERS)
        assert resp.status_code == 200
        with app.app_context():
            post = Post.query.filter_by(
                author_id=sample_user.id, post_type='need_paid').first()
            post_id = post.id
        resp = client.post(
            f'/social/posts/{post_id}/comment',
            data={'body': '@Second any ideas?'},
            headers=JSON_HEADERS)
        assert resp.status_code == 200
        with app.app_context():
            assert Notification.query.filter_by(
                recipient_id=second_user.id, type='mention').first() is None

    def test_webhook_secure_notifies_audience(
            self, app, client, logged_in_user, sample_user, second_user):
        """When the escrow lands, the need goes public — and only
        then do the space member and the @mentioned user hear about
        it (plus the poster gets the payment_secured notice)."""
        from models.admin_settings import AdminSettings
        space_id = _space_with_member(app, second_user.id)
        resp = client.post(
            '/social/posts/new',
            data=_need_post_data(
                space_id, 'Shout-out to @Second — need a Python dev!'),
            headers=JSON_HEADERS)
        assert resp.status_code == 200
        with app.app_context():
            bounty = Bounty.query.filter_by(
                poster_id=sample_user.id, payment_status='unsecured').first()
            assert bounty is not None
            bounty_id = bounty.id
            AdminSettings.set('STRIPE_SECRET_KEY', 'sk_test_fake')
            AdminSettings.set('STRIPE_PUBLISHABLE_KEY', 'pk_test_fake')
            AdminSettings.set('STRIPE_WEBHOOK_SECRET', 'whsec_fake')
            db.session.commit()
        event = {
            'id': 'evt_secure_audit25',
            'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_secure_audit25',
                'metadata': {'bounty_id': bounty_id},
            }},
        }
        with patch('stripe.Webhook.construct_event', return_value=event):
            resp = client.post(
                '/bounties/stripe-webhook', data=b'{}',
                headers={'Stripe-Signature': 't=1,v1=fake'})
        assert resp.status_code == 200
        with app.app_context():
            assert db.session.get(Bounty, bounty_id).payment_status == 'secured'
            assert Notification.query.filter_by(
                recipient_id=sample_user.id,
                type='payment_secured').first() is not None
            assert Notification.query.filter_by(
                recipient_id=second_user.id, type='space_post').first() is not None
            assert Notification.query.filter_by(
                recipient_id=second_user.id, type='mention').first() is not None


class TestMessagesComposeAndPicker:
    def test_compose_email_lookup_is_case_insensitive(
            self, app, client, logged_in_user, sample_user):
        """Compose by email with a different case than the stored
        address still finds the recipient (like the OAuth callback)."""
        with app.app_context():
            mixed = User(
                email='MixedCase@Example.com', full_name='Mixed Case',
                role='user', is_active=True, email_verified=True)
            db.session.add(mixed)
            db.session.commit()
        resp = client.post(
            '/messages/compose',
            data={'recipient_email': 'mixedcase@example.com',
                  'content': 'Hello, mixed case!'},
            follow_redirects=False)
        assert resp.status_code == 302
        assert '/messages/sent' in resp.headers['Location']

    def test_compose_email_lookup_trims_whitespace(
            self, app, client, logged_in_user, sample_user, second_user):
        resp = client.post(
            '/messages/compose',
            data={'recipient_email': '  second@example.com  ',
                  'content': 'Hello with spaces!'},
            follow_redirects=False)
        assert resp.status_code == 302
        assert '/messages/sent' in resp.headers['Location']

    def test_api_users_excludes_deleted_accounts(
            self, app, client, logged_in_user, sample_user, second_user):
        """A soft-deleted user keeps a placeholder email/name — the
        picker must not offer them as someone to message."""
        with app.app_context():
            target = db.session.get(User, second_user.id)
            target.soft_delete()
            db.session.commit()
        resp = client.get('/messages/api/users?q=Deleted')
        assert resp.status_code == 200
        assert resp.get_json() == []
        resp = client.get('/messages/api/users?q=second')
        assert resp.status_code == 200
        assert all(u['id'] != second_user.id for u in resp.get_json())


class TestNotifySkipsDeletedUsers:
    def test_notify_to_deleted_user_creates_nothing(
            self, app, second_user, sample_user):
        """A deleted account's email is a placeholder
        (deleted_<id>@example.com) and they can never log in again:
        notify() must create no notification row — and attempt no
        email to the fake address (message_new is an email type).

        The thread POST to a deleted user is refused upstream (400)
        by the messaging audit's fix, so this test exercises the
        notify() guard directly: other callers (likes, comments,
        matches) can still reach notify() with a deleted recipient.
        """
        from utils.notifications import notify
        with app.app_context():
            author = db.session.get(User, sample_user.id)
            author.soft_delete()
            db.session.commit()
            deleted_id = sample_user.id
            with patch(
                'utils.notifications.send_simple_email'
            ) as send_mail:
                result = notify(
                    deleted_id, 'message_new',
                    'New message from Second',
                    'Hello, are you there?',
                    actor_id=second_user.id,
                    link_url='/messages/',
                )
                db.session.commit()
            assert result is None
            assert Notification.query.filter_by(
                recipient_id=deleted_id).first() is None
        send_mail.assert_not_called()

    def test_thread_post_to_deleted_user_refused(
            self, app, client, login_as, second_user, sample_user):
        """Upstream of notify(): a NEW message to a deleted account
        is refused (400 JSON) — the messaging audit's fix. History
        with the account stays readable; only new sends are blocked.
        """
        with app.app_context():
            author = db.session.get(User, sample_user.id)
            author.soft_delete()
            db.session.commit()
            deleted_id = sample_user.id
        login_as(client, 'second@example.com', 'SecondPass123!')
        resp = client.post(
            f'/messages/thread/{deleted_id}',
            data={'content': 'Hello, are you there?'},
            headers=JSON_HEADERS)
        assert resp.status_code == 400


class TestThreadContext:
    def test_mismatched_bounty_and_pitch_is_404(
            self, app, client, logged_in_user, sample_user, second_user,
            sample_bounty):
        """A thread URL whose pitch belongs to a different need than
        its bounty_id is not a real referral context — 404."""
        with app.app_context():
            other_bounty = Bounty(
                poster_id=sample_user.id,
                title='A completely different need here',
                description='Another need entirely.',
                bounty_type='recruitment',
                bounty_direction='seeking_opportunity',
                status='open', payment_status='released')
            db.session.add(other_bounty)
            db.session.flush()
            pitch = Pitch(
                bounty_id=sample_bounty.id, hunter_id=second_user.id,
                candidate_teaser='Strong Python engineer, 8 years.')
            db.session.add(pitch)
            db.session.commit()
            pitch_id, other_id = pitch.id, other_bounty.id
        resp = client.get(
            f'/messages/thread/{second_user.id}'
            f'?bounty_id={other_id}&pitch_id={pitch_id}')
        assert resp.status_code == 404


class TestMessageNotificationTitlesTranslated:
    def test_message_titles_in_fr_and_es_catalogs(self):
        """The three message notification titles are wrapped for
        translation — both catalogs carry a real translation."""
        from babel.messages.pofile import read_po
        titles = [
            '%(name)s replied to your message',
            'New message from %(name)s',
            '%(name)s messaged you about a referral',
        ]
        for lang in ('fr', 'es'):
            with open(
                f'translations/{lang}/LC_MESSAGES/messages.po',
                encoding='utf-8',
            ) as f:
                catalog = {m.id: m.string for m in read_po(f) if m.id}
            for title in titles:
                assert title in catalog, f'{lang}: missing {title!r}'
                assert catalog[title], f'{lang}: untranslated {title!r}'
