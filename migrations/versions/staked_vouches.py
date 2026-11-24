"""Staked vouches — trust backed by money, not just clicks.

Revision ID: staked_vouches
Revises: nice_urls
Create Date: 2026-11-23

New table (models/staked_vouch.py):
    staked_vouches — one row per stake. A voucher puts a recorded
    amount behind a candidate, optionally tied to a need
    (bounty_id NULL for a general stake placed from the
    candidate's profile). status is 'active' until the need's
    poster (or an admin) resolves it: 'released' (candidate
    completed / hired), 'forfeited' (candidate flaked / no-show),
    'returned' (need cancelled). Resolution records resolved_at +
    resolved_by_id; the row is never deleted.

    The one-active-stake-per-(voucher, candidate, bounty) rule is
    enforced by the model (StakedVouch.create), not by a database
    constraint: a partial unique index cannot cover the NULL
    bounty case (NULL never equals NULL in a unique index), so the
    check lives in application code.

    RECORD ONLY: there is no balance to debit and no Stripe calls
    — a stake is a recorded commitment against the voucher's
    future referral earnings (same record-only pattern as
    stage_reward_events). Statuses are statuses, not money
    movements.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'staked_vouches'
down_revision = 'intro_bounties'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'staked_vouches',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('voucher_id', sa.String(length=36), nullable=False),
        sa.Column('candidate_id', sa.String(length=36), nullable=False),
        sa.Column('bounty_id', sa.String(length=36), nullable=True),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_by_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['voucher_id'], ['users.id']),
        sa.ForeignKeyConstraint(['candidate_id'], ['users.id']),
        sa.ForeignKeyConstraint(['bounty_id'], ['bounties.id']),
        sa.ForeignKeyConstraint(['resolved_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_staked_vouches_voucher_id', 'staked_vouches', ['voucher_id'],
    )
    op.create_index(
        'ix_staked_vouches_candidate_id', 'staked_vouches',
        ['candidate_id'],
    )
    op.create_index(
        'ix_staked_vouches_bounty_id', 'staked_vouches', ['bounty_id'],
    )


def downgrade():
    op.drop_index(
        'ix_staked_vouches_bounty_id', table_name='staked_vouches')
    op.drop_index(
        'ix_staked_vouches_candidate_id', table_name='staked_vouches')
    op.drop_index(
        'ix_staked_vouches_voucher_id', table_name='staked_vouches')
    op.drop_table('staked_vouches')
