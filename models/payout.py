"""Payout ledger (M2): one record per payout of a confirmed match.

The Payout is the source of truth for the referrer payout dashboard;
Match.payout_status mirrors it (kept in sync by utils/payments).
A payout is created when a paid match is dual-confirmed, and moves
pending -> processing -> completed/failed. Manual (Interac) payouts
are recorded by an admin against the same record — the interim path
while Stripe Connect is not approved.

M4: a match can have TWO payouts — the referrer's ('referral') and,
when the referral came through someone's share, the sharer's
('chain_share'). The platform fee is recorded once, on the referrer
payout; the chain payout's platform_fee is 0.
"""
from sqlalchemy.orm import validates

from extensions import db
from models import generate_uuid, utc_now


class Payout(db.Model):
    """One payout owed to a referrer for one confirmed match."""
    __tablename__ = 'payouts'

    # One payout of each kind per match: two simultaneous escrow
    # releases must not create two 'referral' rows for one match —
    # each row gets its own transfer, so a duplicate would pay the
    # referrer twice.
    __table_args__ = (
        db.UniqueConstraint(
            'match_id', 'kind', name='uq_payouts_match_kind'
        ),
    )

    VALID_STATUSES = ['pending', 'processing', 'completed', 'failed']
    VALID_METHODS = ['stripe', 'manual']
    # M4: 'referral' is the referrer's payout; 'chain_share' is the
    # sharer's cut of a two-level chain (utils/chains.py).
    VALID_KINDS = ['referral', 'chain_share']

    # Allowed status transitions. 'failed' can go back to 'pending' or
    # 'processing' for a retry, or to 'completed' when an admin resolves
    # it with a manual payout; 'completed' is terminal.
    TRANSITIONS = {
        'pending': {'processing', 'completed', 'failed'},
        'processing': {'completed', 'failed'},
        'completed': set(),
        'failed': {'pending', 'processing', 'completed'},
    }

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    match_id = db.Column(db.String(36), db.ForeignKey('matches.id'), nullable=False, index=True)
    referrer_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=True, index=True)
    # M4: which payout this is — the referrer's, or the sharer's cut
    # of a two-level chain. For a chain payout, referrer_id is the
    # sharer (the recipient), not the referrer.
    kind = db.Column(db.String(20), nullable=False, default='referral')
    # Gross reward, platform fee kept, and net amount to the referrer.
    gross_amount = db.Column(db.Numeric(12, 2), nullable=False)
    platform_fee = db.Column(db.Numeric(12, 2), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default='cad')
    method = db.Column(db.String(10), nullable=False, default='stripe')
    status = db.Column(db.String(20), nullable=False, default='pending')
    # Stripe transfer (set when the transfer is created) and the later
    # bank payout id (set by the payout.* webhooks, informational).
    stripe_transfer_id = db.Column(db.String(100), nullable=True, index=True)
    stripe_payout_id = db.Column(db.String(100), nullable=True)
    # Set once the escrow PaymentIntent has been captured, so a retry
    # never captures twice (capture is not idempotent by itself).
    captured_at = db.Column(db.DateTime, nullable=True)
    # Manual (Interac) payout details, filled in by the admin.
    manual_reference = db.Column(db.Text, nullable=True)
    manual_paid_at = db.Column(db.DateTime, nullable=True)
    recorded_by_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True)
    failure_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    match = db.relationship('Match', backref='payouts')
    referrer = db.relationship('User', foreign_keys=[referrer_id])
    recorded_by = db.relationship('User', foreign_keys=[recorded_by_id])

    def __repr__(self):
        return f'<Payout {self.id} {self.status} {self.amount}>'

    @validates('status')
    def _validate_status(self, key, value):
        if value is not None and value not in self.VALID_STATUSES:
            raise ValueError(
                f"payout status must be one of {self.VALID_STATUSES}, got {value!r}"
            )
        return value

    @validates('kind')
    def _validate_kind(self, key, value):
        if value is not None and value not in self.VALID_KINDS:
            raise ValueError(
                f"payout kind must be one of {self.VALID_KINDS}, got {value!r}"
            )
        return value

    def set_status(self, new_status):
        """Move to new_status, enforcing the transition map. Raises
        ValueError on an illegal transition (a bug, not user input)."""
        if new_status not in self.VALID_STATUSES:
            raise ValueError(f"unknown payout status {new_status!r}")
        allowed = self.TRANSITIONS.get(self.status or 'pending', set())
        if new_status != self.status and new_status not in allowed:
            raise ValueError(
                f"illegal payout transition {self.status!r} -> {new_status!r}"
            )
        self.status = new_status

    def to_dict(self):
        return {
            'id': self.id,
            'match_id': self.match_id,
            'referrer_id': self.referrer_id,
            'bounty_id': self.bounty_id,
            'kind': self.kind,
            'gross_amount': float(self.gross_amount) if self.gross_amount else None,
            'platform_fee': float(self.platform_fee) if self.platform_fee else None,
            'amount': float(self.amount) if self.amount else None,
            'currency': self.currency,
            'method': self.method,
            'status': self.status,
            'stripe_transfer_id': self.stripe_transfer_id,
            'manual_reference': self.manual_reference,
            'failure_reason': self.failure_reason,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
