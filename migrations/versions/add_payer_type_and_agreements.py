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
    op.add_column('bounties', sa.Column('payer_type', sa.String(20), server_default='poster'))
    op.add_column('bounties', sa.Column('third_party_payer_id', sa.String(36), sa.ForeignKey('users.id'), nullable=True))
    
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
    op.drop_column('bounties', 'third_party_payer_id')
    op.drop_column('bounties', 'payer_type')