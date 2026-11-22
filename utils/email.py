"""Email helpers for Paid2Match (Flask-Mail + itsdangerous signed tokens).

Used for signup email confirmation and password reset. All SMTP settings
come from environment variables (see config.py / EMAIL_SETUP.md) — nothing
is hardcoded here.

If mail is not configured (no MAIL_SERVER), send functions log a warning
and return False instead of raising, so signup never crashes for lack of
an SMTP server. The caller is responsible for telling the user.
"""
import logging

from flask import current_app, url_for
from flask_mail import Message
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from extensions import mail

logger = logging.getLogger(__name__)

CONFIRM_SALT = 'email-confirm'
RESET_SALT = 'password-reset'


def _serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'])


def generate_confirmation_token(email):
    """Signed, expiring token for the given (lowercased) email address."""
    return _serializer().dumps(email.lower(), salt=CONFIRM_SALT)


def confirm_confirmation_token(token):
    """Return the email if the token is valid, else None (expired/tampered)."""
    try:
        return _serializer().loads(
            token,
            salt=CONFIRM_SALT,
            max_age=current_app.config.get('EMAIL_CONFIRM_TOKEN_MAX_AGE', 86400),
        )
    except (SignatureExpired, BadSignature):
        return None


def generate_reset_token(email):
    """Signed, expiring token for the given (lowercased) email address."""
    return _serializer().dumps(email.lower(), salt=RESET_SALT)


def confirm_reset_token(token):
    """Return the email if the token is valid, else None (expired/tampered)."""
    try:
        return _serializer().loads(
            token,
            salt=RESET_SALT,
            max_age=current_app.config.get('PASSWORD_RESET_TOKEN_MAX_AGE', 3600),
        )
    except (SignatureExpired, BadSignature):
        return None


def _send(to_email, subject, body):
    """Send a plain-text email. Returns True on success, False otherwise."""
    if not current_app.config.get('MAIL_SERVER'):
        logger.warning('Mail not configured (MAIL_SERVER unset) — skipping email to %s', to_email)
        return False
    try:
        msg = Message(subject=subject, recipients=[to_email], body=body)
        mail.send(msg)
        return True
    except Exception:
        logger.exception('Failed to send email to %s', to_email)
        return False


def send_simple_email(to_email, subject, body):
    """Generic plain-text email (used by notifications). Returns True if sent."""
    return _send(to_email, subject, body)


def send_referral_invite_email(referral):
    """Email the referred person their consent link. Returns True if sent."""
    invite_url = url_for('referrals.consent', token=referral.invite_token, _external=True)
    bounty = referral.bounty
    body = (
        f"Hi {referral.referred_name},\n\n"
        f"{referral.referrer.full_name} thinks you would be a great fit for "
        f"a referral need on Paid2Match: \"{bounty.title}\".\n\n"
        f"Why they referred you: {referral.message}\n\n"
        "Nobody is referred without knowing — please confirm (or decline) "
        "using the link below:\n\n"
        f"{invite_url}\n\n"
        "If you don't have a Paid2Match account yet, you'll be able to "
        "create one first — the link will still work.\n\n"
        "— The Paid2Match team"
    )
    return _send(referral.referred_email, 'Someone referred you on Paid2Match', body)


def send_verification_email(user):
    """Email the signup confirmation link. Returns True if sent."""
    token = generate_confirmation_token(user.email)
    confirm_url = url_for('auth.confirm_email', token=token, _external=True)
    body = (
        f"Hi {user.full_name},\n\n"
        "Welcome to Paid2Match! Please confirm your email address by clicking "
        "the link below (valid for 24 hours):\n\n"
        f"{confirm_url}\n\n"
        "If you did not create this account, you can ignore this email.\n\n"
        "— The Paid2Match team"
    )
    return _send(user.email, 'Confirm your Paid2Match account', body)


def send_password_reset_email(user):
    """Email the password reset link. Returns True if sent."""
    token = generate_reset_token(user.email)
    reset_url = url_for('auth.reset_password', token=token, _external=True)
    body = (
        f"Hi {user.full_name},\n\n"
        "You (or someone pretending to be you) asked to reset your Paid2Match "
        "password. Click the link below to choose a new one (valid for 1 hour):\n\n"
        f"{reset_url}\n\n"
        "If you did not ask for this, you can ignore this email — your "
        "password will not change.\n\n"
        "— The Paid2Match team"
    )
    return _send(user.email, 'Reset your Paid2Match password', body)
