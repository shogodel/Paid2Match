"""Stripe Connect account (M2): one Express account per referrer.

The referrer onboards through Stripe (identity/KYC via Stripe); this
record mirrors the account state so the app can decide whether a
referrer can be paid, without calling Stripe on every page. It is kept
in sync by the `account.updated` webhook and by refresh-on-return.
No secrets live here — only the account id and capability flags.
"""
import json

from extensions import db
from models import generate_uuid, utc_now


class ConnectAccount(db.Model):
    """A referrer's Stripe Connect Express account."""
    __tablename__ = 'connect_accounts'

    # pending: created, onboarding not finished. complete: payouts
    # enabled. restricted: Stripe needs more info (requirements due).
    ONBOARDING_STATUSES = ['pending', 'complete', 'restricted']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, unique=True, index=True)
    stripe_account_id = db.Column(db.String(100), nullable=False, unique=True, index=True)
    onboarding_status = db.Column(db.String(20), nullable=False, default='pending')
    charges_enabled = db.Column(db.Boolean, default=False)
    payouts_enabled = db.Column(db.Boolean, default=False)
    details_submitted = db.Column(db.Boolean, default=False)
    # JSON list of Stripe's currently_due requirements (as text).
    requirements_due = db.Column(db.Text, nullable=True)
    # Last successful bank payout Stripe reported (payout.paid webhook).
    last_payout_at = db.Column(db.DateTime, nullable=True)
    last_payout_amount = db.Column(db.Numeric(12, 2), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    user = db.relationship('User', backref='connect_account', uselist=False)

    def __repr__(self):
        return f'<ConnectAccount {self.user_id} {self.stripe_account_id}>'

    @property
    def is_ready(self):
        """True when Stripe can pay this referrer."""
        return bool(self.payouts_enabled and self.charges_enabled)

    @property
    def kyc_passed(self):
        """True when Stripe has verified the referrer's identity — the
        Stripe KYC input of the trust score (§3.1)."""
        return bool(self.details_submitted and self.payouts_enabled)

    @property
    def requirements_list(self):
        """The currently_due requirements as a list (parsed from JSON)."""
        if not self.requirements_due:
            return []
        try:
            return json.loads(self.requirements_due)
        except (ValueError, TypeError):
            return []

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'stripe_account_id': self.stripe_account_id,
            'onboarding_status': self.onboarding_status,
            'charges_enabled': self.charges_enabled,
            'payouts_enabled': self.payouts_enabled,
            'details_submitted': self.details_submitted,
            'last_payout_at': self.last_payout_at.isoformat() if self.last_payout_at else None,
            'last_payout_amount': float(self.last_payout_amount) if self.last_payout_amount else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
