"""Staked vouches — trust backed by money, not just clicks.

A staked vouch is a recorded commitment: a voucher puts part of
their balance behind a candidate. If the candidate flakes, the
voucher loses the stake; if the candidate completes the need, the
stake is released. The amount is a signal of conviction — a vouch
with money on the line weighs more than a click.

IMPORTANT — RECORD ONLY, NO MONEY MOVES: there is no real balance
to debit and no Stripe calls anywhere in this feature (same
record-only pattern as models/staged_reward.py, whose events stay
'pending_stripe' until payout wiring exists). A stake is a
RECORDED commitment against the voucher's future referral
earnings, resolved as a record only: 'released', 'forfeited' and
'returned' are statuses, not money movements. The UI must never
claim money moved — every surface says the stake is recorded, and
the resolution copy describes the outcome, not a payment. If real
payouts ever land, the debit/forfeit wiring joins this module
then; until then, nothing here touches Stripe or any balance.

Rules:
- amount must be > 0, currency one of CAD/EUR/USD (validated via
  utils.currency.validate_currency — no FX conversion anywhere,
  the amount is recorded and shown exactly as entered).
- The voucher cannot stake on themselves.
- One active stake per (voucher, candidate, bounty): a second
  active stake for the same triple is blocked (enforced by
  StakedVouch.create, which the create route uses). A resolved
  stake frees the triple — the voucher may stake again afterwards.
- Only an active stake can be resolved, by the need's poster (when
  the stake relates to a need) or an admin. Resolution records
  resolved_at + resolved_by_id; it never deletes the row.
"""
from decimal import Decimal

from flask_babel import lazy_gettext as _l
from sqlalchemy.orm import validates

from extensions import db
from models import generate_uuid, utc_now
from utils.currency import validate_currency

# Resolution outcomes (what the resolver picks) -> resulting status.
# 'completed': the candidate completed the need / was hired — the
# stake is released (the voucher's backing was well placed).
# 'flaked': the candidate flaked or no-showed — the stake is
# forfeited (the voucher's backing cost them).
# 'cancelled': the need was cancelled — the stake is returned
# (nobody's fault, the commitment is void).
RESOLUTIONS = {
    'completed': 'released',
    'flaked': 'forfeited',
    'cancelled': 'returned',
}


class StakedVouch(db.Model):
    """One stake: a voucher backing a candidate with a recorded
    amount, optionally tied to a need."""
    __tablename__ = 'staked_vouches'

    STATUSES = ['active', 'released', 'forfeited', 'returned']
    STATUS_LABELS = {
        'active': _l('Active'),
        'released': _l('Released'),
        'forfeited': _l('Forfeited'),
        'returned': _l('Returned'),
    }
    # Badge colours for the status chips (Bootstrap classes).
    STATUS_BADGE_CLASSES = {
        'active': 'bg-warning text-dark',
        'released': 'bg-success',
        'forfeited': 'bg-danger',
        'returned': 'bg-secondary',
    }

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    # The user putting money on the line.
    voucher_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    # The user vouched for.
    candidate_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    # The need this stake relates to — NULL for a general stake
    # placed from the candidate's profile.
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=True, index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default='CAD')
    status = db.Column(db.String(20), nullable=False, default='active')
    created_at = db.Column(db.DateTime, default=utc_now)
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolved_by_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True)

    voucher = db.relationship('User', foreign_keys=[voucher_id])
    candidate = db.relationship('User', foreign_keys=[candidate_id])
    bounty = db.relationship('Bounty')
    resolved_by = db.relationship('User', foreign_keys=[resolved_by_id])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Cross-field rule (voucher != candidate) can't live in
        # @validates — validators fire per attribute, in whatever
        # order the attributes are set. __init__ runs only on
        # construction (SQLAlchemy does not call it when loading
        # rows from the DB), so pre-existing rows are never
        # re-validated.
        self.validate()

    def validate(self):
        """Enforce the cross-field rules. Raises ValueError."""
        if (
            self.voucher_id is not None
            and self.candidate_id is not None
            and self.voucher_id == self.candidate_id
        ):
            raise ValueError('You cannot stake on yourself.')

    @validates('amount')
    def _validate_amount(self, key, value):
        if value is None:
            raise ValueError('A stake needs an amount greater than zero.')
        amount = Decimal(str(value))
        if amount <= 0:
            raise ValueError('A stake needs an amount greater than zero.')
        return value

    @validates('currency')
    def _validate_currency(self, key, value):
        """Multi-currency: only CAD/EUR/USD, stored uppercased."""
        return validate_currency(value)

    @validates('status')
    def _validate_status(self, key, value):
        if value not in self.STATUSES:
            raise ValueError(f'Invalid staked vouch status: {value!r}')
        return value

    @classmethod
    def active_for(cls, voucher_id, candidate_id, bounty_id):
        """The active stake for a (voucher, candidate, bounty)
        triple, or None. A NULL bounty matches only NULL-bounty
        stakes (a general stake and a need stake are different
        triples)."""
        query = cls.query.filter_by(
            voucher_id=voucher_id,
            candidate_id=candidate_id,
            status='active',
        )
        if bounty_id is None:
            query = query.filter(cls.bounty_id.is_(None))
        else:
            query = query.filter_by(bounty_id=bounty_id)
        return query.first()

    @classmethod
    def create(cls, voucher_id, candidate_id, amount, currency='CAD',
               bounty_id=None):
        """Create + persist a stake, enforcing the one-active-stake
        rule. Raises ValueError when the triple already has an
        active stake (or when the model validation fails). The
        caller owns the commit — the row is added to the session
        and flushed so the duplicate check sees it."""
        if cls.active_for(voucher_id, candidate_id, bounty_id):
            raise ValueError(
                'You already have an active stake on this person '
                'for this need.'
            )
        stake = cls(
            voucher_id=voucher_id,
            candidate_id=candidate_id,
            bounty_id=bounty_id,
            amount=amount,
            currency=currency,
        )
        db.session.add(stake)
        db.session.flush()
        return stake

    def resolve(self, outcome, resolved_by_id):
        """Resolve an active stake. `outcome` is one of
        'completed' / 'flaked' / 'cancelled' (see RESOLUTIONS);
        records resolved_at + resolved_by_id. Raises ValueError
        for a bad outcome or a non-active stake. Record only —
        no money moves."""
        if outcome not in RESOLUTIONS:
            raise ValueError(f'Unknown stake outcome: {outcome!r}')
        if self.status != 'active':
            raise ValueError(
                f'Only an active stake can be resolved '
                f'(this one is {self.status}).'
            )
        self.status = RESOLUTIONS[outcome]
        self.resolved_at = utc_now()
        self.resolved_by_id = resolved_by_id
        return self

    @property
    def status_label(self):
        """The translated display label for this status."""
        return self.STATUS_LABELS.get(self.status, self.status)

    @property
    def badge_class(self):
        """The Bootstrap badge class for this status."""
        return self.STATUS_BADGE_CLASSES.get(self.status, 'bg-secondary')

    def __repr__(self):
        return (
            f'<StakedVouch {self.voucher_id} -> {self.candidate_id} '
            f'{self.amount} {self.currency} ({self.status})>'
        )

    def to_dict(self):
        return {
            'id': self.id,
            'voucher_id': self.voucher_id,
            'candidate_id': self.candidate_id,
            'bounty_id': self.bounty_id,
            'amount': float(self.amount) if self.amount is not None else None,
            'currency': self.currency,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'resolved_at': self.resolved_at.isoformat() if self.resolved_at else None,
            'resolved_by_id': self.resolved_by_id,
        }
