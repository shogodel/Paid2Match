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
    # M4: the Share this referral came through (A shares a need post,
    # B refers from that share — B's pitch carries the share id, which
    # is what makes the two-level payout split possible). NULL for
    # referrals that did not come through a share.
    via_share_id = db.Column(db.String(36), db.ForeignKey('shares.id'), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    hunter_message = db.Column(db.Text, nullable=True)
    employer_response = db.Column(db.Text, nullable=True)
    # When the poster first responded (interested/rejected). Set by
    # respond_to_pitch; used for the no-ghosting SLA (M2). NULL for
    # pitches responded before M2 — the SLA falls back to updated_at.
    responded_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    matches = db.relationship('Match', backref='pitch', lazy='dynamic')
    messages = db.relationship('Message', backref='pitch', lazy='dynamic')
    via_share = db.relationship('Share')

    def __repr__(self):
        return f'<Pitch {self.id} - {self.bounty_id}>'

    def to_dict(self):
        return {
            'id': self.id,
            'bounty_id': self.bounty_id,
            'hunter_id': self.hunter_id,
            'candidate_teaser': self.candidate_teaser,
            'via_share_id': self.via_share_id,
            'status': self.status,
            'hunter_message': self.hunter_message,
            'employer_response': self.employer_response,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }