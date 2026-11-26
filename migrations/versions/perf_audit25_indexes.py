"""Add indexes for the ORDER BY ... LIMIT sorts on hot pages.

Revision ID: perf_audit25_indexes
Revises: message_read_at
Create Date: 2026-11-25

Audit 2026-11-25 measured these five sorts running without an
index (full scan + sort on every render):

- posts.created_at — the homepage preview (ORDER BY created_at
  DESC LIMIT 4) and every feed/space/search listing that orders
  newest-first.
- bounties.created_at — the homepage featured needs (ORDER BY
  created_at DESC LIMIT 3) and the bounty board.
- win_stories.created_at — the wins wall (ORDER BY created_at
  DESC over all stories).
- notifications.created_at — recent_notifications (ORDER BY
  created_at DESC LIMIT 10), which the navbar context processor
  runs on EVERY page render.
- profiles.karma — the Top Helpers rail (ORDER BY karma DESC
  LIMIT 3) on the homepage and the feed.

No other indexes were added: the audit grepped every ForeignKey
and found the hot-path ones (Message.sender_id/recipient_id,
Notification.recipient_id, Post.author_id/bounty_id/space_id,
Follow, Match, Pitch, Referral, Endorsement, PostView,
ProfileView) already indexed, and the remaining unindexed FKs are
rarely-joined admin/audit columns.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'perf_audit25_indexes'
down_revision = 'founding_window'
branch_labels = None
depends_on = None

_INDEXES = [
    ('ix_posts_created_at', 'posts', ['created_at']),
    ('ix_bounties_created_at', 'bounties', ['created_at']),
    ('ix_win_stories_created_at', 'win_stories', ['created_at']),
    ('ix_notifications_created_at', 'notifications', ['created_at']),
    ('ix_profiles_karma', 'profiles', ['karma']),
]


def upgrade():
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)


def downgrade():
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
