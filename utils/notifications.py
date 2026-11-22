"""Notification helper (M1): one call creates the in-app notification and,
for the six key events, an email too. Email sending never raises — if
mail is unconfigured or fails, the in-app notification still lands and
the failure is logged (utils/email.py handles that).
"""
import logging

from flask import has_request_context, request

from extensions import db
from models.notification import Notification
from models.user import User
from utils.email import send_simple_email

logger = logging.getLogger(__name__)


def _absolute_url(link_url):
    if not link_url:
        return None
    if link_url.startswith('http'):
        return link_url
    if has_request_context():
        return request.host_url.rstrip('/') + link_url
    return link_url


def notify(recipient_id, type, title, body=None, actor_id=None,
           link_url=None, send_email=None):
    """Create a notification for recipient_id.

    send_email=None means: email iff the type is one of the six key events
    (Notification.EMAIL_TYPES). Pass False to suppress email for a key
    event, True to force it for a social event. Never notifies the actor
    about their own action. Does not commit — the caller commits.
    """
    if not recipient_id or recipient_id == actor_id:
        return None
    notification = Notification(
        recipient_id=recipient_id,
        actor_id=actor_id,
        type=type,
        title=title,
        body=body,
        link_url=link_url,
    )
    db.session.add(notification)
    db.session.flush()

    should_email = send_email if send_email is not None else type in Notification.EMAIL_TYPES
    if should_email:
        recipient = db.session.get(User, recipient_id)
        if recipient and recipient.email:
            email_body = body or title
            url = _absolute_url(link_url)
            if url:
                email_body = f"{email_body}\n\nView it here: {url}"
            send_simple_email(recipient.email, f'[Paid2Match] {title}', email_body)
    return notification


def unread_count(user_id):
    """Number of unread notifications for the navbar badge."""
    if not user_id:
        return 0
    return Notification.query.filter_by(recipient_id=user_id, is_read=False).count()
