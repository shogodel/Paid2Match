"""Bounty model."""
import math
from datetime import datetime, timedelta, timezone
from extensions import db
from sqlalchemy.orm import validates
from models import generate_uuid, utc_now


class Bounty(db.Model):
    """Job/recruitment bounty model."""
    __tablename__ = 'bounties'

    # M4: two verticals are live — recruitment (v1) and services
    # referrals (M4). Other types stay locked until a later milestone.
    VALID_BOUNTY_TYPES = ['recruitment', 'services']
    BOUNTY_TYPE_LABELS = {
        'recruitment': 'Recruitment',
        'services': 'Services',
    }
    # Category system (M4): each vertical has its own categories. A
    # need's category must belong to its vertical (enforced in the
    # post/edit routes, not by the model — the model does not know
    # which vertical a category belongs to at validation time).
    CATEGORIES = {
        'recruitment': {
            'trades': 'Skilled trades',
            'hospitality': 'Hospitality & food',
            'healthcare': 'Healthcare',
            'office_admin': 'Office & admin',
            'sales': 'Sales',
            'tech': 'Tech',
            'other': 'Other',
        },
        'services': {
            'home_services': 'Home services',
            'creative_design': 'Creative & design',
            'professional': 'Professional services',
            'wellness': 'Health & wellness',
            'tech_services': 'Tech services',
            'other': 'Other',
        },
    }
    VALID_STATUSES = ['pending', 'open', 'funded', 'closed', 'completed', 'expired']
    VALID_PAYMENT_STATUSES = ['unsecured', 'secured', 'released', 'refunded']
    VALID_PAYER_TYPES = ['poster', 'third_party']
    VALID_DIRECTIONS = ['seeking_opportunity', 'offering_opportunity']
    VALID_DIRECTIONS_DISPLAY = {
        'seeking_opportunity': 'Seeking Opportunity',
        'offering_opportunity': 'Offering Opportunity'
    }

    @validates('bounty_type')
    def _validate_bounty_type(self, key, value):
        if value is not None and value not in self.VALID_BOUNTY_TYPES:
            raise ValueError(
                f"bounty_type must be one of {self.VALID_BOUNTY_TYPES}, got {value!r}"
            )
        return value

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    poster_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    bounty_type = db.Column(db.String(30), nullable=False)
    bounty_direction = db.Column(db.String(30), nullable=False, default='seeking_opportunity')
    # M4: category within the vertical (see CATEGORIES above).
    category = db.Column(db.String(30), nullable=True)
    # M4: skills the poster is looking for (comma-separated free text).
    # The post form collected this field from day one but it was never
    # saved — Bounty had no column for it (audit §3.10).
    skills = db.Column(db.Text, nullable=True)
    reward_amount = db.Column(db.Numeric(12, 2), nullable=True)
    transaction_fee = db.Column(db.Numeric(12, 2), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='open')
    payment_status = db.Column(db.String(20), nullable=False, default='unsecured')
    payer_type = db.Column(db.String(20), default='poster')
    third_party_payer_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True)
    location = db.Column(db.String(200), nullable=True)
    deadline = db.Column(db.DateTime, nullable=True)
    success_criteria = db.Column(db.Text, nullable=True)
    is_featured = db.Column(db.Boolean, default=False)
    is_free = db.Column(db.Boolean, default=False)
    stripe_payment_intent = db.Column(db.String(100), nullable=True)

    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    pitches = db.relationship('Pitch', backref='bounty', lazy='dynamic', cascade='all, delete-orphan')
    matches = db.relationship('Match', backref='bounty', lazy='dynamic', cascade='all, delete-orphan')
    messages = db.relationship('Message', backref='bounty', lazy='dynamic')
    upgrades = db.relationship('BountyUpgrade', backref='bounty', lazy='dynamic', cascade='all, delete-orphan')
    third_party_payer = db.relationship('User', foreign_keys=[third_party_payer_id])

    def __repr__(self):
        return f'<Bounty {self.title}>'

    @property
    def type_label(self):
        """Human label for the vertical ('Recruitment', 'Services')."""
        return self.BOUNTY_TYPE_LABELS.get(self.bounty_type, self.bounty_type)

    @property
    def category_label(self):
        """Human label for the category, or None when uncategorized."""
        if not self.category:
            return None
        return self.CATEGORIES.get(self.bounty_type, {}).get(
            self.category, self.category
        )

    @property
    def skill_list(self):
        """The skills field as a list of stripped, non-empty names."""
        if not self.skills:
            return []
        return [s.strip() for s in self.skills.split(',') if s.strip()]

    @property
    def display_name(self):
        dir_label = self.VALID_DIRECTIONS_DISPLAY.get(self.bounty_direction, self.bounty_direction)
        amount_str = f"${float(self.reward_amount):,.0f}" if self.reward_amount and self.reward_amount > 0 else "Free"
        return f"{dir_label} — {amount_str}"

    @property
    def is_free_bounty(self):
        return not self.reward_amount or self.reward_amount == 0

    @property
    def is_publicly_visible(self):
        """True if this bounty may appear on the public board.

        A paid bounty is only visible once its payment is secured; free
        ($0 / no-reward) bounties are always visible.
        """
        return (
            self.payment_status == 'secured'
            or self.is_free
            or self.is_free_bounty
        )

    @classmethod
    def publicly_visible_filter(cls):
        """SQLAlchemy criterion matching is_publicly_visible, for board queries."""
        from sqlalchemy import or_
        return or_(
            cls.payment_status == 'secured',
            cls.is_free == True,  # noqa: E712 (SQLAlchemy boolean comparison)
            cls.reward_amount.is_(None),
            cls.reward_amount <= 0,
        )

    @property
    def payer_name(self):
        if self.payer_type == 'third_party' and self.third_party_payer:
            return self.third_party_payer.full_name
        return 'You'

    @property
    def is_expired(self):
        if not self.deadline:
            return False
        return datetime.now(timezone.utc) > self.deadline.replace(tzinfo=timezone.utc)

    @property
    def time_remaining(self):
        if not self.deadline:
            return None
        now = datetime.now(timezone.utc)
        deadline_utc = self.deadline.replace(tzinfo=timezone.utc) if self.deadline.tzinfo is None else self.deadline
        delta = deadline_utc - now
        return max(delta, timedelta(0))

    @property
    def time_remaining_display(self):
        remaining = self.time_remaining
        if remaining is None:
            return None
        if self.is_expired:
            return "Expired"
        total_seconds = int(remaining.total_seconds())
        if total_seconds <= 0:
            return "Expired"
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    @property
    def active_upgrades(self):
        from models.bounty_upgrade import BountyUpgrade
        now = datetime.now(timezone.utc)
        return BountyUpgrade.query.filter(
            BountyUpgrade.bounty_id == self.id,
            BountyUpgrade.is_active == True,
            (BountyUpgrade.expires_at == None) | (BountyUpgrade.expires_at > now)
        ).all()

    @property
    def is_featured(self):
        from models.bounty_upgrade import BountyUpgrade
        return BountyUpgrade.query.filter(
            BountyUpgrade.bounty_id == self.id,
            BountyUpgrade.upgrade_type == 'featured',
            BountyUpgrade.is_active == True
        ).count() > 0

    @property
    def is_highlighted(self):
        from models.bounty_upgrade import BountyUpgrade
        return BountyUpgrade.query.filter(
            BountyUpgrade.bounty_id == self.id,
            BountyUpgrade.upgrade_type == 'highlight',
            BountyUpgrade.is_active == True
        ).count() > 0

    @property
    def is_urgent(self):
        from models.bounty_upgrade import BountyUpgrade
        return BountyUpgrade.query.filter(
            BountyUpgrade.bounty_id == self.id,
            BountyUpgrade.upgrade_type == 'urgent',
            BountyUpgrade.is_active == True
        ).count() > 0

    @property
    def upgrade_badges(self):
        from models.bounty_upgrade import BountyUpgrade
        import math

        now = datetime.now(timezone.utc)
        badges = []

        for u in BountyUpgrade.query.filter(
            BountyUpgrade.bounty_id == self.id,
            BountyUpgrade.is_active == True
        ).all():
            if u.is_valid and (not u.expires_at or u.expires_at.replace(tzinfo=timezone.utc) > now):
                info = u.info
                badges.append({
                    'type': u.upgrade_type,
                    'badge': info.get('badge', ''),
                    'badge_class': info.get('badge_class', 'bg-secondary'),
                    'expires_at': u.expires_at,
                    'days_left': math.ceil((u.expires_at - now).total_seconds() / 86400) if u.expires_at else None
                })

        return badges

    def get_active_agreement(self):
        from models.bounty_payment_agreement import BountyPaymentAgreement
        if BountyPaymentAgreement:
            return BountyPaymentAgreement.query.filter_by(
                bounty_id=self.id,
                status='accepted'
            ).first()
        return None

    def to_dict(self):
        return {
            'id': self.id,
            'poster_id': self.poster_id,
            'title': self.title,
            'description': self.description,
            'bounty_type': self.bounty_type,
            'bounty_direction': self.bounty_direction,
            'category': self.category,
            'skills': self.skills,
            'reward_amount': float(self.reward_amount) if self.reward_amount else None,
            'is_free': self.is_free_bounty,
            'status': self.status,
            'payment_status': self.payment_status,
            'payer_type': self.payer_type,
            'third_party_payer_id': self.third_party_payer_id,
            'location': self.location,
            'deadline': self.deadline.isoformat() if self.deadline else None,
            'is_featured': self.is_featured,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
