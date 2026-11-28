"""In-app notifications (M1 social core, M2 money events).

Key events get both an in-app notification and an email (§5.2 M1 + M2):
new referral, referral accepted/rejected, match confirmed, payment
secured, payment released, new message, payout pending/failed. Social
events (like, comment, follow, share, referral invite/consent/
completion, reviews, disputes, verification) get in-app notifications
only — emailing every like would be spam.
"""
from extensions import db
from models import generate_uuid, utc_now


class Notification(db.Model):
    """One notification for one user."""
    __tablename__ = 'notifications'

    # The key events (also emailed) + social events (in-app only).
    # M2 adds payout/verification/review/dispute events: money failing
    # to move deserves an email; the rest are in-app only.
    TYPES = [
        'referral_new', 'referral_accepted', 'referral_rejected',
        'match_confirmed', 'payment_secured', 'payment_released',
        'message_new',
        'like', 'comment', 'follow', 'share',
        'referral_invite', 'referral_consented', 'referral_declined',
        'referral_completed', 'share_led_to_hire',
        'payout_pending', 'payout_failed', 'connect_ready',
        'review_visible', 'dispute_opened',
        'verification_approved', 'verification_rejected',
        # M3: dispute resolutions (both parties), endorsements,
        # linked-account decisions, pledge revocation.
        'dispute_resolved', 'endorsement',
        'linked_verified', 'linked_rejected', 'pledge_revoked',
        # Phase 2: @mentions, and new posts in a space you follow
        # (members with notification level 'all' only).
        'mention', 'space_post',
        # Paid spaces: the owner is told when a space becomes paid
        # with them as owner.
        'space_paid',
        # Tips (audit 2026-11-28): the recipient is told when a tip
        # lands, waits on payout setup, or its payout fails.
        'tip_received', 'tip_pending', 'tip_failed',
        # Paid spaces (audit 2026-11-28): the owner is told when a
        # member joins or when a membership lapses.
        'space_member_joined', 'space_member_left',
    ]
    # Types that also send an email to the recipient.
    EMAIL_TYPES = {
        'referral_new', 'referral_accepted', 'referral_rejected',
        'match_confirmed', 'payment_secured', 'payment_released',
        'message_new',
        'payout_pending', 'payout_failed',
        # Tips are money events, like payouts: a tip waiting on
        # payout setup or a failed tip payout deserves an email, not
        # just a bell the recipient may never open (audit 2026-11-28).
        'tip_received', 'tip_pending', 'tip_failed',
    }

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    recipient_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    actor_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True)
    type = db.Column(db.String(30), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=True)
    link_url = db.Column(db.String(500), nullable=True)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utc_now, index=True)

    actor = db.relationship('User', foreign_keys=[actor_id])

    def __repr__(self):
        return f'<Notification {self.type} -> {self.recipient_id}>'

    def to_dict(self):
        return {
            'id': self.id,
            'recipient_id': self.recipient_id,
            'actor_id': self.actor_id,
            'type': self.type,
            'title': self.title,
            'body': self.body,
            'link_url': self.link_url,
            'is_read': self.is_read,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
