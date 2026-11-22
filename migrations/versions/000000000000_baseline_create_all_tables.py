"""Baseline: create the tables the original app was built with.

Revision ID: baseline_create_all
Revises: (none — this is the new base of the chain)
Create Date: 2026-11-21 (M2 housekeeping)

The migration chain used to start at fa6ff45fcb16, which assumes the
`bounties` table already exists — the original tables were created with
db.create_all() before migrations existed, so a fresh database could
never run the chain from scratch. This migration recreates that
pre-migration schema (users, profiles, bounties, pitches, matches,
messages, admin_settings, disputes) exactly as it stood just before
fa6ff45fcb16, and fa6ff45fcb16 now revises this baseline.

Reconstruction rules (each column below is in the schema ONLY if no
later migration adds it):
- bounties: reward_amount is NOT NULL here (fa6ff45fcb16 makes it
  nullable); bounty_direction is NOT here (added by fa6ff45fcb16);
  transaction_fee is NOT here (e78f404f8c48); payer_type /
  third_party_payer_id are NOT here (add_payer_type_and_agreements);
  is_featured IS here (dropped by 03ddca00060d); is_free and
  stripe_payment_intent ARE here (no migration ever adds them).
- users: is_deleted / deleted_at are NOT here (d12676eac83c);
  email_verified / email_verified_at are NOT here (m0_foundation);
  founding_referrer is NOT here (m2_money_trust).
- profiles: updated_at + the LinkedIn-style fields are NOT here
  (75314a6263e5 adds them, a0872675a8ca drops most again);
  privacy_level is NOT here (a0872675a8ca); headline/linkedin_url/
  github_url/twitter_handle/remote_ok/relocation_assistance are NOT
  here (m0_foundation); karma/helped_count/free_bumps + the social
  links are NOT here (m1_social_core). language IS here — no
  migration adds it (c971ab3f5436 is titled "Add language field"
  but only creates foreign keys).
- matches: transaction_fee is NOT here (e78f404f8c48).
- pitches: responded_at is NOT here (m2_money_trust).
- disputes: outcome is NOT here (m2_money_trust).
- bounty_upgrades, bounty_payment_agreements, audit_logs and all
  M0/M1 tables are created by their own later migrations.

Safe for the live database: it is already at m1_social_core, so this
migration never runs there — it only matters for fresh databases.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'baseline_create_all'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'users',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('email', sa.String(length=120), nullable=False),
        sa.Column('password_hash', sa.String(length=256), nullable=True),
        sa.Column('full_name', sa.String(length=120), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('last_login', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
    )
    op.create_index('ix_users_email', 'users', ['email'])

    op.create_table(
        'profiles',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('profile_type', sa.String(length=30), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=True),
        sa.Column('display_name', sa.String(length=120), nullable=True),
        sa.Column('bio', sa.Text(), nullable=True),
        sa.Column('location', sa.String(length=200), nullable=True),
        sa.Column('avatar_url', sa.String(length=500), nullable=True),
        sa.Column('reputation_score', sa.Integer(), nullable=True),
        sa.Column('verified', sa.Boolean(), nullable=True),
        sa.Column('company_name', sa.String(length=200), nullable=True),
        sa.Column('skills', sa.Text(), nullable=True),
        sa.Column('website', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_profiles_user_id', 'profiles', ['user_id'])

    op.create_table(
        'bounties',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('poster_id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('bounty_type', sa.String(length=30), nullable=False),
        sa.Column('reward_amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('payment_status', sa.String(length=20), nullable=False),
        sa.Column('location', sa.String(length=200), nullable=True),
        sa.Column('deadline', sa.DateTime(), nullable=True),
        sa.Column('success_criteria', sa.Text(), nullable=True),
        sa.Column('is_featured', sa.Boolean(), nullable=True),
        sa.Column('is_free', sa.Boolean(), nullable=True),
        sa.Column('stripe_payment_intent', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['poster_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bounties_poster_id', 'bounties', ['poster_id'])

    op.create_table(
        'pitches',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('bounty_id', sa.String(length=36), nullable=False),
        sa.Column('hunter_id', sa.String(length=36), nullable=False),
        sa.Column('candidate_teaser', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('hunter_message', sa.Text(), nullable=True),
        sa.Column('employer_response', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['bounty_id'], ['bounties.id']),
        sa.ForeignKeyConstraint(['hunter_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_pitches_bounty_id', 'pitches', ['bounty_id'])
    op.create_index('ix_pitches_hunter_id', 'pitches', ['hunter_id'])

    op.create_table(
        'matches',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('bounty_id', sa.String(length=36), nullable=False),
        sa.Column('pitch_id', sa.String(length=36), nullable=True),
        sa.Column('hunter_id', sa.String(length=36), nullable=False),
        sa.Column('employer_id', sa.String(length=36), nullable=False),
        sa.Column('hunter_confirmed', sa.Boolean(), nullable=True),
        sa.Column('employer_confirmed', sa.Boolean(), nullable=True),
        sa.Column('payout_amount', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('platform_fee', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('payout_status', sa.String(length=20), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['bounty_id'], ['bounties.id']),
        sa.ForeignKeyConstraint(['employer_id'], ['users.id']),
        sa.ForeignKeyConstraint(['hunter_id'], ['users.id']),
        sa.ForeignKeyConstraint(['pitch_id'], ['pitches.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_matches_bounty_id', 'matches', ['bounty_id'])
    op.create_index('ix_matches_employer_id', 'matches', ['employer_id'])
    op.create_index('ix_matches_hunter_id', 'matches', ['hunter_id'])

    op.create_table(
        'messages',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('sender_id', sa.String(length=36), nullable=False),
        sa.Column('recipient_id', sa.String(length=36), nullable=False),
        sa.Column('bounty_id', sa.String(length=36), nullable=True),
        sa.Column('pitch_id', sa.String(length=36), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('is_read', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['bounty_id'], ['bounties.id']),
        sa.ForeignKeyConstraint(['pitch_id'], ['pitches.id']),
        sa.ForeignKeyConstraint(['recipient_id'], ['users.id']),
        sa.ForeignKeyConstraint(['sender_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_messages_bounty_id', 'messages', ['bounty_id'])
    op.create_index('ix_messages_created_at', 'messages', ['created_at'])
    op.create_index('ix_messages_pitch_id', 'messages', ['pitch_id'])
    op.create_index('ix_messages_recipient_id', 'messages', ['recipient_id'])
    op.create_index('ix_messages_sender_id', 'messages', ['sender_id'])

    op.create_table(
        'admin_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=100), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key'),
    )
    op.create_index('ix_admin_settings_key', 'admin_settings', ['key'])

    op.create_table(
        'disputes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('match_id', sa.String(length=36), nullable=False),
        sa.Column('raised_by_id', sa.String(length=36), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('resolution', sa.Text(), nullable=True),
        sa.Column('resolved_by_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['match_id'], ['matches.id']),
        sa.ForeignKeyConstraint(['raised_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['resolved_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_disputes_match_id', 'disputes', ['match_id'])
    op.create_index('ix_disputes_raised_by_id', 'disputes', ['raised_by_id'])


def downgrade():
    op.drop_index('ix_disputes_raised_by_id', table_name='disputes')
    op.drop_index('ix_disputes_match_id', table_name='disputes')
    op.drop_table('disputes')

    op.drop_index('ix_admin_settings_key', table_name='admin_settings')
    op.drop_table('admin_settings')

    op.drop_index('ix_messages_sender_id', table_name='messages')
    op.drop_index('ix_messages_recipient_id', table_name='messages')
    op.drop_index('ix_messages_pitch_id', table_name='messages')
    op.drop_index('ix_messages_created_at', table_name='messages')
    op.drop_index('ix_messages_bounty_id', table_name='messages')
    op.drop_table('messages')

    op.drop_index('ix_matches_hunter_id', table_name='matches')
    op.drop_index('ix_matches_employer_id', table_name='matches')
    op.drop_index('ix_matches_bounty_id', table_name='matches')
    op.drop_table('matches')

    op.drop_index('ix_pitches_hunter_id', table_name='pitches')
    op.drop_index('ix_pitches_bounty_id', table_name='pitches')
    op.drop_table('pitches')

    op.drop_index('ix_bounties_poster_id', table_name='bounties')
    op.drop_table('bounties')

    op.drop_index('ix_profiles_user_id', table_name='profiles')
    op.drop_table('profiles')

    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')
