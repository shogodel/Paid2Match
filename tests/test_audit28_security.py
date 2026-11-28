"""Audit 2026-11-28 — security slice (tips + paid communities).

Regression tests for the paid-space access-control holes found in
this audit:

* the notifications route minted a FREE membership in a paid space
  (any logged-in user could read every paid post without paying);
* paid-space posts leaked into the public feeds and search for
  strangers (post_detail gated the single view, the queries did not);
* the comments JSON endpoint, the like/comment/share/vote/bookmark
  interaction routes, and the saved page served paid-space content
  (or let strangers interact with it) without a membership check.

Also covers the new-code security surface that checked out clean
(tip amount/recipient tampering, webhook signature on the new
branches, tip-message escaping, admin-only space pricing) so a
regression there is caught too. All Stripe calls are mocked — no
network calls, no real keys, no real money movement.
"""
from unittest.mock import patch

from extensions import db
from models import Space, SpaceMembership
from models.poll import create_poll
from models.post import Comment, Post
from models.space_payout import SpacePayout
from models.tip import Tip
from tests.test_m3_webhook import _set_stripe_keys
from tests.test_paid_spaces import (
    _make_membership,
    _make_post,
    _make_space,
)


def _login(client, login_as, user):
    login_as(client, user.email)


class TestNotificationsRouteCannotJoinPaidSpaceFree:
    def test_non_member_gets_no_membership(
            self, app, client, sample_user, second_user, third_user,
            login_as):
        """POST notifications on a paid space must not create a
        membership — the old code created a free one (no Stripe
        subscription), which made can_view() true."""
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_post(app, sample_user, space)
        _login(client, login_as, third_user)
        resp = client.post(
            f'/social/spaces/{space.slug}/notifications',
            data={'level': 'all'},
        )
        assert resp.status_code == 302
        assert SpaceMembership.query.filter_by(
            user_id=third_user.id, space_id=space.id,
        ).first() is None
        # And the pitch page still shows — no free read access.
        resp = client.get(f'/social/spaces/{space.slug}')
        assert resp.status_code == 200
        assert b'Secret members-only post body.' not in resp.data

    def test_lapsed_member_cannot_rejoin_free_via_notifications(
            self, app, client, sample_user, second_user, login_as):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        membership = _make_membership(
            second_user, space, status='canceled')
        _login(client, login_as, second_user)
        resp = client.post(
            f'/social/spaces/{space.slug}/notifications',
            data={'level': 'all'},
        )
        assert resp.status_code == 302
        assert SpaceMembership.query.filter_by(
            user_id=second_user.id, space_id=space.id,
        ).count() == 1
        # The lapsed membership was NOT silently reactivated.
        assert db.session.get(
            SpaceMembership, membership.id).status == 'canceled'

    def test_active_member_can_still_set_level(
            self, app, client, sample_user, second_user, login_as):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_membership(second_user, space)
        _login(client, login_as, second_user)
        resp = client.post(
            f'/social/spaces/{space.slug}/notifications',
            data={'level': 'muted'},
        )
        assert resp.status_code == 302
        membership = SpaceMembership.query.filter_by(
            user_id=second_user.id, space_id=space.id,
        ).first()
        assert membership.notification_level == 'muted'


class TestPaidPostsHiddenFromFeedsAndSearch:
    def test_latest_feed_hides_paid_post_from_stranger(
            self, app, client, sample_user, second_user, third_user,
            login_as):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_post(app, sample_user, space)
        _make_membership(second_user, space)
        _login(client, login_as, third_user)
        resp = client.get('/social/feed?tab=latest')
        assert resp.status_code == 200
        assert b'Secret members-only post body.' not in resp.data
        _login(client, login_as, second_user)
        resp = client.get('/social/feed?tab=latest')
        assert b'Secret members-only post body.' in resp.data

    def test_search_hides_paid_post_from_stranger(
            self, app, client, sample_user, second_user, third_user,
            login_as):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_post(app, sample_user, space)
        _make_membership(second_user, space)
        _login(client, login_as, third_user)
        resp = client.get('/social/search?q=Secret')
        assert resp.status_code == 200
        assert b'Secret members-only post body.' not in resp.data
        _login(client, login_as, second_user)
        resp = client.get('/social/search?q=Secret')
        assert b'Secret members-only post body.' in resp.data

    def test_share_of_paid_post_does_not_leak_into_latest_feed(
            self, app, client, sample_user, second_user, third_user,
            login_as):
        """A member sharing a paid post must not carry it into a
        stranger's Latest feed via the shares query."""
        from models.post import Share
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        post = _make_post(app, sample_user, space)
        _make_membership(second_user, space)
        db.session.add(Share(user_id=second_user.id, post_id=post.id))
        db.session.commit()
        _login(client, login_as, third_user)
        resp = client.get('/social/feed?tab=latest')
        assert resp.status_code == 200
        assert b'Secret members-only post body.' not in resp.data


class TestPaidPostInteractionsGated:
    def _paid_post(self, app, sample_user, second_user):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        post = _make_post(app, sample_user, space)
        _make_membership(second_user, space)
        return space, post

    def test_comments_json_blocked_for_stranger(
            self, app, client, sample_user, second_user, third_user,
            login_as):
        _, post = self._paid_post(app, sample_user, second_user)
        db.session.add(Comment(
            author_id=sample_user.id, post_id=post.id,
            body='Members-only comment text.',
        ))
        db.session.commit()
        _login(client, login_as, third_user)
        resp = client.get(f'/social/posts/{post.id}/comments')
        assert resp.status_code == 404
        _login(client, login_as, second_user)
        resp = client.get(f'/social/posts/{post.id}/comments')
        assert resp.status_code == 200
        assert b'Members-only comment text.' in resp.data

    def test_like_comment_share_vote_bookmark_blocked_for_stranger(
            self, app, client, sample_user, second_user, third_user,
            login_as):
        _, post = self._paid_post(app, sample_user, second_user)
        poll = create_poll(post, ['Option A', 'Option B'])
        db.session.commit()
        _login(client, login_as, third_user)
        assert client.post(
            f'/social/posts/{post.id}/like').status_code == 404
        assert client.post(
            f'/social/posts/{post.id}/comment',
            data={'body': 'Trying to comment.'}).status_code == 404
        assert client.post(
            f'/social/posts/{post.id}/share',
            data={'comment': 'Trying to share.'}).status_code == 404
        assert client.post(
            f'/social/polls/{poll.id}/vote',
            data={'option_id': poll.options[0].id}).status_code == 404
        assert client.post(
            f'/social/posts/{post.id}/save').status_code == 404
        assert Comment.query.filter_by(post_id=post.id).count() == 0

    def test_member_can_still_interact(
            self, app, client, sample_user, second_user, login_as):
        _, post = self._paid_post(app, sample_user, second_user)
        _login(client, login_as, second_user)
        assert client.post(
            f'/social/posts/{post.id}/like').status_code in (200, 302)
        resp = client.post(
            f'/social/posts/{post.id}/comment',
            data={'body': 'Member comment.'},
        )
        assert resp.status_code in (200, 302)
        assert Comment.query.filter_by(
            post_id=post.id, author_id=second_user.id).count() == 1

    def test_saved_page_hides_lapsed_paid_post(
            self, app, client, sample_user, second_user, login_as):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        post = _make_post(app, sample_user, space)
        membership = _make_membership(second_user, space)
        _login(client, login_as, second_user)
        assert client.post(
            f'/social/posts/{post.id}/save').status_code in (200, 302)
        resp = client.get('/social/saved')
        assert b'Secret members-only post body.' in resp.data
        membership.status = 'canceled'
        db.session.commit()
        resp = client.get('/social/saved')
        assert resp.status_code == 200
        assert b'Secret members-only post body.' not in resp.data


class TestTipTamperingAndAuth:
    def test_negative_amount_rejected_no_tip_created(
            self, app, client, sample_user, sample_hunter, login_as):
        _login(client, login_as, sample_user)
        resp = client.post(
            f'/tips/create/{sample_hunter.id}',
            data={'amount': '-5', 'currency': 'CAD'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert Tip.query.count() == 0

    def test_post_id_of_someone_elses_post_does_not_redirect_there(
            self, app, client, sample_user, sample_hunter, login_as):
        """The return target only uses post_id when the post really
        belongs to the recipient — a tampered post_id cannot steer
        the tipper (or the flow) onto another user's post."""
        _set_stripe_keys()
        other_post = Post(
            author_id=sample_user.id, post_type='update',
            body='Not the recipient post.',
        )
        db.session.add(other_post)
        db.session.commit()
        _login(client, login_as, sample_user)
        with patch('stripe.checkout.Session.create',
                   return_value=type('S', (), {
                       'url': 'https://checkout.test/tip'})()):
            resp = client.post(
                f'/tips/create/{sample_hunter.id}',
                data={'amount': '5', 'currency': 'CAD',
                      'post_id': other_post.id},
            )
        assert resp.status_code == 302
        assert f'/social/posts/{other_post.id}' not in (
            resp.headers['Location'] or '')
        tip = Tip.query.filter_by(recipient_id=sample_hunter.id).first()
        assert tip is not None and tip.amount_cents == 500

    def test_anonymous_tip_picker_redirects_to_login(
            self, app, client, sample_hunter):
        resp = client.get(f'/tips/new/{sample_hunter.id}')
        assert resp.status_code == 302
        assert '/auth/login' in resp.headers['Location']


class TestWebhookSignatureOnNewBranches:
    def test_unsigned_forged_tip_checkout_rejected(
            self, app, client, sample_user, sample_hunter):
        """A forged checkout.session.completed for a tip, sent with
        no Stripe-Signature header, must be refused before any
        fulfill handler runs — the tip stays pending."""
        _set_stripe_keys()
        tip = Tip(
            tipper_id=sample_user.id, recipient_id=sample_hunter.id,
            amount_cents=500, currency='CAD', status='pending',
        )
        db.session.add(tip)
        db.session.commit()
        resp = client.post(
            '/bounties/stripe-webhook',
            data=b'{"id": "evt_forged", "type": "checkout.session.completed", '
                 b'"data": {"object": {"id": "cs_forged", "metadata": '
                 b'{"m3_kind": "tip", "tip_id": "%s"}, '
                 b'"payment_intent": "pi_forged"}}}' % tip.id.encode(),
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert db.session.get(Tip, tip.id).status == 'pending'
        assert db.session.get(
            Tip, tip.id).stripe_payment_intent_id is None

    def test_unsigned_forged_invoice_paid_rejected(
            self, app, client, sample_user, second_user):
        """Same for the new invoice.paid branch — no signature, no
        owner payout row."""
        _set_stripe_keys()
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        _make_membership(second_user, space)
        resp = client.post(
            '/bounties/stripe-webhook',
            data=b'{"id": "evt_forged_inv", "type": "invoice.paid", '
                 b'"data": {"object": {"id": "in_forged", '
                 b'"subscription": "sub_space_1", "amount_paid": 1000, '
                 b'"currency": "cad", "charge": "ch_forged"}}}',
            content_type='application/json',
        )
        assert resp.status_code == 400
        assert SpacePayout.query.count() == 0


class TestTipMessageEscaping:
    def test_script_in_tip_message_is_escaped_in_admin_list(
            self, app, client, sample_user, sample_hunter, sample_admin,
            login_as):
        tip = Tip(
            tipper_id=sample_user.id, recipient_id=sample_hunter.id,
            amount_cents=500, currency='CAD', status='completed',
            message='<script>alert(1)</script>',
        )
        db.session.add(tip)
        db.session.commit()
        _login(client, login_as, sample_admin)
        resp = client.get('/admin/tips')
        assert resp.status_code == 200
        assert b'&lt;script&gt;alert(1)&lt;/script&gt;' in resp.data
        assert b'<script>alert(1)</script>' not in resp.data


class TestAdminSpacePricingIsAdminOnly:
    def test_non_admin_cannot_set_space_owner_or_price(
            self, app, client, sample_user, second_user, login_as):
        space = _make_space(owner=None, paid=False)
        _login(client, login_as, second_user)
        resp = client.post(
            f'/admin/spaces/{space.id}/edit',
            data={'owner_email': second_user.email, 'is_paid': 'on',
                  'price': '99'},
        )
        assert resp.status_code == 302
        fresh = db.session.get(Space, space.id)
        assert fresh.is_paid is False
        assert fresh.owner_id is None
        assert fresh.price_cents is None
