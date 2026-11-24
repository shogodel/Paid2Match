"""Human-reviewed employer pledge tests (models/employer_pledge.py).

Covers the public register at /pledge, the pledge form, the
one-active-pledge rule, admin approve/revoke (with audit log), and
the "Human-Reviewed" badge on profiles and bounty detail.

This is the human-reviewed EmployerPledge — NOT the M3
self-published Pledge (models/endorsement.py), which has its own
tests in test_phase3.py / test_trust_m3.py.
"""
import pytest

from extensions import db
from models.audit_log import AuditLog
from models.employer_pledge import EmployerPledge
from models.user import User

PLEDGE_TEXT = (
    'We pledge that no AI screens our applicants: '
    'a human reads every application.'
)


def _make_pledge(user_id, org_name, status='pending'):
    """Create an EmployerPledge row directly."""
    pledge = EmployerPledge(
        user_id=user_id, org_name=org_name, status=status
    )
    db.session.add(pledge)
    db.session.commit()
    return pledge


# ---------------------------------------------------------------- register


def test_register_lists_approved_only(
    client, app, sample_user, second_user, third_user
):
    """Approved pledges show; pending and revoked pledges do not."""
    with app.app_context():
        _make_pledge(sample_user.id, 'Approved Corp', 'approved')
        _make_pledge(second_user.id, 'Pending Corp', 'pending')
        _make_pledge(third_user.id, 'Revoked Corp', 'revoked')
    resp = client.get('/pledge/')
    assert resp.status_code == 200
    assert b'Approved Corp' in resp.data
    assert b'Pending Corp' not in resp.data
    assert b'Revoked Corp' not in resp.data
    assert PLEDGE_TEXT.encode() in resp.data


def test_register_orders_by_org_name(client, app, sample_user, second_user):
    """The register lists approved pledges ordered by org name."""
    with app.app_context():
        _make_pledge(sample_user.id, 'Zebra Ltd', 'approved')
        _make_pledge(second_user.id, 'Alpha Ltd', 'approved')
    resp = client.get('/pledge/')
    assert resp.status_code == 200
    assert resp.data.index(b'Alpha Ltd') < resp.data.index(b'Zebra Ltd')


def test_register_empty_message(client):
    """No approved pledges yet — the register says so."""
    resp = client.get('/pledge/')
    assert resp.status_code == 200
    assert b'No approved pledges yet.' in resp.data


# ---------------------------------------------------------------- new pledge


def test_new_pledge_requires_login(client):
    """Anonymous users are sent to the login page."""
    resp = client.get('/pledge/new')
    assert resp.status_code == 302
    assert '/auth/login' in resp.headers['Location']


def test_new_pledge_form_shows_pledge_text(logged_in_user):
    """The form shows the fixed pledge text."""
    resp = logged_in_user.get('/pledge/new')
    assert resp.status_code == 200
    assert PLEDGE_TEXT.encode() in resp.data


def test_pledge_creation_happy_path(logged_in_user, app, sample_user):
    """Posting org name + confirm creates a pending pledge."""
    resp = logged_in_user.post('/pledge/new', data={
        'org_name': 'Acme Hiring',
        'confirm': 'y',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'submitted for review' in resp.data
    with app.app_context():
        pledge = EmployerPledge.query.filter_by(
            user_id=sample_user.id
        ).first()
        assert pledge is not None
        assert pledge.org_name == 'Acme Hiring'
        assert pledge.status == 'pending'
        assert pledge.decided_at is None
        assert pledge.decided_by_id is None


def test_new_pledge_requires_confirm(logged_in_user, app, sample_user):
    """No confirm checkbox, no pledge."""
    resp = logged_in_user.post('/pledge/new', data={
        'org_name': 'Acme Hiring',
    })
    assert resp.status_code == 200
    with app.app_context():
        assert EmployerPledge.query.filter_by(
            user_id=sample_user.id
        ).count() == 0


def test_duplicate_blocked_while_pending(
    logged_in_user, app, sample_user
):
    """A pending pledge blocks a second one."""
    with app.app_context():
        _make_pledge(sample_user.id, 'First Corp', 'pending')
    resp = logged_in_user.post('/pledge/new', data={
        'org_name': 'Second Corp',
        'confirm': 'y',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'already have an active pledge' in resp.data
    with app.app_context():
        pledges = EmployerPledge.query.filter_by(
            user_id=sample_user.id
        ).all()
        assert len(pledges) == 1
        assert pledges[0].org_name == 'First Corp'


def test_duplicate_blocked_while_approved(
    logged_in_user, app, sample_user
):
    """An approved pledge blocks a second one."""
    with app.app_context():
        _make_pledge(sample_user.id, 'Approved Corp', 'approved')
    resp = logged_in_user.get('/pledge/new', follow_redirects=True)
    assert resp.status_code == 200
    assert b'already have an active pledge' in resp.data
    with app.app_context():
        assert EmployerPledge.query.filter_by(
            user_id=sample_user.id
        ).count() == 1


def test_repledge_allowed_after_revoke(logged_in_user, app, sample_user):
    """A revoked pledge may be re-pledged — a fresh pending row."""
    with app.app_context():
        _make_pledge(sample_user.id, 'Old Corp', 'revoked')
    resp = logged_in_user.post('/pledge/new', data={
        'org_name': 'New Corp',
        'confirm': 'y',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b'submitted for review' in resp.data
    with app.app_context():
        pledges = EmployerPledge.query.filter_by(
            user_id=sample_user.id
        ).order_by(EmployerPledge.created_at).all()
        assert len(pledges) == 2
        assert pledges[0].status == 'revoked'
        assert pledges[1].status == 'pending'
        assert pledges[1].org_name == 'New Corp'


# ---------------------------------------------------------------- admin


def test_admin_approve(
    logged_in_admin, app, sample_user, sample_admin
):
    """Admin approve flips the pledge to approved, records the
    decider, and writes an audit log entry."""
    with app.app_context():
        pledge = _make_pledge(sample_user.id, 'Acme Hiring', 'pending')
        pledge_id = pledge.id
    resp = logged_in_admin.post(
        f'/admin/employer-pledges/{pledge_id}/approve',
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'Employer pledge approved' in resp.data
    with app.app_context():
        pledge = db.session.get(EmployerPledge, pledge_id)
        assert pledge.status == 'approved'
        assert pledge.decided_at is not None
        assert pledge.decided_by_id == sample_admin.id
        entry = AuditLog.query.filter_by(
            action='approve_employer_pledge',
            target_type='employer_pledge',
            target_id=pledge_id,
        ).first()
        assert entry is not None
        assert entry.admin_id == sample_admin.id


def test_admin_revoke(logged_in_admin, app, sample_user, sample_admin):
    """Admin revoke flips an approved pledge to revoked and writes
    an audit log entry."""
    with app.app_context():
        pledge = _make_pledge(sample_user.id, 'Acme Hiring', 'approved')
        pledge_id = pledge.id
    resp = logged_in_admin.post(
        f'/admin/employer-pledges/{pledge_id}/revoke',
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'Employer pledge revoked' in resp.data
    with app.app_context():
        pledge = db.session.get(EmployerPledge, pledge_id)
        assert pledge.status == 'revoked'
        assert pledge.decided_at is not None
        assert pledge.decided_by_id == sample_admin.id
        entry = AuditLog.query.filter_by(
            action='revoke_employer_pledge',
            target_type='employer_pledge',
            target_id=pledge_id,
        ).first()
        assert entry is not None


def test_admin_list_shows_pending_queue(
    logged_in_admin, app, sample_user
):
    """The admin queue lists pending pledges by default."""
    with app.app_context():
        _make_pledge(sample_user.id, 'Queue Corp', 'pending')
    resp = logged_in_admin.get('/admin/employer-pledges')
    assert resp.status_code == 200
    assert b'Queue Corp' in resp.data


def test_non_admin_forbidden_from_admin_routes(
    logged_in_user, app, sample_user
):
    """A non-admin is redirected away from the pledge review queue
    and cannot approve a pledge."""
    with app.app_context():
        pledge = _make_pledge(sample_user.id, 'Acme Hiring', 'pending')
        pledge_id = pledge.id
    resp = logged_in_user.get('/admin/employer-pledges')
    assert resp.status_code == 302
    resp = logged_in_user.post(
        f'/admin/employer-pledges/{pledge_id}/approve'
    )
    assert resp.status_code == 302
    with app.app_context():
        pledge = db.session.get(EmployerPledge, pledge_id)
        assert pledge.status == 'pending'


def test_admin_approve_idempotent(
    logged_in_admin, app, sample_user
):
    """Approving an already-approved pledge is a no-op (no second
    audit entry)."""
    with app.app_context():
        pledge = _make_pledge(sample_user.id, 'Acme Hiring', 'approved')
        pledge_id = pledge.id
    resp = logged_in_admin.post(
        f'/admin/employer-pledges/{pledge_id}/approve',
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'already reviewed' in resp.data
    with app.app_context():
        assert AuditLog.query.filter_by(
            action='approve_employer_pledge', target_id=pledge_id
        ).count() == 0


# ---------------------------------------------------------------- badge


def test_badge_on_public_profile(
    client, app, sample_user
):
    """An approved pledger shows the Human-Reviewed badge on their
    public profile."""
    with app.app_context():
        _make_pledge(sample_user.id, 'Acme Hiring', 'approved')
    resp = client.get(f'/profile/{sample_user.handle}')
    assert resp.status_code == 200
    assert b'Human-Reviewed' in resp.data


def test_no_badge_for_pending_pledger(
    client, app, sample_user
):
    """A pending pledger shows no Human-Reviewed badge."""
    with app.app_context():
        _make_pledge(sample_user.id, 'Acme Hiring', 'pending')
    resp = client.get(f'/profile/{sample_user.handle}')
    assert resp.status_code == 200
    assert b'Human-Reviewed' not in resp.data


def test_badge_on_bounty_detail(
    client, app, sample_user, sample_bounty
):
    """An approved pledger shows the badge next to the poster on
    bounty detail."""
    with app.app_context():
        _make_pledge(sample_user.id, 'Acme Hiring', 'approved')
    resp = client.get(f'/needs/{sample_bounty.url_slug}')
    assert resp.status_code == 200
    assert b'Human-Reviewed' in resp.data


def test_badge_on_own_profile(logged_in_user, app, sample_user):
    """An approved pledger sees the badge on their own profile page."""
    with app.app_context():
        _make_pledge(sample_user.id, 'Acme Hiring', 'approved')
    resp = logged_in_user.get('/profile/')
    assert resp.status_code == 200
    assert b'Human-Reviewed' in resp.data


def test_has_approved_pledge_property(app, sample_user, second_user):
    """User.has_approved_pledge reflects the approved pledge only."""
    with app.app_context():
        _make_pledge(sample_user.id, 'Acme Hiring', 'approved')
        _make_pledge(second_user.id, 'Pending Corp', 'pending')
        user = db.session.get(User, sample_user.id)
        other = db.session.get(User, second_user.id)
        assert user.has_approved_pledge is True
        assert other.has_approved_pledge is False
