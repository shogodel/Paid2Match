"""Stripe webhook event log (money audit 2026-11-24).

Stripe redelivers webhook events (retries, and our own endpoint is
live at two URLs — /needs/stripe-webhook and the legacy
/bounties/stripe-webhook). Most handlers are idempotent on their own
(session id, transfer id, payout status), but some are not: a
redelivered upgrade-EXTENSION event re-extends the upgrade a second
time for one payment, and a redelivered bounty checkout re-notifies
the poster. The event log is the single dedup layer: the webhook
records the Stripe event id (unique) before dispatching, and a
duplicate delivery is acknowledged without reprocessing.

The row is written in the same transaction as the business changes
(the webhook commits after each handler), so a crash before the
first commit leaves nothing recorded and the redelivery processes
normally; a crash after it leaves both the event and its effects
recorded.
"""
from extensions import db
from models import generate_uuid, utc_now


class StripeEventLog(db.Model):
    """One processed Stripe webhook event, keyed by Stripe's event id."""
    __tablename__ = 'stripe_event_logs'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    event_id = db.Column(db.String(100), nullable=False, unique=True, index=True)
    event_type = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    def __repr__(self):
        return f'<StripeEventLog {self.event_id} {self.event_type}>'
