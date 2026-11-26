"""Win stories — short first-person accounts of completed hires.

The wins wall (/wins) shows one story per role per completed bounty:
the poster, the referrer, and the hired candidate each tell their
side of the same hire. A story only ever appears on the wall when
its bounty has a completed match (both sides confirmed) — the wall
query enforces that, so a story written against an uncompleted
match never leaks onto the public wall.

One story per role per bounty (``uq_win_stories_bounty_role``): the
poster gets one story, the referrer gets one, the hired candidate
gets one. The database constraint is the backstop; the route checks
first and refuses a second story with a friendly message.

The hired candidate is usually not a Paid2Match user — candidates
exist only as teasers on pitches, there is no candidate user link
on Match — so the 'hired' story is written by one of the two
participants who know the hire (the poster or the referrer). See
blueprints/wins.py eligible_roles() for the rule.
"""

from flask_babel import lazy_gettext as _l
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import validates

from extensions import db
from models import generate_uuid, utc_now


class WinStory(db.Model):
    """One short story about one completed hire, told from one side."""
    __tablename__ = 'win_stories'
    __table_args__ = (
        # One story per role per bounty — the poster, the referrer
        # and the hired candidate each get exactly one.
        UniqueConstraint(
            'bounty_id', 'role', name='uq_win_stories_bounty_role'
        ),
    )

    VALID_ROLES = ['poster', 'referrer', 'hired']
    ROLE_LABELS = {
        'poster': _l('Poster'),
        'referrer': _l('Referrer'),
        'hired': _l('Hired candidate'),
    }
    BODY_MAX_LENGTH = 1000

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=False, index=True)
    # The completed match this story is about. Nullable: a story is
    # filed per bounty, and the match link is a convenience for the
    # wall (amount earned, match context) — the wall falls back to
    # the bounty's completed match when it is not set.
    match_id = db.Column(db.String(36), db.ForeignKey('matches.id'), nullable=True, index=True)
    author_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    role = db.Column(db.String(10), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now, index=True)

    bounty = db.relationship('Bounty', backref='win_stories')
    match = db.relationship('Match', backref='win_stories')
    author = db.relationship('User', backref='win_stories')

    def __repr__(self):
        return f'<WinStory {self.id} {self.role} {self.bounty_id}>'

    @validates('role')
    def _validate_role(self, key, value):
        if value is not None and value not in self.VALID_ROLES:
            raise ValueError(
                f"win story role must be one of {self.VALID_ROLES}, "
                f"got {value!r}"
            )
        return value

    @validates('body')
    def _validate_body(self, key, value):
        """Stories stay short: at most BODY_MAX_LENGTH characters.
        The form enforces this first with a proper error message;
        this guard is the backstop for direct construction."""
        if value is not None and len(value) > self.BODY_MAX_LENGTH:
            raise ValueError(
                f"win story body must be at most "
                f"{self.BODY_MAX_LENGTH} characters, got {len(value)}"
            )
        return value

    @property
    def role_label(self):
        """Human label for the role ('Poster', 'Referrer',
        'Hired candidate')."""
        return self.ROLE_LABELS.get(self.role, self.role)

    def to_dict(self):
        return {
            'id': self.id,
            'bounty_id': self.bounty_id,
            'match_id': self.match_id,
            'author_id': self.author_id,
            'role': self.role,
            'body': self.body,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
