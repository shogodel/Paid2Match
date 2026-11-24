"""Email helpers for Paid2Match (Flask-Mail).

Used for referral invites, notifications, and the weekly digest. Auth is
OAuth-only (Google / GitHub) — there is no signup confirmation or
password reset mail anymore. All SMTP settings come from environment
variables (see config.py / EMAIL_SETUP.md) — nothing is hardcoded here.

If mail is not configured (no MAIL_SERVER), send functions log a warning
and return False instead of raising. The caller is responsible for
telling the user.
"""
import logging

from flask import current_app, url_for
from flask_mail import Message

from extensions import mail

logger = logging.getLogger(__name__)


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
