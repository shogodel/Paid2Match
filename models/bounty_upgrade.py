"""BountyUpgrade model for premium placement features."""
import uuid
from datetime import datetime, timezone
from extensions import db


def generate_uuid():
    return str(uuid.uuid4())


def utc_now():
    return datetime.now(timezone.utc)


class BountyUpgrade(db.Model):
    """Premium placement upgrade for bounties."""
    __tablename__ = 'bounty_upgrades'

    UPGRADE_TYPES = {
        'highlight': {
            'name': 'Highlight',
            'price_per_day': 1.00,
            'badge': '⭐ Highlighted',
            'badge_class': 'bg-warning text-dark',
            'description': 'Yellow highlight background, top of board placement'
        },
        'urgent': {
            'name': 'Urgent',
            'price_per_day': 2.00,
            'badge': '🔥 Urgent',
            'badge_class': 'bg-danger',
            'description': 'Red urgent badge, priority placement, shows in Urgent filter'
        },
        'featured': {
            'name': 'Featured',
            'price_per_day': 3.00,
            'badge': '✨ Featured',
            'badge_class': 'bg-warning',
            'description': 'Appears on ALL boards, homepage featured section, top placement'
        }
    }

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=False, index=True)
    upgrade_type = db.Column(db.String(20), nullable=False)
    duration_days = db.Column(db.Integer, nullable=False)
    price_paid = db.Column(db.Numeric(12, 2), nullable=False)
    stripe_payment_intent = db.Column(db.String(100), nullable=True)
    stripe_session_id = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    starts_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f'<BountyUpgrade {self.upgrade_type} for {self.bounty_id}>'

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        return datetime.now(timezone.utc) > self.expires_at.replace(tzinfo=timezone.utc)

    @property
    def is_valid(self):
        return self.is_active and not self.is_expired

    @property
    def info(self):
        return self.UPGRADE_TYPES.get(self.upgrade_type, {})

    @property
    def badge(self):
        info = self.info
        return info.get('badge', '')

    @property
    def badge_class(self):
        info = self.info
        return info.get('badge_class', 'bg-secondary')

    def to_dict(self):
        return {
            'id': self.id,
            'bounty_id': self.bounty_id,
            'upgrade_type': self.upgrade_type,
            'duration_days': self.duration_days,
            'price_paid': float(self.price_paid) if self.price_paid else None,
            'is_active': self.is_active,
            'is_valid': self.is_valid,
            'starts_at': self.starts_at.isoformat() if self.starts_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'badge': self.badge,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }