"""Tests for admin blueprint."""
import pytest


class TestAdminDashboard:
    def test_dashboard_requires_admin(self, client, sample_user):
        r = client.get('/admin/')
        assert r.status_code == 302

    def test_dashboard_loads_for_admin(self, logged_in_admin, client):
        r = logged_in_admin.get('/admin/')
        assert r.status_code == 200
        assert b'Dashboard' in r.data

    def test_dashboard_shows_stats(self, logged_in_admin, client, sample_bounty):
        r = logged_in_admin.get('/admin/')
        assert r.status_code == 200


class TestAdminUsers:
    def test_users_requires_admin(self, client):
        r = client.get('/admin/users')
        assert r.status_code == 302

    def test_users_page_loads(self, logged_in_admin, client):
        r = logged_in_admin.get('/admin/users')
        assert r.status_code == 200

    def test_activate_user(self, logged_in_admin, client, app, sample_hunter):
        from extensions import db
        with app.app_context():
            from models.user import User
            u = db.session.get(User, sample_hunter.id)
            u.is_active = False
            db.session.commit()

        r = logged_in_admin.post(
            f'/admin/users/{sample_hunter.id}/activate',
            follow_redirects=False
        )
        assert r.status_code == 302

        with app.app_context():
            from models.user import User
            from extensions import db
            u = db.session.get(User, sample_hunter.id)
            assert u.is_active == True

    def test_deactivate_user(self, logged_in_admin, client, app, sample_hunter):
        r = logged_in_admin.post(
            f'/admin/users/{sample_hunter.id}/deactivate',
            follow_redirects=False
        )
        assert r.status_code == 302

        with app.app_context():
            from models.user import User
            from extensions import db
            u = db.session.get(User, sample_hunter.id)
            assert u.is_active == False

    def test_admin_can_verify_user_email(self, logged_in_admin, client, app):
        """Admin manually marks an unverified user's email verified."""
        from werkzeug.security import generate_password_hash
        from extensions import db
        from models.user import User
        with app.app_context():
            user = User(
                email='verify@example.com',
                full_name='Verify User',
                password_hash=generate_password_hash('VerifyPass123!'),
                role='user',
                is_active=True,
                email_verified=False,
            )
            db.session.add(user)
            db.session.commit()
            user_id = user.id

        r = logged_in_admin.post(f'/admin/users/{user_id}/verify-email',
                                 follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(User, user_id).email_verified is True


class TestAdminBounties:
    def test_bounties_page_loads(self, logged_in_admin, client, sample_bounty):
        r = logged_in_admin.get('/admin/bounties')
        assert r.status_code == 200

    def test_approve_bounty(self, logged_in_admin, client, app, sample_paid_bounty):
        r = logged_in_admin.post(
            f'/admin/bounties/{sample_paid_bounty.id}/approve',
            follow_redirects=False
        )
        assert r.status_code == 302

        with app.app_context():
            from extensions import db
            from models.bounty import Bounty
            b = db.session.get(Bounty, sample_paid_bounty.id)
            assert b.status == 'open'

    def test_reject_bounty(self, logged_in_admin, client, app, sample_paid_bounty):
        r = logged_in_admin.post(
            f'/admin/bounties/{sample_paid_bounty.id}/reject',
            follow_redirects=False
        )
        assert r.status_code == 302

        with app.app_context():
            from extensions import db
            from models.bounty import Bounty
            b = db.session.get(Bounty, sample_paid_bounty.id)
            assert b.status == 'closed'


class TestAdminSettings:
    def test_settings_loads(self, logged_in_admin, client):
        r = logged_in_admin.get('/admin/settings')
        assert r.status_code == 200

    def test_create_admin(self, client, app, monkeypatch):
        # The old test only asserted a 302 — and it never created an
        # admin at all: with sample_admin in the DB the route takes
        # the 'Admin already exists' path. This one bootstraps for
        # real (no admin in the DB, setup token supplied) and checks
        # the user exists with the admin role afterwards.
        monkeypatch.setenv('ADMIN_SETUP_TOKEN', 'test-setup-token')
        r = client.post('/admin/create-admin', data={
            'token': 'test-setup-token',
            'email': 'newadmin@test.com',
            'password': 'NewAdmin123!',
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            from models.user import User
            user = User.query.filter_by(email='newadmin@test.com').first()
            assert user is not None
            assert user.role == 'admin'
            assert user.is_active is True

    def test_create_admin_blocked_when_admin_exists(
            self, logged_in_admin, client, app, sample_admin):
        # The path the old test was actually exercising: an admin
        # already exists, so the bootstrap refuses and creates
        # nobody.
        r = logged_in_admin.post('/admin/create-admin', data={
            'email': 'newadmin@test.com',
            'password': 'NewAdmin123!',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'Admin already exists' in r.data
        with app.app_context():
            from models.user import User
            assert User.query.filter_by(
                email='newadmin@test.com').first() is None

    def test_create_admin_needs_setup_token(self, client, app, monkeypatch):
        # No ADMIN_SETUP_TOKEN configured: self-service creation is
        # disabled entirely (403), even with no admin in the DB.
        monkeypatch.delenv('ADMIN_SETUP_TOKEN', raising=False)
        r = client.post('/admin/create-admin', data={
            'token': 'whatever',
            'email': 'newadmin@test.com',
            'password': 'NewAdmin123!',
        })
        assert r.status_code == 403
        with app.app_context():
            from models.user import User
            assert User.query.filter_by(
                email='newadmin@test.com').first() is None