"""M3 social monetization + trust v2: bumps/boosts, Pro subscriptions,
linked accounts, endorsements, pledge register, post views, search
appearances, post bump/boost/auto-hide columns, profile-viewer-names
opt-in.

Revision ID: m3_monetization
Revises: m2_money_trust
Create Date: 2026-11-21

New tables (models/bump.py, subscription.py, linked_account.py,
endorsement.py, post_view.py):
    bumps, bump_pack_purchases, boosts, recurring_bumps,
    pro_subscriptions, linked_accounts, endorsements, pledges,
    post_views, search_appearances.

Columns:
    posts.bumped_at / bumped_until / boosted_until (models/post.py) —
    the active bump/boost window; the Bump/Boost rows are the ledger.
    posts.auto_hidden_at + comments.auto_hidden_at — set when the
    report threshold auto-hides content (moderation v1); distinguishes
    an auto-hide (restorable on dismiss) from an admin hide.
    profiles.profile_view_names (models/profile.py) — opt-in to show
    your name in Pro users' 90-day profile viewer lists (default off).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm3_monetization'
down_revision = 'm2_money_trust'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bumps',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('post_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('stripe_session_id', sa.String(length=200), nullable=True),
        sa.Column('price_cents', sa.Integer(), nullable=True),
        sa.Column('views_at_bump', sa.Integer(), nullable=True),
        sa.Column('likes_at_bump', sa.Integer(), nullable=True),
        sa.Column('comments_at_bump', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bumps_post_id', 'bumps', ['post_id'])
    op.create_index('ix_bumps_user_id', 'bumps', ['user_id'])

    op.create_table(
        'bump_pack_purchases',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('pack_size', sa.Integer(), nullable=False),
        sa.Column('price_cents', sa.Integer(), nullable=False),
        sa.Column('stripe_session_id', sa.String(length=200), nullable=False),
        sa.Column('bumps_remaining', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_session_id'),
    )
    op.create_index('ix_bump_pack_purchases_user_id', 'bump_pack_purchases', ['user_id'])

    op.create_table(
        'boosts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('post_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('days', sa.Integer(), nullable=False),
        sa.Column('price_cents', sa.Integer(), nullable=False),
        sa.Column('stripe_session_id', sa.String(length=200), nullable=False),
        sa.Column('views_at_boost', sa.Integer(), nullable=True),
        sa.Column('likes_at_boost', sa.Integer(), nullable=True),
        sa.Column('comments_at_boost', sa.Integer(), nullable=True),
        sa.Column('starts_at', sa.DateTime(), nullable=False),
        sa.Column('ends_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_session_id'),
    )
    op.create_index('ix_boosts_post_id', 'boosts', ['post_id'])
    op.create_index('ix_boosts_user_id', 'boosts', ['user_id'])

    op.create_table(
        'recurring_bumps',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('post_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('stripe_subscription_id', sa.String(length=200), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('cancel_at_period_end', sa.Boolean(), nullable=True),
        sa.Column('last_bumped_at', sa.DateTime(), nullable=True),
        sa.Column('next_bump_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_subscription_id'),
    )
    op.create_index('ix_recurring_bumps_post_id', 'recurring_bumps', ['post_id'])
    op.create_index('ix_recurring_bumps_user_id', 'recurring_bumps', ['user_id'])

    op.create_table(
        'pro_subscriptions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('stripe_subscription_id', sa.String(length=200), nullable=False),
        sa.Column('stripe_customer_id', sa.String(length=200), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('cancel_at_period_end', sa.Boolean(), nullable=True),
        sa.Column('current_period_start', sa.DateTime(), nullable=True),
        sa.Column('current_period_end', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_subscription_id'),
        sa.UniqueConstraint('user_id'),
    )
    op.create_index('ix_pro_subscriptions_user_id', 'pro_subscriptions', ['user_id'])

    op.create_table(
        'linked_accounts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('platform', sa.String(length=20), nullable=False),
        sa.Column('handle', sa.String(length=200), nullable=True),
        sa.Column('url', sa.String(length=500), nullable=True),
        sa.Column('verification_method', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('verification_code', sa.String(length=20), nullable=True),
        sa.Column('manual_review', sa.Boolean(), nullable=True),
        sa.Column('admin_note', sa.Text(), nullable=True),
        sa.Column('visibility', sa.String(length=20), nullable=False),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'platform', name='uq_linked_user_platform'),
    )
    op.create_index('ix_linked_accounts_user_id', 'linked_accounts', ['user_id'])

    op.create_table(
        'endorsements',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('endorser_id', sa.String(length=36), nullable=False),
        sa.Column('endorsee_id', sa.String(length=36), nullable=False),
        sa.Column('skill', sa.String(length=100), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['endorsee_id'], ['users.id']),
        sa.ForeignKeyConstraint(['endorser_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'endorser_id', 'endorsee_id', 'skill',
            name='uq_endorsement_pair_skill',
        ),
    )
    op.create_index('ix_endorsements_endorsee_id', 'endorsements', ['endorsee_id'])
    op.create_index('ix_endorsements_endorser_id', 'endorsements', ['endorser_id'])

    op.create_table(
        'pledges',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('company_name', sa.String(length=200), nullable=False),
        sa.Column('website', sa.String(length=500), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('revoke_reason', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id'),
    )
    op.create_index('ix_pledges_user_id', 'pledges', ['user_id'])

    op.create_table(
        'post_views',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('post_id', sa.String(length=36), nullable=False),
        sa.Column('viewer_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.ForeignKeyConstraint(['viewer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_post_views_created_at', 'post_views', ['created_at'])
    op.create_index('ix_post_views_post_id', 'post_views', ['post_id'])
    op.create_index('ix_post_views_viewer_id', 'post_views', ['viewer_id'])

    op.create_table(
        'search_appearances',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('searched_user_id', sa.String(length=36), nullable=False),
        sa.Column('search_query', sa.String(length=300), nullable=True),
        sa.Column('viewer_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['searched_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['viewer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_search_appearances_created_at', 'search_appearances', ['created_at'])
    op.create_index('ix_search_appearances_searched_user_id', 'search_appearances', ['searched_user_id'])
    op.create_index('ix_search_appearances_viewer_id', 'search_appearances', ['viewer_id'])

    with op.batch_alter_table('posts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auto_hidden_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('bumped_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('bumped_until', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('boosted_until', sa.DateTime(), nullable=True))

    with op.batch_alter_table('comments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auto_hidden_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('profiles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('profile_view_names', sa.Boolean(), nullable=True))

    op.execute('UPDATE profiles SET profile_view_names = FALSE WHERE profile_view_names IS NULL')


def downgrade():
    with op.batch_alter_table('profiles', schema=None) as batch_op:
        batch_op.drop_column('profile_view_names')

    with op.batch_alter_table('comments', schema=None) as batch_op:
        batch_op.drop_column('auto_hidden_at')

    with op.batch_alter_table('posts', schema=None) as batch_op:
        batch_op.drop_column('boosted_until')
        batch_op.drop_column('bumped_until')
        batch_op.drop_column('bumped_at')
        batch_op.drop_column('auto_hidden_at')

    op.drop_index('ix_search_appearances_viewer_id', table_name='search_appearances')
    op.drop_index('ix_search_appearances_searched_user_id', table_name='search_appearances')
    op.drop_index('ix_search_appearances_created_at', table_name='search_appearances')
    op.drop_table('search_appearances')

    op.drop_index('ix_post_views_viewer_id', table_name='post_views')
    op.drop_index('ix_post_views_post_id', table_name='post_views')
    op.drop_index('ix_post_views_created_at', table_name='post_views')
    op.drop_table('post_views')

    op.drop_index('ix_pledges_user_id', table_name='pledges')
    op.drop_table('pledges')

    op.drop_index('ix_endorsements_endorser_id', table_name='endorsements')
    op.drop_index('ix_endorsements_endorsee_id', table_name='endorsements')
    op.drop_table('endorsements')

    op.drop_index('ix_linked_accounts_user_id', table_name='linked_accounts')
    op.drop_table('linked_accounts')

    op.drop_index('ix_pro_subscriptions_user_id', table_name='pro_subscriptions')
    op.drop_table('pro_subscriptions')

    op.drop_index('ix_recurring_bumps_user_id', table_name='recurring_bumps')
    op.drop_index('ix_recurring_bumps_post_id', table_name='recurring_bumps')
    op.drop_table('recurring_bumps')

    op.drop_index('ix_boosts_user_id', table_name='boosts')
    op.drop_index('ix_boosts_post_id', table_name='boosts')
    op.drop_table('boosts')

    op.drop_index('ix_bump_pack_purchases_user_id', table_name='bump_pack_purchases')
    op.drop_table('bump_pack_purchases')

    op.drop_index('ix_bumps_user_id', table_name='bumps')
    op.drop_index('ix_bumps_post_id', table_name='bumps')
    op.drop_table('bumps')
