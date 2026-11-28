"""Performance regression tests — audit 2026-11-28 (tips + paid
communities slice).

New-code findings checked here:

* The tip button partial (templates/partials/tip_button.html) is
  included once per post card in the feed. It touches no table —
  ``post.author`` is already selectinloaded by the feed query and
  the modal is pure template — so it must add zero queries per
  post and zero statements against the tips table.
* ``tips_received_summary`` (utils/tips.py) is one aggregate
  (SUM + COUNT) — the public profile must run it exactly once
  per render, not once per consumer.
* ``Space.can_view`` (models/space.py) ran its own membership
  lookup even when the space-detail route had just loaded the
  same row — a duplicate query on every paid-space render (pitch
  page and member view alike). The route now passes its loaded
  membership in; the lookup must run at most once per render.
"""
from extensions import db
from models import Space, SpaceMembership
from models.post import Post
from models.tip import Tip
from tests.test_paid_spaces import _make_membership, _make_space
from tests.test_perf_audit25 import (
    _cold_get, _make_post, _make_user, _seed_feed, query_log,
)
from tests.test_perf_audit27 import _counted_profile_render


def _membership_statements(query_log):
    """The per-user membership lookup (filter_by(user_id, space_id))
    — NOT the member_count COUNT, which filters by space_id only
    and legitimately queries the same table once per render."""
    return [s for s in query_log
            if 'space_memberships.user_id = ?' in s]


def test_feed_tip_button_adds_no_tip_queries(app, client, login_as,
                                             query_log, sample_user):
    """Discover feed with the per-post tip button: no statement
    touches the tips table, and the count stays inside the
    audit-2025 budget (<120 on the 30-post seed)."""
    _seed_feed(sample_user)
    r = _cold_get(client, login_as, query_log,
                  '/social/feed?tab=discover', sample_user)
    assert r.status_code == 200
    assert b'Tip' in r.data
    assert not [s for s in query_log if 'tips' in s], (
        'feed render queried the tips table')
    assert len(query_log) < 120, f'{len(query_log)} queries'


def test_public_profile_tips_summary_is_one_query(
        app, client, login_as, query_log, sample_user):
    """Public profile of a user with completed + pending tips: the
    tips aggregate runs exactly once per render, pending tips are
    excluded, and the total renders."""
    author = _make_user('tipped28@example.com', 'Tipped28')
    db.session.add(Tip(
        tipper_id=sample_user.id, recipient_id=author.id,
        amount_cents=500, currency='CAD', status='completed',
    ))
    db.session.add(Tip(
        tipper_id=sample_user.id, recipient_id=author.id,
        amount_cents=1000, currency='CAD', status='completed',
    ))
    db.session.add(Tip(
        tipper_id=sample_user.id, recipient_id=author.id,
        amount_cents=2500, currency='CAD', status='pending',
    ))
    for j in range(3):
        _make_post(author, f'Tipped post {j}', likes=1,
                   likers=[sample_user, author])
    db.session.commit()
    r, n = _counted_profile_render(
        client, login_as, query_log, author.handle, sample_user)
    assert r.status_code == 200
    tip_stmts = [s for s in query_log if 'FROM tips' in s]
    assert len(tip_stmts) == 1, f'{len(tip_stmts)} tips statements'
    assert b'$15' in r.data or b'15.00' in r.data
    assert n < 35, f'{n} queries'


def test_paid_space_pitch_membership_loaded_once(
        app, client, login_as, query_log, sample_user):
    """Pitch page for a non-member: the membership lookup runs
    once (route + can_view share the row), not twice."""
    owner = _make_user('owner28@example.com', 'Owner28')
    space = _make_space(owner=owner, paid=True, price_cents=1500)
    r = _cold_get(client, login_as, query_log,
                  f'/social/spaces/{space.slug}', sample_user)
    assert r.status_code == 200
    assert b'This is a paid space' in r.data
    assert b'$15.00' in r.data
    assert len(_membership_statements(query_log)) == 1, (
        _membership_statements(query_log))


def test_paid_space_member_detail_membership_loaded_once(
        app, client, login_as, query_log, sample_user):
    """Member view of a paid space: same single membership lookup,
    and the member-only post renders."""
    owner = _make_user('owner28b@example.com', 'Owner28b')
    space = _make_space(owner=owner, paid=True, price_cents=1500,
                        slug='paid-club-b')
    _make_membership(sample_user, space)
    db.session.add(Post(
        author_id=owner.id, post_type='update',
        body='Members-only post body.', space_id=space.id,
    ))
    db.session.commit()
    r = _cold_get(client, login_as, query_log,
                  f'/social/spaces/{space.slug}', sample_user)
    assert r.status_code == 200
    assert b'Members-only post body.' in r.data
    assert len(_membership_statements(query_log)) == 1, (
        _membership_statements(query_log))


def test_can_view_without_membership_still_queries(
        app, sample_user, second_user, query_log):
    """can_view with no membership argument keeps the old behaviour
    (one lookup) — the other call sites (post create / detail)
    rely on it."""
    owner = _make_user('owner28c@example.com', 'Owner28c')
    space = _make_space(owner=owner, paid=True, price_cents=1500,
                        slug='paid-club-c')
    del query_log[:]
    assert space.can_view(sample_user) is False
    assert len(_membership_statements(query_log)) == 1
    del query_log[:]
    assert space.can_view(sample_user, membership=None) is False
    assert _membership_statements(query_log) == []
    _make_membership(sample_user, space)
    assert space.can_view(sample_user) is True
