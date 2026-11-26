"""Tests for messages blueprint."""
import pytest


class TestMessagesInbox:
    def test_inbox_requires_login(self, client):
        r = client.get('/messages/')
        assert r.status_code == 302

    def test_inbox_loads(self, logged_in_user, client, sample_message):
        r = logged_in_user.get('/messages/')
        assert r.status_code == 200

    def test_inbox_empty(self, logged_in_user, client):
        r = logged_in_user.get('/messages/')
        assert r.status_code == 200


class TestMessagesSent:
    def test_sent_requires_login(self, client):
        r = client.get('/messages/sent')
        assert r.status_code == 302

    def test_sent_loads(self, logged_in_user, client, sample_message):
        r = logged_in_user.get('/messages/sent')
        assert r.status_code == 200


class TestMessagesCompose:
    def test_compose_requires_login(self, client):
        r = client.get('/messages/compose')
        assert r.status_code == 302

    def test_compose_page_loads(self, logged_in_user, client):
        r = logged_in_user.get('/messages/compose')
        assert r.status_code in (200, 302)

    def test_compose_send_message(self, logged_in_user, client, app, sample_user, sample_hunter):
        r = logged_in_user.post('/messages/compose', data={
            'recipient_email': 'hunter@example.com',
            'content': 'Hello, I am interested in your pitch!',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert '/messages/sent' in r.location

    def test_compose_recipient_not_found(self, logged_in_user, client):
        r = logged_in_user.post('/messages/compose', data={
            'recipient_email': 'nobody@nowhere.com',
            'content': 'This recipient does not exist.',
        })
        assert r.status_code in (200, 302)

    def test_compose_blocked_by_recipient(self, logged_in_user, client, app,
                                          sample_user, sample_hunter):
        # A block means "they cannot follow or message you" — the
        # follow route enforces it, messaging must too.
        from extensions import db
        from models.message import Message
        from models.moderation import Block
        db.session.add(Block(
            blocker_id=sample_hunter.id, blocked_id=sample_user.id,
            kind='block'))
        db.session.commit()
        r = logged_in_user.post('/messages/compose', data={
            'recipient_email': 'hunter@example.com',
            'content': 'You blocked me but I can still write?',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0

    def test_compose_empty_content_rejected(self, logged_in_user, client, app,
                                            sample_hunter):
        # Message.content is NOT NULL — an empty send used to crash
        # with an IntegrityError (500).
        from models.message import Message
        r = logged_in_user.post('/messages/compose', data={
            'recipient_email': 'hunter@example.com',
            'content': '   ',
        })
        assert r.status_code in (200, 302)
        assert Message.query.count() == 0


class TestMessagesRead:
    def test_read_requires_login(self, client, sample_message):
        r = client.get(f'/messages/{sample_message.id}/read')
        assert r.status_code == 302

    def test_read_by_recipient_redirects_to_thread(
            self, logged_in_hunter, client, sample_message, sample_hunter):
        # The flat /read URL marks the message read and 302s into the
        # thread that contains it (the direct thread with the sender).
        r = logged_in_hunter.get(f'/messages/{sample_message.id}/read')
        assert r.status_code == 302
        assert f'/messages/thread/{sample_message.sender_id}' in r.location

    def test_read_by_sender_redirects_to_thread(
            self, logged_in_user, client, sample_message, sample_hunter):
        r = logged_in_user.get(f'/messages/{sample_message.id}/read')
        assert r.status_code == 302
        assert f'/messages/thread/{sample_hunter.id}' in r.location

    def test_read_marks_as_read(self, logged_in_hunter, client, app, sample_message):
        with app.app_context():
            from models.message import Message
            from extensions import db
            msg = db.session.get(Message, sample_message.id)
            assert msg.is_read == False

        r = logged_in_hunter.get(f'/messages/{sample_message.id}/read')
        assert r.status_code == 302

        with app.app_context():
            from models.message import Message
            from extensions import db
            msg = db.session.get(Message, sample_message.id)
            assert msg.is_read == True

    def test_read_access_denied(self, logged_in_admin, client, sample_message):
        r = logged_in_admin.get(f'/messages/{sample_message.id}/read')
        assert r.status_code == 302


class TestMessagesReply:
    def test_reply_requires_login(self, client, sample_message):
        r = client.post(f'/messages/{sample_message.id}/reply')
        assert r.status_code == 302

    def test_reply_by_recipient(self, logged_in_hunter, client, app, sample_message):
        r = logged_in_hunter.post(
            f'/messages/{sample_message.id}/reply',
            data={'content': 'Thanks for your interest!'},
            follow_redirects=False
        )
        assert r.status_code == 302

    def test_reply_access_denied(self, logged_in_user, client, sample_message):
        r = logged_in_user.post(
            f'/messages/{sample_message.id}/reply',
            data={'content': 'This should not work.'}
        )
        assert r.status_code == 302

    def test_reply_blocked_by_sender(self, logged_in_hunter, client, app,
                                     sample_user, sample_hunter,
                                     sample_message):
        # The original sender blocked the replier — the reply is refused.
        from extensions import db
        from models.message import Message
        from models.moderation import Block
        db.session.add(Block(
            blocker_id=sample_user.id, blocked_id=sample_hunter.id,
            kind='block'))
        db.session.commit()
        r = logged_in_hunter.post(
            f'/messages/{sample_message.id}/reply',
            data={'content': 'Thanks for your interest!'},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert Message.query.count() == 1  # only the original

    def test_reply_empty_content_rejected(self, logged_in_hunter, client, app,
                                          sample_message):
        from models.message import Message
        r = logged_in_hunter.post(
            f'/messages/{sample_message.id}/reply',
            data={'content': '   '},
        )
        assert r.status_code in (200, 302)
        assert Message.query.count() == 1  # only the original


class TestMessagesApi:
    def test_api_conversations_requires_login(self, client):
        r = client.get('/messages/api/conversations')
        assert r.status_code == 302

    def test_api_conversations(self, logged_in_user, client, sample_message):
        r = logged_in_user.get('/messages/api/conversations')
        assert r.status_code == 200
        assert r.is_json

# ---------------------------------------------------------------- threads


def _make_message(app, sender_id, recipient_id, content,
                  bounty_id=None, pitch_id=None, created_at=None):
    """Create a message directly in the DB (helper for thread tests)."""
    from extensions import db
    from models.message import Message
    with app.app_context():
        msg = Message(
            sender_id=sender_id, recipient_id=recipient_id,
            bounty_id=bounty_id, pitch_id=pitch_id, content=content,
        )
        if created_at is not None:
            msg.created_at = created_at
        db.session.add(msg)
        db.session.commit()
        db.session.refresh(msg)
        return msg


def _thread_link_count(body):
    """Number of conversation rows on the inbox page (one thread link
    per row)."""
    return body.count(b'/messages/thread/')


class TestThreadGrouping:
    def test_same_pitch_is_one_thread(self, logged_in_user, client, app,
                                      sample_user, sample_hunter,
                                      sample_bounty, sample_pitch):
        # Two messages about the same pitch (+ a direct one) = 2 threads.
        _make_message(app, sample_user.id, sample_hunter.id,
                      'First about the pitch',
                      bounty_id=sample_bounty.id,
                      pitch_id=sample_pitch.id)
        _make_message(app, sample_hunter.id, sample_user.id,
                      'Reply about the pitch',
                      bounty_id=sample_bounty.id,
                      pitch_id=sample_pitch.id)
        _make_message(app, sample_user.id, sample_hunter.id,
                      'A direct hello')
        r = logged_in_user.get('/messages/')
        assert r.status_code == 200
        assert _thread_link_count(r.data) == 2

    def test_different_pitch_is_two_threads(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        from extensions import db
        from models.pitch import Pitch
        with app.app_context():
            other_pitch = Pitch(
                bounty_id=sample_bounty.id, hunter_id=sample_hunter.id,
                candidate_teaser='Another candidate for the same bounty.',
                status='pending',
            )
            db.session.add(other_pitch)
            db.session.commit()
            db.session.refresh(other_pitch)
            other_pitch_id = other_pitch.id
        _make_message(app, sample_user.id, sample_hunter.id,
                      'About pitch one',
                      bounty_id=sample_bounty.id,
                      pitch_id=sample_pitch.id)
        _make_message(app, sample_user.id, sample_hunter.id,
                      'About pitch two',
                      bounty_id=sample_bounty.id,
                      pitch_id=other_pitch_id)
        r = logged_in_user.get('/messages/')
        assert r.status_code == 200
        assert _thread_link_count(r.data) == 2
        # Both rows link to their own pitch context.
        assert sample_pitch.id.encode() in r.data
        assert other_pitch_id.encode() in r.data

    def test_direct_is_own_thread(self, logged_in_user, client, app,
                                   sample_user, sample_hunter,
                                   sample_bounty, sample_pitch):
        # A direct message and a pitch message with the same person
        # are different threads.
        _make_message(app, sample_user.id, sample_hunter.id,
                      'About the pitch',
                      bounty_id=sample_bounty.id,
                      pitch_id=sample_pitch.id)
        _make_message(app, sample_user.id, sample_hunter.id,
                      'Just saying hi')
        r = logged_in_user.get('/messages/')
        assert r.status_code == 200
        assert _thread_link_count(r.data) == 2
        # The direct thread has no query string on its link.
        assert f'/messages/thread/{sample_hunter.id}"'.encode() in r.data

    def test_inbox_row_shows_last_snippet_and_unread(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter):
        _make_message(app, sample_user.id, sample_hunter.id,
                      'First hello')
        _make_message(app, sample_user.id, sample_hunter.id,
                      'Second hello, the latest')
        r = logged_in_hunter.get('/messages/')
        assert r.status_code == 200
        assert b'Second hello, the latest' in r.data
        # Two unread in this thread: the per-thread badge shows 2.
        assert b'>2</span>' in r.data


class TestThreadView:
    def test_thread_requires_login(self, client, sample_hunter):
        r = client.get(f'/messages/thread/{sample_hunter.id}')
        assert r.status_code == 302

    def test_thread_shows_history_oldest_first(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter):
        from datetime import datetime, timezone, timedelta
        base = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
        _make_message(app, sample_user.id, sample_hunter.id,
                      'AAA first message', created_at=base)
        _make_message(app, sample_hunter.id, sample_user.id,
                      'BBB second message',
                      created_at=base + timedelta(minutes=5))
        _make_message(app, sample_user.id, sample_hunter.id,
                      'CCC third message',
                      created_at=base + timedelta(minutes=10))
        r = logged_in_hunter.get(f'/messages/thread/{sample_user.id}')
        assert r.status_code == 200
        assert b'AAA first message' in r.data
        assert b'BBB second message' in r.data
        assert b'CCC third message' in r.data
        assert (r.data.index(b'AAA first message')
                < r.data.index(b'BBB second message')
                < r.data.index(b'CCC third message'))
        # Reply box present.
        assert b'name="content"' in r.data
        assert b'csrf_token' in r.data

    def test_thread_reply_keeps_pitch_context(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        from extensions import db
        from models.message import Message
        _make_message(app, sample_user.id, sample_hunter.id,
                      'About your pitch',
                      bounty_id=sample_bounty.id,
                      pitch_id=sample_pitch.id)
        url = (f'/messages/thread/{sample_user.id}'
               f'?bounty_id={sample_bounty.id}'
               f'&pitch_id={sample_pitch.id}')
        r = logged_in_hunter.post(url, data={
            'content': 'Thanks, let us talk!',
            'bounty_id': sample_bounty.id,
            'pitch_id': sample_pitch.id,
        }, follow_redirects=False)
        assert r.status_code == 302
        assert url.split('?')[0] in r.location
        with app.app_context():
            replies = Message.query.filter_by(
                sender_id=sample_hunter.id,
                recipient_id=sample_user.id,
            ).all()
            assert len(replies) == 1
            assert replies[0].bounty_id == sample_bounty.id
            assert replies[0].pitch_id == sample_pitch.id

    def test_thread_reply_blocked(self, logged_in_hunter, client, app,
                                  sample_user, sample_hunter):
        from extensions import db
        from models.message import Message
        from models.moderation import Block
        db.session.add(Block(
            blocker_id=sample_user.id, blocked_id=sample_hunter.id,
            kind='block'))
        db.session.commit()
        r = logged_in_hunter.post(
            f'/messages/thread/{sample_user.id}',
            data={'content': 'Can you hear me?'},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert Message.query.count() == 0

    def test_thread_reply_empty_rejected(self, logged_in_hunter, client,
                                         app, sample_user, sample_hunter):
        from models.message import Message
        r = logged_in_hunter.post(
            f'/messages/thread/{sample_user.id}',
            data={'content': '   '},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert Message.query.count() == 0

    def test_thread_shows_bounty_context(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        _make_message(app, sample_user.id, sample_hunter.id,
                      'About your pitch',
                      bounty_id=sample_bounty.id,
                      pitch_id=sample_pitch.id)
        url = (f'/messages/thread/{sample_user.id}'
               f'?bounty_id={sample_bounty.id}'
               f'&pitch_id={sample_pitch.id}')
        r = logged_in_hunter.get(url)
        assert r.status_code == 200
        assert sample_bounty.title.encode() in r.data


class TestUnreadBadges:
    def test_badge_count_matches_db(self, logged_in_hunter, client, app,
                                    sample_user, sample_hunter,
                                    second_user):
        # 2 unread for the hunter (from the user) + 1 from second_user.
        _make_message(app, sample_user.id, sample_hunter.id, 'One')
        _make_message(app, sample_user.id, sample_hunter.id, 'Two')
        _make_message(app, second_user.id, sample_hunter.id, 'Three')
        r = logged_in_hunter.get('/messages/')
        assert r.status_code == 200
        # Desktop nav badge + mobile tab badge both show the total (3).
        assert b'id="nav-messages-badge">3</span>' in r.data
        assert r.data.count(b'>3</span>') >= 2

    def test_badge_absent_when_nothing_unread(
            self, logged_in_hunter, client, sample_message):
        r = logged_in_hunter.get('/messages/')
        assert r.status_code == 200
        # sample_message is unread, so the badge IS there; read it via
        # the thread, then it must disappear.
        assert b'nav-messages-badge' in r.data
        r = logged_in_hunter.get(
            f'/messages/thread/{sample_message.sender_id}')
        assert r.status_code == 200
        r = logged_in_hunter.get('/messages/')
        assert b'nav-messages-badge' not in r.data

    def test_opening_thread_zeroes_its_count(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter, second_user):
        from extensions import db
        from models.message import Message
        _make_message(app, sample_user.id, sample_hunter.id, 'One')
        _make_message(app, sample_user.id, sample_hunter.id, 'Two')
        _make_message(app, second_user.id, sample_hunter.id, 'Three')
        # Open the thread with sample_user: only ITS messages go read.
        r = logged_in_hunter.get(f'/messages/thread/{sample_user.id}')
        assert r.status_code == 200
        with app.app_context():
            unread = Message.query.filter_by(
                recipient_id=sample_hunter.id, is_read=False).all()
            assert len(unread) == 1
            assert unread[0].sender_id == second_user.id
        # The badge now shows just the remaining 1.
        r = logged_in_hunter.get('/messages/')
        assert b'id="nav-messages-badge">1</span>' in r.data


class TestReferralDetailThread:
    def test_referral_detail_still_shows_messages(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, sample_bounty, sample_pitch):
        from extensions import db
        from models.referral import Referral
        with app.app_context():
            referral = Referral(
                referrer_id=sample_hunter.id,
                bounty_id=sample_bounty.id,
                pitch_id=sample_pitch.id,
                referred_name='Candidate Name',
                referred_email='candidate@example.com',
                message='I would like to refer this candidate.',
                status='invited',
            )
            db.session.add(referral)
            db.session.commit()
            db.session.refresh(referral)
            referral_id = referral.id
        _make_message(app, sample_user.id, sample_hunter.id,
                      'Referral thread hello',
                      bounty_id=sample_bounty.id,
                      pitch_id=sample_pitch.id)
        r = logged_in_user.get(f'/referrals/{referral_id}')
        assert r.status_code == 200
        assert b'Referral thread hello' in r.data
