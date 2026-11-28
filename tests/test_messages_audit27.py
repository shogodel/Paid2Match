"""Audit 2026-11-27 (messaging workstream) regression tests.

Each test below pins a bug found by this audit; they fail on the
pre-fix code. Bugs covered:

- Pitch-accept auto-message has no block check: accepting a pitch
  DMs the hunter even when the hunter blocked the poster (and even
  when the hunter's account is deleted/deactivated).
- GET /messages/api/conversations returns user emails (dead
  endpoint — no JS consumer — same class as the picker email leak
  fixed 2026-11-25).
- Referral-thread post has no deleted/deactivated-recipient check
  (thread POST and compose refuse; the referral post did not).
- Thread POST, compose and legacy reply accept messages to
  admin-deactivated (is_active=False, not deleted) recipients,
  while the picker hides those users.
- Mobile bottom-bar Messages badge is overwritten by the
  notifications poll: updateUnreadBadges() in static/js/main.js
  selects the bare `.tab-badge` class, which matches BOTH the
  Alerts badge and the Messages badge, so the 60s notifications
  poll rewrites the Messages badge with the notifications count
  (and hides it when the notifications count is 0).
"""
from pathlib import Path

from extensions import db
from models.message import Message
from models.moderation import Block
from models.notification import Notification
from models.pitch import Pitch
from models.referral import Referral


def _make_referral(app, referrer_id, bounty_id, pitch_id):
    with app.app_context():
        referral = Referral(
            referrer_id=referrer_id,
            bounty_id=bounty_id,
            pitch_id=pitch_id,
            referred_name='Candidate Name',
            referred_email='candidate@example.com',
            message='I would like to refer this candidate.',
            status='invited',
        )
        db.session.add(referral)
        db.session.commit()
        db.session.refresh(referral)
        return referral


def _make_message(app, sender_id, recipient_id, content):
    with app.app_context():
        msg = Message(
            sender_id=sender_id,
            recipient_id=recipient_id,
            content=content,
        )
        db.session.add(msg)
        db.session.commit()
        db.session.refresh(msg)
        return msg


class TestPitchAcceptAutoMessage:
    def test_blocked_hunter_gets_no_auto_message(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, sample_pitch):
        # The hunter blocked the poster — accepting the pitch must
        # not DM them anyway.
        with app.app_context():
            db.session.add(Block(
                blocker_id=sample_hunter.id,
                blocked_id=sample_user.id,
                kind='block'))
            db.session.commit()
        r = logged_in_user.post(
            f'/needs/pitches/{sample_pitch.id}/respond',
            data={'response': 'interested'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(Pitch, sample_pitch.id).status \
                == 'interested'
            # The pitch is accepted; only the auto-DM is skipped.
            assert Message.query.count() == 0

    def test_deleted_hunter_gets_no_auto_message(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, sample_pitch):
        with app.app_context():
            hunter = db.session.get(type(sample_hunter), sample_hunter.id)
            hunter.soft_delete()
            db.session.commit()
        r = logged_in_user.post(
            f'/needs/pitches/{sample_pitch.id}/respond',
            data={'response': 'interested'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(Pitch, sample_pitch.id).status \
                == 'interested'
            assert Message.query.count() == 0

    def test_deactivated_hunter_gets_no_auto_message(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, sample_pitch):
        with app.app_context():
            hunter = db.session.get(type(sample_hunter), sample_hunter.id)
            hunter.is_active = False
            db.session.commit()
        r = logged_in_user.post(
            f'/needs/pitches/{sample_pitch.id}/respond',
            data={'response': 'interested'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert Message.query.count() == 0

    def test_unblocked_hunter_still_gets_auto_message(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, sample_pitch):
        # Guard: the happy path still sends the acceptance DM.
        r = logged_in_user.post(
            f'/needs/pitches/{sample_pitch.id}/respond',
            data={'response': 'interested'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert Message.query.count() == 1


    def test_seen_shows_on_referral_page_itself(
            self, client, login_as, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        # read_at is set when the poster opens the referral page
        # (prior audit), but the page itself never showed the sender
        # a "Seen" receipt — only the /messages thread view did.
        referral = _make_referral(
            app, sample_hunter.id, sample_bounty.id, sample_pitch.id)
        login_as(client, 'hunter@example.com')
        r = client.post(
            f'/referrals/{referral.id}/message',
            data={'content': 'Referral thread hello'},
            follow_redirects=False)
        assert r.status_code == 302
        login_as(client, 'testuser@example.com')
        client.get(f'/referrals/{referral.id}')  # poster reads it
        login_as(client, 'hunter@example.com')
        r = client.get(f'/referrals/{referral.id}')
        assert r.status_code == 200
        assert b'Seen' in r.data


class TestApiConversationsNoEmail:
    def test_response_carries_no_email(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, sample_message):
        r = logged_in_user.get('/messages/api/conversations')
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list) and len(data) == 1
        assert 'email' not in data[0]
        assert data[0]['id'] == sample_hunter.id


class TestReferralPostRecipientChecks:
    def test_post_to_deleted_poster_refused(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        referral = _make_referral(
            app, sample_hunter.id, sample_bounty.id, sample_pitch.id)
        with app.app_context():
            poster = db.session.get(type(sample_user), sample_user.id)
            poster.soft_delete()
            db.session.commit()
        r = logged_in_hunter.post(
            f'/referrals/{referral.id}/message',
            data={'content': 'Hello?'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert Message.query.count() == 0

    def test_post_to_deactivated_poster_refused(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        referral = _make_referral(
            app, sample_hunter.id, sample_bounty.id, sample_pitch.id)
        with app.app_context():
            poster = db.session.get(type(sample_user), sample_user.id)
            poster.is_active = False
            db.session.commit()
        r = logged_in_hunter.post(
            f'/referrals/{referral.id}/message',
            data={'content': 'Hello?'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert Message.query.count() == 0


class TestDeactivatedRecipientRefused:
    def test_thread_post_to_deactivated_user_refused(
            self, logged_in_user, client, app,
            sample_user, sample_hunter):
        with app.app_context():
            hunter = db.session.get(type(sample_hunter), sample_hunter.id)
            hunter.is_active = False
            db.session.commit()
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': 'Hello?'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert Message.query.count() == 0

    def test_compose_to_deactivated_user_refused(
            self, logged_in_user, client, app,
            sample_user, sample_hunter):
        with app.app_context():
            hunter = db.session.get(type(sample_hunter), sample_hunter.id)
            hunter.is_active = False
            db.session.commit()
        r = logged_in_user.post(
            '/messages/compose',
            data={'recipient_email': 'hunter@example.com',
                  'content': 'Hello?'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert Message.query.count() == 0

    def test_legacy_reply_to_deactivated_sender_refused(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_message):
        # sample_message: sample_user -> hunter. Hunter replies;
        # the sender (sample_user) has since been deactivated.
        with app.app_context():
            sender = db.session.get(type(sample_user), sample_user.id)
            sender.is_active = False
            db.session.commit()
        r = logged_in_hunter.post(
            f'/messages/{sample_message.id}/reply',
            data={'content': 'Reply?'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            # Only the original message exists; no reply was created.
            assert Message.query.count() == 1


class TestMobileTabBadges:
    def test_messages_and_alerts_badges_have_distinct_ids(
            self, logged_in_user, client, app,
            sample_user, sample_hunter):
        # One unread message + one unread notification, so both
        # bottom-bar badges render.
        _make_message(
            app, sample_hunter.id, sample_user.id, 'Unread hello')
        with app.app_context():
            db.session.add(Notification(
                recipient_id=sample_user.id,
                type='like', title='Someone liked your post'))
            db.session.commit()
        r = logged_in_user.get('/messages/')
        assert r.status_code == 200
        body = r.data.decode()
        assert 'id="tab-notifications-badge"' in body
        assert 'id="tab-messages-badge"' in body

    def test_notification_poll_does_not_target_messages_badge(self):
        # The notifications poll's badge updater must address the
        # Alerts badge by id — a bare `.tab-badge` selector matches
        # the Messages badge too and rewrites it with the
        # notifications count.
        js = (Path(__file__).resolve().parent.parent
              / 'static' / 'js' / 'main.js').read_text()
        assert '#nav-notifications-badge, .tab-badge' not in js
        assert '#tab-notifications-badge' in js
