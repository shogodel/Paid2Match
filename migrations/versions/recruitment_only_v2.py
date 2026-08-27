from alembic import op

revision = 'recruitment_only_v2'
down_revision = 'recruitment_only_v1'
branch_labels = None
depends_on = None


def upgrade():
    # Defense-in-depth: guarantee at the database layer that bounty_type can
    # only ever be 'recruitment' (the migration backfill already normalized
    # existing rows). SQLite needs batch_alter_table to add a constraint.
    with op.batch_alter_table('bounties') as batch_op:
        batch_op.create_check_constraint(
            'ck_bounties_bounty_type',
            "bounty_type = 'recruitment'",
        )


def downgrade():
    with op.batch_alter_table('bounties') as batch_op:
        batch_op.drop_constraint('ck_bounties_bounty_type', type_='check')
