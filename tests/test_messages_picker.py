"""User picker + message search (messages-beast workstream B).

Covers GET /messages/api/users and the ?q= filter on GET /messages/,
plus the picker -> empty-thread -> first-message flow.
"""
from werkzeug.security import generate_password_hash

from extensions import db
from models.message import Message
from models.moderation import Block
from models.user import User


def _make_user(app, email, full_name, handle=None):
    """Create a user (handle auto-generated from the name unless given)."""
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


class TestUserPicker:
    def test_requires_login(self, client):
        r = client.get('/messages/api/users?q=hu')
        assert r.status_code == 302

    def test_short_query_returns_empty_list(self, logged_in_user, client):
        for q in ('', 'a'):
            r = logged_in_user.get(f'/messages/api/users?q={q}')
            assert r.status_code == 200
            assert r.get_json() == []

    def test_matches_by_name(self, logged_in_user, client, sample_hunter):
        r = logged_in_user.get('/messages/api/users?q=Hunter')
        data = r.get_json()
        assert any(u['id'] == sample_hunter.id for u in data)
        match = next(u for u in data if u['id'] == sample_hunter.id)
        assert match['full_name'] == 'Hunter User'
        # Audit 2026-11-25: the picker must not leak email addresses.
        assert 'email' not in match
        assert match['handle']

    def test_matches_by_handle(self, logged_in_user, client, app,
                               sample_user):
        pro = _make_user(app, 'pro@example.com', 'Pro Person',
                         handle='montrealpro')
        r = logged_in_user.get('/messages/api/users?q=montrealpro')
        data = r.get_json()
        assert [u['id'] for u in data] == [pro.id]

    def test_matches_by_email(self, logged_in_user, client, sample_hunter):
        r = logged_in_user.get('/messages/api/users?q=hunter@example')
        data = r.get_json()
        assert any(u['id'] == sample_hunter.id for u in data)

    def test_search_is_case_insensitive(self, logged_in_user, client,
                                        sample_hunter):
        r = logged_in_user.get('/messages/api/users?q=HUNTER')
        data = r.get_json()
        assert any(u['id'] == sample_hunter.id for u in data)

    def test_excludes_current_user(self, logged_in_user, client):
        r = logged_in_user.get('/messages/api/users?q=Test')
        data = r.get_json()
        # 'Test User' is the logged-in user — must not appear.
        assert all(u['full_name'] != 'Test User' for u in data)

    def test_excludes_users_who_blocked_you(
            self, logged_in_user, client, app, sample_user, sample_hunter):
        db.session.add(Block(
            blocker_id=sample_hunter.id, blocked_id=sample_user.id,
            kind='block'))
        db.session.commit()
        r = logged_in_user.get('/messages/api/users?q=Hunter')
        assert r.get_json() == []

    def test_does_not_exclude_users_you_blocked(
            self, logged_in_user, client, app, sample_user, sample_hunter):
        # Blocking someone yourself does not hide them from your picker
        # — only their block of you does.
        db.session.add(Block(
            blocker_id=sample_user.id, blocked_id=sample_hunter.id,
            kind='block'))
        db.session.commit()
        r = logged_in_user.get('/messages/api/users?q=Hunter')
        data = r.get_json()
        assert any(u['id'] == sample_hunter.id for u in data)

    def test_max_ten_results(self, logged_in_user, client, app):
        for i in range(12):
            _make_user(app, f'picker{i}@example.com', f'Picker Person {i}')
        r = logged_in_user.get('/messages/api/users?q=Picker')
        assert len(r.get_json()) == 10


class TestComposePickerFlow:
    def test_compose_page_has_picker(self, logged_in_user, client):
        r = logged_in_user.get('/messages/compose')
        assert r.status_code == 200
        assert b'id="user-search"' in r.data
        assert b'/messages/api/users' in r.data
        # Old exact-email fallback is still there.
        assert b'name="recipient_email"' in r.data

    def test_empty_thread_renders_with_reply_box(
            self, logged_in_user, client, sample_hunter):
        r = logged_in_user.get(f'/messages/thread/{sample_hunter.id}')
        assert r.status_code == 200
        assert b'No messages yet' in r.data
        assert b'name="content"' in r.data
        assert b'csrf_token' in r.data

    def test_first_message_from_empty_thread(
            self, logged_in_user, client, app, sample_user, sample_hunter):
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': 'Hello from the picker!'},
            follow_redirects=False)
        assert r.status_code == 302
        assert f'/messages/thread/{sample_hunter.id}' in r.location
        assert Message.query.count() == 1
        msg = Message.query.first()
        assert msg.sender_id == sample_user.id
        assert msg.recipient_id == sample_hunter.id
        assert msg.content == 'Hello from the picker!'
        assert msg.bounty_id is None and msg.pitch_id is None
        # And the thread now shows the message.
        r = logged_in_user.get(f'/messages/thread/{sample_hunter.id}')
        assert b'Hello from the picker!' in r.data

    def test_thread_send_refused_when_blocked(
            self, logged_in_user, client, app, sample_user, sample_hunter):
        db.session.add(Block(
            blocker_id=sample_hunter.id, blocked_id=sample_user.id,
            kind='block'))
        db.session.commit()
        r = logged_in_user.post(
            f'/messages/thread/{sample_hunter.id}',
            data={'content': 'You blocked me but I can still write?'},
            follow_redirects=False)
        assert r.status_code == 302
        assert Message.query.count() == 0


class TestInboxSearch:
    def test_search_box_present_and_query_kept(
            self, logged_in_user, client, sample_message):
        r = logged_in_user.get('/messages/?q=hunter')
        assert r.status_code == 200
        assert b'name="q"' in r.data
        assert b'value="hunter"' in r.data

    def test_empty_query_shows_all_threads(
            self, logged_in_user, client, app, sample_user, sample_hunter,
            second_user):
        _make_message(app, sample_user.id, sample_hunter.id,
                      'Talk about the python job')
        _make_message(app, second_user.id, sample_user.id,
                      'Talk about the design job')
        r = logged_in_user.get('/messages/')
        assert r.status_code == 200
        assert b'python job' in r.data
        assert b'design job' in r.data
        r = logged_in_user.get('/messages/?q=')
        assert b'python job' in r.data
        assert b'design job' in r.data

    def test_filters_by_message_content(
            self, logged_in_user, client, app, sample_user, sample_hunter,
            second_user):
        _make_message(app, sample_user.id, sample_hunter.id,
                      'Talk about the python job')
        _make_message(app, second_user.id, sample_user.id,
                      'Talk about the design job')
        r = logged_in_user.get('/messages/?q=python')
        assert b'python job' in r.data
        assert b'design job' not in r.data
        r = logged_in_user.get('/messages/?q=DESIGN')
        assert b'design job' in r.data
        assert b'python job' not in r.data

    def test_filters_by_sender_name(
            self, logged_in_user, client, app, sample_user, sample_hunter,
            second_user):
        _make_message(app, sample_user.id, sample_hunter.id,
                      'Talk about the python job')
        _make_message(app, second_user.id, sample_user.id,
                      'Talk about the design job')
        r = logged_in_user.get('/messages/?q=Hunter')
        assert b'python job' in r.data
        assert b'design job' not in r.data
        r = logged_in_user.get('/messages/?q=Second')
        assert b'design job' in r.data
        assert b'python job' not in r.data

    def test_no_match_shows_empty_state(
            self, logged_in_user, client, sample_message):
        r = logged_in_user.get('/messages/?q=zzzznothinghere')
        assert r.status_code == 200
        assert b'No messages yet.' in r.data
