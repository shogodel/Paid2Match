"""Space owner payouts (paid communities).

Each monthly payment for a paid space splits into the platform fee
(SPACE_PLATFORM_FEE_PERCENT, default 20%) and the space owner's
share (the rest, 80% by default). One row per paid invoice:

* owner has a ready Connect account -> the share is transferred to
  it (stripe.Transfer.create, idempotent on the payout id) and the
  row moves pending -> processing -> completed (transfer.paid) /
  failed, mirroring the Payout lifecycle in utils/payments.py;
* owner has no ready Connect account -> the row stays
  'pending_stripe' (the same status warm-intro rewards use for
  money owed but not yet payable through Stripe) and the owner is
  prompted to connect payouts. The next invoice for that owner
  retries older pending rows, so no share is lost when onboarding
  completes later.

One row per Stripe invoice id: a redelivered invoice.paid event
finds the existing row and never transfers twice.
"""
from decimal import Decimal

from extensions import db
from models import generate_uuid, utc_now


class SpacePayout(db.Model):
    """One owner's share of one paid-space invoice."""
    __tablename__ = 'space_payouts'

    # 'pending_stripe': owed, owner cannot be paid yet (no ready
    # Connect account) or the transfer has not been attempted.
    # 'processing': transfer created, awaiting transfer.paid.
    # 'completed' / 'failed': terminal, set by transfer events.
    VALID_STATUSES = ['pending_stripe', 'processing', 'completed', 'failed']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    space_id = db.Column(
        db.String(36), db.ForeignKey('spaces.id'), nullable=False, index=True
    )
    owner_id = db.Column(
        db.String(36), db.ForeignKey('users.id'), nullable=False, index=True
    )
    member_id = db.Column(
        db.String(36), db.ForeignKey('users.id'), nullable=False, index=True
    )
    # The Stripe invoice this share comes from — unique, so a
    # redelivered invoice.paid event is a no-op.
    stripe_invoice_id = db.Column(
        db.String(100), nullable=False, unique=True, index=True
    )
    stripe_subscription_id = db.Column(db.String(100), nullable=True, index=True)
    # The invoice's charge (or payment intent): the transfer's
    # source transaction. Stored on the row so a pending payout can
    # be retried after the owner finishes onboarding — a transient
    # attribute would be lost between requests.
    source_charge_id = db.Column(db.String(100), nullable=True)
    # Gross invoice amount, platform fee kept, and net to the owner
    # (all in cents — the invoice itself is in cents).
    gross_cents = db.Column(db.Integer, nullable=False)
    platform_fee_cents = db.Column(db.Integer, nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default='cad')
    status = db.Column(
        db.String(20), nullable=False, default='pending_stripe'
    )
    stripe_transfer_id = db.Column(db.String(100), nullable=True, index=True)
    failure_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    space = db.relationship('Space', backref='payouts')
    owner = db.relationship('User', foreign_keys=[owner_id])
    member = db.relationship('User', foreign_keys=[member_id])

    def __repr__(self):
        return f'<SpacePayout {self.id} {self.status} {self.amount_cents}>'

    @property
    def amount(self):
        """Owner share in dollars (Decimal), for display."""
        return Decimal(self.amount_cents) / 100

    @property
    def gross(self):
        return Decimal(self.gross_cents) / 100
