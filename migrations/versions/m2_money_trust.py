"""M2 money + trust v1: Connect accounts, payout ledger, reviews,
verification requests, founding-referrer flag, pitch response time,
dispute outcomes.

Revision ID: m2_money_trust
Revises: m1_social_core
Create Date: 2026-11-21

New tables (models/connect_account.py, payout.py, review.py,
verification.py):
    connect_accounts, payouts, reviews, verification_requests.

Columns:
    users.founding_referrer (models/user.py) — admin-set flag; founding
    referrers pay a 15% platform fee instead of the standard 20%.
    pitches.responded_at (models/pitch.py) — when the poster first
    responded; drives the no-ghosting SLA. NULL for pitches responded
    before M2 (the SLA falls back to updated_at).
    disputes.outcome (models/dispute.py) — 'upheld' / 'dismissed' on a
    resolved dispute; drives the trust-score dispute penalty.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm2_money_trust'
down_revision = 'm1_social_core'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'connect_accounts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('stripe_account_id', sa.String(length=100), nullable=False),
        sa.Column('onboarding_status', sa.String(length=20), nullable=False),
        sa.Column('charges_enabled', sa.Boolean(), nullable=True),
        sa.Column('payouts_enabled', sa.Boolean(), nullable=True),
        sa.Column('details_submitted', sa.Boolean(), nullable=True),
        sa.Column('requirements_due', sa.Text(), nullable=True),
        sa.Column('last_payout_at', sa.DateTime(), nullable=True),
        sa.Column('last_payout_amount', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_account_id'),
        sa.UniqueConstraint('user_id'),
    )
    op.create_index('ix_connect_accounts_stripe_account_id', 'connect_accounts', ['stripe_account_id'])
    op.create_index('ix_connect_accounts_user_id', 'connect_accounts', ['user_id'])

    op.create_table(
        'payouts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('match_id', sa.String(length=36), nullable=False),
        sa.Column('referrer_id', sa.String(length=36), nullable=False),
        sa.Column('bounty_id', sa.String(length=36), nullable=True),
        sa.Column('gross_amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('platform_fee', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('method', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('stripe_transfer_id', sa.String(length=100), nullable=True),
        sa.Column('stripe_payout_id', sa.String(length=100), nullable=True),
        sa.Column('captured_at', sa.DateTime(), nullable=True),
        sa.Column('manual_reference', sa.Text(), nullable=True),
        sa.Column('manual_paid_at', sa.DateTime(), nullable=True),
        sa.Column('recorded_by_id', sa.String(length=36), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['bounty_id'], ['bounties.id']),
        sa.ForeignKeyConstraint(['match_id'], ['matches.id']),
        sa.ForeignKeyConstraint(['recorded_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['referrer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_payouts_bounty_id', 'payouts', ['bounty_id'])
    op.create_index('ix_payouts_match_id', 'payouts', ['match_id'])
    op.create_index('ix_payouts_referrer_id', 'payouts', ['referrer_id'])
    op.create_index('ix_payouts_stripe_transfer_id', 'payouts', ['stripe_transfer_id'])

    op.create_table(
        'reviews',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('match_id', sa.String(length=36), nullable=False),
        sa.Column('reviewer_id', sa.String(length=36), nullable=False),
        sa.Column('reviewee_id', sa.String(length=36), nullable=False),
        sa.Column('reviewee_role', sa.String(length=10), nullable=False),
        sa.Column('rating_primary', sa.Integer(), nullable=False),
        sa.Column('rating_secondary', sa.Integer(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('revealed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['match_id'], ['matches.id']),
        sa.ForeignKeyConstraint(['reviewee_id'], ['users.id']),
        sa.ForeignKeyConstraint(['reviewer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('match_id', 'reviewer_id', name='uq_review_match_reviewer'),
    )
    op.create_index('ix_reviews_match_id', 'reviews', ['match_id'])
    op.create_index('ix_reviews_reviewee_id', 'reviews', ['reviewee_id'])
    op.create_index('ix_reviews_reviewer_id', 'reviews', ['reviewer_id'])

    op.create_table(
        'verification_requests',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('admin_note', sa.Text(), nullable=True),
        sa.Column('reviewed_by_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['reviewed_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_verification_requests_user_id', 'verification_requests', ['user_id'])

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('founding_referrer', sa.Boolean(), nullable=True))

    with op.batch_alter_table('pitches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('responded_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('disputes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('outcome', sa.String(length=20), nullable=True))

    # Backfill the flag for pre-existing users (default False).
    op.execute("UPDATE users SET founding_referrer = FALSE WHERE founding_referrer IS NULL")


def downgrade():
    with op.batch_alter_table('disputes', schema=None) as batch_op:
        batch_op.drop_column('outcome')

    with op.batch_alter_table('pitches', schema=None) as batch_op:
        batch_op.drop_column('responded_at')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('founding_referrer')

    op.drop_index('ix_verification_requests_user_id', table_name='verification_requests')
    op.drop_table('verification_requests')

    op.drop_index('ix_reviews_reviewer_id', table_name='reviews')
    op.drop_index('ix_reviews_reviewee_id', table_name='reviews')
    op.drop_index('ix_reviews_match_id', table_name='reviews')
    op.drop_table('reviews')

    op.drop_index('ix_payouts_stripe_transfer_id', table_name='payouts')
    op.drop_index('ix_payouts_referrer_id', table_name='payouts')
    op.drop_index('ix_payouts_match_id', table_name='payouts')
    op.drop_index('ix_payouts_bounty_id', table_name='payouts')
    op.drop_table('payouts')

    op.drop_index('ix_connect_accounts_user_id', table_name='connect_accounts')
    op.drop_index('ix_connect_accounts_stripe_account_id', table_name='connect_accounts')
    op.drop_table('connect_accounts')
