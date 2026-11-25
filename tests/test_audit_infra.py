"""Audit regressions (2026-11-24, infra area): config, migrations,
Dockerfile entrypoint and the video-upload size override.

Each test here pins one bug found by the audit so it cannot come
back silently.
"""
import io
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app
from extensions import db


# ------------------------------------------------------- create_app / config


def test_config_overrides_bind_the_engine(tmp_path):
    """A URI passed via config_overrides is the one the engine uses
    (the migration tests depend on this for their tmp databases)."""
    db_file = tmp_path / 'override.db'
    test_app = create_app('testing', config_overrides={
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_file}',
    })
    with test_app.app_context():
        assert str(db.engine.url) == f'sqlite:///{db_file}'


def test_post_create_app_uri_change_is_ignored(tmp_path):
    """Documents the trap config_overrides exists for:
    Flask-SQLAlchemy creates its engines eagerly in db.init_app(),
    so changing SQLALCHEMY_DATABASE_URI after create_app() returns
    does NOT move the engine. (This is how the migration tests used
    to run against the shared test DB while believing they used a
    tmp file.)"""
    test_app = create_app('testing')
    test_app.config['SQLALCHEMY_DATABASE_URI'] = (
        f"sqlite:///{tmp_path}/ignored.db"
    )
    with test_app.app_context():
        assert 'ignored.db' not in str(db.engine.url)


def test_production_refuses_to_boot_without_secret_key(monkeypatch):
    """Production/staging without SECRET_KEY must fail at boot, not
    start and 500 on the first session-touching request."""
    import config as config_module
    monkeypatch.setattr(
        config_module.ProductionConfig, 'SECRET_KEY', None
    )
    monkeypatch.setattr(
        config_module.StagingConfig, 'SECRET_KEY', None
    )
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        create_app('production')
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        create_app('staging')


def test_production_boots_with_secret_key():
    """The fail-fast above must not break a properly configured
    production app (SECRET_KEY comes from the test env)."""
    test_app = create_app('production', config_overrides={
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
    })
    assert test_app.config['DEBUG'] is False
    assert test_app.config['SESSION_COOKIE_SECURE'] is True


def test_no_dead_stripe_or_session_config():
    """Stripe keys live in AdminSettings (DB), not the environment,
    and sessions are Flask's default signed cookies (Flask-Session
    is not installed) — the dead STRIPE_* env settings and
    SESSION_TYPE must not come back."""
    from config import Config
    assert not hasattr(Config, 'STRIPE_API_KEY')
    assert not hasattr(Config, 'STRIPE_PUBLISHABLE_KEY')
    assert not hasattr(Config, 'STRIPE_WEBHOOK_SECRET')
    assert not hasattr(Config, 'SESSION_TYPE')
    # The kill-switch is the one live Stripe setting.
    assert hasattr(Config, 'STRIPE_ENABLED')


# ------------------------------------------------------- homepage stats cache


def test_homepage_stats_cache_expires(app, client, sample_user):
    """The homepage stats cache is 5 minutes, not forever: the old
    code stored no timestamp, so the counts froze until restart."""
    from models.user import User
    from werkzeug.security import generate_password_hash

    client.get('/')
    assert app.config['homepage_stats']['total_users'] == 1
    with app.app_context():
        db.session.add(User(
            email='second-stats@example.com', full_name='Second Stats',
            password_hash=generate_password_hash('SecondPass123!'),
            role='user', is_active=True, email_verified=True,
        ))
        db.session.commit()
    # Still cached — a second request inside the TTL sees old stats.
    client.get('/')
    assert app.config['homepage_stats']['total_users'] == 1
    # Past the TTL the next request recomputes.
    app.config['homepage_stats_cached_at'] = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    )
    client.get('/')
    assert app.config['homepage_stats']['total_users'] == 2


# ------------------------------------------------------- Dockerfile / wsgi


def test_dockerfile_serves_wsgi_app():
    """The image must serve wsgi:app (production by default), not
    app:create_app() (development by default when FLASK_ENV is
    unset — DEBUG on, secure cookies off)."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo_root, 'Dockerfile')) as f:
        dockerfile = f.read()
    cmd_lines = [line for line in dockerfile.splitlines()
                 if line.strip().startswith('CMD')]
    assert len(cmd_lines) == 1
    assert 'wsgi:app' in cmd_lines[0]
    assert 'create_app()' not in cmd_lines[0]


def test_wsgi_defaults_to_production(tmp_path):
    """With FLASK_ENV unset, importing wsgi must build the
    production app (DEBUG off, secure session cookies). Runs in a
    subprocess so the import's side effects stay isolated; cwd is a
    tmp dir so no .env can sneak FLASK_ENV back in."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env.pop('FLASK_ENV', None)
    env['SECRET_KEY'] = 'wsgi-default-test-key'
    env['DATABASE_URL'] = f"sqlite:///{tmp_path / 'wsgi.db'}"
    env['PYTHONPATH'] = repo_root
    result = subprocess.run(
        [sys.executable, '-c',
         'import wsgi; '
         'print(wsgi.app.config["DEBUG"], '
         'wsgi.app.config["SESSION_COOKIE_SECURE"])'],
        env=env, cwd=str(tmp_path), capture_output=True, text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.strip() == 'False True'


# ------------------------------------------------------- video upload override


@pytest.fixture
def audit_upload_dir(app, tmp_path):
    """Point the video-vouch upload folder at a tmp dir."""
    folder = tmp_path / 'video-vouches'
    app.config['VIDEO_VOUCH_UPLOAD_FOLDER'] = str(folder)
    return folder


def _make_real_video(path, seconds):
    """A real mp4 of `seconds` seconds (silent test pattern)."""
    subprocess.run(
        ['ffmpeg', '-y', '-v', 'error',
         '-f', 'lavfi', '-i',
         f'testsrc=duration={seconds}:size=64x64:rate=10',
         '-t', str(seconds), '-pix_fmt', 'yuv420p',
         '-c:v', 'libx264', str(path)],
        check=True, timeout=120,
    )


@pytest.fixture(scope='module')
def audit_real_video_bytes(tmp_path_factory):
    """Bytes of a real 2-second mp4, generated once per module."""
    if not shutil.which('ffmpeg'):
        pytest.skip('needs ffmpeg')
    path = tmp_path_factory.mktemp('video') / 'short.mp4'
    _make_real_video(path, 2)
    return path.read_bytes()


def test_video_upload_above_global_cap_is_accepted(
        app, client, login_as, sample_hunter, sample_bounty,
        audit_upload_dir, audit_real_video_bytes):
    """The video route raises the per-request limit to
    VIDEO_VOUCH_MAX_BYTES: a file above the global 16MB
    MAX_CONTENT_LENGTH must still upload. Proven with shrunk
    limits bracketing a real video's size, so the test need not
    send megabytes. A real video is required: since the audit,
    the fail-closed ffprobe check rejects fake bytes."""
    from models.video_vouch import VideoVouch
    size = len(audit_real_video_bytes)
    app.config['MAX_CONTENT_LENGTH'] = size // 2
    app.config['VIDEO_VOUCH_MAX_BYTES'] = size * 2
    login_as(client, 'hunter@example.com')
    r = client.post(
        '/video-vouches/upload',
        data={
            'video': (io.BytesIO(audit_real_video_bytes), 'pitch.mp4'),
            'kind': 'pitch',
            'bounty_id': sample_bounty.id,
        },
        content_type='multipart/form-data',
    )
    # 302 = accepted and redirected; a broken override would 413.
    assert r.status_code == 302
    with app.app_context():
        assert VideoVouch.query.count() == 1


def test_global_cap_still_applies_off_the_video_route(
        app, logged_in_user, sample_user):
    """The 50MB override is scoped to the video upload route: other
    routes still enforce the global MAX_CONTENT_LENGTH."""
    app.config['MAX_CONTENT_LENGTH'] = 1024
    r = logged_in_user.post('/needs/post', data={
        'title': 'A title long enough to pass validation here',
        'description': 'x' * 2048,
        'bounty_type': 'recruitment',
        'bounty_direction': 'seeking_opportunity',
    })
    assert r.status_code == 413


# ------------------------------------------------------- migration downgrade


def test_fa6ff45fcb16_downgrade_with_free_bounty(tmp_path):
    """Regression: fa6ff45fcb16's downgrade tightened reward_amount
    back to NOT NULL without backfilling, so rolling back failed on
    any database holding a free bounty (reward_amount NULL). The
    downgrade must now succeed and the free bounty survives (with
    reward 0)."""
    import flask_migrate
    from sqlalchemy import text

    db_file = tmp_path / 'downgrade.db'
    test_app = create_app('testing', config_overrides={
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_file}',
    })
    with test_app.app_context():
        flask_migrate.upgrade(revision='fa6ff45fcb16')
        db.session.execute(text(
            "INSERT INTO users (id, email, full_name, role, is_active) "
            "VALUES ('u1', 'dg@example.com', 'DG Proof', 'user', 1)"
        ))
        db.session.execute(text(
            "INSERT INTO bounties (id, poster_id, title, description, "
            "bounty_type, bounty_direction, reward_amount, status, "
            "payment_status) VALUES ('b1', 'u1', 'Free bounty', 'desc', "
            "'recruitment', 'seeking_opportunity', NULL, 'open', "
            "'released')"
        ))
        db.session.commit()
        flask_migrate.downgrade(revision='baseline_create_all')
        version = db.session.execute(
            text('SELECT version_num FROM alembic_version')
        ).scalar()
        assert version == 'baseline_create_all'
        reward = db.session.execute(
            text("SELECT reward_amount FROM bounties WHERE id = 'b1'")
        ).scalar()
        assert float(reward) == 0
