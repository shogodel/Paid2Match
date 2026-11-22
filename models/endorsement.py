"""Endorsements + the human-reviewed pledge register (M3, §3).

Endorsements (§3.3): any user with 250+ karma can endorse another for
a skill ("great with trades hiring"). Endorsements from Trusted+
users weigh double in the trust score's community component — and each
user can give at most 5 endorsements per month, so they can't be
farmed in bulk.

Pledge register (§3.4 + the human-first research): an employer pledges
human-reviewed hiring — no AI screening, and a human response to
every referral within 5 business days. The register is public; each
pledger shows their no-ghosting compliance, and the platform publishes
its platform-wide compliance rate. A pledger with an open SLA breach
shows as suspended on the register (computed, not stored — the breach
is the fact). Revocation is an admin action.
"""
from extensions import db
from models import generate_uuid, utc_now

# Endorsement rules (§3.3): 250 karma to give one (KARMA_ENDORSE in
# utils/karma.py), 5 per endorser per calendar month, Trusted+
# endorsements weigh double in the trust score.
ENDORSEMENTS_PER_MONTH = 5
ENDORSEMENT_TRUSTED_WEIGHT = 2
ENDORSEMENT_BASE_WEIGHT = 1
# Community points from endorsements, capped (§3.1: endorsements = 5).
ENDORSEMENT_MAX_POINTS = 5


class Endorsement(db.Model):
    """One user endorsing another for a skill."""
    __tablename__ = 'endorsements'
    __table_args__ = (
        db.UniqueConstraint(
            'endorser_id', 'endorsee_id', 'skill',
            name='uq_endorsement_pair_skill',
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    endorser_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    endorsee_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    skill = db.Column(db.String(100), nullable=False)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    endorser = db.relationship('User', foreign_keys=[endorser_id])
    endorsee = db.relationship('User', foreign_keys=[endorsee_id])

    def __repr__(self):
        return f'<Endorsement {self.endorser_id} -> {self.endorsee_id} ({self.skill})>'


class Pledge(db.Model):
    """An employer's human-reviewed hiring pledge. One pledge per user."""
    __tablename__ = 'pledges'

    STATUSES = ['active', 'revoked']

    # The pledge text — fixed, the same for every pledger.
    PLEDGE_TEXT = (
        'We pledge human-reviewed hiring: no AI screening of candidates, '
        'and a human response to every referral within 5 business days.'
    )

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, unique=True, index=True)
    company_name = db.Column(db.String(200), nullable=False)
    website = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='active')
    created_at = db.Column(db.DateTime, default=utc_now)
    revoked_at = db.Column(db.DateTime, nullable=True)
    revoke_reason = db.Column(db.Text, nullable=True)

    user = db.relationship('User', backref='pledge')

    def __repr__(self):
        return f'<Pledge {self.company_name} ({self.status})>'
