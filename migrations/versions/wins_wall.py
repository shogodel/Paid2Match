"""Wins wall — short stories about completed hires.

Revision ID: wins_wall
Revises: nice_urls
Create Date: 2026-11-23

New table (models/win_story.py):
    win_stories — one row per story. The poster, the referrer and
    the hired candidate each get one story per completed bounty
    (uq_win_stories_bounty_role). match_id is nullable: a story is
    filed per bounty, and the wall falls back to the bounty's
    completed match when the link is not set.

The live paid-out counter (utils/paid_out.py) needs no migration:
it reads the existing payouts table.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'wins_wall'
down_revision = 'staked_vouches'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'win_stories',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('bounty_id', sa.String(length=36), nullable=False),
        sa.Column('match_id', sa.String(length=36), nullable=True),
        sa.Column('author_id', sa.String(length=36), nullable=False),
        sa.Column('role', sa.String(length=10), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['author_id'], ['users.id']),
        sa.ForeignKeyConstraint(['bounty_id'], ['bounties.id']),
        sa.ForeignKeyConstraint(['match_id'], ['matches.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'bounty_id', 'role', name='uq_win_stories_bounty_role'
        ),
    )
    op.create_index(
        'ix_win_stories_bounty_id', 'win_stories', ['bounty_id'],
    )
    op.create_index(
        'ix_win_stories_match_id', 'win_stories', ['match_id'],
    )
    op.create_index(
        'ix_win_stories_author_id', 'win_stories', ['author_id'],
    )


def downgrade():
    op.drop_index('ix_win_stories_author_id', table_name='win_stories')
    op.drop_index('ix_win_stories_match_id', table_name='win_stories')
    op.drop_index('ix_win_stories_bounty_id', table_name='win_stories')
    op.drop_table('win_stories')
