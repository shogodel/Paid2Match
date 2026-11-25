"""GitHub sign-in ("Continue with GitHub") tests — login redirect,
callback match-or-create, hidden-email fallback via /user/emails, state
verification, failure paths. All HTTP calls to GitHub are mocked — no
network calls in the suite."""
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


def _configure_github(app):
    """Pretend GitHub OAuth is configured (test credentials only)."""
    app.config['GITHUB_CLIENT_ID'] = 'test-github-client-id'
    app.config['GITHUB_CLIENT_SECRET'] = 'test-github-client-secret'


def _set_state(client, state='test-state'):
    with client.session_transaction() as sess:
        sess['github_oauth_state'] = state


def _github_mocks(user=None, emails=None, token_status=200,
                  user_status=200, emails_status=200, token_json=None):
    """Patches for the GitHub HTTP calls: POST to the token endpoint,
    GET to /user (and /user/emails when the profile hides the email).
    Returns (post_patch, get_patch, calls) where calls records every
    GET URL, so tests can assert the emails fallback ran (or not)."""
    calls = []
    user_payload = user if user is not None else {
        'login': 'octocat',
        'name': 'Octo Cat',
        'email': 'newuser@example.com',
        'html_url': 'https://github.com/octocat',
    }
    emails_payload = emails if emails is not None else []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith('/user/emails'):
            return _mock_resp(status=emails_status, json_data=emails_payload)
        return _mock_resp(status=user_status, json_data=user_payload)

    post = patch(
        'utils.linked_accounts.requests.post',
        return_value=_mock_resp(
            status=token_status,
            json_data=token_json if token_json is not None
            else {'access_token': 'fake-access-token'},
        ),
    )
    get = patch('utils.linked_accounts.requests.get', side_effect=fake_get)
    return post, get, calls


class TestGitHubLoginRoute:
    def test_redirects_to_github_when_configured(self, app, client):
        _configure_github(app)
        resp = client.get('/auth/github/login')
        assert resp.status_code == 302
        parsed = urlparse(resp.location)
        assert parsed.netloc == 'github.com'
        assert parsed.path == '/login/oauth/authorize'
        params = parse_qs(parsed.query)
        assert params['client_id'] == ['test-github-client-id']
        assert params['redirect_uri'] == [
            'http://localhost/auth/github/callback'
        ]
        # Sign-in asks for user:email too, so a hidden email can still
        # be read for login (account linking only asks read:user).
        assert params['scope'] == ['read:user user:email']
        assert params['state'][0]
        with client.session_transaction() as sess:
            assert sess['github_oauth_state'] == params['state'][0]

    def test_redirect_uri_uses_forwarded_https_behind_proxy(
            self, app, client):
        # Same proxy setup as Google: the redirect_uri sent to GitHub
        # must be the public https:// URL, or GitHub rejects it.
        _configure_github(app)
        resp = client.get('/auth/github/login', headers={
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'paid2match.work',
            'X-Forwarded-Port': '443',
        })
        assert resp.status_code == 302
        params = parse_qs(urlparse(resp.location).query)
        assert params['redirect_uri'] == [
            'https://paid2match.work/auth/github/callback'
        ]

    def test_unconfigured_redirects_to_login(self, app, client):
        # TestingConfig sets no GitHub credentials.
        assert not app.config.get('GITHUB_CLIENT_ID')
        resp = client.get('/auth/github/login', follow_redirects=True)
        assert resp.status_code == 200
        assert b'GitHub sign-in is not available' in resp.data

    def test_logged_in_user_goes_to_board(self, app, client, logged_in_user):
        _configure_github(app)
        resp = client.get('/auth/github/login')
        assert resp.status_code == 302
        assert resp.location.endswith('/needs/')


class TestGitHubButton:
    def test_button_hidden_when_unconfigured(self, client):
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with GitHub' not in resp.data

    def test_button_shown_when_configured(self, app, client):
        _configure_github(app)
        resp = client.get('/auth/login')
        assert resp.status_code == 200
        assert b'Continue with GitHub' in resp.data
        assert b'/auth/github/login' in resp.data


class TestGitHubCallbackCreate:
    def test_creates_new_verified_user_and_logs_in(self, app, client):
        _configure_github(app)
        _set_state(client)
        post, get, calls = _github_mocks()
        with post, get:
            resp = client.get(
                '/auth/github/callback?code=fake-code&state=test-state'
            )
        assert resp.status_code == 302
        assert resp.location.endswith('/needs/')
        # Public email: the /user/emails fallback must NOT run.
        assert calls == ['https://api.github.com/user']
        with app.app_context():
            user = User.query.filter_by(email='newuser@example.com').first()
            assert user is not None
            assert user.full_name == 'Octo Cat'
            assert user.role == 'user'
            assert user.is_active is True
            assert user.email_verified is True
            assert user.email_verified_at is not None
            assert user.last_login is not None
        with client.session_transaction() as sess:
            assert sess.get('_user_id') == user.id

    def test_name_falls_back_to_github_login(self, app, client):
        _configure_github(app)
        _set_state(client)
        post, get, _ = _github_mocks(user={
            'login': 'octocat',
            'name': None,
            'email': 'login-fallback@example.com',
        })
        with post, get:
            client.get('/auth/github/callback?code=x&state=test-state')
        with app.app_context():
            user = User.query.filter_by(
                email='login-fallback@example.com').first()
            assert user is not None
            assert user.full_name == 'octocat'

    def test_name_falls_back_to_email_local_part(self, app, client):
        _configure_github(app)
        _set_state(client)
        post, get, _ = _github_mocks(user={
            'login': None,
            'name': None,
            'email': 'noname@example.com',
        })
        with post, get:
            client.get('/auth/github/callback?code=x&state=test-state')
        with app.app_context():
            user = User.query.filter_by(email='noname@example.com').first()
            assert user is not None
            assert user.full_name == 'noname'

    def test_hidden_email_falls_back_to_user_emails(self, app, client):
        _configure_github(app)
        _set_state(client)
        post, get, calls = _github_mocks(
            user={
                'login': 'privatecat',
                'name': 'Private Cat',
                'email': None,  # hidden on the public profile
            },
            emails=[
                {'email': 'old@example.com', 'primary': False,
                 'verified': True},
                {'email': 'private@example.com', 'primary': True,
                 'verified': True},
                {'email': 'unverified@example.com', 'primary': False,
                 'verified': False},
            ],
        )
        with post, get:
            resp = client.get(
                '/auth/github/callback?code=x&state=test-state'
            )
        assert resp.status_code == 302
        assert resp.location.endswith('/needs/')
        assert 'https://api.github.com/user/emails' in calls
        with app.app_context():
            # The primary VERIFIED address wins, not the first entry.
            user = User.query.filter_by(
                email='private@example.com').first()
            assert user is not None
            assert user.full_name == 'Private Cat'
            assert user.email_verified is True

    def test_emails_bad_json_rejected(self, app, client):
        # The /user/emails fallback returning a 200 whose body is not
        # JSON must fail cleanly, not 500 on resp.json().
        _configure_github(app)
        _set_state(client)

        def fake_get(url, **kwargs):
            if url.endswith('/user/emails'):
                return _mock_bad_json()
            return _mock_resp(json_data={
                'login': 'privatecat', 'name': 'Private Cat',
                'email': None,
            })

        post = patch(
            'utils.linked_accounts.requests.post',
            return_value=_mock_resp(
                json_data={'access_token': 'fake-access-token'}
            ),
        )
        get = patch(
            'utils.linked_accounts.requests.get', side_effect=fake_get
        )
        with post, get:
            resp = client.get(
                '/auth/github/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'Could not read your email from GitHub' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_no_verified_email_rejected(self, app, client):
        _configure_github(app)
        _set_state(client)
        post, get, _ = _github_mocks(
            user={'login': 'ghost', 'name': 'Ghost', 'email': None},
            emails=[
                {'email': 'unverified@example.com', 'primary': True,
                 'verified': False},
            ],
        )
        with post, get:
            resp = client.get(
                '/auth/github/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'did not share a verified email address' in resp.data
        with app.app_context():
            assert User.query.count() == 0


class TestGitHubCallbackMatch:
    def test_matches_existing_user_and_marks_verified(self, app, client):
        _configure_github(app)
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
        # GitHub returns the same email in a different case — the match
        # is case-insensitive.
        post, get, _ = _github_mocks(user={
            'login': 'existinguser',
            'name': 'Existing User',
            'email': 'Existing@Example.com',
        })
        with post, get:
            resp = client.get(
                '/auth/github/callback?code=x&state=test-state'
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
        _configure_github(app)
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
        post, get, _ = _github_mocks(user={
            'login': 'goneuser',
            'name': 'Gone User',
            'email': 'gone@example.com',
        })
        with post, get:
            resp = client.get(
                '/auth/github/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'deactivated' in resp.data
        with client.session_transaction() as sess:
            assert '_user_id' not in sess


class TestGitHubCallbackRejections:
    def test_state_mismatch_rejected(self, app, client):
        _configure_github(app)
        _set_state(client, state='real-state')
        post, get, _ = _github_mocks()
        with post, get:
            resp = client.get(
                '/auth/github/callback?code=x&state=wrong-state',
                follow_redirects=True,
            )
        assert b'invalid or has expired' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_missing_state_rejected(self, app, client):
        _configure_github(app)
        # No state in the session at all (callback without a login start).
        post, get, _ = _github_mocks()
        with post, get:
            resp = client.get(
                '/auth/github/callback?code=x&state=whatever',
                follow_redirects=True,
            )
        assert b'invalid or has expired' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_state_is_one_time_use(self, app, client):
        _configure_github(app)
        _set_state(client)
        post, get, _ = _github_mocks()
        with post, get:
            first = client.get(
                '/auth/github/callback?code=x&state=test-state'
            )
            assert first.status_code == 302
            assert first.location.endswith('/needs/')
            # Replay the same callback: the state was consumed.
            client.get('/auth/logout')
            replay = client.get(
                '/auth/github/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'invalid or has expired' in replay.data

    def test_github_error_param_rejected(self, app, client):
        _configure_github(app)
        _set_state(client)
        resp = client.get(
            '/auth/github/callback?error=access_denied&state=test-state',
            follow_redirects=True,
        )
        assert b'cancelled or failed' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_token_exchange_failure_rejected(self, app, client):
        _configure_github(app)
        _set_state(client)
        post, get, _ = _github_mocks(token_status=400, token_json={})
        with post, get:
            resp = client.get(
                '/auth/github/callback?code=bad-code&state=test-state',
                follow_redirects=True,
            )
        assert b'rejected the sign-in' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_token_exchange_network_error_rejected(self, app, client):
        _configure_github(app)
        _set_state(client)
        with patch(
            'utils.linked_accounts.requests.post',
            side_effect=requests.RequestException('boom'),
        ):
            resp = client.get(
                '/auth/github/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'Could not reach the provider' in resp.data
        with app.app_context():
            assert User.query.count() == 0

    def test_profile_fetch_failure_rejected(self, app, client):
        _configure_github(app)
        _set_state(client)
        post, get, _ = _github_mocks(user_status=500)
        with post, get:
            resp = client.get(
                '/auth/github/callback?code=x&state=test-state',
                follow_redirects=True,
            )
        assert b'Could not read your profile from GitHub' in resp.data
        with app.app_context():
            assert User.query.count() == 0
