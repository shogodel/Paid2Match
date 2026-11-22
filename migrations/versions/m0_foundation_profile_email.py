"""M0 foundation: profile LinkedIn-style fields + email verification flags.

Revision ID: m0_foundation
Revises: recruitment_only_v2
Create Date: 2026-11-21

Profile columns (models/profile.py):
    The profile forms and routes already collect headline, linkedin_url,
    github_url, twitter_handle, remote_ok and relocation_assistance, but the
    model never declared them, so profile setup crashed with a TypeError
    (constructor rejects unmapped kwargs) and profile edits silently
    discarded the data. Migration 75314a6263e5 once added some of these
    columns, but a0872675a8ca dropped them again, so at the current head NONE
    of them exist in the database — this migration adds them back, named after
    the form fields the routes already use (twitter_handle, remote_ok) rather
    than the dropped legacy names (twitter_url, remote_preference).

User columns (models/user.py):
    email_verified / email_verified_at support signup email confirmation.
    Existing accounts are grandfathered as verified (UPDATE below) — they
    were created before verification existed, and locking them out would
    block the live user(s).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm0_foundation'
down_revision = 'recruitment_only_v2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('profiles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('headline', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('linkedin_url', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('github_url', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('twitter_handle', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('remote_ok', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('relocation_assistance', sa.Boolean(), nullable=True))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email_verified', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('email_verified_at', sa.DateTime(), nullable=True))

    # Grandfather pre-existing accounts as verified (see module docstring).
    # TRUE works on both PostgreSQL (live) and SQLite (tests).
    op.execute("UPDATE users SET email_verified = TRUE WHERE email_verified IS NULL")


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('email_verified_at')
        batch_op.drop_column('email_verified')

    with op.batch_alter_table('profiles', schema=None) as batch_op:
        batch_op.drop_column('relocation_assistance')
        batch_op.drop_column('remote_ok')
        batch_op.drop_column('twitter_handle')
        batch_op.drop_column('github_url')
        batch_op.drop_column('linkedin_url')
        batch_op.drop_column('headline')
