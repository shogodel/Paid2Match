"""Google sign-in ("Continue with Google") tests — login redirect,
callback match-or-create, state verification, failure paths. All HTTP
calls to Google are mocked — no network calls in the suite."""
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests
from werkzeug.security import generate_password_hash

from extensions import db
from models.user import User


def _mock_resp(status=200, json_data=None):
    resp = SimpleNamespace(status_code=status)
    resp.json = lambda: json_data if json_data is not None else {}
    return resp


def _configure_google(app):
    """Pretend Google OAuth is configured (test credentials only)."""
    app.config['GOOGLE_CLIENT_ID'] = 'test-client-id.apps.googleusercontent.com'
    app.config['GOOGLE_CLIENT_SECRET'] = 'test-client-secret'


def _set_state(client, state='test-state'):
    with client.session_transaction() as sess:
        sess['google_oauth_state'] = state


def _google_mocks(token_json=None, userinfo=None, token_status=200,
                  userinfo_status=200):
    """Context managers patching the two Google HTTP calls."""
    post = patch(
        'utils.google_oauth.requests.post',
        return_value=_mock_resp(
            status=token_status,
            json_data=token_json if token_json is not None
            else {'access_token': 'fake-access-token'},
        ),
    )
    get = patch(
        'utils.google_oauth.requests.get',
        return_value=_mock_resp(
            status=userinfo_status,
            json_data=userinfo if userinfo is not None else {
                'email': 'newuser@example.com',
                'email_verified': True,
                'name': 'New Google User',
            },
        ),
    )
    return post, get


class TestGoogleLoginRoute:
    def test_redirects_to_google_when_configured(self, app, client):
        _configure_google(app)
        resp = client.get('/auth/google/login')
        assert resp.status_code == 302
        parsed = urlparse(resp.location)
        assert parsed.netloc == 'accounts.google.com'
        assert parsed.path == '/o/oauth2/v2/auth'
        params = parse_qs(parsed.query)
        assert params['response_type'] == ['code']
        assert params['client_id'] == [
            'test-client-id.apps.googleusercontent.com'
        ]
        assert params['redirect_uri'] == [
            'http://localhost/auth/google/callback'
        ]
        assert params['scope'] == ['openid email profile']
        assert params['state'][0]
        # The state in the URL matches the one stored in the session.
        with client.session_transaction() as sess:
            assert sess['google_oauth_state'] == params['state'][0]

    def test_redirect_uri_uses_forwarded_https_behind_proxy(
            self, app, client):
        # In production the app sits behind SWAG, which terminates HTTPS
        # and forwards X-Forwarded-Proto/Host. The redirect_uri sent to
        # Google must be the public https:// URL, or Google rejects it
        # with redirect_uri_mismatch.
        _configure_google(app)
        resp = client.get('/auth/google/login', headers={
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'paid2match.work',
            'X-Forwarded-Port': '443',
        })
        assert resp.status_code == 302
        params = parse_qs(urlparse(resp.location).query)
        assert params['redirect_uri'] == [
            'https://paid2match.work/auth/google/callback'
        ]

    def test_unconfigured_redirects_to_login(self, app, client):
        # TestingConfig sets no Google credentials.
        assert not app.config.get('GOOGLE_CLIENT_ID')
        resp = client.get('/auth/google/login', follow_redirects=True)
        assert resp.status_code == 200
        assert b'Google sign-in is not available' in resp.data

    def test_logged_in_user_goes_to_board(self, app, client, logged_in_user):
        _configure_google(app)
        resp = client.get('/auth/google/login')
        assert resp.status_code == 302
        assert resp.location.endswith('/bounties/')


class TestGoogleButton:
    def test_button_hidden_when_unconfigured(self, client):
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with Google' not in resp.data

    def test_button_shown_when_configured(self, app, client):
        _configure_google(app)
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with Google' in resp.data
        assert b'/auth/google/login' in resp.data


class TestGoogleCallbackCreate:
    def test_creates_new_verified_user_and_logs_in(self, app, client):
        _configure_google(app)
        _set_state(client)
        post, get = _google_mocks()
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=fake-code&state=test-state'
            )
        assert resp.status_code == 302
        assert resp.location.endswith('/bounties/')
        with app.app_context():
            user = User.query.filter_by(email='newuser@example.com').first()
            assert user is not None
            assert user.full_name == 'New Google User'
            assert user.role == 'user'
            assert user.is_active is True
            assert user.email_verified is True
            assert user.email_verified_at is not None
            assert user.last_login is not None
        # Logged in: the session carries the user id.
        with client.session_transaction() as sess:
            assert sess.get('_user_id') == user.id

    def test_name_falls_back_to_email_local_part(self, app, client):
        _configure_google(app)
        _set_state(client)
        post, get = _google_mocks(userinfo={
            'email': 'noname@example.com',
            'email_verified': True,
        })
        with post, get:
            client.get('/auth/google/callback?code=x&state=test-state')
        with app.app_context():
            user = User.query.filter_by(email='noname@example.com').first()
            assert user is not None
            assert user.full_name == 'noname'

class TestGoogleCallbackMatch:
    def test_matches_existing_user_and_marks_verified(self, app, client):
        _configure_google(app)
        with app.app_context():
            user = User(
                email='existing@example.com',
                full_name='Existing User',
                password_hash=generate_password_hash('OldPass123!'),
                role='user',
                is_active=True,
                email_verified=False,  # never confirmed by email
            )
            db.session.add(user)
            db.session.commit()
            user_id = user.id
        _set_state(client)
        # Google returns the same email in a different case — the match
        # is case-insensitive.
        post, get = _google_mocks(userinfo={
            'email': 'Existing@Example.com',
            'email_verified': True,
            'name': 'Existing User',
        })
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=x&state=test-state'
            )
        assert resp.status_code == 302
        assert resp.location.endswith('/bounties/')
        with app.app_context():
            assert User.query.count() == 1
            user = db.session.get(User, user_id)
            assert user.email_verified is True
            assert user.email_verified_at is not None
        with client.session_transaction() as sess:
            assert sess.get('_user_id') == user_id

    def test_deactivated_user_is_not_logged_in(self, app, client):
        _configure_google(app)
        with app.app_context():
            user = User(
                email='gone@example.com',
                full_name='Gone User',
                password_hash=generate_password_hash('OldPass123!'),
                role='user',
                is_active=False,
                email_verified=True,
            )
            db.session.add(user)
            db.session.commit()
        _set_state(client)
        post, get = _google_mocks(userinfo={
            'email': 'gone@example.com',
            'email_verified': True,
            'name': 'Gone User',
        })
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'deactivated' in resp.data
        with client.session_transaction() as sess:
            assert '_user_id' not in sess


class TestGoogleCallbackRejections:
    def test_state_mismatch_rejected(self, app, client):
        _configure_google(app)
        _set_state(client, state='real-state')
        post, get = _google_mocks()
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=x&state=wrong-state',
                follow_redirects=True,
            )
        assert b'invalid or has expired' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_missing_state_rejected(self, app, client):
        _configure_google(app)
        # No state in the session at all (callback without a login start).
        post, get = _google_mocks()
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=x&state=whatever',
                follow_redirects=True,
            )
        assert b'invalid or has expired' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_state_is_one_time_use(self, app, client):
        _configure_google(app)
        _set_state(client)
        post, get = _google_mocks()
        with post, get:
            first = client.get(
                '/auth/google/callback?code=x&state=test-state'
            )
            assert first.status_code == 302
            assert first.location.endswith('/bounties/')
            # Replay the same callback: the state was consumed.
            client.get('/auth/logout')
            replay = client.get(
                '/auth/google/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'invalid or has expired' in replay.data

    def test_google_error_param_rejected(self, app, client):
        _configure_google(app)
        _set_state(client)
        resp = client.get(
            '/auth/google/callback?error=access_denied&state=test-state',
            follow_redirects=True,
        )
        assert b'cancelled or failed' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_token_exchange_failure_rejected(self, app, client):
        _configure_google(app)
        _set_state(client)
        post, get = _google_mocks(token_status=400, token_json={})
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=bad-code&state=test-state',
                follow_redirects=True,
            )
        assert b'rejected the sign-in' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_token_exchange_network_error_rejected(self, app, client):
        _configure_google(app)
        _set_state(client)
        with patch(
            'utils.google_oauth.requests.post',
            side_effect=requests.RequestException('boom'),
        ):
            resp = client.get(
                '/auth/google/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'Could not reach Google' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_userinfo_without_email_rejected(self, app, client):
        _configure_google(app)
        _set_state(client)
        post, get = _google_mocks(userinfo={'email_verified': True})
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'did not share an email address' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_unverified_google_email_rejected(self, app, client):
        _configure_google(app)
        _set_state(client)
        post, get = _google_mocks(userinfo={
            'email': 'unverified@example.com',
            'email_verified': False,
            'name': 'Unverified',
        })
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'has not verified that email address' in resp.data
        with app.app_context():
            assert User.query.count() == 0


class TestGoogleCallbackRedirects:
    """After a successful OAuth login the callback honours the stored
    ?next= URL and a waiting referral invite (same as the old password
    login did)."""

    def test_callback_honours_safe_next_url(self, app, client):
        _configure_google(app)
        _set_state(client)
        with client.session_transaction() as sess:
            sess['next'] = '/social/spaces'
        post, get = _google_mocks()
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=x&state=test-state'
            )
        assert resp.status_code == 302
        assert resp.location.endswith('/social/spaces')

    def test_callback_rejects_unsafe_next_url(self, app, client):
        _configure_google(app)
        _set_state(client)
        with client.session_transaction() as sess:
            sess['next'] = 'https://evil.example.com/x'
        post, get = _google_mocks()
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=x&state=test-state'
            )
        assert resp.status_code == 302
        assert resp.location.endswith('/bounties/')

    def test_callback_honours_pending_referral(self, app, client):
        _configure_google(app)
        _set_state(client)
        with client.session_transaction() as sess:
            sess['pending_referral_token'] = 'invite-token-123'
        post, get = _google_mocks()
        with post, get:
            resp = client.get(
                '/auth/google/callback?code=x&state=test-state'
            )
        assert resp.status_code == 302
        assert resp.location.endswith('/referrals/invite/invite-token-123')
