"""Tips — send money to a user.

A tip is a one-way payment from one user to another via Stripe
Checkout (payment mode). The tipper pays the full amount; the
platform keeps TIP_PLATFORM_FEE_PERCENT (default 20%, admin-settable
via AdminSettings — a separate setting from the hire platform fee)
and the recipient's share is transferred to their Stripe Connect
account from the Checkout charge (separate-charges-and-transfers,
same rails as the escrow release in utils/payments.py).

Statuses:
- 'pending' — the tip row exists but the money has not reached the
  recipient yet. That covers two cases: the Checkout session has
  not completed (no payment intent yet), and the payment completed
  but the recipient has no ready Connect account (same
  pending-until-onboarded pattern as a hire payout waiting on
  onboarding). Only a pending tip WITH a payment intent can be paid
  out — see utils.tips.retry_pending_tips.
- 'completed' — the recipient's share was transferred.
- 'failed' — the transfer failed after capture (the platform holds
  the money; the admin listing shows it for resolution).

Tips are final — there is no refund path.

Money is recorded in integer cents and always charged in CAD by
Stripe (utils.payments.CURRENCY). ``currency`` is the display
currency the tipper picked (CAD/EUR/USD, record-only — the same
pattern as bounties: no FX conversion anywhere, the number is
charged as CAD).
"""
from extensions import db
from models import generate_uuid, utc_now


class Tip(db.Model):
    """A tip from one user to another."""
    __tablename__ = 'tips'

    STATUSES = ['pending', 'completed', 'failed']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    tipper_id = db.Column(
        db.String(36), db.ForeignKey('users.id'), nullable=False, index=True
    )
    recipient_id = db.Column(
        db.String(36), db.ForeignKey('users.id'), nullable=False, index=True
    )
    # Gross amount the tipper paid, in integer cents.
    amount_cents = db.Column(db.Integer, nullable=False)
    # Display currency (CAD/EUR/USD) — record-only, Stripe charges CAD.
    currency = db.Column(db.String(3), nullable=False, default='CAD')
    # Optional short message from the tipper to the recipient.
    message = db.Column(db.String(280), nullable=True)
    # Stripe Checkout session + PaymentIntent (set at fulfillment).
    stripe_session_id = db.Column(
        db.String(100), nullable=True, unique=True, index=True
    )
    stripe_payment_intent_id = db.Column(db.String(100), nullable=True)
    stripe_transfer_id = db.Column(db.String(100), nullable=True)
    # Platform fee locked in at fulfillment, in cents (None until paid).
    platform_fee_cents = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    # Why a transfer failed (admin visibility; None otherwise).
    failure_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    completed_at = db.Column(db.DateTime, nullable=True)

    tipper = db.relationship(
        'User', foreign_keys=[tipper_id], backref='tips_sent'
    )
    recipient = db.relationship(
        'User', foreign_keys=[recipient_id], backref='tips_received'
    )

    def __repr__(self):
        return (
            f'<Tip {self.id} {self.tipper_id} -> {self.recipient_id} '
            f'{self.amount_cents} {self.status}>'
        )

    @property
    def net_cents(self):
        """The recipient's share, in cents (None until the fee is
        locked in at fulfillment)."""
        if self.platform_fee_cents is None:
            return None
        return self.amount_cents - self.platform_fee_cents
