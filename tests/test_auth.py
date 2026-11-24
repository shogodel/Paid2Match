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
