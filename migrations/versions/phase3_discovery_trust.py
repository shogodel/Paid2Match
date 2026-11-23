"""Phase 3: bounty staged rewards (display only).

Revision ID: phase3_discovery_trust
Revises: phase2_community
Create Date: 2026-11-23

New columns (models/bounty.py):
    bounties.stage_submit_reward, stage_interview_reward,
    stage_hire_reward — optional amounts the poster declares per
    referral stage (Paraform model). Display only: no payout moves
    until Stripe is wired (see models/staged_reward.py).

New table (models/staged_reward.py):
    stage_reward_events — one row per completed (pitch, stage),
    unique on (pitch_id, stage) so a stage is never recorded twice.
    Every row is status 'pending_stripe' until the payout wiring
    slice lands.

The $50 paid-bounty floor (MIN_PAID_BOUNTY_CAD) needs no schema
change: it is enforced by a model @validates guard + the forms, and
it never fires on rows loaded from the DB, so pre-floor bounties
are untouched.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'phase3_discovery_trust'
down_revision = 'phase2_community'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'stage_reward_events',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('bounty_id', sa.String(length=36), nullable=False),
        sa.Column('pitch_id', sa.String(length=36), nullable=False),
        sa.Column('stage', sa.String(length=20), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['bounty_id'], ['bounties.id']),
        sa.ForeignKeyConstraint(['pitch_id'], ['pitches.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'pitch_id', 'stage', name='uq_stage_event_pitch_stage'
        ),
    )
    op.create_index(
        'ix_stage_reward_events_bounty_id', 'stage_reward_events',
        ['bounty_id'],
    )
    op.create_index(
        'ix_stage_reward_events_pitch_id', 'stage_reward_events',
        ['pitch_id'],
    )

    # M0–M4 convention: batch_alter_table for every column add
    # (SQLite cannot ALTER ADD CONSTRAINT; batch mode recreates the
    # table).
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'stage_submit_reward', sa.Numeric(12, 2), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'stage_interview_reward', sa.Numeric(12, 2), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'stage_hire_reward', sa.Numeric(12, 2), nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.drop_column('stage_hire_reward')
        batch_op.drop_column('stage_interview_reward')
        batch_op.drop_column('stage_submit_reward')

    op.drop_index(
        'ix_stage_reward_events_pitch_id', table_name='stage_reward_events'
    )
    op.drop_index(
        'ix_stage_reward_events_bounty_id', table_name='stage_reward_events'
    )
    op.drop_table('stage_reward_events')
