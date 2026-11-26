"""M2 migration tests — the full chain must upgrade AND downgrade on a
fresh SQLite database (M1 finding: the old base migration assumed the
`bounties` table already existed, so a fresh DB could not run the
chain at all).

These tests build their own app on a temp file database (a file, not
:memory:, because Flask-Migrate opens its own connection).
"""
import flask_migrate
from sqlalchemy import inspect, text

from app import create_app
from extensions import db


def test_full_chain_upgrades_and_downgrades(tmp_path):
    db_file = tmp_path / 'migration_chain.db'
    # The URI must reach create_app() via config_overrides:
    # Flask-SQLAlchemy creates its engines eagerly in db.init_app(),
    # so setting SQLALCHEMY_DATABASE_URI on the app afterwards is
    # silently ignored and the migration would run against the
    # shared test database instead of this tmp file.
    test_app = create_app('testing', config_overrides={
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_file}',
    })
    with test_app.app_context():
        flask_migrate.upgrade()
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())
        # Every table the models declare exists after the upgrade.
        expected = set(db.metadata.tables.keys())
        assert expected <= tables, f'missing tables: {expected - tables}'
        # The head revision is the message_read_at
        # migration.
        version = db.session.execute(
            text('SELECT version_num FROM alembic_version')
        ).scalar()
        assert version == 'message_read_at'
        # Prove the migration really ran on THIS file (not the
        # shared test DB): read the version straight out of the
        # tmp file with a fresh sqlite3 connection, bypassing the
        # app engine entirely.
        import sqlite3
        with sqlite3.connect(db_file) as conn:
            file_version = conn.execute(
                'SELECT version_num FROM alembic_version'
            ).fetchone()[0]
        assert file_version == 'message_read_at'
        # Columns match the models exactly for every table.
        for table in expected:
            actual_cols = {c['name'] for c in inspector.get_columns(table)}
            model_cols = {c.name for c in db.metadata.tables[table].columns}
            assert actual_cols == model_cols, (
                f'{table}: missing {model_cols - actual_cols}, '
                f'extra {actual_cols - model_cols}'
            )
        # The M2 tables exist with their key columns.
        assert 'connect_accounts' in tables
        assert 'payouts' in tables
        assert 'reviews' in tables
        assert 'verification_requests' in tables
        assert 'founding_referrer' in {
            c['name'] for c in inspector.get_columns('users')
        }
        assert 'responded_at' in {
            c['name'] for c in inspector.get_columns('pitches')
        }
        assert 'outcome' in {
            c['name'] for c in inspector.get_columns('disputes')
        }
        # The M3 tables exist with their key columns.
        assert 'bumps' in tables
        assert 'bump_pack_purchases' in tables
        assert 'boosts' in tables
        assert 'recurring_bumps' in tables
        assert 'pro_subscriptions' in tables
        assert 'linked_accounts' in tables
        assert 'endorsements' in tables
        assert 'pledges' in tables
        assert 'post_views' in tables
        assert 'search_appearances' in tables
        assert 'bumped_at' in {
            c['name'] for c in inspector.get_columns('posts')
        }
        assert 'profile_view_names' in {
            c['name'] for c in inspector.get_columns('profiles')
        }
        # The M4 columns exist.
        bounty_cols = {c['name'] for c in inspector.get_columns('bounties')}
        assert 'category' in bounty_cols
        assert 'skills' in bounty_cols
        assert 'via_share_id' in {
            c['name'] for c in inspector.get_columns('pitches')
        }
        assert 'via_share_id' in {
            c['name'] for c in inspector.get_columns('referrals')
        }
        assert 'kind' in {
            c['name'] for c in inspector.get_columns('payouts')
        }
        # The Phase 2 tables exist with their key columns.
        assert 'spaces' in tables
        assert 'space_memberships' in tables
        assert 'polls' in tables
        assert 'poll_options' in tables
        assert 'poll_votes' in tables
        assert 'bookmarks' in tables
        assert 'space_id' in {
            c['name'] for c in inspector.get_columns('posts')
        }
        assert 'notification_level' in {
            c['name'] for c in inspector.get_columns('space_memberships')
        }
        # The Phase 3 tables/columns exist.
        assert 'stage_reward_events' in tables
        assert 'stage_submit_reward' in bounty_cols
        assert 'stage_interview_reward' in bounty_cols
        assert 'stage_hire_reward' in bounty_cols
        assert 'status' in {
            c['name'] for c in inspector.get_columns('stage_reward_events')
        }
        # The employer pledge table exists with its key columns.
        assert 'employer_pledges' in tables
        assert 'org_name' in {
            c['name'] for c in inspector.get_columns('employer_pledges')
        }
        assert 'decided_by_id' in {
            c['name'] for c in inspector.get_columns('employer_pledges')
        }
        # The multi-currency column exists on bounties.
        assert 'currency' in bounty_cols
        # The video vouches table exists with its key columns.
        assert 'video_vouches' in tables
        assert 'filename' in {
            c['name'] for c in inspector.get_columns('video_vouches')
        }
        assert 'duration_seconds' in {
            c['name'] for c in inspector.get_columns('video_vouches')
        }
        # The nice URL columns exist.
        assert 'slug' in bounty_cols
        assert 'shortcode' in bounty_cols
        assert 'handle' in {
            c['name'] for c in inspector.get_columns('users')
        }
        # The intro bounty tables exist with their key columns.
        assert 'intro_bounties' in tables
        assert 'intro_claims' in tables
        assert 'intro_reward_events' in tables
        assert 'target_name' in {
            c['name'] for c in inspector.get_columns('intro_bounties')
        }
        assert 'currency' in {
            c['name'] for c in inspector.get_columns('intro_bounties')
        }
        assert 'decided_at' in {
            c['name'] for c in inspector.get_columns('intro_claims')
        }
        assert 'claim_id' in {
            c['name']
            for c in inspector.get_columns('intro_reward_events')
        }
        # The staked vouches table exists with its key columns.
        assert 'staked_vouches' in tables
        assert 'voucher_id' in {
            c['name'] for c in inspector.get_columns('staked_vouches')
        }
        assert 'resolved_by_id' in {
            c['name'] for c in inspector.get_columns('staked_vouches')
        }
        # The win stories table exists with its key columns.
        assert 'win_stories' in tables
        assert 'role' in {
            c['name'] for c in inspector.get_columns('win_stories')
        }
        assert 'body' in {
            c['name'] for c in inspector.get_columns('win_stories')
        }
        # The money-audit table exists with its key columns, and the
        # duplicate-proof unique constraints exist on matches and
        # payouts.
        assert 'stripe_event_logs' in tables
        assert 'event_id' in {
            c['name'] for c in inspector.get_columns('stripe_event_logs')
        }
        assert 'event_type' in {
            c['name'] for c in inspector.get_columns('stripe_event_logs')
        }
        match_uniques = {
            c['name']
            for c in inspector.get_unique_constraints('matches')
        }
        assert 'uq_matches_pitch_id' in match_uniques
        payout_uniques = {
            c['name']
            for c in inspector.get_unique_constraints('payouts')
        }
        assert 'uq_payouts_match_kind' in payout_uniques

        # The full chain downgrades cleanly back to base.
        flask_migrate.downgrade(revision='base')
        tables = set(inspect(db.engine).get_table_names())
        assert tables == {'alembic_version'}, f'leftover tables: {tables}'


def test_baseline_is_the_chain_root(tmp_path):
    """The baseline migration is the single root (down_revision None)
    and fa6ff45fcb16 now follows it — no second head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    import os
    cfg = Config()
    cfg.set_main_option(
        'script_location',
        os.path.join(os.path.dirname(os.path.dirname(__file__)), 'migrations'),
    )
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert heads == ['message_read_at'], (
        f'multiple heads: {heads}'
    )
    roots = [r for r in script.walk_revisions() if r.down_revision is None]
    assert len(roots) == 1
    assert roots[0].revision == 'baseline_create_all'


def test_services_bounty_inserts_on_migrated_db(tmp_path):
    """Regression: recruitment_only_v2 added a CHECK constraint
    (bounty_type = 'recruitment') that M4's services vertical never
    dropped, so on a migrated database a services bounty failed at
    the DB layer — while the rest of the suite (db.create_all, no
    constraint) happily created them. A services bounty must insert
    on a database that ran the full chain."""
    db_file = tmp_path / 'services_migrated.db'
    test_app = create_app('testing', config_overrides={
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_file}',
    })
    with test_app.app_context():
        flask_migrate.upgrade()
        from models.user import User
        from models.bounty import Bounty
        user = User(
            email='services-mig@example.com', full_name='Services Mig',
            role='user', is_active=True, email_verified=True,
        )
        db.session.add(user)
        db.session.commit()
        bounty = Bounty(
            poster_id=user.id,
            title='Need a plumber for a bathroom refit in Laval',
            description='Looking for a licensed plumber for a full '
                        'bathroom refit, available next month.',
            bounty_type='services',
            bounty_direction='seeking_opportunity',
            status='open',
            payment_status='released',
            location='Laval, QC',
        )
        db.session.add(bounty)
        db.session.commit()
        assert db.session.get(Bounty, bounty.id).bounty_type == 'services'
        # And the constraint itself is gone from the schema.
        ddl = db.session.execute(text(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'bounties'"
        )).scalar()
        assert 'ck_bounties_bounty_type' not in ddl


def test_upgrade_tolerates_live_db_without_recruitment_check(tmp_path):
    """Regression (2026-11-24 deploy): the live database never got
    ck_bounties_bounty_type — its alembic version was stamped to
    recruitment_only_v2 by direct SQL (phantom-revision fix), so
    that migration never ran there. The drop migration must not
    fail when the constraint is absent."""
    db_file = tmp_path / 'live_db_no_check.db'
    test_app = create_app('testing', config_overrides={
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_file}',
    })
    with test_app.app_context():
        flask_migrate.upgrade(revision='wins_wall')
        # Simulate the live DB: remove the constraint the stamp
        # skipped creating, then run the rest of the chain.
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        conn = db.engine.connect()
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        with ops.batch_alter_table('bounties') as batch_op:
            batch_op.drop_constraint(
                'ck_bounties_bounty_type', type_='check'
            )
        conn.close()
        flask_migrate.upgrade()
        version = db.session.execute(
            text('SELECT version_num FROM alembic_version')
        ).scalar()
        assert version == 'message_read_at'
        # And a services bounty still inserts on the upgraded DB.
        from models.user import User
        from models.bounty import Bounty
        user = User(
            email='no-check-mig@example.com', full_name='No Check Mig',
            role='user', is_active=True, email_verified=True,
        )
        db.session.add(user)
        db.session.commit()
        bounty = Bounty(
            poster_id=user.id,
            title='Need an electrician for a panel upgrade in Laval',
            description='Looking for a licensed electrician for a '
                        'panel upgrade, available next month.',
            bounty_type='services',
            bounty_direction='seeking_opportunity',
            status='open',
            payment_status='released',
            location='Laval, QC',
        )
        db.session.add(bounty)
        db.session.commit()
        assert db.session.get(Bounty, bounty.id).bounty_type == 'services'
