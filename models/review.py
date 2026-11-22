"""Transaction-tied reviews (M2, §3.3): double-blind, two dimensions.

You can only review someone you completed a referral with (as referrer
or poster). Neither side sees the other's review until both have
submitted or 14 days have passed since the first review — the reveal
is computed lazily (no cron needed): a review is visible when its
sibling exists, when it was explicitly revealed, or when the 14-day
window has elapsed. Only revealed reviews feed the trust score.
"""
from datetime import timedelta

from sqlalchemy import func

from extensions import db
from models import generate_uuid, utc_now


class Review(db.Model):
    """One side's review of the other after a completed referral."""
    __tablename__ = 'reviews'
    __table_args__ = (
        db.UniqueConstraint(
            'match_id', 'reviewer_id', name='uq_review_match_reviewer'
        ),
    )

    REVIEWEE_ROLES = ['referrer', 'poster']
    # Two dimensions, not one star (§3.3). Referrers are rated on
    # referral quality + communication; posters on communication/speed +
    # payment reliability.
    DIMENSION_LABELS = {
        'referrer': ('Referral quality', 'Communication'),
        'poster': ('Communication & speed', 'Payment reliability'),
    }
    REVEAL_DAYS = 14

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    match_id = db.Column(db.String(36), db.ForeignKey('matches.id'), nullable=False, index=True)
    reviewer_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    reviewee_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    # Which side is being reviewed: 'referrer' (the hunter) or 'poster'.
    reviewee_role = db.Column(db.String(10), nullable=False)
    rating_primary = db.Column(db.Integer, nullable=False)
    rating_secondary = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text, nullable=True)
    # Set when both sides have submitted (see maybe_reveal_reviews).
    revealed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    match = db.relationship('Match', backref='reviews')
    reviewer = db.relationship('User', foreign_keys=[reviewer_id])
    reviewee = db.relationship('User', foreign_keys=[reviewee_id])

    def __repr__(self):
        return f'<Review {self.reviewer_id} -> {self.reviewee_id} ({self.match_id})>'

    @property
    def average(self):
        """Mean of the two dimensions (1–5)."""
        return (self.rating_primary + self.rating_secondary) / 2

    @property
    def dimension_labels(self):
        return self.DIMENSION_LABELS.get(
            self.reviewee_role, ('Rating', 'Rating')
        )

    def is_revealed(self):
        """True when this review may be shown: explicitly revealed, the
        other side has submitted, or 14 days elapsed since the first
        review for this match."""
        if self.revealed_at is not None:
            return True
        sibling = Review.query.filter_by(match_id=self.match_id).filter(
            Review.id != self.id
        ).first()
        if sibling is not None:
            return True
        earliest = db.session.query(func.min(Review.created_at)).filter_by(
            match_id=self.match_id
        ).scalar()
        if earliest is None:
            return False
        if earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=utc_now().tzinfo)
        return utc_now() >= earliest + timedelta(days=self.REVEAL_DAYS)

    def to_dict(self):
        return {
            'id': self.id,
            'match_id': self.match_id,
            'reviewer_id': self.reviewer_id,
            'reviewee_id': self.reviewee_id,
            'reviewee_role': self.reviewee_role,
            'rating_primary': self.rating_primary,
            'rating_secondary': self.rating_secondary,
            'comment': self.comment,
            'revealed': self.is_revealed(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


def maybe_reveal_reviews(match_id):
    """If both sides have now reviewed a match, mark both reviews
    revealed and return True. Does not commit — the caller commits."""
    reviews = Review.query.filter_by(match_id=match_id).all()
    if len(reviews) < 2:
        return False
    now = utc_now()
    revealed_any = False
    for review in reviews:
        if review.revealed_at is None:
            review.revealed_at = now
            revealed_any = True
    return revealed_any


def revealed_reviews_for_user(user_id):
    """Reviews received by this user that are visible (for the trust
    score and the profile page)."""
    return [r for r in Review.query.filter_by(reviewee_id=user_id).all()
            if r.is_revealed()]
