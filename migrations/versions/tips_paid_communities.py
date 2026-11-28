"""Tips + paid communities (paywalled Spaces) — combined.

Revision ID: tips_paid_communities
Revises: perf_audit25_indexes
Create Date: 2026-11-27

Single revision combining the two feature slices (each shipped its
own revision on the same parent — tips_feature and paid_spaces —
merged here so the chain stays linear with one head):

TIPS (models/tip.py):
- tips: one row per tip. The tipper pays the full amount via Stripe
  Checkout (payment mode); the platform keeps TIP_PLATFORM_FEE_PERCENT
  (default 20%, admin-settable, separate from the hire fee) and the
  recipient's share is transferred to their Stripe Connect account
  from the Checkout charge. A tip whose recipient has no ready Connect
  account yet stays 'pending' until they connect
  (retry_pending_tips, wired into on_connect_ready). Tips are final —
  no refund path.

PAID SPACES (models/space.py, models/space_payout.py):
- spaces: owner_id (FK users, nullable — a space can only be
  switched to paid when it has an owner), is_paid, price_cents
  (monthly, cents), currency (CAD for now).
- space_memberships: stripe_subscription_id, status,
  current_period_end, cancel_at_period_end (paid-membership
  lifecycle, driven by Stripe webhooks).
- space_payouts: one row per paid invoice — the owner's share, with
  status 'pending_stripe' until it can be transferred to their
  Connect account (unique stripe_invoice_id, so a redelivered
  invoice.paid event never pays twice).

Batch mode throughout (the repo pattern — see currency_bounty):
SQLite has no ALTER support for constraints, so a plain
op.add_column with an inline FK fails the whole chain; batch mode
keeps this working on SQLite and PostgreSQL alike. NOT NULL
columns ride on a server default for the backfill, which is then
dropped so the column matches the model exactly.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'tips_paid_communities'
down_revision = 'perf_audit25_indexes'
branch_labels = None
depends_on = None


def upgrade():
    # --- Tips ---
    op.create_table(
        'tips',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tipper_id', sa.String(length=36), nullable=False),
        sa.Column('recipient_id', sa.String(length=36), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('message', sa.String(length=280), nullable=True),
        sa.Column('stripe_session_id', sa.String(length=100), nullable=True),
        sa.Column('stripe_payment_intent_id', sa.String(length=100), nullable=True),
        sa.Column('stripe_transfer_id', sa.String(length=100), nullable=True),
        sa.Column('platform_fee_cents', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['recipient_id'], ['users.id']),
        sa.ForeignKeyConstraint(['tipper_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stripe_session_id', name='uq_tips_stripe_session_id'),
    )
    op.create_index('ix_tips_tipper_id', 'tips', ['tipper_id'])
    op.create_index('ix_tips_recipient_id', 'tips', ['recipient_id'])
    op.create_index('ix_tips_stripe_session_id', 'tips', ['stripe_session_id'])
    op.create_index('ix_tips_status', 'tips', ['status'])

    # --- Paid spaces ---
    with op.batch_alter_table('spaces', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'owner_id', sa.String(36),
            sa.ForeignKey('users.id', name='fk_spaces_owner_id'),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'is_paid', sa.Boolean(), nullable=False, server_default='0',
        ))
        batch_op.add_column(sa.Column(
            'price_cents', sa.Integer(), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'currency', sa.String(3), nullable=False,
            server_default='cad',
        ))
    with op.batch_alter_table('spaces', schema=None) as batch_op:
        batch_op.alter_column(
            'is_paid', server_default=None, existing_type=sa.Boolean(),
        )
        batch_op.alter_column(
            'currency', server_default=None,
            existing_type=sa.String(3),
        )
    with op.batch_alter_table('space_memberships', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'stripe_subscription_id', sa.String(100), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'status', sa.String(20), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'current_period_end', sa.DateTime(), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'cancel_at_period_end', sa.Boolean(), nullable=False,
            server_default='0',
        ))
    with op.batch_alter_table('space_memberships', schema=None) as batch_op:
        batch_op.alter_column(
            'cancel_at_period_end', server_default=None,
            existing_type=sa.Boolean(),
        )
    op.create_index(
        'ix_space_memberships_stripe_subscription_id',
        'space_memberships', ['stripe_subscription_id'],
    )
    op.create_table(
        'space_payouts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('space_id', sa.String(36), nullable=False),
        sa.Column('owner_id', sa.String(36), nullable=False),
        sa.Column('member_id', sa.String(36), nullable=False),
        sa.Column('stripe_invoice_id', sa.String(100), nullable=False),
        sa.Column('stripe_subscription_id', sa.String(100), nullable=True),
        sa.Column('source_charge_id', sa.String(100), nullable=True),
        sa.Column('gross_cents', sa.Integer(), nullable=False),
        sa.Column('platform_fee_cents', sa.Integer(), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False, server_default='cad'),
        sa.Column(
            'status', sa.String(20), nullable=False,
            server_default='pending_stripe',
        ),
        sa.Column('stripe_transfer_id', sa.String(100), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['space_id'], ['spaces.id']),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id']),
        sa.ForeignKeyConstraint(['member_id'], ['users.id']),
        sa.UniqueConstraint(
            'stripe_invoice_id', name='uq_space_payouts_invoice_id'
        ),
    )
    op.create_index(
        'ix_space_payouts_stripe_invoice_id',
        'space_payouts', ['stripe_invoice_id'],
    )
    op.create_index(
        'ix_space_payouts_stripe_transfer_id',
        'space_payouts', ['stripe_transfer_id'],
    )


def downgrade():
    # --- Paid spaces (reverse of upgrade) ---
    op.drop_index(
        'ix_space_payouts_stripe_transfer_id', table_name='space_payouts'
    )
    op.drop_index(
        'ix_space_payouts_stripe_invoice_id', table_name='space_payouts'
    )
    op.drop_table('space_payouts')
    op.drop_index(
        'ix_space_memberships_stripe_subscription_id',
        table_name='space_memberships',
    )
    with op.batch_alter_table('space_memberships', schema=None) as batch_op:
        batch_op.drop_column('cancel_at_period_end')
        batch_op.drop_column('current_period_end')
        batch_op.drop_column('status')
        batch_op.drop_column('stripe_subscription_id')
    with op.batch_alter_table('spaces', schema=None) as batch_op:
        batch_op.drop_column('currency')
        batch_op.drop_column('price_cents')
        batch_op.drop_column('is_paid')
        batch_op.drop_column('owner_id')
    # --- Tips ---
    op.drop_index('ix_tips_status', table_name='tips')
    op.drop_index('ix_tips_stripe_session_id', table_name='tips')
    op.drop_index('ix_tips_recipient_id', table_name='tips')
    op.drop_index('ix_tips_tipper_id', table_name='tips')
    op.drop_table('tips')
