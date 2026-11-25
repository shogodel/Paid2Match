"""Tests for auth blueprint — OAuth-only login page, logout.

There is no email/password login or registration anymore: sign-in is
Google or GitHub (see test_google_oauth.py / test_github_oauth.py).
"""
import pytest


class TestAuthLogin:
    """Tests for GET /auth/login."""

    def test_login_page_loads(self, client):
        """Login page returns 200."""
        r = client.get('/auth/login')
        assert r.status_code == 200

    def test_login_page_has_no_password_form(self, client):
        """The login page is OAuth buttons only — no email/password
        form, no register/forgot-password links."""
        html = client.get('/auth/login').data.decode()
        assert 'type="password"' not in html
        assert '/auth/register' not in html
        assert '/auth/forgot-password' not in html
        assert '/auth/resend-verification' not in html

    def test_login_post_is_not_allowed(self, client):
        """POST /auth/login no longer exists (OAuth-only)."""
        r = client.post('/auth/login', data={
            'email': 'testuser@example.com',
            'password': 'TestPass123!',
        })
        assert r.status_code == 405

    def test_register_route_is_gone(self, client):
        """GET /auth/register 404s — registration is via OAuth sign-in."""
        assert client.get('/auth/register').status_code == 404
        assert client.post('/auth/register', data={}).status_code == 404

    def test_password_reset_routes_are_gone(self, client):
        """Forgot/reset/resend/confirm routes 404."""
        assert client.get('/auth/forgot-password').status_code == 404
        assert client.get('/auth/reset-password/abc').status_code == 404
        assert client.get('/auth/resend-verification').status_code == 404
        assert client.get('/auth/confirm/abc').status_code == 404

    def test_authenticated_user_redirected(self, logged_in_user, client):
        """Already logged-in user is redirected from login page."""
        r = logged_in_user.get('/auth/login')
        assert r.status_code == 302


class TestLoginProviderMatrix:
    """Every provider combination renders the right buttons AND the
    right intro line. Google/GitHub show when configured; Facebook /
    LinkedIn additionally need their kill-switch flag. With no
    provider at all there is no intro line — only the warning."""

    _PROVIDERS = ('Google', 'GitHub', 'Facebook', 'LinkedIn')

    def _configure(self, app, enabled):
        for name in ('GOOGLE', 'GITHUB', 'FACEBOOK', 'LINKEDIN'):
            app.config[f'{name}_CLIENT_ID'] = None
            app.config[f'{name}_CLIENT_SECRET'] = None
        app.config['FACEBOOK_LOGIN_ENABLED'] = False
        app.config['LINKEDIN_LOGIN_ENABLED'] = False
        for provider in enabled:
            key = provider.upper()
            app.config[f'{key}_CLIENT_ID'] = f'test-{key.lower()}-id'
            app.config[f'{key}_CLIENT_SECRET'] = f'test-{key.lower()}-secret'
        if 'Facebook' in enabled:
            app.config['FACEBOOK_LOGIN_ENABLED'] = True
        if 'LinkedIn' in enabled:
            app.config['LINKEDIN_LOGIN_ENABLED'] = True

    def _expected_intro(self, enabled):
        if not enabled:
            return None
        if len(enabled) == 1:
            return f'Sign in with {enabled[0]} — no password needed.'
        joined = ', '.join(enabled[:-1]) + ' or ' + enabled[-1]
        return f'Sign in with {joined} — no password needed.'

    @pytest.mark.parametrize('enabled', [
        (),
        ('Google',),
        ('GitHub',),
        ('Facebook',),
        ('LinkedIn',),
        ('Google', 'GitHub'),
        ('Google', 'GitHub', 'Facebook'),
        ('Google', 'GitHub', 'LinkedIn'),
        ('Google', 'GitHub', 'Facebook', 'LinkedIn'),
        ('Facebook', 'LinkedIn'),
    ])
    def test_provider_combination(self, app, client, enabled):
        self._configure(app, enabled)
        html = client.get('/auth/login').data.decode()
        for provider in self._PROVIDERS:
            button = f'Continue with {provider}'
            link = f"/auth/{provider.lower()}/login"
            if provider in enabled:
                assert button in html, f'{button} missing for {enabled}'
                assert link in html, f'{link} missing for {enabled}'
            else:
                assert button not in html, f'{button} shown for {enabled}'
                assert link not in html, f'{link} shown for {enabled}'
        expected = self._expected_intro(enabled)
        if expected is None:
            assert 'Sign in with' not in html
            assert 'Sign-in is not available right now' in html
        else:
            assert expected in html, f'intro wrong for {enabled}'
            assert 'Sign-in is not available right now' not in html

    def test_flag_off_hides_button_but_keeps_intro_honest(
            self, app, client):
        # Facebook + LinkedIn credentials present but both flags off:
        # Google/GitHub only, and the intro says exactly that.
        self._configure(app, ('Google', 'GitHub'))
        app.config['FACEBOOK_CLIENT_ID'] = 'test-facebook-id'
        app.config['FACEBOOK_CLIENT_SECRET'] = 'test-facebook-secret'
        app.config['LINKEDIN_CLIENT_ID'] = 'test-linkedin-id'
        app.config['LINKEDIN_CLIENT_SECRET'] = 'test-linkedin-secret'
        html = client.get('/auth/login').data.decode()
        assert 'Continue with Facebook' not in html
        assert 'Continue with LinkedIn' not in html
        assert 'Sign in with Google or GitHub' in html


class TestAuthLogout:
    """Tests for GET /auth/logout."""

    def test_logout_requires_login(self, client):
        """Logout redirects unauthenticated users."""
        r = client.get('/auth/logout', follow_redirects=False)
        assert r.status_code == 302

    def test_logout_success(self, logged_in_user, client):
        """Logged-in user is logged out."""
        r = logged_in_user.get('/auth/logout', follow_redirects=False)
        assert r.status_code == 302
        assert '/needs/' in r.location

    def test_auth_index_redirects(self, client):
        """GET /auth/ redirects to login."""
        r = client.get('/auth/')
        assert r.status_code == 302


class TestLanguageSwitcherRedirect:
    """The language switcher's ?next= uses the same safe-redirect
    logic as login — a backslash target must not escape the site."""

    def test_backslash_next_url_rejected(self, client):
        r = client.get('/set-language/en?next=/\\evil.example.com/x')
        assert r.status_code == 302
        assert 'evil.example.com' not in r.location
        assert r.location.endswith('/')

    def test_safe_next_url_honoured(self, client):
        r = client.get('/set-language/en?next=/help')
        assert r.status_code == 302
        assert r.location.endswith('/help')


class TestDeactivatedUserSession:
    """Flask-Login only checks is_active at login time — a session or
    remember-cookie reload never re-checks it. The user loader must
    reject deactivated/deleted users so they resolve to anonymous
    instead of a loaded user object. (UserMixin.is_authenticated
    already gates on is_active, so @login_required routes reject them
    either way — the loader fix is the belt to that braces, closing
    any code path that uses current_user without checking
    is_authenticated first.)"""

    def test_user_loader_returns_none_for_inactive_user(
            self, app, sample_user):
        from extensions import db
        from models.user import User
        with app.app_context():
            loader = app.login_manager._user_callback
            assert loader(str(sample_user.id)) is not None
            user = db.session.get(User, sample_user.id)
            user.is_active = False
            db.session.commit()
            assert loader(str(sample_user.id)) is None

    def test_user_loader_returns_none_for_deleted_user(
            self, app, sample_user):
        from extensions import db
        from models.user import User
        with app.app_context():
            loader = app.login_manager._user_callback
            user = db.session.get(User, sample_user.id)
            user.soft_delete()
            db.session.commit()
            assert loader(str(sample_user.id)) is None

    def test_deactivated_user_is_logged_out_on_next_request(
            self, app, client, sample_user, login_as):
        from flask import g
        login_as(client, sample_user.email)
        assert client.get('/profile/linked').status_code == 200
        with app.app_context():
            from extensions import db
            from models.user import User
            user = db.session.get(User, sample_user.id)
            user.is_active = False
            db.session.commit()
        # A real next request gets a fresh flask.g; the test shares
        # the app context, so drop Flask-Login's cached user.
        g.pop('_login_user', None)
        r = client.get('/profile/linked')
        assert r.status_code == 302
        assert '/auth/login' in r.location

    def test_deleted_user_is_logged_out_on_next_request(
            self, app, client, sample_user, login_as):
        from flask import g
        login_as(client, sample_user.email)
        assert client.get('/profile/linked').status_code == 200
        with app.app_context():
            from extensions import db
            from models.user import User
            user = db.session.get(User, sample_user.id)
            user.soft_delete()
            db.session.commit()
        g.pop('_login_user', None)
        r = client.get('/profile/linked')
        assert r.status_code == 302
        assert '/auth/login' in r.location


class TestDeleteAccount:
    """Account deletion is confirmed by typing DELETE (OAuth-only auth
    has no password to check)."""

    def test_delete_page_loads(self, logged_in_user, client):
        r = logged_in_user.get('/auth/delete-account')
        assert r.status_code == 200
        assert b'type DELETE' in r.data
        assert b'type="password"' not in r.data

    def test_delete_requires_typed_confirmation(
            self, app, logged_in_user, client, sample_user):
        r = logged_in_user.post('/auth/delete-account', data={
            'confirmation': 'nope',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'Type DELETE to confirm' in r.data
        with app.app_context():
            from extensions import db
            from models.user import User
            assert db.session.get(User, sample_user.id).is_deleted is False

    def test_delete_with_confirmation_soft_deletes(
            self, app, logged_in_user, client, sample_user):
        r = logged_in_user.post('/auth/delete-account', data={
            'confirmation': 'DELETE',
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            from extensions import db
            from models.user import User
            assert db.session.get(User, sample_user.id).is_deleted is True

    def test_delete_anonymizes_profile_links_and_public_page(
            self, app, logged_in_user, client, sample_user):
        """The delete page promises the profile is removed from public
        view and the name becomes 'Deleted User' — but display_name,
        avatar, company, skills and social links survived, linked
        accounts survived, and /profile/<handle> still rendered the
        whole page (name, posts, links) under the old handle."""
        from extensions import db
        from models.linked_account import LinkedAccount
        from models.profile import Profile
        from models.user import User
        with app.app_context():
            profile = Profile.query.filter_by(
                user_id=sample_user.id).first()
            profile.display_name = 'Test User'
            profile.avatar_url = 'https://example.com/avatar.png'
            profile.company_name = 'Acme Inc'
            profile.skills = 'Python, Sales'
            profile.website = 'https://example.com'
            profile.linkedin_url = 'https://linkedin.com/in/testuser'
            profile.github_url = 'https://github.com/testuser'
            profile.twitter_handle = '@testuser'
            profile.instagram_url = 'https://instagram.com/testuser'
            profile.facebook_url = 'https://facebook.com/testuser'
            profile.tiktok_url = 'https://tiktok.com/@testuser'
            profile.youtube_url = 'https://youtube.com/@testuser'
            db.session.add(LinkedAccount(
                user_id=sample_user.id, platform='github',
                verification_method='oauth', status='verified',
                handle='testuser',
                url='https://github.com/testuser',
            ))
            db.session.commit()
            handle = db.session.get(User, sample_user.id).handle
            user_id = sample_user.id
        # Sanity: the public profile renders before deletion.
        assert client.get(f'/profile/{handle}').status_code == 200
        r = logged_in_user.post('/auth/delete-account', data={
            'confirmation': 'DELETE',
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            profile = Profile.query.filter_by(user_id=user_id).first()
            assert not profile.display_name
            assert not profile.avatar_url
            assert not profile.company_name
            assert not profile.skills
            assert not profile.website
            assert not profile.linkedin_url
            assert not profile.github_url
            assert not profile.twitter_handle
            assert not profile.instagram_url
            assert not profile.facebook_url
            assert not profile.tiktok_url
            assert not profile.youtube_url
            assert LinkedAccount.query.filter_by(
                user_id=user_id).count() == 0
        # The public page is gone under both the handle and the UUID.
        assert client.get(f'/profile/{handle}').status_code == 404
        assert client.get(f'/profile/{user_id}').status_code == 404

    def test_delete_form_carries_csrf_token(
            self, app, logged_in_user, client, sample_user):
        """The delete form is a raw <form>, not a WTForm — it must
        carry its own csrf_token input. Without it the POST 400s
        wherever CSRF is enabled (production), while the suite (CSRF
        off) never noticed."""
        import re
        app.config['WTF_CSRF_ENABLED'] = True
        # No token at all: rejected, proving CSRF is actually on.
        r = logged_in_user.post('/auth/delete-account', data={
            'confirmation': 'DELETE',
        })
        assert r.status_code == 400
        # The token rendered in the form: deletion succeeds.
        html = logged_in_user.get('/auth/delete-account').data.decode()
        token = re.search(
            r'name="csrf_token" value="([^"]+)"', html
        ).group(1)
        r = logged_in_user.post('/auth/delete-account', data={
            'confirmation': 'DELETE',
            'csrf_token': token,
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            from extensions import db
            from models.user import User
            assert db.session.get(User, sample_user.id).is_deleted is True
