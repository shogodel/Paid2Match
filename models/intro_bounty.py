"""Warm-intro bounties: pay for an introduction, not just a hire.

"I'll pay $50 to anyone who introduces me to the hiring manager at
X." A poster offers a reward for a warm introduction to a named
person or company; anyone who can make that intro claims it; the
poster confirms the intro happened, and the reward is RECORDED as
owed — never paid out by this code.

The reward record follows the StageRewardEvent pattern
(models/staged_reward.py): IntroRewardEvent rows carry status
'pending_stripe' — the only status that exists until the payout
wiring lands. No live Stripe keys exist in the container, so
nothing here calls Stripe or moves money; the UI says the reward
is pending, plainly.
"""
from extensions import db
from sqlalchemy.orm import validates
from models import generate_uuid, utc_now
from utils.currency import DEFAULT_CURRENCY, validate_currency


class IntroBounty(db.Model):
    """A paid request for a warm introduction."""
    __tablename__ = 'intro_bounties'

    VALID_STATUSES = ['open', 'claimed', 'completed', 'cancelled']

    @validates('currency')
    def _validate_currency(self, key, value):
        """Multi-currency: only CAD/EUR/USD, stored uppercased."""
        return validate_currency(value)

    @validates('amount')
    def _validate_amount(self, key, value):
        """An intro bounty must pay something: amount must be
        greater than zero. The form catches this first with a
        proper error message; this guard is the backstop for
        direct construction."""
        if value is None or value <= 0:
            raise ValueError(
                f"amount must be greater than 0, got {value!r}"
            )
        return value

    @validates('status')
    def _validate_status(self, key, value):
        if value is not None and value not in self.VALID_STATUSES:
            raise ValueError(
                f"status must be one of {self.VALID_STATUSES}, "
                f"got {value!r}"
            )
        return value

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    poster_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    # The person (or company) the poster wants to be introduced to.
    target_name = db.Column(db.String(200), nullable=False)
    # The target's organization, when the target is a person.
    target_org = db.Column(db.String(200), nullable=True)
    # What the introduction is for, in the poster's own words.
    context = db.Column(db.Text, nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    # Multi-currency: the currency the reward is denominated in.
    # No FX conversion anywhere — amounts are recorded and shown
    # as entered, in this currency.
    currency = db.Column(db.String(3), nullable=False, default=DEFAULT_CURRENCY)
    status = db.Column(db.String(20), nullable=False, default='open')
    created_at = db.Column(db.DateTime, default=utc_now)

    poster = db.relationship('User', backref='intro_bounties')
    claims = db.relationship(
        'IntroClaim', backref='intro_bounty', lazy='dynamic',
        cascade='all, delete-orphan',
    )

    def __repr__(self):
        return f'<IntroBounty {self.target_name} ({self.status})>'

    @property
    def pending_claims(self):
        """Claims still awaiting the poster's decision, oldest first."""
        return self.claims.filter_by(status='pending').order_by(
            IntroClaim.created_at
        ).all()

    @property
    def has_pending_claims(self):
        return self.claims.filter_by(status='pending').count() > 0

    @property
    def has_confirmed_claim(self):
        return self.claims.filter_by(status='confirmed').count() > 0

    @property
    def can_be_cancelled(self):
        """A bounty can be cancelled while it is open or claimed —
        but never once an intro has been confirmed (the reward is
        then owed)."""
        return (
            self.status in ('open', 'claimed')
            and not self.has_confirmed_claim
        )


class IntroClaim(db.Model):
    """One user's offer to make the introduction: "I know them,
    I can make this intro." One pending claim per (intro bounty,
    claimer) — enforced in the claim route; a claimer cannot
    claim their own intro bounty."""
    __tablename__ = 'intro_claims'

    VALID_STATUSES = ['pending', 'confirmed', 'rejected']

    @validates('status')
    def _validate_status(self, key, value):
        if value is not None and value not in self.VALID_STATUSES:
            raise ValueError(
                f"status must be one of {self.VALID_STATUSES}, "
                f"got {value!r}"
            )
        return value

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    intro_bounty_id = db.Column(
        db.String(36), db.ForeignKey('intro_bounties.id'),
        nullable=False, index=True,
    )
    claimer_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    # How the claimer knows the target, in their own words.
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    decided_at = db.Column(db.DateTime, nullable=True)

    claimer = db.relationship('User', backref='intro_claims')

    def __repr__(self):
        return f'<IntroClaim {self.claimer_id} ({self.status})>'

    @classmethod
    def pending_for(cls, intro_bounty_id, claimer_id):
        """The claimer's pending claim on this bounty, if any."""
        return cls.query.filter_by(
            intro_bounty_id=intro_bounty_id,
            claimer_id=claimer_id,
            status='pending',
        ).first()


class IntroRewardEvent(db.Model):
    """A confirmed intro's reward, recorded for a future payout.

    Follows the StageRewardEvent pattern: one event per confirmed
    claim (unique on claim_id, so a reward can never be recorded
    — or paid — twice), status 'pending_stripe' until the payout
    wiring lands. Recording is all this model does: no Stripe
    call, no money moves. The currency lives on the IntroBounty,
    like the bounty carries it for StageRewardEvent."""
    __tablename__ = 'intro_reward_events'
    __table_args__ = (
        db.UniqueConstraint('claim_id', name='uq_intro_reward_claim'),
    )

    # The only status until Stripe payout wiring exists.
    STATUSES = ['pending_stripe']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    intro_bounty_id = db.Column(
        db.String(36), db.ForeignKey('intro_bounties.id'),
        nullable=False, index=True,
    )
    claim_id = db.Column(
        db.String(36), db.ForeignKey('intro_claims.id'),
        nullable=False, index=True,
    )
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending_stripe')
    created_at = db.Column(db.DateTime, default=utc_now)

    intro_bounty = db.relationship(
        'IntroBounty', backref='reward_events'
    )
    claim = db.relationship('IntroClaim', backref='reward_event')

    def __repr__(self):
        return (
            f'<IntroRewardEvent ${self.amount} ({self.status})>'
        )

    @classmethod
    def record_for(cls, claim):
        """Record that this claim's intro was confirmed and its
        reward is owed. Idempotent: a second call for the same
        claim returns the existing event. Records the event ONLY
        — no payout is attempted (there is no payout path until
        Stripe is wired). Does not commit — the caller commits."""
        if claim is None:
            return None
        existing = cls.query.filter_by(claim_id=claim.id).first()
        if existing is not None:
            return existing
        event = cls(
            intro_bounty_id=claim.intro_bounty_id,
            claim_id=claim.id,
            amount=claim.intro_bounty.amount,
            status='pending_stripe',
        )
        db.session.add(event)
        return event
