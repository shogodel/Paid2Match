"""Dispute model."""
from extensions import db
from models import generate_uuid, utc_now


class Dispute(db.Model):
    """Dispute raised for a match."""
    __tablename__ = 'disputes'

    VALID_STATUSES = ['open', 'under_review', 'resolved', 'closed']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    match_id = db.Column(db.String(36), db.ForeignKey('matches.id'), nullable=False, index=True)
    raised_by_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='open')
    resolution = db.Column(db.Text, nullable=True)
    resolved_by_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    resolved_at = db.Column(db.DateTime, nullable=True)

    raised_by = db.relationship('User', foreign_keys=[raised_by_id])
    resolved_by = db.relationship('User', foreign_keys=[resolved_by_id])

    def __repr__(self):
        return f'<Dispute {self.id}>'

    def to_dict(self):
        return {
            'id': self.id,
            'match_id': self.match_id,
            'raised_by_id': self.raised_by_id,
            'reason': self.reason,
            'status': self.status,
            'resolution': self.resolution,
            'resolved_by_id': self.resolved_by_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'resolved_at': self.resolved_at.isoformat() if self.resolved_at else None
        }