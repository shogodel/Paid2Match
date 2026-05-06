"""User model."""
from flask_login import UserMixin

from extensions import db
from models import generate_uuid, utc_now


class User(db.Model, UserMixin):
    """User account model."""
    __tablename__ = 'users'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=True)
    full_name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')
    is_active = db.Column(db.Boolean, default=True)
    is_deleted = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    last_login = db.Column(db.DateTime, nullable=True)

    profiles = db.relationship('Profile', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    bounties = db.relationship('Bounty', backref='poster', lazy='dynamic', foreign_keys='Bounty.poster_id')
    pitches = db.relationship('Pitch', backref='hunter', lazy='dynamic', foreign_keys='Pitch.hunter_id')
    matches_as_hunter = db.relationship('Match', backref='hunter', lazy='dynamic', foreign_keys='Match.hunter_id')
    matches_as_employer = db.relationship('Match', backref='employer', lazy='dynamic', foreign_keys='Match.employer_id')
    messages_sent = db.relationship('Message', backref='sender', lazy='dynamic', foreign_keys='Message.sender_id')
    messages_received = db.relationship('Message', backref='recipient', lazy='dynamic', foreign_keys='Message.recipient_id')

    def __repr__(self):
        return f'<User {self.email}>'

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = utc_now()
        self.is_active = False
        self.email = f'deleted_{self.id[:8]}@example.com'
        self.full_name = 'Deleted User'
        self.password_hash = None

    @classmethod
    def get_active(cls, user_id):
        return cls.query.filter_by(id=user_id, is_deleted=False).first()

    @classmethod
    def active_users(cls):
        return cls.query.filter_by(is_deleted=False)

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'full_name': self.full_name,
            'role': self.role,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None
        }