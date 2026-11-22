"""Add payer_type and payment agreements tables

Revision ID: add_payer_type_and_agreements
Revises: d12676eac83c
Create Date: 2024-04-24
"""
from alembic import op
import sqlalchemy as sa


revision = 'add_payer_type_and_agreements'
down_revision = 'd12676eac83c'
branch_labels = None
depends_on = None


def upgrade():
    # M2 fix: the original used plain op.add_column with a ForeignKey,
    # which SQLite rejects ("No support for ALTER of constraints").
    # Batch mode recreates the table, so the FK is added cleanly — but
    # batch mode requires named constraints, hence the explicit name.
    # The live (PostgreSQL) database already ran this migration — this
    # change only affects fresh databases.
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.add_column(sa.Column('payer_type', sa.String(20), server_default='poster'))
        batch_op.add_column(sa.Column('third_party_payer_id', sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            'fk_bounties_third_party_payer_id', 'users',
            ['third_party_payer_id'], ['id'],
        )

    op.create_table(
        'bounty_payment_agreements',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('bounty_id', sa.String(36), sa.ForeignKey('bounties.id'), nullable=False, index=True),
        sa.Column('inviter_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('payer_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.String(20), server_default='pending'),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('invited_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('responded_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('bounty_payment_agreements')
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.drop_constraint('fk_bounties_third_party_payer_id', type_='foreignkey')
        batch_op.drop_column('third_party_payer_id')
        batch_op.drop_column('payer_type')