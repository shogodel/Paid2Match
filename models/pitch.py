"""Pitch model."""
from extensions import db
from models import generate_uuid, utc_now


class Pitch(db.Model):
    """Pitch/hunter application to a bounty."""
    __tablename__ = 'pitches'

    VALID_STATUSES = ['pending', 'viewed', 'interested', 'rejected', 'accepted']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=False, index=True)
    hunter_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    candidate_teaser = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    hunter_message = db.Column(db.Text, nullable=True)
    employer_response = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    matches = db.relationship('Match', backref='pitch', lazy='dynamic')
    messages = db.relationship('Message', backref='pitch', lazy='dynamic')

    def __repr__(self):
        return f'<Pitch {self.id} - {self.bounty_id}>'

    def to_dict(self):
        return {
            'id': self.id,
            'bounty_id': self.bounty_id,
            'hunter_id': self.hunter_id,
            'candidate_teaser': self.candidate_teaser,
            'status': self.status,
            'hunter_message': self.hunter_message,
            'employer_response': self.employer_response,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }