"""Performance regression tests — audit 2026-11-27.

The 2026-11-26 reputation build (Trust card + average response
time) re-ran trust inputs the profile page had already computed:
on a steady-state public-profile render the same response_rate
aggregate, average_response_time query, completed-referrals COUNT,
verified-linked-accounts load, verification-tier bulk loads and
approved-pledge lookup each ran 2–3 times, and the follow check
ran twice. The fixes memoize those pure reads per request on
flask.g (same pattern as the audit-2025 _trust_cache /
_pledge_cache) and let the profile route hand verification_tier
the signals trust already loaded. Steady-state profile render:
41 -> 28 queries on the audit-2025 seed (measured the same way
as the 41 baseline — the test fixture shares one app context, so
both numbers also benefit from the pre-existing _trust_cache
leak across renders; a cold production render is higher in both
cases); first render stays higher (one-off ProfileView INSERT +
commit expires the session).
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from extensions import db
from models.bounty import Bounty
from models.pitch import Pitch
from tests.test_perf_audit25 import (
    _cold_get, _make_post, _make_user, query_log,
)


def _counted_profile_render(client, login_as, query_log, handle, viewer):
    """Steady-state profile render: warms the per-session
    ProfileView row first (its INSERT + commit costs ~20 extra
    queries once), then measures the render itself."""
    _cold_get(client, login_as, query_log, f'/profile/{handle}', viewer)
    r = _cold_get(client, login_as, query_log, f'/profile/{handle}', viewer)
    assert r.status_code == 200
    return r, len(query_log)


def test_trust_scalars_memoized_within_request(app, sample_user,
                                               query_log):
    """response_rate / average_response_time /
    completed_referrals_count / verified_linked_count: after the
    first call in a request, repeats issue no query — the Trust
    card, the route and reputation_metrics() share one computation."""
    from utils import trust
    from utils.linked_accounts import verified_linked_count
    from utils.reputation import reputation_metrics
    author = _make_user('memo27@example.com', 'Memo27')
    _make_post(author, 'Memo post', likes=1,
               likers=[sample_user, author])
    db.session.commit()
    with app.test_request_context():
        trust.trust_level(author)
        del query_log[:]
        # These three are inside the trust computation, so repeats
        # after trust_level are free.
        assert trust.response_rate(author.id) is None
        assert trust.completed_referrals_count(author.id) == 0
        assert verified_linked_count(author.id) == 0
        assert query_log == []
        # average_response_time is display-only: trust_level does not
        # compute it, so the first call queries once and the second
        # call (Trust card + reputation strip) is free.
        del query_log[:]
        assert trust.average_response_time(author.id) is None
        assert len(query_log) == 1
        del query_log[:]
        assert trust.average_response_time(author.id) is None
        assert query_log == []
        # reputation_metrics() reuses all of the above; only its own
        # fill_rate bounty lookup may query (1 when there are no
        # terminal bounties, 2 when there are).
        del query_log[:]
        metrics = reputation_metrics(author.id)
        assert metrics['response_rate'] is None
        assert metrics['response_time'] is None
        assert len(query_log) <= 1


def test_verification_tier_from_breakdown_matches_query_path(
        app, sample_user):
    """The signals path gives the same tier as the 4-query path,
    for a user with no signals and for one with email verified."""
    from utils.trust import trust_level
    from utils.verification_tiers import (
        tier_signals_from_breakdown, verification_tier,
    )
    author = _make_user('tier27@example.com', 'Tier27')
    db.session.commit()
    with app.test_request_context():
        _, _, breakdown = trust_level(author)
        signals = tier_signals_from_breakdown(author, breakdown)
        assert signals == {
            'email_verified': True,
            'linked_verified': False,
            'admin_verified': False,
            'kyc': False,
        }
        assert (verification_tier(
            author, signals=signals)[0]
            == verification_tier(author)[0] == 1)


def test_has_approved_pledge_memoized_within_request(
        app, sample_user, query_log):
    """The template reads user.has_approved_pledge twice per
    profile render (header + Trust card); the second read is free."""
    author = _make_user('pledge27@example.com', 'Pledge27')
    db.session.commit()
    with app.test_request_context():
        db.session.expire_all()
        assert author.has_approved_pledge is False
        del query_log[:]
        assert author.has_approved_pledge is False
        assert query_log == []


def test_public_profile_query_count_audit27(app, client, login_as,
                                           query_log, sample_user):
    """Steady-state public profile: 41 queries before this audit's
    fixes (duplicate trust / tier / pledge / follow loads), ~29
    after. The Trust card still renders with its breakdown."""
    author = _make_user('profiled27@example.com', 'Profiled27')
    for j in range(5):
        _make_post(author, f'Profile post {j}', likes=2, comments=1,
                   likers=[sample_user, author])
    db.session.commit()
    r, n = _counted_profile_render(
        client, login_as, query_log, author.handle, sample_user)
    assert n < 35, f'{n} queries'
    assert b'Trust' in r.data
    assert b'Why this score' in r.data


def test_public_profile_with_response_time_query_count(
        app, client, login_as, query_log, sample_user):
    """Profile of a poster with answered pitches: the Trust card
    shows "Responds in ..." from ONE response-time query, shared
    by the card and the reputation strip."""
    author = _make_user('rt27@example.com', 'RT27')
    for j in range(3):
        _make_post(author, f'RT post {j}', likes=1,
                   likers=[sample_user, author])
    bounty = Bounty(
        poster_id=author.id, title='RT need',
        description='Need someone. ' * 5, bounty_type='recruitment',
        bounty_direction='seeking_opportunity', reward_amount=100,
        status='open', payment_status='released',
    )
    db.session.add(bounty)
    db.session.flush()
    now = datetime.now(timezone.utc)
    for i in range(4):
        pitch = Pitch(bounty_id=bounty.id, hunter_id=sample_user.id,
                      candidate_teaser='candidate', status='interested')
        pitch.created_at = now - timedelta(hours=10 + i)
        pitch.responded_at = now - timedelta(hours=8 + i)
        db.session.add(pitch)
    db.session.commit()
    r, n = _counted_profile_render(
        client, login_as, query_log, author.handle, sample_user)
    assert n < 35, f'{n} queries'
    assert b'Responds in' in r.data
    # The response-time and response-rate aggregates run at most
    # once per render, not once per consumer (Trust card +
    # reputation strip). (The count can be 0 here: the test
    # fixture holds one app context open for the whole test, so
    # flask.g — and the per-request memo on it — is shared across
    # the warm-up and measured renders; in production each request
    # gets a fresh g and the count is exactly 1.)
    assert sum(1 for s in query_log
               if 'coalesce(pitches.responded_at' in s) <= 1
    assert sum(1 for s in query_log
               if 'coalesce(sum(CASE' in s) <= 1
