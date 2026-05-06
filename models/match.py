"""Match model."""
from extensions import db
from models import generate_uuid, utc_now


class Match(db.Model):
    """Matched bounty-hunter pair."""
    __tablename__ = 'matches'

    VALID_PAYOUT_STATUSES = ['pending', 'processing', 'completed', 'failed']

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