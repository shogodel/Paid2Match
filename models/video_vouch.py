"""Video vouches — 30-second video pitches and vouches.

A candidate pitches in a 30-second video for a referral need
(kind='pitch', tied to a bounty), and a referrer vouches for someone
on camera (kind='vouch', tied to the person vouched for). No CVs, no
AI screening — humans vouching for humans.

The stored filename is a random uuid, so playback URLs are
unguessable and the serve route needs no login. Files live under
VIDEO_VOUCH_UPLOAD_FOLDER (see config.py and
docs/VIDEO_VOUCH_UPLOADS.md — the live container needs a volume
mount or uploads are wiped on rebuild).
"""
from sqlalchemy.orm import validates

from extensions import db
from models import generate_uuid, utc_now


class VideoVouch(db.Model):
    """One uploaded video: a pitch for a need, or a vouch for a person."""
    __tablename__ = 'video_vouches'

    KINDS = ['pitch', 'vouch']
    STATUSES = ['active', 'removed']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    kind = db.Column(db.String(10), nullable=False)
    # The uploader: the candidate pitching, or the referrer vouching.
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    # The person vouched for — required when kind='vouch', NULL for pitches.
    target_user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True, index=True)
    # The need pitched for — required when kind='pitch', NULL for vouches.
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=True, index=True)
    # Random uuid-based stored name (unguessable) — the only name
    # that ever appears in a playback URL.
    filename = db.Column(db.String(255), nullable=False, unique=True)
    original_filename = db.Column(db.String(255), nullable=True)
    duration_seconds = db.Column(db.Float, nullable=True)
    file_size = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='active')
    created_at = db.Column(db.DateTime, default=utc_now)

    user = db.relationship('User', foreign_keys=[user_id])
    target = db.relationship('User', foreign_keys=[target_user_id])
    bounty = db.relationship('Bounty')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Cross-field rules (kind/target/bounty) can't live in
        # @validates — validators fire per attribute, in whatever
        # order the attributes are set, so kind may not be set yet
        # when target_user_id is validated. __init__ runs only on
        # construction (SQLAlchemy does not call it when loading rows
        # from the DB), so pre-existing rows are never re-validated.
        self.validate()

    def validate(self):
        """Enforce the kind/target/bounty rules. Raises ValueError."""
        if self.kind not in self.KINDS:
            raise ValueError(f'Invalid video vouch kind: {self.kind!r}')
        if self.kind == 'vouch' and not self.target_user_id:
            raise ValueError('A vouch needs the person being vouched for.')
        if self.kind == 'pitch' and not self.bounty_id:
            raise ValueError('A pitch needs the need being pitched for.')

    @validates('kind')
    def _validate_kind(self, key, value):
        if value not in self.KINDS:
            raise ValueError(f'Invalid video vouch kind: {value!r}')
        return value

    @validates('status')
    def _validate_status(self, key, value):
        if value not in self.STATUSES:
            raise ValueError(f'Invalid video vouch status: {value!r}')
        return value

    def __repr__(self):
        return f'<VideoVouch {self.kind} {self.id} by {self.user_id}>'

    def to_dict(self):
        return {
            'id': self.id,
            'kind': self.kind,
            'user_id': self.user_id,
            'target_user_id': self.target_user_id,
            'bounty_id': self.bounty_id,
            'filename': self.filename,
            'original_filename': self.original_filename,
            'duration_seconds': self.duration_seconds,
            'file_size': self.file_size,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
