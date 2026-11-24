"""Human-reviewed employer pledge register.

Revision ID: employer_pledge
Revises: phase3_discovery_trust
Create Date: 2026-11-23

New table (models/employer_pledge.py):
    employer_pledges — one row per employer pledge. An employer
    submits a pledge ('pending'); an admin approves ('approved',
    listed on the public register at /pledge) or revokes it
    ('revoked', may re-pledge). decided_at / decided_by_id record
    the admin decision.

This is separate from the M3 'pledges' table (models/endorsement.py
Pledge): that one is self-published with a no-ghosting SLA, this
one is human-reviewed by an admin.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'employer_pledge'
down_revision = 'phase3_discovery_trust'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'employer_pledges',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('org_name', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('decided_by_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['decided_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_employer_pledges_user_id', 'employer_pledges', ['user_id'],
    )


def downgrade():
    op.drop_index(
        'ix_employer_pledges_user_id', table_name='employer_pledges'
    )
    op.drop_table('employer_pledges')
