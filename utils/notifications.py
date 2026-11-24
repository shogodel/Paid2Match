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


# Phase 3: the bell panel + full notifications page need more than the
# count — the items themselves, with actor avatars, and an icon per
# type (Lucide names from static/img/icons.svg).

NOTIFICATION_ICONS = {
    'like': 'heart',
    'comment': 'message-circle',
    'mention': 'message-circle',
    'follow': 'user',
    'share': 'share-2',
    'space_post': 'layers',
    'message_new': 'mail',
    'referral_new': 'handshake',
    'referral_accepted': 'handshake',
    'referral_rejected': 'handshake',
    'referral_invite': 'handshake',
    'referral_consented': 'handshake',
    'referral_declined': 'handshake',
    'referral_completed': 'handshake',
    'share_led_to_hire': 'handshake',
    'match_confirmed': 'handshake',
    'payment_secured': 'wallet',
    'payment_released': 'wallet',
    'payout_pending': 'wallet',
    'payout_failed': 'wallet',
    'connect_ready': 'wallet',
    'review_visible': 'star',
    'dispute_opened': 'flag',
    'dispute_resolved': 'flag',
    'verification_approved': 'badge-check',
    'verification_rejected': 'badge-check',
    'endorsement': 'award',
    'linked_verified': 'link',
    'linked_rejected': 'link',
    'pledge_revoked': 'shield-check',
    'employer_pledge_approved': 'shield-check',
    'employer_pledge_revoked': 'shield-check',
}


def notification_icon(notification_type):
    """The Lucide icon for a notification type (bell by default)."""
    return NOTIFICATION_ICONS.get(notification_type, 'bell')


def recent_notifications(user_id, limit=10):
    """The user's latest notifications, newest first — the bell
    panel's content. Returns [{'notification', 'actor', 'actor_profile'}]
    so the panel can show actor avatars without extra queries per row
    (one profile query for all actors)."""
    from models.profile import Profile
    from models.user import User
    notifications = Notification.query.filter_by(
        recipient_id=user_id,
    ).order_by(Notification.created_at.desc()).limit(limit).all()
    actor_ids = {n.actor_id for n in notifications if n.actor_id}
    actors = {
        u.id: u
        for u in User.query.filter(User.id.in_(actor_ids)).all()
    } if actor_ids else {}
    profiles = {
        p.user_id: p
        for p in Profile.query.filter(Profile.user_id.in_(actor_ids)).all()
    } if actor_ids else {}
    return [{
        'notification': n,
        'actor': actors.get(n.actor_id),
        'actor_profile': profiles.get(n.actor_id),
    } for n in notifications]


def notification_to_dict(notification):
    """One notification as JSON for the panel refresh + the toast
    poll. Actor details are included so the client can render the row
    without another round trip."""
    from models.profile import Profile
    from models.user import User
    from flask import url_for
    actor = None
    actor_profile = None
    if notification.actor_id:
        actor = db.session.get(User, notification.actor_id)
        actor_profile = Profile.query.filter_by(
            user_id=notification.actor_id
        ).first()
    return {
        'id': notification.id,
        'type': notification.type,
        'icon': notification_icon(notification.type),
        'title': notification.title,
        'body': notification.body,
        'link_url': notification.link_url,
        'is_read': notification.is_read,
        'created_at': (
            notification.created_at.isoformat()
            if notification.created_at else None
        ),
        'actor_name': actor.full_name if actor else None,
        'actor_url': (
            url_for('profile.public_profile', user_id=actor.handle or actor.id)
            if actor else None
        ),
        'actor_avatar_url': (
            actor_profile.avatar_url if actor_profile else None
        ),
    }
