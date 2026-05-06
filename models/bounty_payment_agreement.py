"""Bounty Payment Agreement model."""
from extensions import db
from models import generate_uuid, utc_now


class BountyPaymentAgreement(db.Model):
    """Agreement between bounty poster and third-party payer."""
    __tablename__ = 'bounty_payment_agreements'

    VALID_STATUSES = ['pending', 'accepted', 'declined']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=False, index=True)
    inviter_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    payer_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    status = db.Column(db.String(20), default='pending')
    message = db.Column(db.Text, nullable=True)
    invited_at = db.Column(db.DateTime, default=utc_now)
    responded_at = db.Column(db.DateTime, nullable=True)

    bounty = db.relationship('Bounty', backref='payment_agreements')
    inviter = db.relationship('User', foreign_keys=[inviter_id])
    payer = db.relationship('User', foreign_keys=[payer_id])

    def __repr__(self):
        return f'<BountyPaymentAgreement {self.bounty_id} -> {self.payer_id}>'

    @property
    def is_pending(self):
        return self.status == 'pending'

    @property
    def is_accepted(self):
        return self.status == 'accepted'

    @property
    def is_declined(self):
        return self.status == 'declined'

    def accept(self):
        self.status = 'accepted'
        self.responded_at = utc_now()
        db.session.commit()

    def decline(self):
        self.status = 'declined'
        self.responded_at = utc_now()
        db.session.commit()