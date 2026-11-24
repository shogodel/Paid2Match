"""Facebook sign-in ("Continue with Facebook") tests — login redirect,
callback match-or-create, missing-email failure, state verification,
failure paths. All HTTP calls to Facebook are mocked — no network calls
in the suite."""
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


def _configure_facebook(app):
    """Pretend Facebook OAuth is configured (test credentials only),
    with the login button enabled."""
    app.config['FACEBOOK_CLIENT_ID'] = 'test-facebook-client-id'
    app.config['FACEBOOK_CLIENT_SECRET'] = 'test-facebook-client-secret'
    app.config['FACEBOOK_LOGIN_ENABLED'] = True


def _set_state(client, state='test-state'):
    with client.session_transaction() as sess:
        sess['facebook_oauth_state'] = state


def _facebook_mocks(user=None, token_status=200, user_status=200,
                    token_json=None):
    """Patch for Facebook's HTTP calls. Both the token exchange and the
    /me profile fetch are GETs, dispatched by URL: the token endpoint
    returns an access token, /me returns the user payload."""
    user_payload = user if user is not None else {
        'id': '12345',
        'name': 'Face Book',
        'email': 'newuser@example.com',
    }

    def fake_get(url, **kwargs):
        if '/oauth/access_token' in url:
            return _mock_resp(
                status=token_status,
                json_data=token_json if token_json is not None
                else {'access_token': 'fake-access-token',
                      'token_type': 'bearer'},
            )
        return _mock_resp(status=user_status, json_data=user_payload)

    return patch('utils.facebook_oauth.requests.get', side_effect=fake_get)


class TestFacebookLoginRoute:
    def test_redirects_to_facebook_when_configured(self, app, client):
        _configure_facebook(app)
        resp = client.get('/auth/facebook/login')
        assert resp.status_code == 302
        parsed = urlparse(resp.location)
        assert parsed.netloc == 'www.facebook.com'
        assert parsed.path == '/v25.0/dialog/oauth'
        params = parse_qs(parsed.query)
        assert params['client_id'] == ['test-facebook-client-id']
        assert params['redirect_uri'] == [
            'http://localhost/auth/facebook/callback'
        ]
        assert params['scope'] == ['public_profile,email']
        assert params['response_type'] == ['code']
        assert params['state'][0]
        with client.session_transaction() as sess:
            assert sess['facebook_oauth_state'] == params['state'][0]

    def test_redirect_uri_uses_forwarded_https_behind_proxy(
            self, app, client):
        # Same proxy setup as Google/GitHub: the redirect_uri sent to
        # Facebook must be the public https:// URL, or Meta rejects it.
        _configure_facebook(app)
        resp = client.get('/auth/facebook/login', headers={
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'paid2match.work',
            'X-Forwarded-Port': '443',
        })
        assert resp.status_code == 302
        params = parse_qs(urlparse(resp.location).query)
        assert params['redirect_uri'] == [
            'https://paid2match.work/auth/facebook/callback'
        ]

    def test_unconfigured_redirects_to_login(self, app, client):
        # TestingConfig sets no Facebook credentials.
        assert not app.config.get('FACEBOOK_CLIENT_ID')
        resp = client.get('/auth/facebook/login', follow_redirects=True)
        assert resp.status_code == 200
        assert b'Facebook sign-in is not available' in resp.data

    def test_logged_in_user_goes_to_board(self, app, client, logged_in_user):
        _configure_facebook(app)
        resp = client.get('/auth/facebook/login')
        assert resp.status_code == 302
        assert resp.location.endswith('/needs/')


class TestFacebookButton:
    def test_button_hidden_when_unconfigured(self, client):
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with Facebook' not in resp.data

    def test_button_shown_when_configured(self, app, client):
        _configure_facebook(app)
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with Facebook' in resp.data
        assert b'/auth/facebook/login' in resp.data
        assert b'Sign in with Google, GitHub, or Facebook' in resp.data

    def test_button_hidden_when_flag_off(self, app, client):
        # Credentials configured but the kill-switch off (the Meta
        # app is still in Development until business verification
        # passes): no button, no Facebook in the intro line.
        app.config['FACEBOOK_CLIENT_ID'] = 'test-facebook-client-id'
        app.config['FACEBOOK_CLIENT_SECRET'] = 'test-facebook-client-secret'
        assert not app.config.get('FACEBOOK_LOGIN_ENABLED')
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with Facebook' not in resp.data
        assert b'/auth/facebook/login' not in resp.data
        assert b'Sign in with Google or GitHub' in resp.data
        assert b'Sign in with Google, GitHub, or Facebook' not in resp.data


class TestFacebookCallbackCreate:
    def test_creates_new_verified_user_and_logs_in(self, app, client):
        _configure_facebook(app)
        _set_state(client)
        with _facebook_mocks():
            resp = client.get(
                '/auth/facebook/callback?code=fake-code&state=test-state'
            )
        assert resp.status_code == 302
        assert resp.location.endswith('/needs/')
        with app.app_context():
            user = User.query.filter_by(email='newuser@example.com').first()
            assert user is not None
            assert user.full_name == 'Face Book'
            assert user.role == 'user'
            assert user.is_active is True
            assert user.email_verified is True
            assert user.email_verified_at is not None
            assert user.last_login is not None
        with client.session_transaction() as sess:
            assert sess.get('_user_id') == user.id

    def test_name_falls_back_to_email_local_part(self, app, client):
        _configure_facebook(app)
        _set_state(client)
        with _facebook_mocks(user={
            'id': '67890',
            'name': None,
            'email': 'noname@example.com',
        }):
            client.get('/auth/facebook/callback?code=x&state=test-state')
        with app.app_context():
            user = User.query.filter_by(email='noname@example.com').first()
            assert user is not None
            assert user.full_name == 'noname'

    def test_no_email_rejected_cleanly(self, app, client):
        # Phone-number signups and declined email permission: Facebook
        # returns no email. No account is created; the message points
        # at Google or GitHub.
        _configure_facebook(app)
        _set_state(client)
        with _facebook_mocks(user={
            'id': '11111',
            'name': 'Phone User',
            'email': None,
        }):
            resp = client.get(
                '/auth/facebook/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'did not share an email address' in resp.data
        assert b'Google or GitHub' in resp.data
        with app.app_context():
            assert User.query.count() == 0


class TestFacebookCallbackMatch:
    def test_matches_existing_user_and_marks_verified(self, app, client):
        _configure_facebook(app)
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
        # Facebook returns the same email in a different case — the
        # match is case-insensitive.
        with _facebook_mocks(user={
            'id': '22222',
            'name': 'Existing User',
            'email': 'Existing@Example.com',
        }):
            resp = client.get(
                '/auth/facebook/callback?code=x&state=test-state'
            )
        assert resp.status_code == 302
        assert resp.location.endswith('/needs/')
        with app.app_context():
            assert User.query.count() == 1
            user = db.session.get(User, user_id)
            assert user.email_verified is True
            assert user.email_verified_at is not None
        with client.session_transaction() as sess:
            assert sess.get('_user_id') == user_id

    def test_deactivated_user_is_not_logged_in(self, app, client):
        _configure_facebook(app)
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
        with _facebook_mocks(user={
            'id': '33333',
            'name': 'Gone User',
            'email': 'gone@example.com',
        }):
            resp = client.get(
                '/auth/facebook/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'deactivated' in resp.data
        with client.session_transaction() as sess:
            assert '_user_id' not in sess


class TestFacebookCallbackRejections:
    def test_state_mismatch_rejected(self, app, client):
        _configure_facebook(app)
        _set_state(client, state='real-state')
        with _facebook_mocks():
            resp = client.get(
                '/auth/facebook/callback?code=x&state=wrong-state',
                follow_redirects=True,
            )
        assert b'invalid or has expired' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_missing_state_rejected(self, app, client):
        _configure_facebook(app)
        # No state in the session at all (callback without a login start).
        with _facebook_mocks():
            resp = client.get(
                '/auth/facebook/callback?code=x&state=whatever',
                follow_redirects=True,
            )
        assert b'invalid or has expired' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_facebook_error_param_rejected(self, app, client):
        _configure_facebook(app)
        _set_state(client)
        resp = client.get(
            '/auth/facebook/callback?error=access_denied&state=test-state',
            follow_redirects=True,
        )
        assert b'cancelled or failed' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_token_exchange_failure_rejected(self, app, client):
        _configure_facebook(app)
        _set_state(client)
        with _facebook_mocks(token_status=400, token_json={}):
            resp = client.get(
                '/auth/facebook/callback?code=bad-code&state=test-state',
                follow_redirects=True,
            )
        assert b'rejected the sign-in' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_token_exchange_network_error_rejected(self, app, client):
        _configure_facebook(app)
        _set_state(client)
        with patch(
            'utils.facebook_oauth.requests.get',
            side_effect=requests.RequestException('boom'),
        ):
            resp = client.get(
                '/auth/facebook/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'Could not reach Facebook' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_profile_fetch_failure_rejected(self, app, client):
        _configure_facebook(app)
        _set_state(client)
        with _facebook_mocks(user_status=500):
            resp = client.get(
                '/auth/facebook/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'Could not read your profile from Facebook' in resp.data
        with app.app_context():
            assert User.query.count() == 0
