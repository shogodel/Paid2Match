"""Phase 2 community: topic spaces, polls, bookmarks, post flair.

Revision ID: phase2_community
Revises: m4_vertical_chains
Create Date: 2026-11-22

New tables (models/space.py, models/poll.py, models/post.py):
    spaces, space_memberships — topic spaces with per-space
    notification settings (mute / mentions-only / all).
    polls, poll_options, poll_votes — blind polls on posts, one vote
    per user (uq_poll_vote_user_poll).
    bookmarks — saved posts (uq_bookmark_user_post).

New column (models/post.py):
    posts.space_id — the space a post belongs to (NULL = general
    feed). Space posts still appear in the main feeds.

The flair taxonomy (Discussion / Win / Ask / Referral Need /
Available) needs no schema change: it maps onto the existing
post_type values, with 'available' added as a plain string value
(post_type is a String column, not an enum). The seeded spaces are
data, not schema — `flask seed-spaces` creates them (models/space.py
SEEDED_SPACES), so a fresh deploy seeds them after `flask db upgrade`.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'phase2_community'
down_revision = 'm4_vertical_chains'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'spaces',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('slug', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('icon', sa.String(length=30), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_index('ix_spaces_slug', 'spaces', ['slug'])

    op.create_table(
        'space_memberships',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('space_id', sa.String(length=36), nullable=False),
        sa.Column('notification_level', sa.String(length=10), nullable=False),
        sa.Column('joined_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['space_id'], ['spaces.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'user_id', 'space_id', name='uq_space_membership_user_space'
        ),
    )
    op.create_index(
        'ix_space_memberships_space_id', 'space_memberships', ['space_id']
    )
    op.create_index(
        'ix_space_memberships_user_id', 'space_memberships', ['user_id']
    )

    op.create_table(
        'polls',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('post_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('post_id'),
    )
    op.create_index('ix_polls_post_id', 'polls', ['post_id'])

    op.create_table(
        'poll_options',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('poll_id', sa.String(length=36), nullable=False),
        sa.Column('text', sa.String(length=100), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['poll_id'], ['polls.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_poll_options_poll_id', 'poll_options', ['poll_id'])

    op.create_table(
        'poll_votes',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('poll_id', sa.String(length=36), nullable=False),
        sa.Column('option_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['option_id'], ['poll_options.id']),
        sa.ForeignKeyConstraint(['poll_id'], ['polls.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'user_id', 'poll_id', name='uq_poll_vote_user_poll'
        ),
    )
    op.create_index('ix_poll_votes_option_id', 'poll_votes', ['option_id'])
    op.create_index('ix_poll_votes_poll_id', 'poll_votes', ['poll_id'])
    op.create_index('ix_poll_votes_user_id', 'poll_votes', ['user_id'])

    op.create_table(
        'bookmarks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('post_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['post_id'], ['posts.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'post_id', name='uq_bookmark_user_post'),
    )
    op.create_index('ix_bookmarks_post_id', 'bookmarks', ['post_id'])
    op.create_index('ix_bookmarks_user_id', 'bookmarks', ['user_id'])

    # The FK rides on the column itself inside batch mode — SQLite
    # cannot ALTER ADD CONSTRAINT (M0–M4 convention: batch_alter_table
    # for every column add; batch mode recreates the table on SQLite).
    with op.batch_alter_table('posts', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'space_id', sa.String(length=36),
            sa.ForeignKey('spaces.id', name='fk_posts_space_id'),
            nullable=True,
        ))
        batch_op.create_index('ix_posts_space_id', ['space_id'])


def downgrade():
    with op.batch_alter_table('posts', schema=None) as batch_op:
        batch_op.drop_index('ix_posts_space_id')
        batch_op.drop_column('space_id')

    op.drop_index('ix_bookmarks_user_id', table_name='bookmarks')
    op.drop_index('ix_bookmarks_post_id', table_name='bookmarks')
    op.drop_table('bookmarks')

    op.drop_index('ix_poll_votes_user_id', table_name='poll_votes')
    op.drop_index('ix_poll_votes_poll_id', table_name='poll_votes')
    op.drop_index('ix_poll_votes_option_id', table_name='poll_votes')
    op.drop_table('poll_votes')
    op.drop_index('ix_poll_options_poll_id', table_name='poll_options')
    op.drop_table('poll_options')
    op.drop_index('ix_polls_post_id', table_name='polls')
    op.drop_table('polls')

    op.drop_index(
        'ix_space_memberships_user_id', table_name='space_memberships'
    )
    op.drop_index(
        'ix_space_memberships_space_id', table_name='space_memberships'
    )
    op.drop_table('space_memberships')
    op.drop_index('ix_spaces_slug', table_name='spaces')
    op.drop_table('spaces')
