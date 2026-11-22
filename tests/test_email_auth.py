"""Tests for M0 email verification + password reset flows."""
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db, mail
from models.user import User


def _make_user(app, email='verify@example.com', verified=False):
    """Create a user directly in the DB."""
    with app.app_context():
        user = User(
            email=email,
            full_name='Verify User',
            password_hash=generate_password_hash('VerifyPass123!'),
            role='user',
            is_active=True,
            email_verified=verified,
        )
        db.session.add(user)
        db.session.commit()
        return user.id


class TestEmailVerification:
    def test_register_sends_confirmation_email(self, client, app):
        with mail.record_messages() as outbox:
            r = client.post('/auth/register', data={
                'full_name': 'New Verify User',
                'email': 'confirmme@example.com',
                'password': 'NewPass123!',
                'confirm_password': 'NewPass123!',
            }, follow_redirects=False)
        assert r.status_code == 302
        assert len(outbox) == 1
        assert outbox[0].recipients == ['confirmme@example.com']
        assert 'Confirm your Paid2Match account' in outbox[0].subject
        assert '/auth/confirm/' in outbox[0].body

    def test_login_blocked_until_verified(self, client, app):
        _make_user(app)
        r = client.post('/auth/login', data={
            'email': 'verify@example.com',
            'password': 'VerifyPass123!',
        })
        assert r.status_code == 200
        assert b'verify your email' in r.data

    def test_login_works_after_verification(self, client, app):
        _make_user(app, verified=True)
        r = client.post('/auth/login', data={
            'email': 'verify@example.com',
            'password': 'VerifyPass123!',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert '/bounties/' in r.location

    def test_confirm_valid_token_verifies_and_logs_in(self, client, app):
        _make_user(app)
        with app.app_context():
            from utils.email import generate_confirmation_token
            token = generate_confirmation_token('verify@example.com')
        r = client.get(f'/auth/confirm/{token}', follow_redirects=False)
        assert r.status_code == 302
        assert '/profile/setup' in r.location
        with app.app_context():
            user = User.query.filter_by(email='verify@example.com').first()
            assert user.email_verified is True
            assert user.email_verified_at is not None

    def test_confirm_invalid_token_redirects_to_resend(self, client, app):
        r = client.get('/auth/confirm/this-is-not-a-valid-token', follow_redirects=False)
        assert r.status_code == 302
        assert '/auth/resend-verification' in r.location

    def test_confirm_expired_token_rejected(self, client, app):
        _make_user(app)
        with app.app_context():
            from utils.email import generate_confirmation_token, confirm_confirmation_token
            token = generate_confirmation_token('verify@example.com')
            assert confirm_confirmation_token(token) is not None
            # A negative max_age makes any token (age >= 0) expired.
            app.config['EMAIL_CONFIRM_TOKEN_MAX_AGE'] = -1
            assert confirm_confirmation_token(token) is None

    def test_resend_verification_sends_email(self, client, app):
        _make_user(app)
        with mail.record_messages() as outbox:
            r = client.post('/auth/resend-verification', data={
                'email': 'verify@example.com',
            }, follow_redirects=False)
        assert r.status_code == 302
        assert len(outbox) == 1
        assert '/auth/confirm/' in outbox[0].body

    def test_resend_for_unknown_email_sends_nothing_but_same_message(self, client, app):
        with mail.record_messages() as outbox:
            r = client.post('/auth/resend-verification', data={
                'email': 'nobody@example.com',
            }, follow_redirects=True)
        assert r.status_code == 200
        assert len(outbox) == 0
        assert b'unverified account' in r.data


class TestPasswordReset:
    def test_forgot_password_page_loads(self, client):
        r = client.get('/auth/forgot-password')
        assert r.status_code == 200

    def test_forgot_password_sends_reset_email(self, client, app):
        _make_user(app, verified=True)
        with mail.record_messages() as outbox:
            r = client.post('/auth/forgot-password', data={
                'email': 'verify@example.com',
            }, follow_redirects=False)
        assert r.status_code == 302
        assert len(outbox) == 1
        assert outbox[0].recipients == ['verify@example.com']
        assert 'Reset your Paid2Match password' in outbox[0].subject
        assert '/auth/reset-password/' in outbox[0].body

    def test_forgot_password_unknown_email_sends_nothing(self, client, app):
        with mail.record_messages() as outbox:
            r = client.post('/auth/forgot-password', data={
                'email': 'nobody@example.com',
            }, follow_redirects=True)
        assert r.status_code == 200
        assert len(outbox) == 0
        # Same message as for a real account (no enumeration).
        assert b'If an account exists for that email' in r.data

    def test_reset_with_valid_token_changes_password(self, client, app):
        _make_user(app, verified=True)
        with app.app_context():
            from utils.email import generate_reset_token
            token = generate_reset_token('verify@example.com')
        r = client.get(f'/auth/reset-password/{token}')
        assert r.status_code == 200
        r = client.post(f'/auth/reset-password/{token}', data={
            'password': 'BrandNew123!',
            'confirm_password': 'BrandNew123!',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert '/auth/login' in r.location
        with app.app_context():
            user = User.query.filter_by(email='verify@example.com').first()
            assert check_password_hash(user.password_hash, 'BrandNew123!')
        # And the new password logs in.
        r = client.post('/auth/login', data={
            'email': 'verify@example.com',
            'password': 'BrandNew123!',
        }, follow_redirects=False)
        assert r.status_code == 302

    def test_reset_with_invalid_token_redirects(self, client, app):
        r = client.get('/auth/reset-password/not-a-real-token', follow_redirects=False)
        assert r.status_code == 302
        assert '/auth/forgot-password' in r.location

    def test_reset_with_weak_password_rejected(self, client, app):
        _make_user(app, verified=True)
        with app.app_context():
            from utils.email import generate_reset_token
            token = generate_reset_token('verify@example.com')
        r = client.post(f'/auth/reset-password/{token}', data={
            'password': 'weak',
            'confirm_password': 'weak',
        })
        assert r.status_code == 200
        with app.app_context():
            user = User.query.filter_by(email='verify@example.com').first()
            assert check_password_hash(user.password_hash, 'VerifyPass123!')


class TestAdminVerifyEmail:
    def test_admin_can_verify_user_email(self, logged_in_admin, client, app):
        _make_user(app)
        with app.app_context():
            user = User.query.filter_by(email='verify@example.com').first()
            user_id = user.id
        r = logged_in_admin.post(f'/admin/users/{user_id}/verify-email',
                                 follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            user = db.session.get(User, user_id)
            assert user.email_verified is True
        # The user can now log in.
        r = client.post('/auth/login', data={
            'email': 'verify@example.com',
            'password': 'VerifyPass123!',
        }, follow_redirects=False)
        assert r.status_code == 302
