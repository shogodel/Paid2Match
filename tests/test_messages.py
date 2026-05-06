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


class TestMessagesRead:
    def test_read_requires_login(self, client, sample_message):
        r = client.get(f'/messages/{sample_message.id}/read')
        assert r.status_code == 302

    def test_read_by_recipient(self, logged_in_hunter, client, sample_message):
        r = logged_in_hunter.get(f'/messages/{sample_message.id}/read')
        assert r.status_code == 200

    def test_read_by_sender(self, logged_in_user, client, sample_message):
        r = logged_in_user.get(f'/messages/{sample_message.id}/read')
        assert r.status_code == 200

    def test_read_marks_as_read(self, logged_in_hunter, client, app, sample_message):
        with app.app_context():
            from models.message import Message
            from extensions import db
            msg = db.session.get(Message, sample_message.id)
            assert msg.is_read == False

        r = logged_in_hunter.get(f'/messages/{sample_message.id}/read')
        assert r.status_code == 200

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


class TestMessagesApi:
    def test_api_conversations_requires_login(self, client):
        r = client.get('/messages/api/conversations')
        assert r.status_code == 302

    def test_api_conversations(self, logged_in_user, client, sample_message):
        r = logged_in_user.get('/messages/api/conversations')
        assert r.status_code == 200
        assert r.is_json