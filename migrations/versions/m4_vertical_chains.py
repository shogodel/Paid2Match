"""M4 second vertical + referral chains: services vertical, category
system, saved skills, chain attribution, chain payouts.

Revision ID: m4_vertical_chains
Revises: m3_monetization
Create Date: 2026-11-21

Columns:
    bounties.category (models/bounty.py) — category within the
    vertical (Bounty.CATEGORIES); NULL for uncategorized needs.
    bounties.skills (models/bounty.py) — comma-separated skills the
    poster is looking for. The post form collected this from day one
    but it was never saved (audit §3.10).
    pitches.via_share_id + referrals.via_share_id — the Share a
    referral came through (A shares a need post, B refers from that
    share). This is what makes the two-level payout split possible.
    payouts.kind — 'referral' (the referrer's payout) or
    'chain_share' (the sharer's cut of a two-level chain). Existing
    rows are all referrer payouts, backfilled to 'referral'.

The verticals themselves need no migration: bounty_type is a plain
string column, and the valid values live in Bounty.VALID_BOUNTY_TYPES
(recruitment + services from M4).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm4_vertical_chains'
down_revision = 'm3_monetization'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.add_column(sa.Column('category', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('skills', sa.Text(), nullable=True))

    # Named FK constraints: SQLite batch mode cannot create unnamed
    # ones (same lesson as the M2 migration fixes).
    with op.batch_alter_table('pitches', schema=None) as batch_op:
        batch_op.add_column(sa.Column('via_share_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_pitches_via_share_id', 'shares',
            ['via_share_id'], ['id'],
        )
        batch_op.create_index('ix_pitches_via_share_id', ['via_share_id'])

    with op.batch_alter_table('referrals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('via_share_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_referrals_via_share_id', 'shares',
            ['via_share_id'], ['id'],
        )
        batch_op.create_index('ix_referrals_via_share_id', ['via_share_id'])

    with op.batch_alter_table('payouts', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('kind', sa.String(length=20), nullable=True)
        )
    op.execute("UPDATE payouts SET kind = 'referral' WHERE kind IS NULL")
    with op.batch_alter_table('payouts', schema=None) as batch_op:
        batch_op.alter_column('kind', nullable=False)


def downgrade():
    with op.batch_alter_table('payouts', schema=None) as batch_op:
        batch_op.drop_column('kind')

    with op.batch_alter_table('referrals', schema=None) as batch_op:
        batch_op.drop_index('ix_referrals_via_share_id')
        batch_op.drop_constraint(
            'fk_referrals_via_share_id', type_='foreignkey'
        )
        batch_op.drop_column('via_share_id')

    with op.batch_alter_table('pitches', schema=None) as batch_op:
        batch_op.drop_index('ix_pitches_via_share_id')
        batch_op.drop_constraint(
            'fk_pitches_via_share_id', type_='foreignkey'
        )
        batch_op.drop_column('via_share_id')

    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.drop_column('skills')
        batch_op.drop_column('category')
