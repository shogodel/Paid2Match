"""Free referrals + karma (M1 social core, §4).

A Referral is a timestamped who-referred-whom record. Attribution is
first-timestamp-wins, enforced by the unique (bounty_id, referred_email)
constraint: the first person to refer someone for a need owns that
attribution; later attempts for the same person + need are rejected.

Flow: referrer creates the referral (status 'invited') -> the referred
person gets an invite link and consents (status 'consented'; a Pitch is
created so the existing pitch/match machinery takes over) -> poster
reviews the pitch -> dual confirmation completes the match -> the
referral is 'completed' and karma + helped count are awarded.
"""
import secrets

from extensions import db
from models import generate_uuid, utc_now


def _invite_token():
    return secrets.token_urlsafe(32)


class Referral(db.Model):
    """A referral of one person for one need."""
    __tablename__ = 'referrals'
    __table_args__ = (
        db.UniqueConstraint(
            'bounty_id', 'referred_email', name='uq_referral_bounty_person'
        ),
    )

    STATUSES = ['invited', 'consented', 'declined', 'completed', 'cancelled']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    referrer_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=False, index=True)
    # Set when the referred person consents: the Pitch the poster reviews.
    pitch_id = db.Column(db.String(36), db.ForeignKey('pitches.id'), nullable=True, index=True)
    # M4: the Share this referral came through — copied onto the Pitch
    # at consent, which is what makes the two-level payout split
    # possible. NULL for referrals that did not come through a share.
    via_share_id = db.Column(db.String(36), db.ForeignKey('shares.id'), nullable=True, index=True)
    referred_name = db.Column(db.String(120), nullable=False)
    referred_email = db.Column(db.String(120), nullable=False, index=True)
    # Set when the referred person confirms (they may not have an account yet).
    referred_user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True, index=True)
    # Why they fit (50–1000 chars); becomes the pitch teaser on consent.
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='invited')
    invite_token = db.Column(db.String(64), nullable=False, unique=True, default=_invite_token)
    invited_at = db.Column(db.DateTime, default=utc_now)
    consented_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    bounty = db.relationship('Bounty', backref='referrals')
    pitch = db.relationship('Pitch', backref='referral', uselist=False)
    referred_user = db.relationship('User', foreign_keys=[referred_user_id])
    via_share = db.relationship('Share')

    def __repr__(self):
        return f'<Referral {self.referrer_id} -> {self.referred_email} for {self.bounty_id}>'

    @property
    def is_free(self):
        """True when the need carries no bounty (community referral)."""
        return self.bounty.is_free_bounty if self.bounty else True

    def to_dict(self):
        return {
            'id': self.id,
            'referrer_id': self.referrer_id,
            'bounty_id': self.bounty_id,
            'pitch_id': self.pitch_id,
            'via_share_id': self.via_share_id,
            'referred_name': self.referred_name,
            'referred_email': self.referred_email,
            'referred_user_id': self.referred_user_id,
            'status': self.status,
            'invited_at': self.invited_at.isoformat() if self.invited_at else None,
            'consented_at': self.consented_at.isoformat() if self.consented_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }


class KarmaEvent(db.Model):
    """One karma change. Karma is a non-monetary reputation currency
    (§4.2): +50 per completed referral, +10 per like received on your
    posts/comments, +10 when a share you made leads to a hire. Karma is
    computed over a rolling 12-month window — Profile.karma is a cached
    sum of the last 12 months of events, recomputed on every award."""
    __tablename__ = 'karma_events'

    REASONS = ['referral_completed', 'like_received', 'share_led_to_hire']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(30), nullable=False)
    ref_type = db.Column(db.String(30), nullable=True)
    ref_id = db.Column(db.String(36), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    def __repr__(self):
        return f'<KarmaEvent {self.user_id} {self.amount:+d} {self.reason}>'
