"""recruitment only - constrain bounty_type

Revision ID: recruitment_only_v1
Revises: fa6ff45fcb16
Create Date: 2026-08-26 21:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'recruitment_only_v1'
down_revision = 'fa6ff45fcb16'
branch_labels = None
depends_on = None


def upgrade():
    # Backfill any existing non-recruitment bounties to 'recruitment' so the
    # platform is recruitment-only at the data layer.
    op.execute("UPDATE bounties SET bounty_type = 'recruitment' WHERE bounty_type != 'recruitment'")

    # Enforce a server-side default so new rows are always 'recruitment'.
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.alter_column(
            'bounty_type',
            existing_type=sa.String(length=30),
            nullable=False,
            server_default='recruitment',
        )


def downgrade():
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.alter_column(
            'bounty_type',
            existing_type=sa.String(length=30),
            nullable=False,
            server_default=None,
        )
