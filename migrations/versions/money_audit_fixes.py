"""Money audit fixes (2026-11-24): duplicate-proof payouts/matches
and webhook event deduplication.

Revision ID: money_audit_fixes
Revises: pledge_one_active
Create Date: 2026-11-24

Three changes, all about money moving twice:

1. Unique constraint on matches.pitch_id — two simultaneous
   confirmations of one pitch must not create two matches: each
   match releases the same escrow, so a duplicate would pay the
   referrer twice. pitch_id is nullable; NULLs stay distinct under
   a unique constraint in both Postgres and SQLite.

2. Unique constraint on payouts (match_id, kind) — two simultaneous
   escrow releases must not create two 'referral' rows for one
   match: each row gets its own Stripe transfer.

3. New stripe_event_logs table — Stripe redelivers webhook events,
   and not every handler is idempotent on its own: a redelivered
   upgrade-EXTENSION event re-extends the upgrade a second time for
   one payment, and a redelivered bounty checkout re-notifies the
   poster. The webhook records each event id (unique) before
   dispatching and skips duplicates.

The live database holds no matches or payouts yet (5 users, 0
needs), so both constraints build on empty tables.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'money_audit_fixes'
down_revision = 'pledge_one_active'
branch_labels = None
depends_on = None


def upgrade():
    # Constraint changes go through batch mode: SQLite cannot
    # ALTER TABLE ... ADD CONSTRAINT, and the migration suite runs
    # the chain on SQLite.
    with op.batch_alter_table('matches', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_matches_pitch_id', ['pitch_id']
        )
    with op.batch_alter_table('payouts', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_payouts_match_kind', ['match_id', 'kind']
        )
    op.create_table(
        'stripe_event_logs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('event_id', sa.String(length=100), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id'),
    )
    op.create_index(
        'ix_stripe_event_logs_event_id',
        'stripe_event_logs', ['event_id'], unique=False,
    )


def downgrade():
    op.drop_index(
        'ix_stripe_event_logs_event_id', table_name='stripe_event_logs'
    )
    op.drop_table('stripe_event_logs')
    with op.batch_alter_table('payouts', schema=None) as batch_op:
        batch_op.drop_constraint(
            'uq_payouts_match_kind', type_='unique'
        )
    with op.batch_alter_table('matches', schema=None) as batch_op:
        batch_op.drop_constraint('uq_matches_pitch_id', type_='unique')
