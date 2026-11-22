"""Follow graph (M1 social core): one-way follow, like X/Instagram (§1.2)."""
from extensions import db
from models import generate_uuid, utc_now


class Follow(db.Model):
    """follower_id follows followed_id. No self-follows (enforced in the
    route + helper, not the DB — a CHECK would differ across engines)."""
    __tablename__ = 'follows'
    __table_args__ = (
        db.UniqueConstraint('follower_id', 'followed_id', name='uq_follow_pair'),
    )

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    follower_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    followed_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    def __repr__(self):
        return f'<Follow {self.follower_id} -> {self.followed_id}>'
