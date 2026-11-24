"""Multi-currency support (foundation): Bounty.currency.

Revision ID: currency_bounty
Revises: phase3_discovery_trust
Create Date: 2026-11-23

New column (models/bounty.py):
    bounties.currency — String(3), not null, default 'CAD'. The
    currency the reward (and staged rewards) are denominated in.
    No FX conversion anywhere: amounts are recorded and shown as
    entered, in this currency.

Existing rows are backfilled to 'CAD' via the server default, which
is then dropped — the model default owns new rows from here on.
Payout rows are untouched (they keep their lowercase 'cad').
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'currency_bounty'
down_revision = 'employer_pledge'
branch_labels = None
depends_on = None


def upgrade():
    # SQLite only allows adding a NOT NULL column with a non-null
    # default, so the backfill rides on the server default; batch
    # mode keeps this working on SQLite and PostgreSQL alike.
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'currency', sa.String(length=3), nullable=False,
            server_default='CAD',
        ))
    # Backfill is done (every existing row got 'CAD' from the server
    # default). Drop the server default so the model default owns
    # new rows — and so the column matches the model exactly.
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.alter_column(
            'currency', server_default=None,
            existing_type=sa.String(length=3),
        )


def downgrade():
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.drop_column('currency')
