"""Nice URLs: slugs + shortcodes for needs, handles for profiles.

Revision ID: nice_urls
Revises: phase3_discovery_trust
Create Date: 2026-11-23

New columns (models/bounty.py, models/user.py):
    bounties.slug (String 120) — set once from the title, never
        changes, so a need's URL stays stable across edits.
    bounties.shortcode (String 6, unique) — the stable identifier
        the detail route looks up; /needs/<slug>-<shortcode>.
    users.handle (String 30, unique) — /profile/<handle>; the
        /profile/<uuid> URL 301s to it.

The columns are added nullable, backfilled in Python from the
existing rows (few rows live, but the backfill is written
generally: slugs from titles, random shortcodes checked against
the rows already assigned, handles from full names with digits
appended on collision), and only then tightened to NOT NULL with
unique indexes. The backfill imports the same pure functions the
models use (utils/slugs.py) so a migrated row gets exactly what a
new row would get.
"""

from alembic import op
import sqlalchemy as sa

from utils.slugs import handle_from_name, slugify, unique_shortcode


# revision identifiers, used by Alembic.
revision = 'nice_urls'
down_revision = 'video_vouches'
branch_labels = None
depends_on = None


# Lightweight Core tables for the backfill (the models are not
# imported — a migration must not depend on the current model code).
_bounties = sa.table(
    'bounties',
    sa.column('id', sa.String),
    sa.column('title', sa.String),
    sa.column('slug', sa.String),
    sa.column('shortcode', sa.String),
)
_users = sa.table(
    'users',
    sa.column('id', sa.String),
    sa.column('full_name', sa.String),
    sa.column('handle', sa.String),
)


def _backfill():
    """Fill slug/shortcode/handle for every existing row."""
    bind = op.get_bind()

    taken_codes = set()
    for row in bind.execute(
        sa.select(_bounties.c.id, _bounties.c.title)
    ):
        slug = slugify(row.title or '')
        code = unique_shortcode(lambda c: c in taken_codes)
        taken_codes.add(code)
        bind.execute(
            _bounties.update()
            .where(_bounties.c.id == row.id)
            .values(slug=slug, shortcode=code)
        )

    taken_handles = set()
    for row in bind.execute(
        sa.select(_users.c.id, _users.c.full_name)
    ):
        handle = handle_from_name(
            row.full_name or '', taken=lambda h: h in taken_handles
        )
        taken_handles.add(handle)
        bind.execute(
            _users.update()
            .where(_users.c.id == row.id)
            .values(handle=handle)
        )


def upgrade():
    # M0–M4 convention: batch_alter_table for every column add
    # (SQLite cannot ALTER ADD CONSTRAINT; batch mode recreates the
    # table).
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.add_column(sa.Column('slug', sa.String(120), nullable=True))
        batch_op.add_column(
            sa.Column('shortcode', sa.String(6), nullable=True)
        )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('handle', sa.String(30), nullable=True)
        )

    _backfill()

    # Every row now has values — tighten to NOT NULL and add the
    # unique indexes the models declare.
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.alter_column('slug', nullable=False)
        batch_op.alter_column('shortcode', nullable=False)
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('handle', nullable=False)
    op.create_index(
        'ix_bounties_slug', 'bounties', ['slug'], unique=False
    )
    op.create_index(
        'ix_bounties_shortcode', 'bounties', ['shortcode'], unique=True
    )
    op.create_index(
        'ix_users_handle', 'users', ['handle'], unique=True
    )


def downgrade():
    op.drop_index('ix_users_handle', table_name='users')
    op.drop_index('ix_bounties_shortcode', table_name='bounties')
    op.drop_index('ix_bounties_slug', table_name='bounties')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('handle')
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.drop_column('shortcode')
        batch_op.drop_column('slug')
