"""Topic spaces (Phase 2): community skeleton. A space is a place a
post can belong to — by trade, city, or interest — so the network
stops feeling like one empty room. Posts keep appearing in the main
feeds too; the space is an additional home, not a separate silo.

Membership is per-space and carries a notification setting, Discord /
Slack style: 'all' (every new post in the space notifies you),
'mentions' (only @mentions notify you), 'muted' (no space
notifications at all).
"""
from flask_babel import lazy_gettext as _l

from extensions import db
from models import generate_uuid, utc_now


class Space(db.Model):
    """A topic space."""
    __tablename__ = 'spaces'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    slug = db.Column(db.String(50), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500), nullable=True)
    # Lucide icon name from static/img/icons.svg (e.g. 'hard-hat').
    icon = db.Column(db.String(30), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    posts = db.relationship(
        'Post', backref='space', lazy='dynamic',
        foreign_keys='Post.space_id',
    )
    memberships = db.relationship(
        'SpaceMembership', backref='space', lazy='dynamic',
        cascade='all, delete-orphan',
    )

    def __repr__(self):
        return f'<Space {self.slug}>'

    @property
    def member_count(self):
        return self.memberships.count()

    @property
    def post_count(self):
        return self.posts.filter_by(is_hidden=False).count()


class SpaceMembership(db.Model):
    """One user's membership in one space, with their notification
    setting for that space."""
    __tablename__ = 'space_memberships'
    __table_args__ = (
        db.UniqueConstraint(
            'user_id', 'space_id', name='uq_space_membership_user_space'
        ),
    )

    NOTIFICATION_LEVELS = ['all', 'mentions', 'muted']
    NOTIFICATION_LEVEL_LABELS = {
        'all': _l('All posts'),
        'mentions': _l('Mentions only'),
        'muted': _l('Muted'),
    }

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    space_id = db.Column(db.String(36), db.ForeignKey('spaces.id'), nullable=False, index=True)
    notification_level = db.Column(db.String(10), nullable=False, default='all')
    joined_at = db.Column(db.DateTime, default=utc_now)

    user = db.relationship('User', backref='space_memberships')

    def __repr__(self):
        return f'<SpaceMembership {self.user_id} in {self.space_id} ({self.notification_level})>'


# Seeded spaces (Phase 2): one by trade, one by city, one by interest.
# Seeded by the `flask seed-spaces` CLI command (app.py) — idempotent,
# safe to re-run. Slugs are stable: they are the space URLs.
SEEDED_SPACES = [
    {
        'slug': 'tech',
        'name': 'Tech & Software',
        'description': (
            'Developers, designers, and IT folks — hiring, advice, '
            'and war stories from the build side of the network.'
        ),
        'icon': 'layers',
    },
    {
        'slug': 'trades',
        'name': 'Skilled Trades',
        'description': (
            'Construction, electrical, plumbing, and everything built '
            'by hand — needs, referrals, and shop talk.'
        ),
        'icon': 'hard-hat',
    },
    {
        'slug': 'montreal',
        'name': 'Montréal & Laval',
        'description': (
            'Local hiring and referrals around Montréal and Laval — '
            'the neighbourhood the network started in.'
        ),
        'icon': 'map-pin',
    },
]


def seed_spaces():
    """Create the seeded spaces if they don't exist yet. Idempotent —
    re-running it only adds spaces whose slug is missing. Does not
    commit — the caller commits. Returns the spaces created."""
    created = []
    for spec in SEEDED_SPACES:
        if not Space.query.filter_by(slug=spec['slug']).first():
            space = Space(
                slug=spec['slug'],
                name=spec['name'],
                description=spec['description'],
                icon=spec['icon'],
            )
            db.session.add(space)
            created.append(space)
    return created
