"""Match model."""
from sqlalchemy.orm import validates

from extensions import db
from models import generate_uuid, utc_now


class Match(db.Model):
    """Matched bounty-hunter pair."""
    __tablename__ = 'matches'

    VALID_PAYOUT_STATUSES = ['pending', 'processing', 'completed', 'failed']

    # Allowed payout transitions. 'failed' can go back to 'pending' or
    # 'processing' for a retry, or to 'completed' when an admin resolves
    # it with a manual payout; 'completed' is terminal. The old
    # 'pending_release' value was never in VALID_PAYOUT_STATUSES and
    # dead-ended payouts — the @validates guard below makes it
    # impossible to set an invalid status again.
    PAYOUT_TRANSITIONS = {
        'pending': {'processing', 'completed', 'failed'},
        'processing': {'completed', 'failed'},
        'completed': set(),
        'failed': {'pending', 'processing', 'completed'},
    }

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=False, index=True)
    pitch_id = db.Column(db.String(36), db.ForeignKey('pitches.id'), nullable=True)
    hunter_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    employer_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    hunter_confirmed = db.Column(db.Boolean, default=False)
    employer_confirmed = db.Column(db.Boolean, default=False)
    payout_amount = db.Column(db.Numeric(12, 2), nullable=True)
    platform_fee = db.Column(db.Numeric(12, 2), nullable=True)
    transaction_fee = db.Column(db.Numeric(12, 2), nullable=True)
    payout_status = db.Column(db.String(20), nullable=False, default='pending')
    confirmed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    disputes = db.relationship('Dispute', backref='match', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Match {self.id}>'

    @validates('payout_status')
    def _validate_payout_status(self, key, value):
        if value is not None and value not in self.VALID_PAYOUT_STATUSES:
            raise ValueError(
                f"payout_status must be one of {self.VALID_PAYOUT_STATUSES}, got {value!r}"
            )
        return value

    def set_payout_status(self, new_status):
        """Move to new_status, enforcing the transition map. Raises
        ValueError on an illegal transition (a bug, not user input)."""
        if new_status not in self.VALID_PAYOUT_STATUSES:
            raise ValueError(f"unknown payout status {new_status!r}")
        allowed = self.PAYOUT_TRANSITIONS.get(self.payout_status or 'pending', set())
        if new_status != self.payout_status and new_status not in allowed:
            raise ValueError(
                f"illegal payout transition {self.payout_status!r} -> {new_status!r}"
            )
        self.payout_status = new_status

    def to_dict(self):
        return {
            'id': self.id,
            'bounty_id': self.bounty_id,
            'pitch_id': self.pitch_id,
            'hunter_id': self.hunter_id,
            'employer_id': self.employer_id,
            'hunter_confirmed': self.hunter_confirmed,
            'employer_confirmed': self.employer_confirmed,
            'payout_amount': float(self.payout_amount) if self.payout_amount else None,
            'platform_fee': float(self.platform_fee) if self.platform_fee else None,
            'payout_status': self.payout_status,
            'confirmed_at': self.confirmed_at.isoformat() if self.confirmed_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }