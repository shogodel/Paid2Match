"""M1 social core: posts, follows, likes, comments, shares, notifications,
referrals, karma, moderation, profile views + profile karma/social fields.

Revision ID: m1_social_core
Revises: m0_foundation
Create Date: 2026-11-21

New tables (models/post.py, follow.py, notification.py, referral.py,
moderation.py, profile_view.py):
    posts, post_likes, comments, shares, follows, notifications,
    referrals, karma_events, reports, blocks, profile_views.

Profile columns (models/profile.py):
    karma / helped_count / free_bumps (M1 §4.2, §1.3) and the full
    social-link field set (§2.1): instagram_url, facebook_url,
    tiktok_url, youtube_url. Verification of those links is M3.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm1_social_core'
down_revision = 'm0_foundation'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'posts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('author_id', sa.String(length=36), nullable=False),
        sa.Column('post_type', sa.String(length=20), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('photo_url', sa.String(length=500), nullable=True),
        sa.Column('bounty_id', sa.String(length=36), nullable=True),
        sa.Column('is_hidden', sa.Boolean(), nullable=True),
        sa.Column('view_count', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['author_id'], ['users.id']),
        sa.ForeignKeyConstraint(['bounty_id'], ['bounties.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_posts_author_id', 'posts', ['author_id'])
    op.create_index('ix_posts_bounty_id', 'posts', ['bounty_id'])

    op.create_table(
        'post_likes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('post_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'post_id', name='uq_like_user_post'),
    )
    op.create_index('ix_post_likes_post_id', 'post_likes', ['post_id'])
    op.create_index('ix_post_likes_user_id', 'post_likes', ['user_id'])

    op.create_table(
        'comments',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('author_id', sa.String(length=36), nullable=False),
        sa.Column('post_id', sa.String(length=36), nullable=False),
        sa.Column('parent_id', sa.String(length=36), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('is_hidden', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['author_id'], ['users.id']),
        sa.ForeignKeyConstraint(['parent_id'], ['comments.id']),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_comments_author_id', 'comments', ['author_id'])
    op.create_index('ix_comments_parent_id', 'comments', ['parent_id'])
    op.create_index('ix_comments_post_id', 'comments', ['post_id'])

    op.create_table(
        'shares',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('post_id', sa.String(length=36), nullable=False),
        sa.Column('comment', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'post_id', name='uq_share_user_post'),
    )
    op.create_index('ix_shares_post_id', 'shares', ['post_id'])
    op.create_index('ix_shares_user_id', 'shares', ['user_id'])

    op.create_table(
        'follows',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('follower_id', sa.String(length=36), nullable=False),
        sa.Column('followed_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['followed_id'], ['users.id']),
        sa.ForeignKeyConstraint(['follower_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('follower_id', 'followed_id', name='uq_follow_pair'),
    )
    op.create_index('ix_follows_followed_id', 'follows', ['followed_id'])
    op.create_index('ix_follows_follower_id', 'follows', ['follower_id'])

    op.create_table(
        'notifications',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('recipient_id', sa.String(length=36), nullable=False),
        sa.Column('actor_id', sa.String(length=36), nullable=True),
        sa.Column('type', sa.String(length=30), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('link_url', sa.String(length=500), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id']),
        sa.ForeignKeyConstraint(['recipient_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_notifications_recipient_id', 'notifications', ['recipient_id'])
    op.create_index('ix_notifications_type', 'notifications', ['type'])

    op.create_table(
        'referrals',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('referrer_id', sa.String(length=36), nullable=False),
        sa.Column('bounty_id', sa.String(length=36), nullable=False),
        sa.Column('pitch_id', sa.String(length=36), nullable=True),
        sa.Column('referred_name', sa.String(length=120), nullable=False),
        sa.Column('referred_email', sa.String(length=120), nullable=False),
        sa.Column('referred_user_id', sa.String(length=36), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('invite_token', sa.String(length=64), nullable=False),
        sa.Column('invited_at', sa.DateTime(), nullable=True),
        sa.Column('consented_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['bounty_id'], ['bounties.id']),
        sa.ForeignKeyConstraint(['pitch_id'], ['pitches.id']),
        sa.ForeignKeyConstraint(['referred_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['referrer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('bounty_id', 'referred_email', name='uq_referral_bounty_person'),
        sa.UniqueConstraint('invite_token', name='uq_referral_invite_token'),
    )
    op.create_index('ix_referrals_bounty_id', 'referrals', ['bounty_id'])
    op.create_index('ix_referrals_pitch_id', 'referrals', ['pitch_id'])
    op.create_index('ix_referrals_referred_email', 'referrals', ['referred_email'])
    op.create_index('ix_referrals_referred_user_id', 'referrals', ['referred_user_id'])
    op.create_index('ix_referrals_referrer_id', 'referrals', ['referrer_id'])

    op.create_table(
        'karma_events',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('reason', sa.String(length=30), nullable=False),
        sa.Column('ref_type', sa.String(length=30), nullable=True),
        sa.Column('ref_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_karma_events_user_id', 'karma_events', ['user_id'])

    op.create_table(
        'reports',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('reporter_id', sa.String(length=36), nullable=False),
        sa.Column('target_type', sa.String(length=20), nullable=False),
        sa.Column('target_id', sa.String(length=36), nullable=False),
        sa.Column('reason', sa.String(length=20), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('weight', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reviewed_by_id', sa.String(length=36), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['reporter_id'], ['users.id']),
        sa.ForeignKeyConstraint(['reviewed_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_reports_reporter_id', 'reports', ['reporter_id'])
    op.create_index('ix_reports_target_id', 'reports', ['target_id'])

    op.create_table(
        'blocks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('blocker_id', sa.String(length=36), nullable=False),
        sa.Column('blocked_id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['blocked_id'], ['users.id']),
        sa.ForeignKeyConstraint(['blocker_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('blocker_id', 'blocked_id', 'kind', name='uq_block_pair_kind'),
    )
    op.create_index('ix_blocks_blocked_id', 'blocks', ['blocked_id'])
    op.create_index('ix_blocks_blocker_id', 'blocks', ['blocker_id'])

    op.create_table(
        'profile_views',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('profile_user_id', sa.String(length=36), nullable=False),
        sa.Column('viewer_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['profile_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['viewer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_profile_views_created_at', 'profile_views', ['created_at'])
    op.create_index('ix_profile_views_profile_user_id', 'profile_views', ['profile_user_id'])
    op.create_index('ix_profile_views_viewer_id', 'profile_views', ['viewer_id'])

    with op.batch_alter_table('profiles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('karma', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('helped_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('free_bumps', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('instagram_url', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('facebook_url', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('tiktok_url', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('youtube_url', sa.String(length=500), nullable=True))

    # Backfill the counters for pre-existing profiles.
    op.execute("UPDATE profiles SET karma = 0 WHERE karma IS NULL")
    op.execute("UPDATE profiles SET helped_count = 0 WHERE helped_count IS NULL")
    op.execute("UPDATE profiles SET free_bumps = 0 WHERE free_bumps IS NULL")


def downgrade():
    with op.batch_alter_table('profiles', schema=None) as batch_op:
        batch_op.drop_column('youtube_url')
        batch_op.drop_column('tiktok_url')
        batch_op.drop_column('facebook_url')
        batch_op.drop_column('instagram_url')
        batch_op.drop_column('free_bumps')
        batch_op.drop_column('helped_count')
        batch_op.drop_column('karma')

    op.drop_index('ix_profile_views_viewer_id', table_name='profile_views')
    op.drop_index('ix_profile_views_profile_user_id', table_name='profile_views')
    op.drop_index('ix_profile_views_created_at', table_name='profile_views')
    op.drop_table('profile_views')

    op.drop_index('ix_blocks_blocker_id', table_name='blocks')
    op.drop_index('ix_blocks_blocked_id', table_name='blocks')
    op.drop_table('blocks')

    op.drop_index('ix_reports_target_id', table_name='reports')
    op.drop_index('ix_reports_reporter_id', table_name='reports')
    op.drop_table('reports')

    op.drop_index('ix_karma_events_user_id', table_name='karma_events')
    op.drop_table('karma_events')

    op.drop_index('ix_referrals_referrer_id', table_name='referrals')
    op.drop_index('ix_referrals_referred_user_id', table_name='referrals')
    op.drop_index('ix_referrals_referred_email', table_name='referrals')
    op.drop_index('ix_referrals_pitch_id', table_name='referrals')
    op.drop_index('ix_referrals_bounty_id', table_name='referrals')
    op.drop_table('referrals')

    op.drop_index('ix_notifications_type', table_name='notifications')
    op.drop_index('ix_notifications_recipient_id', table_name='notifications')
    op.drop_table('notifications')

    op.drop_index('ix_follows_follower_id', table_name='follows')
    op.drop_index('ix_follows_followed_id', table_name='follows')
    op.drop_table('follows')

    op.drop_index('ix_shares_user_id', table_name='shares')
    op.drop_index('ix_shares_post_id', table_name='shares')
    op.drop_table('shares')

    op.drop_index('ix_comments_post_id', table_name='comments')
    op.drop_index('ix_comments_parent_id', table_name='comments')
    op.drop_index('ix_comments_author_id', table_name='comments')
    op.drop_table('comments')

    op.drop_index('ix_post_likes_user_id', table_name='post_likes')
    op.drop_index('ix_post_likes_post_id', table_name='post_likes')
    op.drop_table('post_likes')

    op.drop_index('ix_posts_bounty_id', table_name='posts')
    op.drop_index('ix_posts_author_id', table_name='posts')
    op.drop_table('posts')
