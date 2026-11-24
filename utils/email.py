"""Email helpers for Paid2Match.

Used for referral invites, notifications, and the weekly digest. Auth is
OAuth-only (Google / GitHub) — there is no signup confirmation or
password reset mail anymore. All settings come from environment
variables (see config.py / EMAIL_SETUP.md) — nothing is hardcoded here.

Sending goes through the Resend HTTPS API when RESEND_API_KEY is set,
and falls back to SMTP (Flask-Mail) otherwise. The API path exists
because DigitalOcean blocks outbound SMTP ports (25/465/587/2525) on
this droplet, while HTTPS (443) is open.

If mail is not configured (neither RESEND_API_KEY nor MAIL_SERVER),
send functions log a warning and return False instead of raising. The
caller is responsible for telling the user.
"""
import logging

import requests
from flask import current_app, url_for
from flask_mail import Message

from extensions import mail

logger = logging.getLogger(__name__)

RESEND_API_URL = 'https://api.resend.com/emails'


def _send_via_resend(to_email, subject, body):
    """Send via the Resend HTTPS API. Returns True on success."""
    api_key = current_app.config.get('RESEND_API_KEY')
    sender = current_app.config.get('MAIL_DEFAULT_SENDER')
    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={'Authorization': f'Bearer {api_key}'},
            json={'from': sender, 'to': [to_email], 'subject': subject, 'text': body},
            timeout=30,
        )
        if resp.status_code == 200:
            return True
        logger.warning('Resend API rejected email to %s: %s %s', to_email, resp.status_code, resp.text[:200])
        return False
    except Exception:
        logger.exception('Failed to send email to %s via Resend API', to_email)
        return False


def _send(to_email, subject, body):
    """Send a plain-text email. Returns True on success, False otherwise."""
    if current_app.config.get('RESEND_API_KEY'):
        return _send_via_resend(to_email, subject, body)
    if not current_app.config.get('MAIL_SERVER'):
        logger.warning('Mail not configured (RESEND_API_KEY and MAIL_SERVER unset) — skipping email to %s', to_email)
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
