"""Enforce one active employer pledge per user at the DB level.

Revision ID: pledge_one_active
Revises: drop_recruitment_only_check
Create Date: 2026-11-24

The one-active-pledge rule (no new pledge while another is pending
or approved) was enforced only by a route-level check
(EmployerPledge.active_for_user) in blueprints/pledge.py. Two
concurrent POSTs could both pass the check and insert two pending
pledges for the same user.

A partial unique index on (user_id) WHERE status IN ('pending',
'approved') makes the rule race-proof. Revoked pledges are not
covered, so re-pledging after a revocation still works. The
employer_pledges table was created 2026-11-24 and holds no rows in
production yet, so the index builds on an empty table.
"""

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = 'pledge_one_active'
down_revision = 'drop_recruitment_only_check'
branch_labels = None
depends_on = None

_INDEX = 'ix_employer_pledges_one_active'
_WHERE = "status IN ('pending', 'approved')"


def upgrade():
    op.create_index(
        _INDEX,
        'employer_pledges',
        ['user_id'],
        unique=True,
        postgresql_where=text(_WHERE),
        sqlite_where=text(_WHERE),
    )


def downgrade():
    op.drop_index(_INDEX, table_name='employer_pledges')
