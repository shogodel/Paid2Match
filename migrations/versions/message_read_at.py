"""Add Message.read_at (2026-11-25): when a message was first read.

Revision ID: message_read_at
Revises: money_audit_fixes
Create Date: 2026-11-25

One change, for the messaging rebuild ("make messaging a beast"):

1. New nullable `read_at` column on messages — the moment a
   message was first marked read. Nullable because an unread
   message has never been read, and because rows predating this
   column have no read timestamp of their own.

The backfill sets read_at = created_at for every row already
marked read (is_read true): the creation time is the best
available stand-in for when it was read, and it keeps the
"read at some point" signal true for old rows. Unread rows keep
read_at NULL.

The backfill is plain SQL that runs on both SQLite and Postgres:
`WHERE is_read` is a boolean test in both (SQLite stores the
boolean as 0/1, Postgres as a real boolean).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'message_read_at'
down_revision = 'money_audit_fixes'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'messages',
        sa.Column('read_at', sa.DateTime(), nullable=True),
    )
    # Backfill: read rows were read at some point; the creation
    # time is the closest timestamp we have. Unread rows stay NULL.
    op.execute(
        'UPDATE messages SET read_at = created_at WHERE is_read'
    )


def downgrade():
    op.drop_column('messages', 'read_at')
