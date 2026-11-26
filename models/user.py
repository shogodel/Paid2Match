"""User model."""
from flask_login import UserMixin
from sqlalchemy import event, select
from sqlalchemy.orm import validates

from extensions import db
from models import generate_uuid, utc_now
from utils.slugs import (
    HANDLE_RE, handle_from_name, is_reserved_handle, is_valid_handle,
)


class User(db.Model, UserMixin):
    """User account model."""
    __tablename__ = 'users'

    @validates('handle')
    def _validate_handle(self, key, value):
        """Handles are 3–30 lowercase letters, digits, dots, dashes
        and underscores, and never a reserved handle (route segments
        and app words — see utils/slugs.RESERVED_HANDLES). None is
        allowed here — the before_insert event fills it in from the
        full name — but an explicitly set bad handle is rejected at
        the model, not just the form."""
        if value is None:
            return value
        if not is_valid_handle(value):
            raise ValueError(
                'handle must match '
                f'{HANDLE_RE.pattern!r}, got {value!r}'
            )
        if is_reserved_handle(value):
            raise ValueError(f'handle {value!r} is reserved')
        return value

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=True)
    full_name = db.Column(db.String(120), nullable=False)
    # Nice URLs: /profile/<handle>. Auto-generated from the full
    # name at insert (see the before_insert event at the bottom of
    # this file); the user may change it during profile setup.
    handle = db.Column(db.String(30), nullable=False, unique=True, index=True)
    role = db.Column(db.String(20), nullable=False, default='user')
    is_active = db.Column(db.Boolean, default=True)
    is_deleted = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime, nullable=True)
    # Email verification (M0): accounts created after this lands must confirm
    # their email before they can log in. Pre-existing accounts were
    # grandfathered as verified by the M0 migration.
    email_verified = db.Column(db.Boolean, default=False)
    email_verified_at = db.Column(db.DateTime, nullable=True)
    # Founding referrer (M2): admin-set flag. Founding referrers pay a
    # 15% platform fee instead of the standard 20% for 12 months.
    founding_referrer = db.Column(db.Boolean, default=False)
    # When the flag was granted (set by the admin toggle; cleared
    # when the flag is removed). The 12-month window runs from here
    # — see utils.payments.founding_benefit_active. NULL means the
    # flag predates window tracking and is grandfathered.
    founding_referrer_since = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    last_login = db.Column(db.DateTime, nullable=True)

    profiles = db.relationship('Profile', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    bounties = db.relationship('Bounty', backref='poster', lazy='dynamic', foreign_keys='Bounty.poster_id')
    pitches = db.relationship('Pitch', backref='hunter', lazy='dynamic', foreign_keys='Pitch.hunter_id')
    matches_as_hunter = db.relationship('Match', backref='hunter', lazy='dynamic', foreign_keys='Match.hunter_id')
    matches_as_employer = db.relationship('Match', backref='employer', lazy='dynamic', foreign_keys='Match.employer_id')
    messages_sent = db.relationship('Message', backref='sender', lazy='dynamic', foreign_keys='Message.sender_id')
    messages_received = db.relationship('Message', backref='recipient', lazy='dynamic', foreign_keys='Message.recipient_id')
    # M1 social core.
    posts = db.relationship('Post', backref='author', lazy='dynamic', foreign_keys='Post.author_id')
    following = db.relationship('Follow', backref='follower', lazy='dynamic',
                                foreign_keys='Follow.follower_id', cascade='all, delete-orphan')
    followers = db.relationship('Follow', backref='followed', lazy='dynamic',
                                foreign_keys='Follow.followed_id', cascade='all, delete-orphan')
    notifications = db.relationship('Notification', backref='recipient', lazy='dynamic',
                                    foreign_keys='Notification.recipient_id', cascade='all, delete-orphan')
    referrals_made = db.relationship('Referral', backref='referrer', lazy='dynamic',
                                     foreign_keys='Referral.referrer_id')

    def __repr__(self):
        return f'<User {self.email}>'

    @property
    def has_approved_pledge(self):
        """True when the user has an approved human-reviewed employer
        pledge (models/employer_pledge.py) — drives the
        "Human-Reviewed" badge on profiles and bounty detail. This is
        the human-reviewed pledge, not the M3 self-published Pledge
        (utils.pledges.has_active_pledge covers that one)."""
        from models.employer_pledge import EmployerPledge
        return EmployerPledge.approved_for_user(self.id) is not None

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
            'handle': self.handle,
            'role': self.role,
            'is_active': self.is_active,
            'email_verified': self.email_verified,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None
        }


@event.listens_for(User, 'before_insert')
def _ensure_handle(mapper, connection, target):
    """Every user gets a handle at insert, generated from the full
    name ('Test User' → 'test.user', digits appended on collision)
    — so the /profile/<uuid> → /profile/<handle> redirect always
    has somewhere to go, even for users who never open profile
    setup. The uniqueness check runs on the event's connection,
    never through the flushing session."""
    if target.handle:
        return
    table = User.__table__

    def _taken(handle):
        # Siblings in the same flush are not in the DB yet (one
        # batched INSERT), so check the pending objects too.
        if any(
            o is not target and o.handle == handle
            for o in db.session.new
            if isinstance(o, User)
        ):
            return True
        return connection.execute(
            select(table.c.id).where(table.c.handle == handle)
        ).first() is not None

    target.handle = handle_from_name(target.full_name or '', taken=_taken)