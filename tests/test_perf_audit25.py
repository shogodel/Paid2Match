"""Performance regression tests — audit 2026-11-25.

The 2026-11-24 audit left N+1 patterns in feed / discover /
endorsements / analytics as "fine at current scale". Measured on a
seeded DB (25 users, 120 posts) they were not: a Discover render
issued 5,361 queries, Following 624, the homepage 109, the people
page 253, and a 15-message thread poll issued 3 queries per message
bubble. The fixes (bulk GROUP BY counts, per-request trust memo,
single-score ranking, bulk analytics / wins-wall loads, one bulk
UPDATE for mark-read, cached navbar context, bulk profile loads)
cut those to 318 / 319 / 64 / 160 / 13 (cold per-request counts —
earlier warm numbers looked lower because the per-request trust
memo leaked across profiler measures). What remains is ~10 queries
per DISTINCT author for the trust computation itself — the bulk
trust engine was deliberately left out (divergence risk: SLA date
math, dispute age buckets, endorsement recursion). These tests pin
the counts so the patterns cannot creep back: thresholds sit well
above the fixed numbers and far below the pre-fix ones.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from werkzeug.security import generate_password_hash

from extensions import db
from models.bounty import Bounty
from models.follow import Follow
from models.match import Match
from models.message import Message
from models.post import Comment, Like, Post, Share
from models.profile import Profile
from models.user import User


@pytest.fixture
def query_log(app):
    """All SQL statements issued on the test engine, in order."""
    log = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        log.append(statement)

    event.listen(db.engine, 'before_cursor_execute', _count)
    yield log
    event.remove(db.engine, 'before_cursor_execute', _count)


def _make_user(email, name, karma=0):
    user = User(
        email=email, full_name=name,
        password_hash=generate_password_hash('x'),
        role='user', is_active=True, email_verified=True,
        handle=email.split('@')[0],
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(Profile(
        user_id=user.id, profile_type='independent',
        display_name=name, karma=karma,
    ))
    return user


def _make_post(author, body, likes=0, comments=0, shares=0, likers=()):
    import uuid as _uuid
    post = Post(author_id=author.id, post_type='update', body=body)
    db.session.add(post)
    db.session.flush()
    # A user can like/share a post only once — top the liker pool up
    # with throwaway users when the caller passed too few.
    pool = list(likers) or [author]
    while len(pool) < max(likes, shares):
        pool.append(_make_user(
            f'liker-{_uuid.uuid4().hex[:8]}@example.com', 'Liker',
        ))
    for i in range(likes):
        db.session.add(Like(user_id=pool[i].id, post_id=post.id))
    for i in range(comments):
        db.session.add(Comment(
            author_id=author.id, post_id=post.id, body=f'comment {i}',
        ))
    for i in range(shares):
        db.session.add(Share(user_id=pool[i].id, post_id=post.id))
    return post


def _seed_feed(viewer, n_authors=6, posts_per_author=5):
    """Authors with posts + engagement; viewer follows the first 3."""
    authors = [_make_user(f'author{i}@example.com', f'Author {i}')
               for i in range(n_authors)]
    posts = []
    for i, author in enumerate(authors):
        for j in range(posts_per_author):
            posts.append(_make_post(
                author, f'Post {i}-{j} by author {i}',
                likes=2, comments=1, shares=1 if j % 2 == 0 else 0,
                likers=authors,
            ))
        if i < 3:
            db.session.add(Follow(
                follower_id=viewer.id, followed_id=author.id,
            ))
    db.session.commit()
    return authors, posts


def _cold_get(client, login_as, query_log, url, user):
    """GET with a cold session (like a real request) and a reset log."""
    login_as(client, user.email)
    db.session.expire_all()
    del query_log[:]
    return client.get(url)


# ---------------------------------------------------------------- counts


def test_engagement_counts_match_properties(app, sample_user):
    from utils.feed import engagement_counts
    post = _make_post(sample_user, 'counted post', likes=2, comments=2,
                      shares=1, likers=[sample_user])
    db.session.add(Comment(author_id=sample_user.id, post_id=post.id,
                           body='hidden', is_hidden=True))
    db.session.commit()
    counts = engagement_counts([post.id])
    assert counts[post.id] == (
        post.like_count, post.comment_count, post.share_count,
    ) == (2, 2, 1)
    assert engagement_counts([]) == {}


def test_discover_score_bulk_matches_per_post(app, sample_user):
    import pytest
    from utils.feed import discover_score, engagement_counts
    post = _make_post(sample_user, 'scored post', likes=3, comments=2,
                      shares=1, likers=[sample_user])
    db.session.commit()
    counts = engagement_counts([post.id])
    # The score's recency term uses utc_now(), so two calls a few
    # microseconds apart differ in the 5th decimal — approx, not ==.
    assert discover_score(post, counts=counts) == pytest.approx(
        discover_score(post), abs=1e-2)


def test_trending_score_bulk_matches_per_post(app, sample_user):
    import pytest
    from utils.feed import engagement_counts, trending_score
    post = _make_post(sample_user, 'trending post', likes=3, comments=2,
                      shares=1, likers=[sample_user])
    db.session.commit()
    counts = engagement_counts([post.id])
    # Same utc_now() recency drift as discover_score — approx, not ==.
    assert trending_score(post, counts=counts) == pytest.approx(
        trending_score(post), abs=1e-2)


# ---------------------------------------------------------------- page budgets


def test_discover_feed_query_count(app, client, login_as, query_log,
                                  sample_user):
    _seed_feed(sample_user)
    r = _cold_get(client, login_as, query_log,
                  '/social/feed?tab=discover', sample_user)
    assert r.status_code == 200
    # Pre-fix: 5,361 queries on a 120-post seed (30 posts here scale
    # the same way: ~2 trust computations + 6 counts per post).
    assert len(query_log) < 120, f'{len(query_log)} queries'
    assert b'Post 0-0' in r.data


def test_following_feed_query_count(app, client, login_as, query_log,
                                   sample_user):
    _seed_feed(sample_user)
    r = _cold_get(client, login_as, query_log,
                  '/social/feed?tab=following', sample_user)
    assert r.status_code == 200
    # Pre-fix: 624 queries on a 120-post seed. What remains is one
    # trust computation per distinct author/candidate (~11 queries
    # each) — the known floor, not per-post work.
    assert len(query_log) < 400, f'{len(query_log)} queries'


def test_trending_feed_query_count(app, client, login_as, query_log,
                                  sample_user):
    _seed_feed(sample_user)
    r = _cold_get(client, login_as, query_log,
                  '/social/feed?tab=trending', sample_user)
    assert r.status_code == 200
    # Same page as Discover (same cards, same rails): the floor is
    # ~11 queries per distinct author for trust levels (the bulk
    # trust engine was deliberately left out of this audit) plus
    # ~35 base queries. The old <80 came from profiler numbers
    # where the per-request trust memo leaked across measures.
    assert len(query_log) < 120, f'{len(query_log)} queries'


def test_home_query_count(app, client, login_as, query_log, sample_user):
    _seed_feed(sample_user)
    r = _cold_get(client, login_as, query_log, '/', sample_user)
    assert r.status_code == 200
    # Pre-fix: 109 queries for a 4-post preview.
    assert len(query_log) < 60, f'{len(query_log)} queries'


def test_people_query_count(app, client, login_as, query_log, sample_user):
    _seed_feed(sample_user)
    r = _cold_get(client, login_as, query_log, '/social/people',
                  sample_user)
    assert r.status_code == 200
    # Pre-fix: 253 queries (one full trust computation per candidate).
    assert len(query_log) < 120, f'{len(query_log)} queries'


def test_profile_query_count(app, client, login_as, query_log,
                            sample_user):
    author = _make_user('profiled@example.com', 'Profiled')
    for j in range(5):
        _make_post(author, f'Profile post {j}', likes=2, comments=1,
                   likers=[sample_user, author])
    db.session.commit()
    r = _cold_get(client, login_as, query_log,
                  f'/profile/{author.handle}', sample_user)
    assert r.status_code == 200
    # Pre-fix: 249 queries (trust + 5 COUNT queries per post card).
    assert len(query_log) < 80, f'{len(query_log)} queries'


def test_post_detail_query_count(app, client, login_as, query_log,
                                sample_user):
    author = _make_user('detail@example.com', 'Detail')
    post = _make_post(author, 'Detail post body', likes=2, comments=2,
                      likers=[sample_user, author])
    db.session.commit()
    r = _cold_get(client, login_as, query_log,
                  f'/social/posts/{post.id}', sample_user)
    assert r.status_code == 200
    # Pre-fix: 60 queries (duplicate trust_level + per-card counts).
    assert len(query_log) < 40, f'{len(query_log)} queries'


def test_inbox_query_count(app, client, login_as, query_log, sample_user):
    others = [_make_user(f'penpal{i}@example.com', f'Penpal {i}')
              for i in range(3)]
    for other in others:
        for m in range(5):
            db.session.add(Message(
                sender_id=sample_user.id if m % 2 == 0 else other.id,
                recipient_id=other.id if m % 2 == 0 else sample_user.id,
                content=f'message {m}',
            ))
    db.session.commit()
    r = _cold_get(client, login_as, query_log, '/messages/',
                  sample_user)
    assert r.status_code == 200
    assert len(query_log) < 15, f'{len(query_log)} queries'


def test_thread_updates_query_count(app, client, login_as, query_log,
                                   sample_user):
    other = _make_user('threadpal@example.com', 'Threadpal')
    for m in range(10):
        db.session.add(Message(
            sender_id=sample_user.id if m % 2 == 0 else other.id,
            recipient_id=other.id if m % 2 == 0 else sample_user.id,
            content=f'message {m}',
        ))
    db.session.commit()
    r = _cold_get(client, login_as, query_log,
                  f'/messages/thread/{other.id}/updates', sample_user)
    assert r.status_code == 200
    # Pre-fix: ~3 queries per message bubble (the navbar context
    # processor re-ran on every render_template) — 48 for 15 — then
    # 22 once the processor was cached, because _mark_read committed
    # after the messages were loaded and the commit expired them, so
    # each bubble reloaded its message row. Fixed floor is 13, none
    # of it per message: load_user + other + 2 current_user reloads
    # + unread-ids + 1 UPDATE + messages + sender/recipient loads
    # + my_profile + 3 navbar context queries.
    assert len(query_log) <= 14, f'{len(query_log)} queries'


def test_thread_mark_read_single_update(app, client, login_as, query_log,
                                       sample_user):
    other = _make_user('marker@example.com', 'Marker')
    for m in range(6):
        db.session.add(Message(
            sender_id=other.id, recipient_id=sample_user.id,
            content=f'unread {m}',
        ))
    db.session.commit()
    r = _cold_get(client, login_as, query_log,
                  f'/messages/thread/{other.id}', sample_user)
    assert r.status_code == 200
    updates = [s for s in query_log
               if s.strip().upper().startswith('UPDATE')]
    # Pre-fix: one UPDATE per unread message.
    assert len(updates) == 1, f'{len(updates)} UPDATEs'
    assert Message.query.filter_by(
        recipient_id=sample_user.id, is_read=False,
    ).count() == 0


def test_wins_wall_query_count(app, client, login_as, query_log,
                              sample_user):
    from models.pitch import Pitch
    from models.win_story import WinStory
    for i in range(3):
        bounty = Bounty(
            poster_id=sample_user.id, title=f'Need {i}',
            description='Need a senior dev ' * 5,
            bounty_type='recruitment',
            bounty_direction='seeking_opportunity',
            reward_amount=1000, status='completed',
            payment_status='released',
        )
        db.session.add(bounty)
        db.session.flush()
        pitch = Pitch(bounty_id=bounty.id, hunter_id=sample_user.id,
                      candidate_teaser='great candidate',
                      status='accepted')
        db.session.add(pitch)
        db.session.flush()
        match = Match(
            bounty_id=bounty.id, pitch_id=pitch.id,
            hunter_id=sample_user.id, employer_id=sample_user.id,
            hunter_confirmed=True, employer_confirmed=True,
            payout_status='completed', payout_amount=1000,
            platform_fee=200,
        )
        db.session.add(match)
        db.session.flush()
        db.session.add(WinStory(
            bounty_id=bounty.id, match_id=match.id,
            author_id=sample_user.id, role='referrer',
            body='Great hire.',
        ))
    db.session.commit()
    r = _cold_get(client, login_as, query_log, '/wins/', sample_user)
    assert r.status_code == 200
    # Pre-fix: ~8 queries per story (bounty + match + pitch +
    # 2 payouts + 2 referrals).
    assert len(query_log) < 25, f'{len(query_log)} queries'


def test_free_analytics_query_count(app, client, login_as, query_log,
                                   sample_user):
    for j in range(5):
        _make_post(sample_user, f'Analytics post {j}', likes=2,
                   comments=1, likers=[sample_user])
    db.session.commit()
    r = _cold_get(client, login_as, query_log, '/social/analytics',
                  sample_user)
    assert r.status_code == 200
    # Pre-fix: 124 queries (3 counts + 2 reached queries per post,
    # plus a trust computation per analytics template row).
    assert len(query_log) < 60, f'{len(query_log)} queries'


def test_pro_analytics_query_count(app, client, login_as, query_log,
                                  sample_user):
    from datetime import datetime as _dt
    from models.bump import Bump
    from models.subscription import ProSubscription
    db.session.add(ProSubscription(
        user_id=sample_user.id, stripe_subscription_id='sub_test_pro',
        status='active',
        current_period_end=_dt.now(timezone.utc) + timedelta(days=30),
    ))
    posts = []
    for j in range(5):
        post = _make_post(sample_user, f'Pro post {j}', likes=2,
                          comments=1, likers=[sample_user])
        posts.append(post)
        db.session.add(Bump(
            post_id=post.id, user_id=sample_user.id, source='free',
            expires_at=_dt.now(timezone.utc) + timedelta(hours=24),
        ))
    db.session.commit()
    r = _cold_get(client, login_as, query_log, '/social/analytics',
                  sample_user)
    assert r.status_code == 200
    # Pro analytics used to issue 2 reached queries + a full
    # bump-performance call per post.
    assert len(query_log) < 80, f'{len(query_log)} queries'


def test_post_card_counts_rendered(app, client, login_as, sample_user):
    _seed_feed(sample_user)
    login_as(client, sample_user.email)
    r = client.get('/social/feed?tab=latest')
    assert r.status_code == 200
    # A post with 2 likes / 1 comment renders those exact numbers
    # from the bulk counts (not the per-post properties).
    assert b'2 likes' in r.data
    assert b'1 comment' in r.data


# ---------------------------------------------------------------- trust


def test_trust_memoized_within_request(app, sample_user, query_log):
    from flask import g
    from utils.trust import compute_trust_score
    with app.test_request_context():
        g.pop('_trust_cache', None)
        compute_trust_score(sample_user)
        del query_log[:]
        compute_trust_score(sample_user)
        assert query_log == []
        # ... and the level check reuses the same computation.
        from utils.trust import trust_level
        del query_log[:]
        trust_level(sample_user)
        # Only the SLA breach check may issue a query (cached too
        # after the first trust_level call); the score itself is free.
        assert len(query_log) <= 1


def test_dispute_loss_penalty_filters_by_user(app, sample_user):
    from models.dispute import Dispute
    from models.pitch import Pitch
    from utils.trust import dispute_loss_penalty
    hunter = _make_user('hunter-d@example.com', 'Hunter D')
    employer = _make_user('employer-d@example.com', 'Employer D')
    other = _make_user('other-d@example.com', 'Other D')
    now = datetime.now(timezone.utc)
    db.session.add(Bounty(
        poster_id=employer.id, title='D need',
        description='Need someone. ' * 5, bounty_type='recruitment',
        bounty_direction='seeking_opportunity', reward_amount=100,
        status='completed', payment_status='released',
    ))
    db.session.flush()
    bounty = Bounty.query.filter_by(poster_id=employer.id).first()
    pitch = Pitch(bounty_id=bounty.id, hunter_id=hunter.id,
                  candidate_teaser='candidate', status='accepted')
    db.session.add(pitch)
    db.session.flush()
    match = Match(
        bounty_id=bounty.id, pitch_id=pitch.id,
        hunter_id=hunter.id, employer_id=employer.id,
        hunter_confirmed=True, employer_confirmed=True,
    )
    db.session.add(match)
    db.session.flush()
    # Upheld, raised by hunter: the employer lost (-15).
    db.session.add(Dispute(
        match_id=match.id, raised_by_id=hunter.id, reason='r',
        status='resolved', outcome='upheld',
        resolved_at=now - timedelta(days=10),
    ))
    # Dismissed, raised by employer: the employer lost (-15 more).
    db.session.add(Dispute(
        match_id=match.id, raised_by_id=employer.id, reason='r',
        status='resolved', outcome='dismissed',
        resolved_at=now - timedelta(days=10),
    ))
    # Old upheld dispute (200 days): -7 for the employer.
    db.session.add(Dispute(
        match_id=match.id, raised_by_id=hunter.id, reason='r',
        status='resolved', outcome='upheld',
        resolved_at=now - timedelta(days=200),
    ))
    # Ancient upheld dispute (400 days): 0.
    db.session.add(Dispute(
        match_id=match.id, raised_by_id=hunter.id, reason='r',
        status='resolved', outcome='upheld',
        resolved_at=now - timedelta(days=400),
    ))
    db.session.commit()
    assert dispute_loss_penalty(employer.id) == 15 + 15 + 7
    assert dispute_loss_penalty(hunter.id) == 0
    assert dispute_loss_penalty(other.id) == 0


def test_response_rate_and_upheld_reports_values(app, sample_user):
    from models.moderation import Report
    from models.pitch import Pitch
    from utils.trust import response_rate, upheld_reports_count
    poster = _make_user('poster-r@example.com', 'Poster R')
    hunter = _make_user('hunter-r@example.com', 'Hunter R')
    bounty = Bounty(
        poster_id=poster.id, title='R need',
        description='Need someone. ' * 5, bounty_type='recruitment',
        bounty_direction='seeking_opportunity', reward_amount=100,
        status='open', payment_status='released',
    )
    db.session.add(bounty)
    db.session.flush()
    for status in ('interested', 'rejected', 'pending'):
        db.session.add(Pitch(
            bounty_id=bounty.id, hunter_id=hunter.id,
            candidate_teaser='candidate', status=status,
        ))
    db.session.commit()
    assert response_rate(poster.id) == pytest.approx(2 / 3)
    assert response_rate(hunter.id) is None
    # One upheld profile report + one upheld post report = 2; a
    # dismissed report and an actioned report on someone else's
    # post do not count.
    post = _make_post(poster, 'reported post')
    other_post = _make_post(hunter, 'other post')
    db.session.add(Report(
        reporter_id=hunter.id, target_type='profile',
        target_id=poster.id, status='actioned', reason='spam',
    ))
    db.session.add(Report(
        reporter_id=hunter.id, target_type='post',
        target_id=post.id, status='actioned', reason='spam',
    ))
    db.session.add(Report(
        reporter_id=hunter.id, target_type='post',
        target_id=post.id, status='dismissed', reason='spam',
    ))
    db.session.add(Report(
        reporter_id=hunter.id, target_type='post',
        target_id=other_post.id, status='actioned', reason='spam',
    ))
    db.session.commit()
    assert upheld_reports_count(poster.id) == 2
    assert upheld_reports_count(hunter.id) == 1


# ---------------------------------------------------------------- bulk parity


def test_bump_performance_bulk_matches_single(app, sample_user):
    from models.bump import Boost, Bump
    from models.referral import Referral
    from utils.bumps import (
        bump_performance_for_post, bump_performance_for_posts,
    )
    bounty = Bounty(
        poster_id=sample_user.id, title='Bump need',
        description='Need someone. ' * 5, bounty_type='recruitment',
        bounty_direction='seeking_opportunity', reward_amount=1000,
        status='open', payment_status='secured',
    )
    db.session.add(bounty)
    db.session.flush()
    post = Post(author_id=sample_user.id, post_type='need_paid',
                body='Need post', bounty_id=bounty.id)
    db.session.add(post)
    db.session.flush()
    _make_post(sample_user, 'other post', likes=0)
    now = datetime.now(timezone.utc)
    db.session.add(Bump(
        post_id=post.id, user_id=sample_user.id, source='free',
        views_at_bump=1, likes_at_bump=1, comments_at_bump=0,
        created_at=now - timedelta(hours=2),
        expires_at=now + timedelta(hours=22),
    ))
    db.session.add(Boost(
        post_id=post.id, user_id=sample_user.id, days=1,
        price_cents=500, stripe_session_id='sess_bulk_test',
        views_at_boost=0, likes_at_boost=0, comments_at_boost=0,
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(days=1),
    ))
    db.session.add(Referral(
        referrer_id=sample_user.id, bounty_id=bounty.id,
        referred_name='Ref Person', referred_email='ref@example.com',
        message='A great fit for this need, experienced and available.',
        created_at=now - timedelta(minutes=30),
    ))
    db.session.add(Like(user_id=sample_user.id, post_id=post.id))
    db.session.commit()
    single = bump_performance_for_post(post.id)
    bulk = bump_performance_for_posts([post.id])[post.id]
    assert bulk == single
    assert len(bulk) == 2
    assert {row['kind'] for row in bulk} == {'bump', 'boost'}
    # The referral created inside both windows is attributed.
    assert all(row['referrals'] == 1 for row in bulk)


def test_money_trail_preload_matches_unpreloaded(app, sample_user,
                                                sample_bounty):
    from models.payout import Payout
    from models.pitch import Pitch
    from utils.money_trail import build_trail
    pitch = Pitch(bounty_id=sample_bounty.id,
                  hunter_id=sample_user.id,
                  candidate_teaser='candidate', status='accepted')
    db.session.add(pitch)
    db.session.flush()
    match = Match(
        bounty_id=sample_bounty.id, pitch_id=pitch.id,
        hunter_id=sample_user.id, employer_id=sample_user.id,
        hunter_confirmed=True, employer_confirmed=True,
        payout_status='completed', payout_amount=1000,
        platform_fee=200,
    )
    db.session.add(match)
    db.session.flush()
    db.session.add(Payout(
        match_id=match.id, referrer_id=sample_user.id,
        bounty_id=sample_bounty.id, kind='referral',
        gross_amount=1000, platform_fee=200, amount=800,
        status='completed',
    ))
    db.session.commit()

    def _sig(trail):
        return [(h['role'], h['amount'], h['currency']) for h in trail]

    plain = build_trail(sample_bounty, match=match)
    payouts = {(p.match_id, p.kind): p for p in Payout.query.filter_by(
        match_id=match.id).all()}
    referral_by_pitch = {}
    preloaded = build_trail(
        sample_bounty, match=match, payouts=payouts,
        referral_by_pitch=referral_by_pitch,
    )
    assert _sig(preloaded) == _sig(plain)
    assert _sig(plain)[-2][0] == 'referrer'
