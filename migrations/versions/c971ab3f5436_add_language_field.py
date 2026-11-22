"""Add language field

Revision ID: c971ab3f5436
Revises: add_payer_type_and_agreements
Create Date: 2026-04-26 00:30:52.665194

M2 NOTE: this migration is a no-op. Its original body created foreign
keys (bounties.third_party_payer_id -> users, and the three
bounty_payment_agreements FKs) plus the bounty_id index — but all of
those already exist: add_payer_type_and_agreements creates them inline
in its create_table / batch add_column. The original body also cannot
run on SQLite: batch-mode create_foreign_key requires a constraint
name (it passed None), and the index already exists. The revision is
kept in the chain so the live database's history is untouched; fresh
databases simply have nothing to do here.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c971ab3f5436'
down_revision = 'add_payer_type_and_agreements'
branch_labels = None
depends_on = None


def upgrade():
    # No-op: the FKs and index this migration used to create already
    # exist (see module docstring).
    pass


def downgrade():
    # No-op: nothing to undo — the constraints belong to
    # add_payer_type_and_agreements, whose downgrade removes them.
    pass
