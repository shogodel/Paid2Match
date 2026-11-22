"""Identity verification requests (M2): the workflow that actually sets
Profile.verified (dead since the beginning — nothing ever set it).

A user requests verification (optionally saying how they can be
checked — a LinkedIn profile, a business registration, a phone call);
an admin approves or rejects in the queue. Approval sets verified=True
on all of the user's profiles and feeds the trust score's identity
component; rejection leaves them unverified with an admin note.
"""
from extensions import db
from models import generate_uuid, utc_now


class VerificationRequest(db.Model):
    """One user's request for admin identity verification."""
    __tablename__ = 'verification_requests'

    STATUSES = ['pending', 'approved', 'rejected']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    # How the user suggests they can be verified (optional free text).
    note = db.Column(db.Text, nullable=True)
    admin_note = db.Column(db.Text, nullable=True)
    reviewed_by_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', foreign_keys=[user_id])
    reviewed_by = db.relationship('User', foreign_keys=[reviewed_by_id])

    def __repr__(self):
        return f'<VerificationRequest {self.user_id} {self.status}>'

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'status': self.status,
            'note': self.note,
            'admin_note': self.admin_note,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
        }
