"""LinkedIn sign-in ("Continue with LinkedIn") tests — login redirect,
callback match-or-create, missing-email failure, state verification,
failure paths. Sign In with LinkedIn using OpenID Connect: authorize
at www.linkedin.com/oauth/v2/authorization, token exchange POST to
/oauth/v2/accessToken, profile from /v2/userinfo. All HTTP calls to
LinkedIn are mocked — no network calls in the suite."""
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


def _mock_bad_json(status=200):
    """A response whose body is not JSON (proxy error page served
    with a 200, truncated body) — resp.json() raises, as requests
    does."""
    resp = SimpleNamespace(status_code=status)

    def _raise():
        raise ValueError('No JSON object could be decoded')

    resp.json = _raise
    return resp


def _configure_linkedin(app):
    """Pretend LinkedIn OAuth is configured (test credentials only),
    with the login button enabled."""
    app.config['LINKEDIN_CLIENT_ID'] = 'test-linkedin-client-id'
    app.config['LINKEDIN_CLIENT_SECRET'] = 'test-linkedin-client-secret'
    app.config['LINKEDIN_LOGIN_ENABLED'] = True


def _configure_facebook(app):
    """Pretend Facebook OAuth is configured (test credentials only),
    with the login button enabled — for the both-providers intro test."""
    app.config['FACEBOOK_CLIENT_ID'] = 'test-facebook-client-id'
    app.config['FACEBOOK_CLIENT_SECRET'] = 'test-facebook-client-secret'
    app.config['FACEBOOK_LOGIN_ENABLED'] = True


def _set_state(client, state='test-state'):
    with client.session_transaction() as sess:
        sess['linkedin_oauth_state'] = state


def _linkedin_mocks(user=None, token_status=200, user_status=200,
                    token_json=None):
    """Patches for the LinkedIn HTTP calls: POST to the token endpoint,
    GET to /v2/userinfo. Returns (post_patch, get_patch), used as
    ``with post, get:``."""
    user_payload = user if user is not None else {
        'sub': 'linkedin-user-id',
        'name': 'Link In',
        'given_name': 'Link',
        'family_name': 'In',
        'email': 'newuser@example.com',
        'email_verified': True,
    }

    post = patch(
        'utils.linked_accounts.requests.post',
        return_value=_mock_resp(
            status=token_status,
            json_data=token_json if token_json is not None
            else {'access_token': 'fake-access-token',
                  'token_type': 'Bearer', 'expires_in': 5184000},
        ),
    )
    get = patch(
        'utils.linked_accounts.requests.get',
        return_value=_mock_resp(status=user_status, json_data=user_payload),
    )
    return post, get


class TestLinkedInLoginRoute:
    def test_redirects_to_linkedin_when_configured(self, app, client):
        _configure_linkedin(app)
        resp = client.get('/auth/linkedin/login')
        assert resp.status_code == 302
        parsed = urlparse(resp.location)
        assert parsed.netloc == 'www.linkedin.com'
        assert parsed.path == '/oauth/v2/authorization'
        params = parse_qs(parsed.query)
        assert params['client_id'] == ['test-linkedin-client-id']
        assert params['redirect_uri'] == [
            'http://localhost/auth/linkedin/callback'
        ]
        assert params['scope'] == ['openid profile email']
        assert params['response_type'] == ['code']
        assert params['state'][0]
        with client.session_transaction() as sess:
            assert sess['linkedin_oauth_state'] == params['state'][0]

    def test_redirect_uri_uses_forwarded_https_behind_proxy(
            self, app, client):
        # Same proxy setup as Google/GitHub/Facebook: the redirect_uri
        # sent to LinkedIn must be the public https:// URL, or
        # LinkedIn rejects it.
        _configure_linkedin(app)
        resp = client.get('/auth/linkedin/login', headers={
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'paid2match.work',
            'X-Forwarded-Port': '443',
        })
        assert resp.status_code == 302
        params = parse_qs(urlparse(resp.location).query)
        assert params['redirect_uri'] == [
            'https://paid2match.work/auth/linkedin/callback'
        ]

    def test_unconfigured_redirects_to_login(self, app, client):
        # TestingConfig sets no LinkedIn credentials.
        assert not app.config.get('LINKEDIN_CLIENT_ID')
        resp = client.get('/auth/linkedin/login', follow_redirects=True)
        assert resp.status_code == 200
        assert b'LinkedIn sign-in is not available' in resp.data

    def test_logged_in_user_goes_to_board(self, app, client, logged_in_user):
        _configure_linkedin(app)
        resp = client.get('/auth/linkedin/login')
        assert resp.status_code == 302
        assert resp.location.endswith('/needs/')


class TestLinkedInButton:
    def test_button_hidden_when_unconfigured(self, client):
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with LinkedIn' not in resp.data

    def test_button_shown_when_configured(self, app, client):
        _configure_linkedin(app)
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with LinkedIn' in resp.data
        assert b'/auth/linkedin/login' in resp.data
        # LinkedIn is the only configured provider here, so the
        # intro lists LinkedIn alone — never providers that are off.
        assert b'Sign in with LinkedIn' in resp.data
        assert b'Sign in with Google' not in resp.data

    def test_button_hidden_when_flag_off(self, app, client):
        # Credentials configured but the kill-switch off (the LinkedIn
        # app does not exist yet — it needs a Company Page first): no
        # button, and with no provider left at all the page shows
        # the unavailable warning instead of an intro line
        # advertising providers that are not there.
        app.config['LINKEDIN_CLIENT_ID'] = 'test-linkedin-client-id'
        app.config['LINKEDIN_CLIENT_SECRET'] = 'test-linkedin-client-secret'
        assert not app.config.get('LINKEDIN_LOGIN_ENABLED')
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with LinkedIn' not in resp.data
        assert b'/auth/linkedin/login' not in resp.data
        assert b'Sign-in is not available right now' in resp.data
        assert b'Sign in with Google' not in resp.data
        assert b'Sign in with LinkedIn' not in resp.data

    def test_intro_lists_all_four_when_both_flags_on(self, app, client):
        _configure_linkedin(app)
        _configure_facebook(app)
        app.config['GOOGLE_CLIENT_ID'] = 'test-google-client-id'
        app.config['GOOGLE_CLIENT_SECRET'] = 'test-google-client-secret'
        app.config['GITHUB_CLIENT_ID'] = 'test-github-client-id'
        app.config['GITHUB_CLIENT_SECRET'] = 'test-github-client-secret'
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with Google' in resp.data
        assert b'Continue with GitHub' in resp.data
        assert b'Continue with LinkedIn' in resp.data
        assert b'Continue with Facebook' in resp.data
        assert (
            b'Sign in with Google, GitHub, Facebook or LinkedIn'
            in resp.data
        )


class TestLinkedInCallbackCreate:
    def test_creates_new_verified_user_and_logs_in(self, app, client):
        _configure_linkedin(app)
        _set_state(client)
        post, get = _linkedin_mocks()
        with post, get:
            resp = client.get(
                '/auth/linkedin/callback?code=fake-code&state=test-state'
            )
        assert resp.status_code == 302
        assert resp.location.endswith('/needs/')
        with app.app_context():
            user = User.query.filter_by(email='newuser@example.com').first()
            assert user is not None
            assert user.full_name == 'Link In'
            assert user.role == 'user'
            assert user.is_active is True
            assert user.email_verified is True
            assert user.email_verified_at is not None
            assert user.last_login is not None
        with client.session_transaction() as sess:
            assert sess.get('_user_id') == user.id

    def test_name_falls_back_to_email_local_part(self, app, client):
        _configure_linkedin(app)
        _set_state(client)
        post, get = _linkedin_mocks(user={
            'sub': 'linkedin-user-2',
            'name': None,
            'email': 'noname@example.com',
            'email_verified': True,
        })
        with post, get:
            client.get('/auth/linkedin/callback?code=x&state=test-state')
        with app.app_context():
            user = User.query.filter_by(email='noname@example.com').first()
            assert user is not None
            assert user.full_name == 'noname'

    def test_no_email_rejected_cleanly(self, app, client):
        # The user declined the email scope: userinfo returns no
        # email. No account is created; the message points at Google
        # or GitHub.
        _configure_linkedin(app)
        _set_state(client)
        post, get = _linkedin_mocks(user={
            'sub': 'linkedin-user-3',
            'name': 'No Email',
            'email': None,
        })
        with post, get:
            resp = client.get(
                '/auth/linkedin/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'did not share an email address' in resp.data
        assert b'Google or GitHub' in resp.data
        with app.app_context():
            assert User.query.count() == 0


class TestLinkedInCallbackMatch:
    def test_matches_existing_user_and_marks_verified(self, app, client):
        _configure_linkedin(app)
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
        # LinkedIn returns the same email in a different case — the
        # match is case-insensitive.
        post, get = _linkedin_mocks(user={
            'sub': 'linkedin-user-4',
            'name': 'Existing User',
            'email': 'Existing@Example.com',
            'email_verified': True,
        })
        with post, get:
            resp = client.get(
                '/auth/linkedin/callback?code=x&state=test-state'
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
        _configure_linkedin(app)
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
        post, get = _linkedin_mocks(user={
            'sub': 'linkedin-user-5',
            'name': 'Gone User',
            'email': 'gone@example.com',
            'email_verified': True,
        })
        with post, get:
            resp = client.get(
                '/auth/linkedin/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'deactivated' in resp.data
        with client.session_transaction() as sess:
            assert '_user_id' not in sess


class TestLinkedInCallbackRejections:
    def test_state_mismatch_rejected(self, app, client):
        _configure_linkedin(app)
        _set_state(client, state='real-state')
        post, get = _linkedin_mocks()
        with post, get:
            resp = client.get(
                '/auth/linkedin/callback?code=x&state=wrong-state',
                follow_redirects=True,
            )
        assert b'invalid or has expired' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_missing_state_rejected(self, app, client):
        _configure_linkedin(app)
        # No state in the session at all (callback without a login start).
        post, get = _linkedin_mocks()
        with post, get:
            resp = client.get(
                '/auth/linkedin/callback?code=x&state=whatever',
                follow_redirects=True,
            )
        assert b'invalid or has expired' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_linkedin_error_param_rejected(self, app, client):
        _configure_linkedin(app)
        _set_state(client)
        resp = client.get(
            '/auth/linkedin/callback?error=user_cancelled_login&state=test-state',
            follow_redirects=True,
        )
        assert b'cancelled or failed' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_token_exchange_failure_rejected(self, app, client):
        _configure_linkedin(app)
        _set_state(client)
        post, get = _linkedin_mocks(token_status=400, token_json={})
        with post, get:
            resp = client.get(
                '/auth/linkedin/callback?code=bad-code&state=test-state',
                follow_redirects=True,
            )
        assert b'rejected the sign-in' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_token_exchange_network_error_rejected(self, app, client):
        _configure_linkedin(app)
        _set_state(client)
        with patch(
            'utils.linked_accounts.requests.post',
            side_effect=requests.RequestException('boom'),
        ):
            resp = client.get(
                '/auth/linkedin/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'Could not reach the provider' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_unverified_linkedin_email_rejected(self, app, client):
        # LinkedIn's userinfo carries email_verified, like Google's —
        # an unverified email must not create a verified account.
        _configure_linkedin(app)
        _set_state(client)
        post, get = _linkedin_mocks(user={
            'sub': 'linkedin-user-6',
            'name': 'Unverified',
            'email': 'unverified@example.com',
            'email_verified': False,
        })
        with post, get:
            resp = client.get(
                '/auth/linkedin/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'has not verified that email address' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_userinfo_bad_json_rejected(self, app, client):
        # A 200 whose body is not JSON must fail cleanly, not 500 on
        # resp.json().
        _configure_linkedin(app)
        _set_state(client)
        post, _ = _linkedin_mocks()
        get = patch(
            'utils.linked_accounts.requests.get',
            return_value=_mock_bad_json(),
        )
        with post, get:
            resp = client.get(
                '/auth/linkedin/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'Could not read your profile from LinkedIn' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_profile_fetch_failure_rejected(self, app, client):
        _configure_linkedin(app)
        _set_state(client)
        post, get = _linkedin_mocks(user_status=500)
        with post, get:
            resp = client.get(
                '/auth/linkedin/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'Could not read your profile from LinkedIn' in resp.data
        with app.app_context():
            assert User.query.count() == 0
