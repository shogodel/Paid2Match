"""Message.read_at migration tests — the message_read_at migration
must add the column AND backfill it on a database that already has
message rows, on SQLite (the migration suite runs the chain on
SQLite; the backfill SQL is plain enough to run on Postgres too).

These tests build their own app on a temp file database (a file,
not :memory:, because Flask-Migrate opens its own connection) —
the same approach as tests/test_migrations.py.
"""
import uuid
from datetime import datetime

import flask_migrate
from sqlalchemy import inspect, text

from app import create_app
from extensions import db

REVISION = 'message_read_at'
PARENT_REVISION = 'money_audit_fixes'


def _make_app(db_file):
    # The URI must reach create_app() via config_overrides:
    # Flask-SQLAlchemy creates its engines eagerly in db.init_app(),
    # so setting SQLALCHEMY_DATABASE_URI on the app afterwards is
    # silently ignored and the migration would run against the
    # shared test database instead of this tmp file.
    return create_app('testing', config_overrides={
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_file}',
    })


def _make_users():
    """Insert the two fixture users with raw SQL and an explicit
    column list: the DB may be at an older revision than the models
    (test_migration_backfills_read_at stops at money_audit_fixes),
    so an ORM insert would reference columns the table does not
    have yet — e.g. users.founding_referrer_since, added by the
    later founding_window migration. Returns namespace objects
    with .id (callers only use the ids)."""
    from types import SimpleNamespace

    now = datetime(2026, 11, 20, 9, 0, 0).isoformat(sep=' ')
    sender_id = str(uuid.uuid4())
    recipient_id = str(uuid.uuid4())
    db.session.execute(text(
        'INSERT INTO users (id, email, full_name, handle, role, '
        'is_active, email_verified, founding_referrer, created_at) '
        'VALUES (:id, :email, :full_name, :handle, :role, '
        ':is_active, :email_verified, :founding_referrer, :created_at)'
    ), [
        {
            'id': sender_id, 'email': 'read-at-sender@example.com',
            'full_name': 'Read At Sender', 'handle': 'read.at.sender',
            'role': 'user', 'is_active': 1, 'email_verified': 1,
            'founding_referrer': 0, 'created_at': now,
        },
        {
            'id': recipient_id, 'email': 'read-at-recipient@example.com',
            'full_name': 'Read At Recipient',
            'handle': 'read.at.recipient',
            'role': 'user', 'is_active': 1, 'email_verified': 1,
            'founding_referrer': 0, 'created_at': now,
        },
    ])
    db.session.commit()
    return (SimpleNamespace(id=sender_id),
            SimpleNamespace(id=recipient_id))


def test_migration_backfills_read_at(tmp_path):
    """Pre-migration rows: a read message gets
    read_at = created_at, an unread message keeps read_at NULL."""
    db_file = tmp_path / 'message_read_at.db'
    test_app = _make_app(db_file)
    with test_app.app_context():
        # Stop one migration short of the head, so the messages
        # table exists WITHOUT read_at — the state a live database
        # is in before this migration runs.
        flask_migrate.upgrade(revision=PARENT_REVISION)
        assert 'read_at' not in {
            c['name'] for c in inspect(db.engine).get_columns('messages')
        }
        sender, recipient = _make_users()
        # Insert through raw SQL: the ORM model already declares
        # read_at, so an ORM insert would reference a column the
        # table does not have yet. Timestamps go in as ISO strings
        # (passing datetimes as raw params trips a Python 3.12
        # sqlite3 DeprecationWarning).
        read_created = datetime(2026, 11, 20, 10, 0, 0)
        unread_created = datetime(2026, 11, 21, 15, 30, 0)
        read_id = str(uuid.uuid4())
        unread_id = str(uuid.uuid4())
        db.session.execute(text(
            'INSERT INTO messages (id, sender_id, recipient_id, '
            'content, is_read, created_at) '
            'VALUES (:id, :sender_id, :recipient_id, :content, '
            ':is_read, :created_at)'
        ), [
            {
                'id': read_id, 'sender_id': sender.id,
                'recipient_id': recipient.id,
                'content': 'This one was read.', 'is_read': 1,
                'created_at': read_created.isoformat(sep=' '),
            },
            {
                'id': unread_id, 'sender_id': sender.id,
                'recipient_id': recipient.id,
                'content': 'This one was not.', 'is_read': 0,
                'created_at': unread_created.isoformat(sep=' '),
            },
        ])
        db.session.commit()

        # Run the migration under test — to that revision exactly, not
        # to head: a later migration (perf_audit25_indexes) now sits
        # after it, and the version assertion below is about this one.
        flask_migrate.upgrade(revision=REVISION)
        version = db.session.execute(
            text('SELECT version_num FROM alembic_version')
        ).scalar()
        assert version == REVISION

        # The column exists now.
        assert 'read_at' in {
            c['name'] for c in inspect(db.engine).get_columns('messages')
        }
        # The backfill, checked at the SQL level.
        read_row = db.session.execute(text(
            'SELECT read_at, created_at FROM messages WHERE id = :id'
        ), {'id': read_id}).one()
        assert read_row.read_at == read_row.created_at
        unread_row = db.session.execute(text(
            'SELECT read_at FROM messages WHERE id = :id'
        ), {'id': unread_id}).one()
        assert unread_row.read_at is None

        # And through the model: read_at round-trips as a datetime
        # equal to created_at for the read row, None for the unread.
        from models.message import Message
        read_msg = db.session.get(Message, read_id)
        assert read_msg.read_at == read_created
        assert read_msg.to_dict()['read_at'] == read_created.isoformat()
        unread_msg = db.session.get(Message, unread_id)
        assert unread_msg.read_at is None
        assert unread_msg.to_dict()['read_at'] is None


def test_new_message_read_at_defaults_none(tmp_path):
    """A message created after the migration has read_at None until
    something sets it, and to_dict() renders it as ISO or None."""
    db_file = tmp_path / 'message_read_at_new.db'
    test_app = _make_app(db_file)
    with test_app.app_context():
        flask_migrate.upgrade()
        sender, recipient = _make_users()
        from models.message import Message
        msg = Message(
            sender_id=sender.id, recipient_id=recipient.id,
            content='Fresh message, never read.',
        )
        db.session.add(msg)
        db.session.commit()
        assert msg.read_at is None
        assert db.session.get(Message, msg.id).read_at is None
        assert msg.to_dict()['read_at'] is None

        # Setting it renders as ISO format in to_dict().
        stamped = datetime(2026, 11, 25, 12, 0, 0)
        msg.read_at = stamped
        db.session.commit()
        assert db.session.get(Message, msg.id).to_dict()['read_at'] == (
            stamped.isoformat()
        )


def test_downgrade_removes_read_at_column(tmp_path):
    """Downgrading back to money_audit_fixes drops the column and
    leaves the rest of the messages table intact."""
    db_file = tmp_path / 'message_read_at_down.db'
    test_app = _make_app(db_file)
    with test_app.app_context():
        flask_migrate.upgrade()
        assert 'read_at' in {
            c['name'] for c in inspect(db.engine).get_columns('messages')
        }
        flask_migrate.downgrade(revision=PARENT_REVISION)
        version = db.session.execute(
            text('SELECT version_num FROM alembic_version')
        ).scalar()
        assert version == PARENT_REVISION
        cols = {
            c['name'] for c in inspect(db.engine).get_columns('messages')
        }
        assert 'read_at' not in cols
        assert {'id', 'sender_id', 'recipient_id', 'content',
                'is_read', 'created_at'} <= cols
        # And the migration re-applies cleanly after the downgrade.
        flask_migrate.upgrade()
        assert 'read_at' in {
            c['name'] for c in inspect(db.engine).get_columns('messages')
        }
