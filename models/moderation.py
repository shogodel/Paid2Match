"""Moderation v0 (M1 social core, §7.1): reports, block/mute.

Reports go to the founder review queue in the admin panel. A report that
is upheld (status 'actioned') costs the content's author 10 trust-score
points (§3.1) — the trust computation reads this table.
"""
from extensions import db
from models import generate_uuid, utc_now


# Moderation v1 (M3): an open report unreviewed for 24 hours is
# overdue — the admin queue flags it and sorts it first.
REVIEW_SLA_HOURS = 24


class Report(db.Model):
    """A user report against a post, comment, or profile."""
    __tablename__ = 'reports'

    TARGET_TYPES = ['post', 'comment', 'profile']
    REASONS = ['spam', 'harassment', 'fake', 'scam', 'other']
    STATUSES = ['open', 'dismissed', 'actioned']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    reporter_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    target_type = db.Column(db.String(20), nullable=False)
    target_id = db.Column(db.String(36), nullable=False, index=True)
    reason = db.Column(db.String(20), nullable=False)
    details = db.Column(db.Text, nullable=True)
    # Flag weight (§4.2): reporters with 2,500+ karma count double.
    weight = db.Column(db.Integer, default=1)
    status = db.Column(db.String(20), nullable=False, default='open')
    reviewed_by_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    reporter = db.relationship('User', foreign_keys=[reporter_id])
    reviewed_by = db.relationship('User', foreign_keys=[reviewed_by_id])

    def __repr__(self):
        return f'<Report {self.target_type}:{self.target_id} {self.reason}>'

    @property
    def is_overdue(self):
        """An open report older than the 24-hour review SLA."""
        if self.status != 'open' or self.created_at is None:
            return False
        from datetime import timedelta
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=utc_now().tzinfo)
        return utc_now() >= created + timedelta(hours=REVIEW_SLA_HOURS)

    @property
    def target_author_id(self):
        """The user id of the reported content's author (None if gone)."""
        from models.post import Post, Comment
        if self.target_type == 'post':
            post = db.session.get(Post, self.target_id)
            return post.author_id if post else None
        if self.target_type == 'comment':
            comment = db.session.get(Comment, self.target_id)
            return comment.author_id if comment else None
        if self.target_type == 'profile':
            return self.target_id
        return None


class Block(db.Model):
    """Block or mute another user. Block: their posts/comments disappear
    from your feeds and they cannot follow or message you. Mute: their
    posts disappear from your feeds only."""
    __tablename__ = 'blocks'
    __table_args__ = (
        db.UniqueConstraint('blocker_id', 'blocked_id', 'kind', name='uq_block_pair_kind'),
    )

    KINDS = ['block', 'mute']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    blocker_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    blocked_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now)

    def __repr__(self):
        return f'<Block {self.blocker_id} {self.kind}s {self.blocked_id}>'
