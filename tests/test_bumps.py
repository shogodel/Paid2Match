"""M3 bump/boost tests — limits, spend order, fulfillment, feed
labels/lift, performance snapshots, recurring bumps."""
from datetime import timedelta
from unittest.mock import patch

import pytest

from extensions import db
from models import Bump, Boost, Match, RecurringBump, utc_now
from models.admin_settings import AdminSettings
from models.bounty import Bounty
from models.follow import Follow
from models.pitch import Pitch
from models.post import Post
from models.profile import Profile
from utils.bumps import (
    apply_bump,
    boost_eligibility,
    bump_eligibility,
    bump_performance_for_post,
    cancel_recurring_bump,
    create_recurring_bump,
    free_bump_balance,
    fulfill_boost,
    fulfill_bump_pack,
    fulfill_single_bump,
    pack_bump_balance,
    run_recurring_bumps,
)
from utils.feed import apply_bump_cap, discover_score, get_following_feed
from utils.trust import trust_level


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_bounty(poster):
    bounty = Bounty(
        poster_id=poster.id, title='Free need', description='x' * 60,
        bounty_type='recruitment', bounty_direction='seeking_opportunity',
        reward_amount=None, status='open', payment_status='released',
        location='Remote',
    )
    db.session.add(bounty)
    db.session.commit()
    db.session.refresh(bounty)
    return bounty


def _confirmed_match(bounty, hunter, poster):
    pitch = Pitch(bounty_id=bounty.id, hunter_id=hunter.id,
                  candidate_teaser='x' * 60, status='accepted')
    db.session.add(pitch)
    db.session.flush()
    match = Match(
        bounty_id=bounty.id, pitch_id=pitch.id,
        hunter_id=hunter.id, employer_id=poster.id,
        hunter_confirmed=True, employer_confirmed=True,
        payout_status='completed',
    )
    db.session.add(match)
    db.session.commit()
    db.session.refresh(match)
    return match


def _make_member(user, poster):
    """Lift a user to Member: 2 completed referrals + verified profile
    (identity 10 + transactions 16 + reliability 10 = 36 >= 30)."""
    profile = Profile.query.filter_by(user_id=user.id).first()
    profile.verified = True
    db.session.commit()
    for _ in range(2):
        _confirmed_match(_free_bounty(poster), user, poster)
    level, _score, _b = trust_level(user)
    assert level == 'Member', f'expected Member, got {level}'
    return user


def _old_post(app, author, hours=25):
    post = Post(author_id=author.id, post_type='update', body='Old post.')
    db.session.add(post)
    db.session.flush()
    post.created_at = utc_now() - timedelta(hours=hours)
    db.session.commit()
    db.session.refresh(post)
    return post


def _new_post(author, body='Fresh post.'):
    post = Post(author_id=author.id, post_type='update', body=body)
    db.session.add(post)
    db.session.commit()
    db.session.refresh(post)
    return post


def _as_naive(dt):
    """SQLite returns stored datetimes naive; strip tzinfo for
    comparisons against stored values."""
    return dt.replace(tzinfo=None) if dt is not None else None


# ---------------------------------------------------------------------------
# Bump limits
# ---------------------------------------------------------------------------

class TestBumpLimits:
    def test_newcomer_cannot_bump(self, app, sample_user, sample_hunter):
        post = _old_post(app, sample_user)
        ok, reason = bump_eligibility(sample_user, post)
        assert ok is False
        assert 'Member' in reason

    def test_post_must_be_24h_old(self, app, sample_user, sample_hunter):
        _make_member(sample_user, sample_hunter)
        post = _new_post(sample_user)
        ok, reason = bump_eligibility(sample_user, post)
        assert ok is False
        assert '24 hours old' in reason

    def test_one_bump_per_post_per_24h(self, app, sample_user, sample_hunter):
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        profile.free_bumps = 2
        db.session.commit()
        bump, error = apply_bump(sample_user, post, 'free')
        assert error is None
        db.session.commit()
        ok, reason = bump_eligibility(sample_user, post)
        assert ok is False
        assert 'already bumped' in reason

    def test_three_bumps_per_user_per_day(self, app, sample_user, sample_hunter):
        _make_member(sample_user, sample_hunter)
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        profile.free_bumps = 5
        db.session.commit()
        posts = [_old_post(app, sample_user) for _ in range(4)]
        for post in posts[:3]:
            _bump, error = apply_bump(sample_user, post, 'free')
            assert error is None
        db.session.commit()
        ok, reason = bump_eligibility(sample_user, posts[3])
        assert ok is False
        assert '3 bumps' in reason

    def test_bump_stamps_post_and_expires_in_24h(
        self, app, sample_user, sample_hunter
    ):
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        profile.free_bumps = 1
        db.session.commit()
        bump, error = apply_bump(sample_user, post, 'free')
        assert error is None
        db.session.commit()
        db.session.refresh(post)
        assert post.is_bumped is True
        assert (post.bumped_until - post.bumped_at) == timedelta(hours=24)
        assert bump.expires_at == post.bumped_until


class TestBumpSpend:
    def test_free_bump_spent(self, app, sample_user, sample_hunter):
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        profile.free_bumps = 1
        db.session.commit()
        assert free_bump_balance(sample_user.id) == 1
        _bump, error = apply_bump(sample_user, post, 'free')
        assert error is None
        db.session.commit()
        assert free_bump_balance(sample_user.id) == 0

    def test_free_bump_blocked_when_none_left(
        self, app, sample_user, sample_hunter
    ):
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        assert free_bump_balance(sample_user.id) == 0
        _bump, error = apply_bump(sample_user, post, 'free')
        assert error == 'You have no free bumps left.'

    def test_pack_bump_spent_fifo(self, app, sample_user, sample_hunter):
        _make_member(sample_user, sample_hunter)
        fulfill_bump_pack(sample_user.id, 5, 800, 'cs_pack_1')
        db.session.commit()
        assert pack_bump_balance(sample_user.id) == 5
        post = _old_post(app, sample_user)
        _bump, error = apply_bump(sample_user, post, 'pack')
        assert error is None
        db.session.commit()
        assert pack_bump_balance(sample_user.id) == 4

    def test_pack_fulfillment_is_idempotent(self, app, sample_user):
        _p, created = fulfill_bump_pack(sample_user.id, 5, 800, 'cs_pack_dup')
        assert created is True
        db.session.commit()
        _p2, created2 = fulfill_bump_pack(sample_user.id, 5, 800, 'cs_pack_dup')
        assert created2 is False
        db.session.commit()
        assert pack_bump_balance(sample_user.id) == 5

    def test_single_bump_fulfillment_is_idempotent(
        self, app, sample_user, sample_hunter
    ):
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        bump, error = fulfill_single_bump(sample_user, post, 'cs_single_1')
        assert error is None
        db.session.commit()
        bump2, error2 = fulfill_single_bump(sample_user, post, 'cs_single_1')
        assert error2 is None
        assert bump2.id == bump.id
        assert Bump.query.filter_by(post_id=post.id).count() == 1

    def test_single_bump_forces_past_limits(
        self, app, sample_user, sample_hunter
    ):
        """Webhook fulfillment uses force=True: a paid bump applies
        even on a fresh post (the money was already taken)."""
        _make_member(sample_user, sample_hunter)
        post = _new_post(sample_user)
        bump, error = fulfill_single_bump(sample_user, post, 'cs_single_2')
        assert error is None
        assert bump is not None


# ---------------------------------------------------------------------------
# Feed labels / lift / cap
# ---------------------------------------------------------------------------

class TestBumpFeed:
    def test_bumped_post_sorts_first_in_following(
        self, app, sample_user, second_user
    ):
        # second_user follows sample_user; sample_user has an old post
        # (bumped) and a fresh post.
        follow = Follow(follower_id=second_user.id, followed_id=sample_user.id)
        db.session.add(follow)
        old = _old_post(app, sample_user)
        fresh = _new_post(sample_user, 'Fresh post.')
        old.bumped_at = utc_now()
        old.bumped_until = utc_now() + timedelta(hours=24)
        db.session.commit()
        items = get_following_feed(second_user.id, per_page=10)['items']
        post_ids = [i['post'].id for i in items if i['kind'] == 'post']
        assert post_ids[0] == old.id
        assert fresh.id in post_ids

    def test_bumped_label_in_feed_html(
        self, app, client, sample_user, second_user, login_as
    ):
        follow = Follow(follower_id=second_user.id, followed_id=sample_user.id)
        db.session.add(follow)
        old = _old_post(app, sample_user)
        old.bumped_at = utc_now()
        old.bumped_until = utc_now() + timedelta(hours=24)
        db.session.commit()
        login_as(client, second_user.email, 'SecondPass123!')
        resp = client.get('/social/feed')
        assert resp.status_code == 200
        assert b'Bumped' in resp.data
        assert b'bumped' in resp.data  # "posted … · bumped …"

    def test_bump_cap_one_in_five(self, app, sample_user):
        posts = [_new_post(sample_user, f'Post {i}') for i in range(10)]
        for post in posts[:6]:
            post.bumped_at = utc_now()
            post.bumped_until = utc_now() + timedelta(hours=24)
        db.session.commit()
        items = [
            {'kind': 'post', 'post': p, 'sort_key': p.effective_at}
            for p in posts
        ]
        capped = apply_bump_cap(items)
        first_five = capped[:5]
        bumped_in_first_five = sum(
            1 for i in first_five if i['post'].is_bumped
        )
        assert bumped_in_first_five == 1
        # Nothing is dropped — the rest follows at the end.
        assert len(capped) == 10

    def test_boost_lifts_discover_score(self, app, sample_user):
        from models.bump import BOOST_DISCOVER_LIFT
        post = _new_post(sample_user)
        base = discover_score(post)
        boost = Boost(
            post_id=post.id, user_id=sample_user.id, days=1,
            price_cents=500, stripe_session_id='cs_boost_lift',
            starts_at=utc_now(), ends_at=utc_now() + timedelta(days=1),
        )
        db.session.add(boost)
        post.boosted_until = utc_now() + timedelta(days=1)
        db.session.commit()
        db.session.refresh(post)
        # The recency component decays between the two calls, so the
        # lift is exact only up to a small epsilon.
        assert discover_score(post) == pytest.approx(
            base + BOOST_DISCOVER_LIFT, abs=0.01
        )

    def test_boosted_label_in_feed_html(
        self, app, client, sample_user, second_user, login_as
    ):
        follow = Follow(follower_id=second_user.id, followed_id=sample_user.id)
        db.session.add(follow)
        post = _new_post(sample_user)
        post.boosted_until = utc_now() + timedelta(days=1)
        db.session.commit()
        login_as(client, second_user.email, 'SecondPass123!')
        resp = client.get('/social/feed')
        assert resp.status_code == 200
        assert b'Boosted' in resp.data


# ---------------------------------------------------------------------------
# Bump performance
# ---------------------------------------------------------------------------

class TestBumpPerformance:
    def test_performance_measures_gains_since_bump(
        self, app, sample_user, sample_hunter, second_user, third_user
    ):
        from models.post import Comment, Like
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        post.view_count = 10
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        profile.free_bumps = 1
        db.session.commit()
        _bump, error = apply_bump(sample_user, post, 'free')
        assert error is None
        db.session.commit()
        # Activity after the bump: views +25, 3 likes, 2 comments.
        post.view_count = 35
        for liker in (sample_hunter, second_user, third_user):
            db.session.add(Like(user_id=liker.id, post_id=post.id))
        for commenter in (sample_hunter, second_user):
            db.session.add(Comment(
                author_id=commenter.id, post_id=post.id,
                body='Nice post.',
            ))
        db.session.commit()
        rows = bump_performance_for_post(post.id)
        assert len(rows) == 1
        assert rows[0]['views'] == 25
        assert rows[0]['likes'] == 3
        assert rows[0]['comments'] == 2
        assert rows[0]['kind'] == 'bump'
        assert rows[0]['source'] == 'free'


# ---------------------------------------------------------------------------
# Boost
# ---------------------------------------------------------------------------

class TestBoost:
    def test_boost_fulfillment_sets_window(self, app, sample_user, sample_hunter):
        _make_member(sample_user, sample_hunter)
        post = _new_post(sample_user)
        boost, error = fulfill_boost(sample_user, post, 3, 'cs_boost_1')
        assert error is None
        db.session.commit()
        db.session.refresh(post)
        assert post.is_boosted is True
        assert (boost.ends_at - boost.starts_at).days == 3

    def test_boost_fulfillment_is_idempotent(self, app, sample_user, sample_hunter):
        _make_member(sample_user, sample_hunter)
        post = _new_post(sample_user)
        boost, _e = fulfill_boost(sample_user, post, 2, 'cs_boost_dup')
        db.session.commit()
        boost2, _e2 = fulfill_boost(sample_user, post, 2, 'cs_boost_dup')
        assert boost2.id == boost.id
        assert Boost.query.filter_by(post_id=post.id).count() == 1

    def test_boost_extends_active_boost(self, app, sample_user, sample_hunter):
        _make_member(sample_user, sample_hunter)
        post = _new_post(sample_user)
        boost1, _e = fulfill_boost(sample_user, post, 2, 'cs_boost_ext1')
        db.session.commit()
        first_end = boost1.ends_at
        boost2, _e2 = fulfill_boost(sample_user, post, 3, 'cs_boost_ext2')
        db.session.commit()
        # The extension starts where the first boost ended.
        assert boost2.starts_at == first_end
        assert (boost2.ends_at - boost2.starts_at).days == 3
        db.session.refresh(post)
        assert post.boosted_until == boost2.ends_at

    def test_boost_gate_newcomer_blocked_member_allowed(
        self, app, sample_user, sample_hunter
    ):
        post = _new_post(sample_user)
        ok, reason = boost_eligibility(sample_user, post)
        assert ok is False
        assert 'Member' in reason
        _make_member(sample_user, sample_hunter)
        ok, _reason = boost_eligibility(sample_user, post)
        assert ok is True

    def test_boost_route_gate_no_stripe_call_for_newcomer(
        self, app, client, sample_user, sample_post, login_as
    ):
        """The route checks the Member gate before creating a Checkout
        session — a Newcomer's buy never reaches Stripe."""
        AdminSettings.set('STRIPE_SECRET_KEY', 'sk_test_fake')
        AdminSettings.set('STRIPE_PUBLISHABLE_KEY', 'pk_test_fake')
        db.session.commit()
        login_as(client, sample_user.email, 'TestPass123!')
        with patch('stripe.checkout.Session.create') as create:
            resp = client.post(
                f'/payments/bumps/boost/{sample_post.id}/buy',
                data={'days': '1'},
            )
        create.assert_not_called()
        assert resp.status_code in (302, 303)
        assert Boost.query.count() == 0


# ---------------------------------------------------------------------------
# Recurring bumps
# ---------------------------------------------------------------------------

class TestRecurringBumps:
    def test_create_applies_first_bump_when_eligible(
        self, app, sample_user, sample_hunter
    ):
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        recurring, error = create_recurring_bump(
            sample_user, post, 'sub_rec_1'
        )
        assert error is None
        db.session.commit()
        db.session.refresh(post)
        assert post.is_bumped is True
        assert recurring.next_bump_at is not None
        assert (
            _as_naive(recurring.next_bump_at) - _as_naive(utc_now())
        ).days >= 2

    def test_create_defers_first_bump_when_ineligible(
        self, app, sample_user, sample_hunter
    ):
        _make_member(sample_user, sample_hunter)
        post = _new_post(sample_user)  # too fresh to bump
        recurring, error = create_recurring_bump(
            sample_user, post, 'sub_rec_2'
        )
        assert error is not None  # first bump deferred, not failed
        assert recurring is not None
        db.session.commit()
        assert RecurringBump.query.filter_by(
            stripe_subscription_id='sub_rec_2'
        ).first() is not None

    def test_runner_applies_due_bumps(self, app, sample_user, sample_hunter):
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        recurring, _e = create_recurring_bump(sample_user, post, 'sub_rec_3')
        db.session.commit()
        # Move the next bump to the past, let the first bump expire,
        # and backdate the first Bump row past the 1-bump-per-24h rule.
        recurring.next_bump_at = utc_now() - timedelta(hours=1)
        post.bumped_until = utc_now() - timedelta(hours=1)
        post.bumped_at = utc_now() - timedelta(days=1)
        first_bump = Bump.query.filter_by(post_id=post.id).first()
        first_bump.created_at = utc_now() - timedelta(days=2)
        db.session.commit()
        results = run_recurring_bumps()
        db.session.commit()
        assert len(results) == 1
        assert results[0][0] == recurring.id
        assert results[0][1] is True
        db.session.refresh(post)
        assert post.is_bumped is True

    def test_runner_defers_when_limits_block(
        self, app, sample_user, sample_hunter
    ):
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        recurring, _e = create_recurring_bump(sample_user, post, 'sub_rec_4')
        db.session.commit()
        # The first bump is still active — the 1-bump-per-24h rule blocks.
        recurring.next_bump_at = utc_now() - timedelta(hours=1)
        db.session.commit()
        results = run_recurring_bumps()
        db.session.commit()
        assert len(results) == 1
        assert results[0][1] is False  # deferred, not applied
        # Next attempt is tomorrow, not 3 days away.
        db.session.refresh(recurring)
        assert (
            _as_naive(recurring.next_bump_at) - _as_naive(utc_now())
        ).days == 0

    def test_cancel_recurring_bump_keeps_running_to_period_end(
        self, app, sample_user, sample_hunter
    ):
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        recurring, _e = create_recurring_bump(sample_user, post, 'sub_rec_5')
        db.session.commit()
        with patch(
            'utils.payments.cancel_subscription_at_period_end',
            return_value=(True, None),
        ):
            ok, error = cancel_recurring_bump(recurring)
        assert (ok, error) == (True, None)
        db.session.commit()
        # Status stays active — bumps keep running until the paid month
        # ends; the webhook marks it canceled when Stripe confirms.
        assert recurring.status == 'active'
        assert recurring.cancel_at_period_end is True

    def test_cancel_recurring_bump_without_stripe_id(
        self, app, sample_user, sample_hunter
    ):
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        recurring = RecurringBump(
            post_id=post.id, user_id=sample_user.id,
            stripe_subscription_id=None, status='active',
            next_bump_at=utc_now() + timedelta(days=3),
        )
        db.session.add(recurring)
        db.session.commit()
        ok, error = cancel_recurring_bump(recurring)
        assert (ok, error) == (True, None)
        db.session.commit()
        assert recurring.status == 'canceled'
        # The runner skips canceled rows.
        assert run_recurring_bumps() == []
