"""Tests for auth blueprint — login, register, logout."""
import pytest


class TestAuthLogin:
    """Tests for GET/POST /auth/login."""

    def test_login_page_loads(self, client):
        """Login page returns 200."""
        r = client.get('/auth/login')
        assert r.status_code == 200

    def test_login_success(self, client, app, sample_user):
        """Valid credentials redirect to bounties board."""
        r = client.post('/auth/login', data={
            'email': 'testuser@example.com',
            'password': 'TestPass123!',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert '/bounties/' in r.location

    def test_login_wrong_password(self, client, sample_user):
        """Wrong password shows error and returns 200."""
        r = client.post('/auth/login', data={
            'email': 'testuser@example.com',
            'password': 'WrongPassword',
        })
        assert r.status_code == 200
        assert b'Invalid email or password' in r.data

    def test_login_nonexistent_user(self, client):
        """Nonexistent email shows same error message."""
        r = client.post('/auth/login', data={
            'email': 'nobody@example.com',
            'password': 'AnyPassword',
        })
        assert r.status_code == 200
        assert b'Invalid email or password' in r.data

    def test_login_inactive_user(self, client, app, sample_user):
        """Deactivated account is rejected."""
        from extensions import db
        with app.app_context():
            from models.user import User
            u = db.session.get(User, sample_user.id)
            u.is_active = False
            db.session.commit()

        r = client.post('/auth/login', data={
            'email': 'testuser@example.com',
            'password': 'TestPass123!',
        })
        assert r.status_code == 200
        assert b'deactivated' in r.data

    def test_login_redirect_next_param(self, client, sample_user):
        """Login preserves next redirect URL."""
        r = client.post('/auth/login?next=/bounties/', data={
            'email': 'testuser@example.com',
            'password': 'TestPass123!',
        })
        assert r.status_code == 302
        assert '/bounties/' in r.location

    def test_authenticated_user_redirected(self, logged_in_user, client):
        """Already logged-in user is redirected from login page."""
        r = logged_in_user.get('/auth/login')
        assert r.status_code == 302


class TestAuthRegister:
    """Tests for GET/POST /auth/register."""

    def test_register_page_loads(self, client):
        """Registration page returns 200."""
        r = client.get('/auth/register')
        assert r.status_code == 200

    def test_register_success(self, client, app):
        """Valid registration creates user + profile and redirects."""
        r = client.post('/auth/register', data={
            'full_name': 'New User',
            'email': 'newuser@example.com',
            'password': 'NewPass123!',
            'confirm_password': 'NewPass123!',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert '/profile/setup' in r.location

        # Verify user created
        with app.app_context():
            from models.user import User
            from extensions import db
            u = User.query.filter_by(email='newuser@example.com').first()
            assert u is not None
            assert u.full_name == 'New User'
            assert u.role == 'user'

    def test_register_duplicate_email(self, client, sample_user):
        """Duplicate email shows error."""
        r = client.post('/auth/register', data={
            'full_name': 'Another User',
            'email': 'testuser@example.com',
            'password': 'Pass123456!',
            'confirm_password': 'Pass123456!',
        })
        assert r.status_code == 200
        assert b'already exists' in r.data

    def test_register_password_mismatch(self, client):
        """Mismatched passwords show error."""
        r = client.post('/auth/register', data={
            'full_name': 'User Name',
            'email': 'unique@example.com',
            'password': 'Pass123456!',
            'confirm_password': 'DifferentPass1!',
        })
        assert r.status_code == 200
        assert b'must match' in r.data

    def test_register_short_password(self, client):
        """Password < 8 chars is rejected."""
        r = client.post('/auth/register', data={
            'full_name': 'User Name',
            'email': 'short@example.com',
            'password': 'Short1!',
            'confirm_password': 'Short1!',
        })
        assert r.status_code == 200
        assert b'at least 8 characters' in r.data


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
        assert '/bounties/' in r.location

    def test_auth_index_redirects(self, client):
        """GET /auth/ redirects to login."""
        r = client.get('/auth/')
        assert r.status_code == 302