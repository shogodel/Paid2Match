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

    # Paid communities: a space can only be switched to paid when it
    # has an owner (set by an admin). The owner receives the
    # member-funded share of each monthly payment (see
    # utils/paid_spaces.py); the platform keeps the configured fee.
    owner_id = db.Column(
        db.String(36), db.ForeignKey('users.id'), nullable=True, index=True
    )
    is_paid = db.Column(db.Boolean, nullable=False, default=False)
    # Monthly price in cents (None when free). Currency is CAD for
    # now — the column exists so a future multi-currency step does
    # not need another migration; checkout always charges CAD.
    price_cents = db.Column(db.Integer, nullable=True)
    currency = db.Column(db.String(3), nullable=False, default='cad')

    owner = db.relationship('User', foreign_keys=[owner_id])

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

    @property
    def price_display(self):
        """Monthly price as a dollars string ('9.99'), or None when
        the space is free / has no price set."""
        if not self.is_paid or not self.price_cents:
            return None
        return f'{self.price_cents / 100:.2f}'

    def can_view(self, user):
        """Whether a user may see this space's posts: free spaces are
        open to everyone; a paid space is visible to its active
        members, its owner, and admins only. ``user`` may be None
        (anonymous) or a User."""
        if not self.is_paid:
            return True
        if user is None:
            return False
        if getattr(user, 'role', None) == 'admin':
            return True
        if self.owner_id and user.id == self.owner_id:
            return True
        membership = SpaceMembership.query.filter_by(
            user_id=user.id, space_id=self.id,
        ).first()
        return bool(membership and membership.is_active)


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

    # Paid communities: a paid membership is a Stripe subscription.
    # Free memberships leave these NULL (status None == free/active).
    # Lifecycle is driven by webhooks, not a cron: cancel-at-period-
    # end keeps status 'active' until the period ends, then
    # customer.subscription.deleted flips it to 'canceled'.
    stripe_subscription_id = db.Column(
        db.String(100), nullable=True, index=True
    )
    status = db.Column(db.String(20), nullable=True)
    current_period_end = db.Column(db.DateTime, nullable=True)
    cancel_at_period_end = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship('User', backref='space_memberships')

    @property
    def is_active(self):
        """Whether this membership currently grants access: free
        memberships (no subscription) are always active; a paid one
        needs status 'active' and an unexpired period."""
        if self.stripe_subscription_id is None:
            return True
        if self.status != 'active':
            return False
        if self.current_period_end is None:
            return True
        end = self.current_period_end
        if end.tzinfo is None:
            end = end.replace(tzinfo=utc_now().tzinfo)
        return utc_now() < end

    @property
    def is_paid(self):
        """Whether this membership is a paid (subscription) one."""
        return self.stripe_subscription_id is not None

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
