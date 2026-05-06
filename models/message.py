"""Message model."""
from extensions import db
from models import generate_uuid, utc_now


class Message(db.Model):
    """Messaging between users."""
    __tablename__ = 'messages'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    sender_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    recipient_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=True, index=True)
    pitch_id = db.Column(db.String(36), db.ForeignKey('pitches.id'), nullable=True, index=True)
    content = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utc_now, index=True)

    def __repr__(self):
        return f'<Message {self.id}>'

    def to_dict(self):
        return {
            'id': self.id,
            'sender_id': self.sender_id,
            'recipient_id': self.recipient_id,
            'bounty_id': self.bounty_id,
            'pitch_id': self.pitch_id,
            'content': self.content,
            'is_read': self.is_read,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }