"""Tests for the help page and the admin account-recovery tool."""
from extensions import db
from models.user import User
from models.audit_log import AuditLog


class TestHelpPage:
    def test_help_page_loads(self, client):
        r = client.get('/help')
        assert r.status_code == 200
        body = r.data.decode()
        assert 'support@paid2match.work' in body
        # Jinja escapes the apostrophe in the rendered HTML.
        assert 'Can&#39;t sign in?' in body

    def test_help_page_is_public(self, client):
        # No login needed — a locked-out user must be able to read it.
        r = client.get('/help')
        assert r.status_code == 200

    def test_login_page_links_to_help(self, client):
        r = client.get('/auth/login')
        assert r.status_code == 200
        assert '/help' in r.data.decode()

    def test_footer_links_to_help(self, client):
        r = client.get('/')
        assert r.status_code == 200
        assert '/help' in r.data.decode()

    def test_settings_shows_backup_nudge(self, logged_in_user, client):
        r = logged_in_user.get('/auth/settings')
        assert r.status_code == 200
        assert 'backup' in r.data.decode().lower()


class TestAdminChangeEmail:
    def test_change_email_happy_path(
            self, logged_in_admin, client, app, sample_user, sample_admin):
        old_email = sample_user.email
        r = logged_in_admin.post(
            f'/admin/users/{sample_user.id}/change-email',
            data={'new_email': 'recovered@example.com'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            user = db.session.get(User, sample_user.id)
            assert user.email == 'recovered@example.com'
            # New address is unverified until the next OAuth sign-in.
            assert user.email_verified is False
            assert user.email_verified_at is None
            log = AuditLog.query.filter_by(
                action='change_user_email',
                target_id=sample_user.id).first()
            assert log is not None
            assert log.admin_id == sample_admin.id
            assert old_email in log.details
            assert 'recovered@example.com' in log.details

    def test_change_email_duplicate_rejected(
            self, logged_in_admin, client, app, sample_user, second_user):
        r = logged_in_admin.post(
            f'/admin/users/{sample_user.id}/change-email',
            data={'new_email': 'second@example.com'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(User, sample_user.id).email == \
                sample_user.email
            assert AuditLog.query.filter_by(
                action='change_user_email').count() == 0

    def test_change_email_invalid_rejected(
            self, logged_in_admin, client, app, sample_user):
        r = logged_in_admin.post(
            f'/admin/users/{sample_user.id}/change-email',
            data={'new_email': 'not-an-email'},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(User, sample_user.id).email == \
                sample_user.email
            assert AuditLog.query.filter_by(
                action='change_user_email').count() == 0

    def test_change_email_same_address_is_noop(
            self, logged_in_admin, client, app, sample_user):
        r = logged_in_admin.post(
            f'/admin/users/{sample_user.id}/change-email',
            data={'new_email': sample_user.email},
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert AuditLog.query.filter_by(
                action='change_user_email').count() == 0

    def test_change_email_non_admin_forbidden(
            self, logged_in_user, client, app, sample_user):
        r = logged_in_user.post(
            f'/admin/users/{sample_user.id}/change-email',
            data={'new_email': 'hacker@example.com'},
            follow_redirects=False)
        assert r.status_code == 302
        assert '/bounties' in r.headers['Location']
        with app.app_context():
            assert db.session.get(User, sample_user.id).email == \
                sample_user.email

    def test_change_email_requires_login(self, client, app, sample_user):
        r = client.post(
            f'/admin/users/{sample_user.id}/change-email',
            data={'new_email': 'anon@example.com'},
            follow_redirects=False)
        assert r.status_code == 302
        assert '/auth/login' in r.headers['Location']
        with app.app_context():
            assert db.session.get(User, sample_user.id).email == \
                sample_user.email
