"""Warm-intro bounties: pay for an introduction, not just a hire.

Revision ID: intro_bounties
Revises: nice_urls
Create Date: 2026-11-23

New tables (models/intro_bounty.py):
    intro_bounties — a poster offers a reward for a warm
        introduction to a named person or company.
    intro_claims — one user's offer to make the introduction
        ("I know them, I can make this intro"). One pending
        claim per (intro bounty, claimer) is enforced in the
        claim route, not by a database constraint.
    intro_reward_events — a confirmed intro's reward, recorded
        for a future payout (status 'pending_stripe', following
        the StageRewardEvent pattern). One event per confirmed
        claim (unique on claim_id).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'intro_bounties'
down_revision = 'nice_urls'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'intro_bounties',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('poster_id', sa.String(length=36), nullable=False),
        sa.Column('target_name', sa.String(length=200), nullable=False),
        sa.Column('target_org', sa.String(length=200), nullable=True),
        sa.Column('context', sa.Text(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['poster_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_intro_bounties_poster_id', 'intro_bounties', ['poster_id'],
    )
    op.create_table(
        'intro_claims',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('intro_bounty_id', sa.String(length=36), nullable=False),
        sa.Column('claimer_id', sa.String(length=36), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ['intro_bounty_id'], ['intro_bounties.id']),
        sa.ForeignKeyConstraint(['claimer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_intro_claims_intro_bounty_id', 'intro_claims',
        ['intro_bounty_id'],
    )
    op.create_index(
        'ix_intro_claims_claimer_id', 'intro_claims', ['claimer_id'],
    )
    op.create_table(
        'intro_reward_events',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('intro_bounty_id', sa.String(length=36), nullable=False),
        sa.Column('claim_id', sa.String(length=36), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ['intro_bounty_id'], ['intro_bounties.id']),
        sa.ForeignKeyConstraint(['claim_id'], ['intro_claims.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('claim_id', name='uq_intro_reward_claim'),
    )
    op.create_index(
        'ix_intro_reward_events_intro_bounty_id', 'intro_reward_events',
        ['intro_bounty_id'],
    )
    op.create_index(
        'ix_intro_reward_events_claim_id', 'intro_reward_events',
        ['claim_id'],
    )


def downgrade():
    op.drop_index(
        'ix_intro_reward_events_claim_id',
        table_name='intro_reward_events',
    )
    op.drop_index(
        'ix_intro_reward_events_intro_bounty_id',
        table_name='intro_reward_events',
    )
    op.drop_table('intro_reward_events')
    op.drop_index('ix_intro_claims_claimer_id', table_name='intro_claims')
    op.drop_index(
        'ix_intro_claims_intro_bounty_id', table_name='intro_claims')
    op.drop_table('intro_claims')
    op.drop_index(
        'ix_intro_bounties_poster_id', table_name='intro_bounties')
    op.drop_table('intro_bounties')
