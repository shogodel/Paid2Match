"""Track when the founding-referrer flag was granted (2026-11-25).

Revision ID: founding_window
Revises: message_read_at
Create Date: 2026-11-25

One change, for the money audit (2026-11-25):

1. New nullable `founding_referrer_since` column on users — the
   moment the admin granted the founding-referrer flag. The 15%
   fee and free Pro run for 12 months from this date (see
   utils.payments.founding_benefit_active); before this column the
   window was enforced only by the admin remembering to remove the
   flag, so the benefits effectively lasted forever.

No backfill: a flag set before this column has no recorded grant
date, and guessing one (e.g. the account creation date) could
wrongly expire someone flagged recently. NULL means grandfathered
— the benefit stays active, exactly as it behaved before.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'founding_window'
down_revision = 'message_read_at'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'users',
        sa.Column('founding_referrer_since', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_column('users', 'founding_referrer_since')
