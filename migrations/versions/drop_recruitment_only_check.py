"""Drop the recruitment-only CHECK constraint on bounties.

Revision ID: drop_recruitment_only_check
Revises: wins_wall
Create Date: 2026-11-24

recruitment_only_v2 added ck_bounties_bounty_type
(bounty_type = 'recruitment') when the platform was recruitment-only.
M4 added the services vertical (Bounty.VALID_BOUNTY_TYPES =
['recruitment', 'services']) and the post form offers it — but no
migration ever dropped the constraint, so on any database migrated
through the chain (including the live PostgreSQL database) creating
a services bounty fails at the DB layer with a CHECK violation.

The test suite never saw this: conftest builds the schema with
db.create_all(), which has no such constraint, so services bounties
insert fine in tests while the feature is broken on migrated DBs.

The model @validates on Bounty.bounty_type is now the only guard on
valid values — the constraint is redundant with it for recruitment
and wrong for services, so it goes.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = 'drop_recruitment_only_check'
down_revision = 'wins_wall'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.drop_constraint(
            'ck_bounties_bounty_type', type_='check'
        )


def downgrade():
    # Services rows cannot satisfy the constraint, so they go back
    # to 'recruitment' first — otherwise re-creating the CHECK fails
    # on any database that holds a services bounty.
    op.execute(
        "UPDATE bounties SET bounty_type = 'recruitment' "
        "WHERE bounty_type != 'recruitment'"
    )
    with op.batch_alter_table('bounties', schema=None) as batch_op:
        batch_op.create_check_constraint(
            'ck_bounties_bounty_type',
            "bounty_type = 'recruitment'",
        )
