"""Audit-2026-11-25 (messaging workstream) regression tests.

Each test below pins a bug found by the audit; they fail on the
pre-fix code. Bugs covered:

- Referral detail marks messages read without read_at (no "Seen").
- Referral thread post has no block check (blocked user can DM the
  blocker through the referral thread).
- Pitch-only thread URLs split the referral thread (reply lands in
  (other, None, pitch) instead of (other, bounty, pitch)); a
  bounty/pitch mismatch creates an orphan-context thread.
- User picker: LIKE wildcards in q enumerate users (``%%`` matches
  everyone); deleted users leak (email + name) and can be messaged.
- Self-messaging: compose / thread POST to yourself creates a
  message and inflates your own unread badge.
- No maximum message length on any send path.
- Verification (held on the pre-fix code): XSS escaped in bubbles,
  polling cannot read another pair's thread, inbox search does not
  leak other users' threads, deleting the last message removes the
  thread cleanly.
"""
from werkzeug.security import generate_password_hash

from extensions import db
from models.bounty import Bounty
from models.message import Message
from models.moderation import Block
from models.pitch import Pitch
from models.referral import Referral
from models.user import User

AJAX = {
    'X-Requested-With': 'XMLHttpRequest',
    'Accept': 'application/json',
}


def _make_user(app, email, full_name, handle=None):
    with app.app_context():
        user = User(
            email=email,
            full_name=full_name,
            password_hash=generate_password_hash('Pass123!'),
            role='user',
            is_active=True,
            email_verified=True,
        )
        if handle:
            user.handle = handle
        db.session.add(user)
        db.session.commit()
        db.session.refresh(user)
        return user


def _make_message(app, sender_id, recipient_id, content,
                  bounty_id=None, pitch_id=None):
    with app.app_context():
        msg = Message(
            sender_id=sender_id,
            recipient_id=recipient_id,
            bounty_id=bounty_id,
            pitch_id=pitch_id,
            content=content,
        )
        db.session.add(msg)
        db.session.commit()
        db.session.refresh(msg)
        return msg


def _make_bounty(app, poster_id, title='Second Bounty For Audit'):
    with app.app_context():
        bounty = Bounty(
            poster_id=poster_id,
            title=title,
            description='A second bounty created by the audit tests. '
                        'Long enough to pass validation.',
            bounty_type='recruitment',
            bounty_direction='seeking_opportunity',
            reward_amount=None,
            status='open',
            payment_status='released',
            location='Remote',
            success_criteria='Candidate accepts offer.',
        )
        db.session.add(bounty)
        db.session.commit()
        db.session.refresh(bounty)
        return bounty


def _make_pitch(app, bounty_id, hunter_id):
    with app.app_context():
        pitch = Pitch(
            bounty_id=bounty_id,
            hunter_id=hunter_id,
            candidate_teaser='Another candidate for the same bounty.',
            status='pending',
        )
        db.session.add(pitch)
        db.session.commit()
        db.session.refresh(pitch)
        return pitch


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


class TestReferralReadReceipt:
    def test_referral_detail_sets_read_at(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        referral = _make_referral(
            app, sample_hunter.id, sample_bounty.id, sample_pitch.id)
        msg = _make_message(
            app, sample_user.id, sample_hunter.id,
            'Referral thread hello',
            bounty_id=sample_bounty.id, pitch_id=sample_pitch.id)
        r = logged_in_hunter.get(f'/referrals/{referral.id}')
        assert r.status_code == 200
        assert b'Referral thread hello' in r.data
        with app.app_context():
            fresh = db.session.get(Message, msg.id)
            assert fresh.is_read is True
            # The referral page is a read path too: the sender must
            # get a "Seen" receipt for a message read here.
            assert fresh.read_at is not None

    def test_seen_shows_after_referral_page_read(
            self, client, login_as, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        referral = _make_referral(
            app, sample_hunter.id, sample_bounty.id, sample_pitch.id)
        _make_message(
            app, sample_user.id, sample_hunter.id,
            'Referral thread hello',
            bounty_id=sample_bounty.id, pitch_id=sample_pitch.id)
        login_as(client, 'hunter@example.com')
        client.get(f'/referrals/{referral.id}')
        login_as(client, 'testuser@example.com')
        url = (f'/messages/thread/{sample_hunter.id}'
               f'?bounty_id={sample_bounty.id}'
               f'&pitch_id={sample_pitch.id}')
        r = client.get(url)
        assert r.status_code == 200
        assert b'Seen' in r.data


class TestReferralBlockEnforcement:
    def test_poster_blocked_referrer_cannot_post(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        referral = _make_referral(
            app, sample_hunter.id, sample_bounty.id, sample_pitch.id)
        db.session.add(Block(
            blocker_id=sample_user.id, blocked_id=sample_hunter.id,
            kind='block'))
        db.session.commit()
        r = logged_in_hunter.post(
            f'/referrals/{referral.id}/message',
            data={'content': 'You blocked me but I can still write?'},
            follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0

    def test_referrer_blocked_poster_cannot_post(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        referral = _make_referral(
            app, sample_hunter.id, sample_bounty.id, sample_pitch.id)
        db.session.add(Block(
            blocker_id=sample_hunter.id, blocked_id=sample_user.id,
            kind='block'))
        db.session.commit()
        r = logged_in_user.post(
            f'/referrals/{referral.id}/message',
            data={'content': 'You blocked me but I can still write?'},
            follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0


class TestThreadContextNormalization:
    def test_pitch_only_url_shows_bounty_thread(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        _make_message(
            app, sample_user.id, sample_hunter.id,
            'About your pitch',
            bounty_id=sample_bounty.id, pitch_id=sample_pitch.id)
        # Only the pitch in the URL — the thread must still resolve
        # to the (bounty, pitch) conversation, not an empty thread.
        r = logged_in_hunter.get(
            f'/messages/thread/{sample_user.id}'
            f'?pitch_id={sample_pitch.id}')
        assert r.status_code == 200
        assert b'About your pitch' in r.data

    def test_pitch_only_reply_joins_bounty_thread(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        _make_message(
            app, sample_user.id, sample_hunter.id,
            'About your pitch',
            bounty_id=sample_bounty.id, pitch_id=sample_pitch.id)
        r = logged_in_hunter.post(
            f'/messages/thread/{sample_user.id}'
            f'?pitch_id={sample_pitch.id}',
            data={'content': 'Reply with pitch only',
                  'pitch_id': sample_pitch.id},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            replies = Message.query.filter_by(
                sender_id=sample_hunter.id, content='Reply with pitch only',
            ).all()
            assert len(replies) == 1
            # The reply inherits the pitch's bounty — it stays in
            # the referral thread instead of forking a new one.
            assert replies[0].bounty_id == sample_bounty.id
            assert replies[0].pitch_id == sample_pitch.id
        # Still a single thread in the inbox, not two.
        r = logged_in_hunter.get('/messages/')
        assert r.status_code == 200
        assert r.data.count(b'/messages/thread/') == 1

    def test_mismatched_bounty_and_pitch_404s(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch,
            second_user):
        other_bounty = _make_bounty(app, second_user.id)
        other_pitch = _make_pitch(app, other_bounty.id, sample_hunter.id)
        r = logged_in_hunter.get(
            f'/messages/thread/{sample_user.id}'
            f'?bounty_id={sample_bounty.id}'
            f'&pitch_id={other_pitch.id}')
        assert r.status_code == 404


class TestPickerPrivacy:
    def test_wildcard_query_matches_nobody(
            self, logged_in_user, client, sample_hunter):
        # '%' and '_' are LIKE wildcards — '%%' must not match every
        # user in the directory.
        r = logged_in_user.get('/messages/api/users?q=%%')
        assert r.status_code == 200
        assert r.get_json() == []
        r = logged_in_user.get('/messages/api/users?q=__')
        assert r.get_json() == []
        r = logged_in_user.get('/messages/api/users?q=hu%er')
        assert r.get_json() == []

    def test_deleted_user_hidden_from_picker(
            self, logged_in_user, client, app, sample_hunter):
        with app.app_context():
            hunter = db.session.get(User, sample_hunter.id)
            hunter.is_deleted = True
            db.session.commit()
        for q in ('Hunter', 'hunter@example'):
            r = logged_in_user.get(f'/messages/api/users?q={q}')
            assert r.status_code == 200
            assert r.get_json() == []

    def test_compose_to_deleted_user_refused(
            self, logged_in_user, client, app, sample_hunter):
        with app.app_context():
            hunter = db.session.get(User, sample_hunter.id)
            hunter.is_deleted = True
            db.session.commit()
        r = logged_in_user.post('/messages/compose', data={
            'recipient_email': 'hunter@example.com',
            'content': 'Hello, are you there?',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0

    def test_thread_post_to_deleted_user_refused(
            self, logged_in_user, client, app, sample_user, sample_hunter):
        with app.app_context():
            hunter = db.session.get(User, sample_hunter.id)
            hunter.is_deleted = True
            db.session.commit()
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': 'Hello, are you there?'},
            follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0

    def test_thread_history_with_deleted_user_still_readable(
            self, logged_in_user, client, app, sample_user, sample_hunter):
        _make_message(app, sample_hunter.id, sample_user.id,
                      'A message from before the account was deleted')
        with app.app_context():
            hunter = db.session.get(User, sample_hunter.id)
            hunter.is_deleted = True
            db.session.commit()
        # Reading history is fine — only NEW messages are refused.
        r = logged_in_user.get(f'/messages/thread/{sample_hunter.id}')
        assert r.status_code == 200
        assert b'before the account was deleted' in r.data


class TestSelfMessage:
    def test_compose_to_self_refused(
            self, logged_in_user, client, app, sample_user):
        r = logged_in_user.post('/messages/compose', data={
            'recipient_email': 'testuser@example.com',
            'content': 'Note to self',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0

    def test_thread_post_to_self_refused(
            self, logged_in_user, client, app, sample_user):
        r = logged_in_user.post(
            f'/messages/thread/{sample_user.id}',
            data={'content': 'Note to self'},
            follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0
        # No self-inflicted unread badge either.
        r = logged_in_user.get('/messages/')
        assert r.status_code == 200
        assert b'nav-messages-badge' not in r.data

    def test_reply_to_own_message_refused(
            self, logged_in_user, client, app, sample_user):
        msg = _make_message(app, sample_user.id, sample_user.id,
                            'Note to self')
        r = logged_in_user.post(
            f'/messages/{msg.id}/reply',
            data={'content': 'Replying to myself'},
            follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 1


class TestMessageLength:
    def test_thread_post_too_long_rejected(
            self, logged_in_user, client, app, sample_user, sample_hunter):
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': 'x' * 5001},
            follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0

    def test_thread_post_at_limit_accepted(
            self, logged_in_user, client, app, sample_user, sample_hunter):
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': 'x' * 5000},
            follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 1

    def test_ajax_send_too_long_rejected(
            self, logged_in_user, client, app, sample_hunter):
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': 'x' * 5001},
            headers=AJAX)
        assert r.status_code == 400
        assert r.is_json
        assert r.get_json()['ok'] is False
        assert Message.query.count() == 0

    def test_compose_too_long_rejected(
            self, logged_in_user, client, app, sample_hunter):
        r = logged_in_user.post('/messages/compose', data={
            'recipient_email': 'hunter@example.com',
            'content': 'x' * 5001,
        }, follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0

    def test_reply_too_long_rejected(
            self, logged_in_hunter, client, app, sample_message):
        r = logged_in_hunter.post(
            f'/messages/{sample_message.id}/reply',
            data={'content': 'x' * 5001},
            follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 1

    def test_referral_post_too_long_rejected(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        referral = _make_referral(
            app, sample_hunter.id, sample_bounty.id, sample_pitch.id)
        r = logged_in_hunter.post(
            f'/referrals/{referral.id}/message',
            data={'content': 'x' * 5001},
            follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0


class TestVerificationHeld:
    """Checks that must already hold on the pre-fix code (no fix
    needed) — they pin the audit's verification of the rebuild."""

    def test_xss_escaped_in_ajax_bubble(
            self, logged_in_user, client, app, sample_hunter):
        payload = '<script>alert(1)</script>'
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': payload},
            headers=AJAX)
        assert r.status_code == 200
        data = r.get_json()
        assert '&lt;script&gt;' in data['html']
        assert '<script>alert' not in data['html']

    def test_poll_cannot_read_another_pair_thread(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, second_user):
        # Messages exist between the hunter and second_user; the
        # logged-in user polls the hunter's thread and must see
        # none of them.
        _make_message(app, sample_hunter.id, second_user.id,
                      'Private between hunter and second')
        r = logged_in_user.get(
            f'/messages/thread/{sample_hunter.id}/updates',
            headers=AJAX)
        assert r.status_code == 200
        assert r.get_json()['messages'] == []

    def test_inbox_search_does_not_leak_other_threads(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, second_user):
        _make_message(app, sample_hunter.id, second_user.id,
                      'ZebraQuokka private content')
        r = logged_in_user.get('/messages/?q=ZebraQuokka')
        assert r.status_code == 200
        # The query itself is echoed in the search box; what must not
        # appear is the other pair's thread (no thread rows at all).
        assert r.data.count(b'/messages/thread/') == 0
        assert b'No messages yet.' in r.data

    def test_delete_last_message_removes_thread_cleanly(
            self, client, login_as, app,
            sample_user, sample_hunter):
        msg = _make_message(app, sample_user.id, sample_hunter.id,
                            'Only message in this thread')
        login_as(client, 'testuser@example.com')
        r = client.post(
            f'/messages/{msg.id}/delete', headers=AJAX)
        assert r.status_code == 200
        assert r.get_json() == {'ok': True}
        r = client.get('/messages/')
        assert r.status_code == 200
        assert b'No messages yet.' in r.data
        assert b'nav-messages-badge' not in r.data
        # The recipient's badge is gone too (nothing unread left).
        login_as(client, 'hunter@example.com')
        r = client.get('/messages/')
        assert b'nav-messages-badge' not in r.data

    def test_deleted_message_notification_link_404s(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter):
        # The only fallout of a hard delete: the notification link
        # (legacy /read URL) for the deleted message 404s instead
        # of landing somewhere wrong. The inbox stays intact.
        msg = _make_message(app, sample_user.id, sample_hunter.id,
                            'Delete me, keep the inbox working')
        with app.app_context():
            db.session.delete(db.session.get(Message, msg.id))
            db.session.commit()
        r = logged_in_hunter.get(f'/messages/{msg.id}/read')
        assert r.status_code == 404
        r = logged_in_hunter.get('/messages/')
        assert r.status_code == 200
