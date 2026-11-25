"""Social models: posts, likes, comments, shares (M1 social core)."""
from flask_babel import lazy_gettext as _l

from extensions import db
from models import generate_uuid, utc_now


class Post(db.Model):
    """A feed post. Seven kinds exist in the UI (§1.1): six stored post
    types here, plus shares, which live in the shares table and render as
    attributed reshare cards in the feed."""
    __tablename__ = 'posts'

    # Phase 2 post-type taxonomy (flair): Discussion / Win / Ask /
    # Referral Need / Available, plus Intro (#intros). Stored types map
    # onto the taxonomy: 'update' is a Discussion, both need types are
    # a Referral Need, 'available' is new (someone open to work).
    POST_TYPES = [
        'need_paid', 'need_free', 'update', 'win', 'ask', 'intro',
        'available',
    ]
    # Labels are lazy_gettext (aliased _l) so the card chips translate;
    # pybabel extracts the _l keyword (see babel.cfg keywords).
    POST_TYPE_LABELS = {
        'need_paid': _l('Referral need'),
        'need_free': _l('Referral need'),
        'update': _l('Discussion'),
        'win': _l('Win'),
        'ask': _l('Ask'),
        'intro': _l('Intro'),
        'available': _l('Available'),
    }
    # Flair filter values (feed ?flair=...) -> stored post types.
    FLAIR_TYPES = {
        'discussion': ['update'],
        'win': ['win'],
        'ask': ['ask'],
        'need': ['need_paid', 'need_free'],
        'available': ['available'],
        'intro': ['intro'],
    }
    FLAIR_LABELS = {
        'discussion': _l('Discussion'),
        'win': _l('Win'),
        'ask': _l('Ask'),
        'need': _l('Referral Need'),
        'available': _l('Available'),
        'intro': _l('Intro'),
    }

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    author_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    post_type = db.Column(db.String(20), nullable=False, default='update')
    body = db.Column(db.Text, nullable=False)
    photo_url = db.Column(db.String(500), nullable=True)
    # Set when the post is (or becomes) a referral need.
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=True, index=True)
    # Phase 2: the topic space this post belongs to (NULL = the
    # general feed). Space posts still appear in the main feeds.
    space_id = db.Column(db.String(36), db.ForeignKey('spaces.id'), nullable=True, index=True)
    # Moderation: hidden posts never appear in feeds or search.
    is_hidden = db.Column(db.Boolean, default=False)
    # M3: set when the post was auto-hidden by the report threshold
    # (moderation v1) — distinguishes an auto-hide (restorable when
    # the reports are dismissed) from an admin hide.
    auto_hidden_at = db.Column(db.DateTime, nullable=True)
    # M3 bump/boost (§1.3): a bump refreshes the effective timestamp
    # for 24h; a boost adds Discover lift. The Bump/Boost rows are the
    # ledger; these columns keep feed queries cheap.
    bumped_at = db.Column(db.DateTime, nullable=True)
    bumped_until = db.Column(db.DateTime, nullable=True)
    boosted_until = db.Column(db.DateTime, nullable=True)
    view_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    bounty = db.relationship('Bounty', backref='linked_posts')
    likes = db.relationship('Like', backref='post', lazy='dynamic', cascade='all, delete-orphan')
    comments = db.relationship('Comment', backref='post', lazy='dynamic', cascade='all, delete-orphan')
    shares = db.relationship('Share', backref='post', lazy='dynamic', cascade='all, delete-orphan')
    # Views are derived data — they go with the post (without this,
    # deleting a viewed post fails on Postgres: post_views.post_id
    # has no ON DELETE CASCADE, and on SQLite it leaves orphans).
    post_views = db.relationship('PostView', backref='post', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Post {self.id} {self.post_type} by {self.author_id}>'

    @property
    def is_need(self):
        return self.post_type in ('need_paid', 'need_free')

    def _as_aware(self, dt):
        if dt is None:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=utc_now().tzinfo)

    @property
    def is_bumped(self):
        """An active bump (within its 24h window)."""
        until = self._as_aware(self.bumped_until)
        return bool(self.bumped_at is not None and until and utc_now() < until)

    @property
    def is_boosted(self):
        """An active boost."""
        until = self._as_aware(self.boosted_until)
        return bool(until and utc_now() < until)

    @property
    def effective_at(self):
        """The timestamp the feed sorts and scores on: the bump time
        while a bump is active, otherwise the original post time. The
        original date is never hidden — the card shows both."""
        if self.is_bumped:
            return self._as_aware(self.bumped_at)
        return self._as_aware(self.created_at)

    @property
    def type_label(self):
        return self.POST_TYPE_LABELS.get(self.post_type, self.post_type)

    @property
    def flair(self):
        """The taxonomy flair key for this post's stored type."""
        for flair, types in self.FLAIR_TYPES.items():
            if self.post_type in types:
                return flair
        return 'discussion'

    @property
    def like_count(self):
        return self.likes.count()

    @property
    def comment_count(self):
        return self.comments.filter_by(is_hidden=False).count()

    @property
    def share_count(self):
        return self.shares.count()

    def to_dict(self):
        return {
            'id': self.id,
            'author_id': self.author_id,
            'post_type': self.post_type,
            'body': self.body,
            'photo_url': self.photo_url,
            'bounty_id': self.bounty_id,
            'is_hidden': self.is_hidden,
            'view_count': self.view_count,
            'like_count': self.like_count,
            'comment_count': self.comment_count,
            'share_count': self.share_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class Like(db.Model):
    """One tap, no downvote (§1.2). A user can like a post once."""
    __tablename__ = 'post_likes'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'post_id', name='uq_like_user_post'),
    )

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    post_id = db.Column(db.String(36), db.ForeignKey('posts.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    def __repr__(self):
        return f'<Like {self.user_id} -> {self.post_id}>'


class Comment(db.Model):
    """Threaded comments, one level deep at launch (§1.2). A reply has
    parent_id set; top-level comments have parent_id NULL."""
    __tablename__ = 'comments'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    author_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    post_id = db.Column(db.String(36), db.ForeignKey('posts.id'), nullable=False, index=True)
    parent_id = db.Column(db.String(36), db.ForeignKey('comments.id'), nullable=True, index=True)
    body = db.Column(db.Text, nullable=False)
    is_hidden = db.Column(db.Boolean, default=False)
    # M3: set when auto-hidden by the report threshold (see Post).
    auto_hidden_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    author = db.relationship('User', backref='comments')
    replies = db.relationship(
        'Comment',
        backref=db.backref('parent', remote_side=[id]),
        lazy='dynamic',
        cascade='all, delete-orphan',
    )

    def __repr__(self):
        return f'<Comment {self.id} on {self.post_id}>'


class Share(db.Model):
    """Reshare a post to your own feed with a comment (§1.2, §4.4). The
    share is attributed to the sharer: if someone in their audience refers
    the winning person for a shared need, the share is part of the chain
    (attribution credit + karma; two-level splits are M4)."""
    __tablename__ = 'shares'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'post_id', name='uq_share_user_post'),
    )

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    post_id = db.Column(db.String(36), db.ForeignKey('posts.id'), nullable=False, index=True)
    comment = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    sharer = db.relationship('User', backref='shares')

    def __repr__(self):
        return f'<Share {self.user_id} -> {self.post_id}>'


class Bookmark(db.Model):
    """A saved post (Phase 2 — closes the Phase 1 Save deferral). One
    bookmark per user per post; the Saved view lists them newest first."""
    __tablename__ = 'bookmarks'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'post_id', name='uq_bookmark_user_post'),
    )

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    post_id = db.Column(db.String(36), db.ForeignKey('posts.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    post = db.relationship(
        'Post', backref=db.backref('bookmarks', cascade='all, delete-orphan')
    )
    user = db.relationship('User', backref='bookmarks')

    def __repr__(self):
        return f'<Bookmark {self.user_id} -> {self.post_id}>'
