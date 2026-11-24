"""Bounty model."""
import math
from datetime import datetime, timedelta, timezone
from extensions import db
from flask_babel import lazy_gettext as _l
from sqlalchemy import event, select
from sqlalchemy.orm import validates
from models import generate_uuid, utc_now
from utils.currency import DEFAULT_CURRENCY, format_money, validate_currency
from utils.slugs import slugify, unique_shortcode

# Phase 3: the minimum bounty on a PAID referral need (CAD). A paid
# need must state a bounty of at least $50 — no more $0 "paid" needs.
# Free ($0) community needs stay allowed: they carry no reward at all
# (reward_amount None or 0), so the floor never touches them. This is
# a product decision Remus can adjust by changing this one constant —
# the form validators and the model guard below all read it.
MIN_PAID_BOUNTY_CAD = 50


class Bounty(db.Model):
    """Job/recruitment bounty model."""
    __tablename__ = 'bounties'

    # M4: two verticals are live — recruitment (v1) and services
    # referrals (M4). Other types stay locked until a later milestone.
    VALID_BOUNTY_TYPES = ['recruitment', 'services']
    BOUNTY_TYPE_LABELS = {
        'recruitment': _l('Recruitment'),
        'services': _l('Services'),
    }
    # Category system (M4): each vertical has its own categories. A
    # need's category must belong to its vertical (enforced in the
    # post/edit routes, not by the model — the model does not know
    # which vertical a category belongs to at validation time).
    CATEGORIES = {
        'recruitment': {
            'trades': _l('Skilled trades'),
            'hospitality': _l('Hospitality & food'),
            'healthcare': _l('Healthcare'),
            'office_admin': _l('Office & admin'),
            'sales': _l('Sales'),
            'tech': _l('Tech'),
            'other': _l('Other'),
        },
        'services': {
            'home_services': _l('Home services'),
            'creative_design': _l('Creative & design'),
            'professional': _l('Professional services'),
            'wellness': _l('Health & wellness'),
            'tech_services': _l('Tech services'),
            'other': _l('Other'),
        },
    }
    VALID_STATUSES = ['pending', 'open', 'funded', 'closed', 'completed', 'expired']
    VALID_PAYMENT_STATUSES = ['unsecured', 'secured', 'released', 'refunded']
    VALID_PAYER_TYPES = ['poster', 'third_party']
    VALID_DIRECTIONS = ['seeking_opportunity', 'offering_opportunity']
    VALID_DIRECTIONS_DISPLAY = {
        'seeking_opportunity': _l('Seeking Opportunity'),
        'offering_opportunity': _l('Offering Opportunity')
    }

    @validates('bounty_type')
    def _validate_bounty_type(self, key, value):
        if value is not None and value not in self.VALID_BOUNTY_TYPES:
            raise ValueError(
                f"bounty_type must be one of {self.VALID_BOUNTY_TYPES}, got {value!r}"
            )
        return value

    @validates('currency')
    def _validate_currency(self, key, value):
        """Multi-currency: only CAD/EUR/USD, stored uppercased. The
        validator also normalises legacy lowercase codes ('cad')."""
        return validate_currency(value)

    @validates('reward_amount')
    def _validate_reward_amount(self, key, value):
        """Phase 3 bounty floor: a positive reward below the floor is
        not a paid need — it is a $0 need pretending to be paid. Free
        needs (None or 0) pass; anything positive must be at least
        MIN_PAID_BOUNTY_CAD. The forms catch this first with a proper
        error message; this guard is the backstop for direct
        construction (and it never fires on rows loaded from the DB,
        so pre-floor bounties are untouched)."""
        if value is not None and 0 < value < MIN_PAID_BOUNTY_CAD:
            raise ValueError(
                f"reward_amount must be 0 (free need) or at least "
                f"${MIN_PAID_BOUNTY_CAD}, got {value!r}"
            )
        return value

    @validates('title')
    def _set_slug_from_title(self, key, value):
        """The slug is set once, from the first title — it never
        changes afterwards, so a need's URL stays stable when the
        poster edits the title (the detail route 301s a stale slug
        to the canonical URL, but a stable slug means old links
        keep working as-is)."""
        if value and not self.slug:
            self.slug = slugify(value)
        return value

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    poster_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    # Nice URLs: /needs/<slug>-<shortcode>. The slug is set once
    # from the title (see the title validator above); the shortcode
    # is the stable identifier the detail route looks up (see the
    # before_insert event at the bottom of this file).
    slug = db.Column(db.String(120), nullable=False, index=True)
    shortcode = db.Column(db.String(6), nullable=False, unique=True, index=True)
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
    # Multi-currency: the currency the reward (and staged rewards)
    # are denominated in. No FX conversion anywhere — amounts are
    # recorded and shown as entered, in this currency.
    currency = db.Column(db.String(3), nullable=False, default=DEFAULT_CURRENCY)
    transaction_fee = db.Column(db.Numeric(12, 2), nullable=True)
    # Phase 3 staged rewards (Paraform model — display only, see
    # models/staged_reward.py): optional amounts the poster declares
    # for each stage of a referral — a referral submitted, a candidate
    # interviewed, a hire made. Declaring them costs nothing and moves
    # no money: completed stages are recorded as StageRewardEvent rows
    # with status 'pending_stripe' until the payout wiring lands (no
    # live Stripe keys exist in the container yet, so there is no
    # payout path to call). All three are optional and independent of
    # the main reward_amount.
    stage_submit_reward = db.Column(db.Numeric(12, 2), nullable=True)
    stage_interview_reward = db.Column(db.Numeric(12, 2), nullable=True)
    stage_hire_reward = db.Column(db.Numeric(12, 2), nullable=True)
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
    def url_slug(self):
        """The detail URL's path segment: '<slug>-<shortcode>'.
        Falls back to the id for objects not flushed yet (no slug
        assigned) — the detail route 301s a bare UUID to the
        canonical URL once the row exists."""
        if self.slug and self.shortcode:
            return f'{self.slug}-{self.shortcode}'
        return self.id

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
        amount_str = format_money(self.reward_amount, self.currency) if self.reward_amount and self.reward_amount > 0 else "Free"
        return f"{dir_label} — {amount_str}"

    @property
    def is_free_bounty(self):
        return not self.reward_amount or self.reward_amount == 0

    @property
    def has_staged_rewards(self):
        """True when the poster declared at least one stage reward."""
        return bool(
            (self.stage_submit_reward and self.stage_submit_reward > 0)
            or (self.stage_interview_reward and self.stage_interview_reward > 0)
            or (self.stage_hire_reward and self.stage_hire_reward > 0)
        )

    @property
    def stage_rewards(self):
        """Declared stage rewards in stage order, as a list of
        {'stage', 'label', 'amount'} dicts — only the stages the
        poster actually set a (positive) amount for."""
        from utils.staged_rewards import STAGE_LABELS
        rewards = []
        for stage, column in (
            ('submit', self.stage_submit_reward),
            ('interview', self.stage_interview_reward),
            ('hire', self.stage_hire_reward),
        ):
            if column and column > 0:
                rewards.append({
                    'stage': stage,
                    'label': STAGE_LABELS[stage],
                    'amount': column,
                })
        return rewards

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
            'currency': self.currency,
            'stage_submit_reward': float(self.stage_submit_reward) if self.stage_submit_reward else None,
            'stage_interview_reward': float(self.stage_interview_reward) if self.stage_interview_reward else None,
            'stage_hire_reward': float(self.stage_hire_reward) if self.stage_hire_reward else None,
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


@event.listens_for(Bounty, 'before_insert')
def _ensure_slug_and_shortcode(mapper, connection, target):
    """Model-level generation — not just the form route: tests and
    scripts create Bounty objects directly. The slug usually comes
    from the title validator; this is the backstop for rows whose
    title was set without it. The shortcode is generated here
    because uniqueness needs a DB lookup (done on the event's
    connection, never through the flushing session). On a full
    slug+shortcode collision the shortcode is regenerated — the
    slug is allowed to repeat, the shortcode is what identifies
    the need."""
    if not target.slug:
        target.slug = slugify(target.title or '')
    table = Bounty.__table__

    def _taken(code):
        # Siblings in the same flush are not in the DB yet (one
        # batched INSERT), so check the pending objects too.
        if any(
            o is not target and o.shortcode == code
            for o in db.session.new
            if isinstance(o, Bounty)
        ):
            return True
        return connection.execute(
            select(table.c.id).where(table.c.shortcode == code)
        ).first() is not None

    if not target.shortcode or _taken(target.shortcode):
        target.shortcode = unique_shortcode(_taken)
