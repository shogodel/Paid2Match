"""Video vouches — 30-second video pitches and vouches.

Revision ID: video_vouches
Revises: phase3_discovery_trust
Create Date: 2026-11-23

New table (models/video_vouch.py):
    video_vouches — one row per uploaded video. kind='pitch' ties
    the video to a bounty (the need pitched for); kind='vouch'
    ties it to a target user (the person vouched for). The
    kind/target/bounty rules are enforced by the model
    (VideoVouch.validate), not by database constraints, so the
    table keeps both FKs nullable. filename is a random uuid
    (unguessable playback URL) and unique. The file itself lives
    under VIDEO_VOUCH_UPLOAD_FOLDER — see
    docs/VIDEO_VOUCH_UPLOADS.md for the volume the live container
    needs, or uploads are wiped on rebuild.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'video_vouches'
down_revision = 'currency_bounty'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'video_vouches',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('target_user_id', sa.String(length=36), nullable=True),
        sa.Column('bounty_id', sa.String(length=36), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['target_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['bounty_id'], ['bounties.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('filename', name='uq_video_vouches_filename'),
    )
    op.create_index(
        'ix_video_vouches_user_id', 'video_vouches', ['user_id'],
    )
    op.create_index(
        'ix_video_vouches_target_user_id', 'video_vouches',
        ['target_user_id'],
    )
    op.create_index(
        'ix_video_vouches_bounty_id', 'video_vouches', ['bounty_id'],
    )


def downgrade():
    op.drop_index('ix_video_vouches_bounty_id', table_name='video_vouches')
    op.drop_index(
        'ix_video_vouches_target_user_id', table_name='video_vouches')
    op.drop_index('ix_video_vouches_user_id', table_name='video_vouches')
    op.drop_table('video_vouches')
