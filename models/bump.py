"""Bump/boost models (M3, §1.3): paid visibility for feed posts.

A bump refreshes a post's effective timestamp for 24 hours (it returns
to the top of the chronological Following feed) and is always labeled
"Bumped". A boost ($5/day) gives a post extra Discover visibility and
is labeled "Boosted". Both snapshot the post's counters when they
start, so bump/boost performance is measurable (views/likes/comments
gained while active).

Pricing (CAD): single bump $2, 5-pack $8, 10-pack $15, recurring bump
$5/month (auto-bump every 3 days), boost $5/day. One free bump is
banked per completed referral (Profile.free_bumps, M1) — free bumps
are spent before paid ones.
"""
from extensions import db
from models import generate_uuid, utc_now

# Bump duration and pricing (CAD cents).
BUMP_DURATION_HOURS = 24
BUMP_SINGLE_PRICE_CENTS = 200
BUMP_PACKS = {5: 800, 10: 1500}  # pack size -> price in cents
RECURRING_BUMP_PRICE_CENTS = 500  # per month
BOOST_PRICE_PER_DAY_CENTS = 500
BOOST_MAX_DAYS = 7

# Limits (§1.3): 1 bump per post per 24h, 3 bumps per user per day, a
# post must be 24h old before it can be bumped, Member+ only.
BUMP_PER_POST_HOURS = 24
BUMP_PER_USER_PER_DAY = 3
BUMP_MIN_POST_AGE_HOURS = 24
# Bumped posts are capped at 1 in 5 in the Following feed.
BUMP_FEED_CAP_DENOMINATOR = 5
# Discover lift for a boosted post (score points — comparable to a
# strong engagement day; published in the Discover rules).
BOOST_DISCOVER_LIFT = 50


class Bump(db.Model):
    """One bump applied to one post. Active while now < expires_at."""
    __tablename__ = 'bumps'

    SOURCES = ['free', 'pack', 'paid', 'recurring']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    post_id = db.Column(db.String(36), db.ForeignKey('posts.id'), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    source = db.Column(db.String(20), nullable=False)
    # Stripe Checkout session that paid for this bump (paid bumps only).
    stripe_session_id = db.Column(db.String(200), nullable=True)
    price_cents = db.Column(db.Integer, nullable=True)
    # Counter snapshots at bump time — performance is current minus snapshot.
    views_at_bump = db.Column(db.Integer, default=0)
    likes_at_bump = db.Column(db.Integer, default=0)
    comments_at_bump = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utc_now)
    expires_at = db.Column(db.DateTime, nullable=False)

    post = db.relationship('Post', backref='bumps')
    user = db.relationship('User', backref='bumps')

    def __repr__(self):
        return f'<Bump {self.post_id} {self.source} until {self.expires_at}>'

    @property
    def is_active(self):
        if self.expires_at is None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=utc_now().tzinfo)
        return utc_now() < expires

    @property
    def performance(self):
        """Views/likes/comments gained since this bump started."""
        from models.post import Post
        post = db.session.get(Post, self.post_id)
        if post is None:
            return {'views': 0, 'likes': 0, 'comments': 0}
        return {
            'views': max((post.view_count or 0) - (self.views_at_bump or 0), 0),
            'likes': max(post.like_count - (self.likes_at_bump or 0), 0),
            'comments': max(post.comment_count - (self.comments_at_bump or 0), 0),
        }


class BumpPackPurchase(db.Model):
    """A purchased bump pack (5/$8 or 10/$15). Bumps are drawn from the
    oldest pack with remaining bumps first."""
    __tablename__ = 'bump_pack_purchases'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    pack_size = db.Column(db.Integer, nullable=False)
    price_cents = db.Column(db.Integer, nullable=False)
    stripe_session_id = db.Column(db.String(200), nullable=False, unique=True)
    bumps_remaining = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    user = db.relationship('User', backref='bump_packs')

    def __repr__(self):
        return f'<BumpPack {self.pack_size} for {self.user_id} ({self.bumps_remaining} left)>'


class Boost(db.Model):
    """A paid boost: extra Discover visibility for N days at $5/day.
    Active while now is between starts_at and ends_at."""
    __tablename__ = 'boosts'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    post_id = db.Column(db.String(36), db.ForeignKey('posts.id'), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    days = db.Column(db.Integer, nullable=False)
    price_cents = db.Column(db.Integer, nullable=False)
    stripe_session_id = db.Column(db.String(200), nullable=False, unique=True)
    views_at_boost = db.Column(db.Integer, default=0)
    likes_at_boost = db.Column(db.Integer, default=0)
    comments_at_boost = db.Column(db.Integer, default=0)
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    post = db.relationship('Post', backref='boosts')
    user = db.relationship('User', backref='boosts')

    def __repr__(self):
        return f'<Boost {self.post_id} {self.days}d until {self.ends_at}>'

    @property
    def is_active(self):
        now = utc_now()
        starts = self.starts_at
        ends = self.ends_at
        if starts is not None and starts.tzinfo is None:
            starts = starts.replace(tzinfo=now.tzinfo)
        if ends is not None and ends.tzinfo is None:
            ends = ends.replace(tzinfo=now.tzinfo)
        return starts <= now < ends if starts and ends else False

    @property
    def performance(self):
        """Views/likes/comments gained since this boost started."""
        from models.post import Post
        post = db.session.get(Post, self.post_id)
        if post is None:
            return {'views': 0, 'likes': 0, 'comments': 0}
        return {
            'views': max((post.view_count or 0) - (self.views_at_boost or 0), 0),
            'likes': max(post.like_count - (self.likes_at_boost or 0), 0),
            'comments': max(post.comment_count - (self.comments_at_boost or 0), 0),
        }


class RecurringBump(db.Model):
    """A $5/month recurring bump for one post: the post is auto-bumped
    every 3 days while the Stripe subscription is active. The runner
    (utils/bumps.py run_recurring_bumps) applies due bumps — it is
    invoked by the `flask run-recurring-bumps` CLI command (a cron
    hook on the server) and by an admin button."""
    __tablename__ = 'recurring_bumps'

    STATUSES = ['active', 'canceled', 'past_due']
    # Kijiji's recurring model: re-bump every 3 days.
    INTERVAL_DAYS = 3

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    post_id = db.Column(db.String(36), db.ForeignKey('posts.id'), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    stripe_subscription_id = db.Column(db.String(200), nullable=True, unique=True)
    status = db.Column(db.String(20), nullable=False, default='active')
    # Canceled at period end: bumps keep running until the paid month ends.
    cancel_at_period_end = db.Column(db.Boolean, default=False)
    last_bumped_at = db.Column(db.DateTime, nullable=True)
    next_bump_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    post = db.relationship('Post', backref='recurring_bumps')
    user = db.relationship('User', backref='recurring_bumps')

    def __repr__(self):
        return f'<RecurringBump {self.post_id} {self.status} next {self.next_bump_at}>'
