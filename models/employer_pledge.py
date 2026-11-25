"""Human-reviewed employer pledge (separate from the M3 pledge badges).

This is the HUMAN-REVIEWED employer pledge: an employer publicly
promises that no AI screens their applicants — a human reads every
application. It is NOT the M3 Pledge in models/endorsement.py (that
one is self-published, active/revoked, and carries a no-ghosting SLA
with compliance tracking). This one is human-reviewed by an admin:
a pledge starts 'pending', an admin approves or revokes it, and only
approved pledges appear on the public register at /pledge.

One active pledge per user: a new pledge is blocked while another is
pending or approved. A revoked pledge may be re-pledged (a fresh row
is created; the revoked row stays as history).
"""
from sqlalchemy import text

from extensions import db
from models import generate_uuid, utc_now


class EmployerPledge(db.Model):
    """An employer's pledge that a human reads every application."""
    __tablename__ = 'employer_pledges'

    __table_args__ = (
        # One active (pending/approved) pledge per user, enforced at
        # the DB level: the route-level active_for_user check races
        # under concurrent POSTs. A revoked pledge is not active, so
        # re-pledging after a revocation still works.
        db.Index(
            'ix_employer_pledges_one_active',
            'user_id',
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'approved')"
            ),
            sqlite_where=text("status IN ('pending', 'approved')"),
        ),
    )

    STATUSES = ['pending', 'approved', 'revoked']
    # Statuses that block a new pledge from the same user.
    ACTIVE_STATUSES = ['pending', 'approved']

    # The pledge text — fixed wording, the same for every pledger.
    # Shown on the form and the register, translated at render time
    # (templates/routes carry the literal string so pybabel extracts
    # it; this constant is the English source of truth).
    PLEDGE_TEXT = (
        'We pledge that no AI screens our applicants: '
        'a human reads every application.'
    )

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(
        db.String(36), db.ForeignKey('users.id'), nullable=False, index=True
    )
    org_name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')
    created_at = db.Column(db.DateTime, default=utc_now)
    decided_at = db.Column(db.DateTime, nullable=True)
    decided_by_id = db.Column(
        db.String(36), db.ForeignKey('users.id'), nullable=True
    )

    user = db.relationship(
        'User', foreign_keys=[user_id], backref='employer_pledges'
    )
    decided_by = db.relationship('User', foreign_keys=[decided_by_id])

    @property
    def is_approved(self):
        return self.status == 'approved'

    @classmethod
    def active_for_user(cls, user_id):
        """The user's pending or approved pledge, if any — a new
        pledge is blocked while one of these exists."""
        return cls.query.filter(
            cls.user_id == user_id,
            cls.status.in_(cls.ACTIVE_STATUSES),
        ).first()

    @classmethod
    def approved_for_user(cls, user_id):
        """The user's approved pledge, if any."""
        return cls.query.filter_by(
            user_id=user_id, status='approved'
        ).first()

    def __repr__(self):
        return f'<EmployerPledge {self.org_name} ({self.status})>'
