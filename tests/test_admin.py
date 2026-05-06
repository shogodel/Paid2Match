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

    def test_create_admin(self, logged_in_admin, client, app):
        r = logged_in_admin.post('/admin/create-admin', data={
            'email': 'newadmin@test.com',
            'password': 'NewAdmin123!',
        }, follow_redirects=False)
        assert r.status_code == 302