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
    test_app = create_app('testing')
    test_app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_file}'
    with test_app.app_context():
        flask_migrate.upgrade()
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())
        # Every table the models declare exists after the upgrade.
        expected = set(db.metadata.tables.keys())
        assert expected <= tables, f'missing tables: {expected - tables}'
        # The head revision is the Phase 3 discovery/trust migration.
        version = db.session.execute(
            text('SELECT version_num FROM alembic_version')
        ).scalar()
        assert version == 'phase3_discovery_trust'
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
    assert heads == ['phase3_discovery_trust'], f'multiple heads: {heads}'
    roots = [r for r in script.walk_revisions() if r.down_revision is None]
    assert len(roots) == 1
    assert roots[0].revision == 'baseline_create_all'
