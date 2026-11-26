"""Tests for AJAX messaging: send, polling, read receipts (Seen)
and delete. Runs against the msg-ajax worktree only."""
from datetime import datetime, timezone

from extensions import db
from models.message import Message
from models.moderation import Block

AJAX = {
    'X-Requested-With': 'XMLHttpRequest',
    'Accept': 'application/json',
}


def _make_message(app, sender_id, recipient_id, content,
                  bounty_id=None, pitch_id=None, is_read=False,
                  read_at=None):
    """Create a message directly in the DB (helper, mirrors
    tests/test_messages.py)."""
    with app.app_context():
        msg = Message(
            sender_id=sender_id, recipient_id=recipient_id,
            bounty_id=bounty_id, pitch_id=pitch_id, content=content,
            is_read=is_read, read_at=read_at,
        )
        db.session.add(msg)
        db.session.commit()
        db.session.refresh(msg)
        return msg


class TestAjaxSend:
    def test_ajax_send_returns_json_and_creates_message(
            self, logged_in_user, client, app, sample_user, sample_hunter):
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': 'Hello via AJAX!'},
            headers=AJAX,
        )
        assert r.status_code == 200
        assert r.is_json
        data = r.get_json()
        assert data['ok'] is True
        assert data['message']['content'] == 'Hello via AJAX!'
        assert data['message']['sender_id'] == sample_user.id
        assert data['message']['recipient_id'] == sample_hunter.id
        assert data['message']['created_at']
        # The rendered bubble comes back so the client can append it
        # without a reload.
        assert 'Hello via AJAX!' in data['html']
        assert 'data-message-id' in data['html']
        with app.app_context():
            assert Message.query.count() == 1

    def test_plain_form_post_still_redirects(
            self, logged_in_user, client, app, sample_user, sample_hunter):
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': 'Hello the old way!'},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert f'/messages/thread/{sample_hunter.id}' in r.location
        with app.app_context():
            assert Message.query.count() == 1

    def test_ajax_send_empty_rejected(
            self, logged_in_user, client, app, sample_hunter):
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': '   '},
            headers=AJAX,
        )
        assert r.status_code == 400
        assert r.is_json
        assert r.get_json()['ok'] is False
        with app.app_context():
            assert Message.query.count() == 0

    def test_ajax_send_blocked_refused(
            self, logged_in_hunter, client, app,
            sample_user, sample_hunter):
        # The user blocked the hunter — the AJAX send is refused
        # exactly like the form send.
        db.session.add(Block(
            blocker_id=sample_user.id, blocked_id=sample_hunter.id,
            kind='block'))
        db.session.commit()
        r = logged_in_hunter.post(
            f'/messages/thread/{sample_user.id}',
            data={'content': 'Can you hear me?'},
            headers=AJAX,
        )
        assert r.status_code == 403
        assert r.is_json
        assert r.get_json()['ok'] is False
        with app.app_context():
            assert Message.query.count() == 0


class TestPolling:
    def test_polling_returns_newer_message_and_marks_it_read(
            self, logged_in_user, client, app,
            sample_user, sample_hunter):
        first = _make_message(
            app, sample_user.id, sample_hunter.id, 'First hello')
        _make_message(
            app, sample_hunter.id, sample_user.id, 'Reply hello')
        r = logged_in_user.get(
            f'/messages/thread/{sample_hunter.id}/updates'
            f'?after={first.id}',
            headers=AJAX,
        )
        assert r.status_code == 200
        assert r.is_json
        data = r.get_json()
        assert data['ok'] is True
        assert len(data['messages']) == 1
        item = data['messages'][0]
        assert item['message']['content'] == 'Reply hello'
        assert item['message']['sender_id'] == sample_hunter.id
        assert 'Reply hello' in item['html']
        # Polling picked it up while the user looks at the thread,
        # so it is marked read with a receipt timestamp.
        with app.app_context():
            msg = Message.query.filter_by(content='Reply hello').one()
            assert msg.is_read is True
            assert msg.read_at is not None

    def test_polling_without_after_returns_thread(
            self, logged_in_user, client, app,
            sample_user, sample_hunter):
        _make_message(app, sample_user.id, sample_hunter.id, 'One')
        _make_message(app, sample_hunter.id, sample_user.id, 'Two')
        r = logged_in_user.get(
            f'/messages/thread/{sample_hunter.id}/updates',
            headers=AJAX,
        )
        assert r.status_code == 200
        assert len(r.get_json()['messages']) == 2

    def test_polling_after_latest_returns_nothing(
            self, logged_in_user, client, app,
            sample_user, sample_hunter):
        only = _make_message(
            app, sample_user.id, sample_hunter.id, 'Only message')
        r = logged_in_user.get(
            f'/messages/thread/{sample_hunter.id}/updates'
            f'?after={only.id}',
            headers=AJAX,
        )
        assert r.status_code == 200
        assert r.get_json()['messages'] == []

    def test_polling_requires_login(self, client, sample_hunter):
        r = client.get(
            f'/messages/thread/{sample_hunter.id}/updates?after=x')
        assert r.status_code == 302


class TestReadReceipts:
    def test_opening_thread_sets_read_at(
            self, logged_in_hunter, client, app, sample_message):
        with app.app_context():
            msg = db.session.get(Message, sample_message.id)
            assert msg.is_read is False
            assert msg.read_at is None
        r = logged_in_hunter.get(
            f'/messages/thread/{sample_message.sender_id}')
        assert r.status_code == 200
        with app.app_context():
            msg = db.session.get(Message, sample_message.id)
            assert msg.is_read is True
            assert msg.read_at is not None

    def test_seen_renders_for_read_sent_message_only(
            self, logged_in_user, client, app,
            sample_user, sample_hunter):
        read_at = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        _make_message(app, sample_user.id, sample_hunter.id,
                      'You saw this one', is_read=True, read_at=read_at)
        _make_message(app, sample_user.id, sample_hunter.id,
                      'Not seen yet')
        r = logged_in_user.get(f'/messages/thread/{sample_hunter.id}')
        assert r.status_code == 200
        # Exactly one Seen (the read message), with its read time.
        assert r.data.count(b'Seen') == 1
        assert b'2026-11-25 12:00' in r.data

    def test_no_seen_when_nothing_read(
            self, logged_in_user, client, app,
            sample_user, sample_hunter):
        _make_message(app, sample_user.id, sample_hunter.id,
                      'Not seen yet')
        r = logged_in_user.get(f'/messages/thread/{sample_hunter.id}')
        assert r.status_code == 200
        assert b'Seen' not in r.data


class TestDeleteMessage:
    def test_delete_own_message_ajax(
            self, logged_in_user, client, app, sample_message):
        r = logged_in_user.post(
            f'/messages/{sample_message.id}/delete', headers=AJAX)
        assert r.status_code == 200
        assert r.is_json
        assert r.get_json() == {'ok': True}
        with app.app_context():
            assert db.session.get(Message, sample_message.id) is None

    def test_delete_own_message_plain_post_redirects(
            self, logged_in_user, client, app,
            sample_user, sample_hunter):
        msg = _make_message(
            app, sample_user.id, sample_hunter.id, 'Delete me please')
        r = logged_in_user.post(
            f'/messages/{msg.id}/delete', follow_redirects=False)
        assert r.status_code == 302
        assert f'/messages/thread/{sample_hunter.id}' in r.location
        with app.app_context():
            assert db.session.get(Message, msg.id) is None

    def test_delete_others_message_ajax_refused(
            self, logged_in_hunter, client, app, sample_message):
        # The hunter is the recipient, not the sender — no delete.
        r = logged_in_hunter.post(
            f'/messages/{sample_message.id}/delete', headers=AJAX)
        assert r.status_code == 403
        assert r.get_json()['ok'] is False
        with app.app_context():
            assert db.session.get(Message, sample_message.id) is not None

    def test_delete_others_message_plain_post_refused(
            self, logged_in_hunter, client, app, sample_message):
        r = logged_in_hunter.post(
            f'/messages/{sample_message.id}/delete',
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(Message, sample_message.id) is not None

    def test_delete_requires_login(self, client, sample_message):
        r = client.post(f'/messages/{sample_message.id}/delete')
        assert r.status_code == 302
