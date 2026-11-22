"""Bump/boost service (M3, §1.3).

Rules (all enforced here, not just in the routes):
- Only Member+ accounts can bump or boost — new accounts can't buy
  their way to the top on day one.
- A post must be 24 hours old before it can be bumped (a fresh post
  is already at the top — bumping it sells nothing). Boosts have no
  age rule: a boost buys reach beyond followers, which is real even
  for a fresh post.
- Max 1 bump per post per 24 hours; max 3 bumps per user per day.
- A bump lasts 24 hours and is always labeled "Bumped" in the feed.

Money: single bumps ($2), packs (5/$8, 10/$15) and boosts ($5/day) go
through Stripe Checkout (payment mode); recurring bumps ($5/month)
are Stripe subscriptions. The webhook fulfills them — fulfillment
functions here are idempotent on the Stripe session id.

Free bumps (1 per completed referral, banked on Profile.free_bumps
in M1) are spent before pack bumps, which are spent before a paid
bump is offered. The routes let the user choose; this module does the
spending.
"""
from datetime import timedelta

from extensions import db
from models import utc_now
from models.bump import (
    BOOST_DISCOVER_LIFT,  # noqa: F401  (re-exported for feed.py)
    BOOST_MAX_DAYS,
    BOOST_PRICE_PER_DAY_CENTS,
    BUMP_DURATION_HOURS,
    BUMP_MIN_POST_AGE_HOURS,
    BUMP_PACKS,
    BUMP_PER_POST_HOURS,
    BUMP_PER_USER_PER_DAY,
    BUMP_SINGLE_PRICE_CENTS,
    RECURRING_BUMP_PRICE_CENTS,
    Boost,
    Bump,
    BumpPackPurchase,
    RecurringBump,
)
from models.post import Post
from models.profile import Profile
from utils.trust import trust_level

__all__ = [
    'BOOST_MAX_DAYS', 'BOOST_PRICE_PER_DAY_CENTS', 'BUMP_PACKS',
    'BUMP_SINGLE_PRICE_CENTS', 'RECURRING_BUMP_PRICE_CENTS',
    'bump_eligibility', 'boost_eligibility', 'apply_bump',
    'free_bump_balance', 'pack_bump_balance', 'bumps_today',
    'fulfill_bump_pack', 'fulfill_single_bump', 'fulfill_boost',
    'create_recurring_bump', 'cancel_recurring_bump', 'run_recurring_bumps',
    'bump_performance_for_post',
]


def _as_aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=utc_now().tzinfo)


def _is_member_or_above(user):
    level, _score, _breakdown = trust_level(user)
    return level in ('Member', 'Trusted', 'Ambassador')


def bumps_today(user_id):
    """Bumps this user has applied today (UTC day)."""
    day_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return Bump.query.filter(
        Bump.user_id == user_id,
        Bump.created_at >= day_start,
    ).count()


def free_bump_balance(user_id):
    profile = Profile.query.filter_by(user_id=user_id).first()
    return (profile.free_bumps or 0) if profile else 0


def pack_bump_balance(user_id):
    """Bumps left across all the user's packs."""
    from sqlalchemy import func
    return db.session.query(
        func.coalesce(func.sum(BumpPackPurchase.bumps_remaining), 0)
    ).filter(BumpPackPurchase.user_id == user_id).scalar() or 0


def _spend_pack_bump(user_id):
    """Draw one bump from the oldest pack with bumps left. Returns
    True when a bump was drawn. Does not commit."""
    pack = (
        BumpPackPurchase.query
        .filter_by(user_id=user_id)
        .filter(BumpPackPurchase.bumps_remaining > 0)
        .order_by(BumpPackPurchase.created_at)
        .first()
    )
    if pack is None:
        return False
    pack.bumps_remaining -= 1
    return True


def bump_eligibility(user, post):
    """(ok, reason) — can this user bump this post right now?"""
    if not _is_member_or_above(user):
        return False, 'Only Member accounts and above can bump posts — complete a referral first.'
    created = _as_aware(post.created_at)
    if created and utc_now() < created + timedelta(hours=BUMP_MIN_POST_AGE_HOURS):
        return False, 'A post must be 24 hours old before it can be bumped.'
    since = utc_now() - timedelta(hours=BUMP_PER_POST_HOURS)
    recent = Bump.query.filter(
        Bump.post_id == post.id,
        Bump.created_at >= since,
    ).first()
    if recent is not None:
        return False, 'This post was already bumped in the last 24 hours.'
    if bumps_today(user.id) >= BUMP_PER_USER_PER_DAY:
        return False, 'You have used your 3 bumps for today.'
    return True, None


def boost_eligibility(user, post):
    """(ok, reason) — can this user boost this post right now?"""
    if not _is_member_or_above(user):
        return False, 'Only Member accounts and above can boost posts — complete a referral first.'
    return True, None


def apply_bump(user, post, source, stripe_session_id=None, price_cents=None,
               force=False):
    """Apply a bump to a post: creates the Bump row (with counter
    snapshots for performance tracking) and stamps the post. Spends a
    free bump or a pack bump for those sources. Returns (bump, error).
    ``force`` (webhook fulfillment of a paid bump) skips the limit
    checks — the money was already taken, so the bump applies. Does
    not commit — the caller commits."""
    if not force:
        ok, reason = bump_eligibility(user, post)
        if not ok:
            return None, reason
    if source == 'free':
        profile = Profile.query.filter_by(user_id=user.id).first()
        if not profile or (profile.free_bumps or 0) <= 0:
            return None, 'You have no free bumps left.'
        profile.free_bumps -= 1
    elif source == 'pack':
        if not _spend_pack_bump(user.id):
            return None, 'You have no pack bumps left.'
    now = utc_now()
    bump = Bump(
        post_id=post.id,
        user_id=user.id,
        source=source,
        stripe_session_id=stripe_session_id,
        price_cents=price_cents,
        views_at_bump=post.view_count or 0,
        likes_at_bump=post.like_count,
        comments_at_bump=post.comment_count,
        created_at=now,
        expires_at=now + timedelta(hours=BUMP_DURATION_HOURS),
    )
    db.session.add(bump)
    post.bumped_at = now
    post.bumped_until = bump.expires_at
    db.session.flush()
    return bump, None


# ---------------------------------------------------------------------------
# Webhook fulfillment (idempotent on the Stripe session id)
# ---------------------------------------------------------------------------

def fulfill_bump_pack(user_id, pack_size, price_cents, stripe_session_id):
    """Credit a purchased bump pack. Returns (purchase, created)."""
    existing = BumpPackPurchase.query.filter_by(
        stripe_session_id=stripe_session_id
    ).first()
    if existing is not None:
        return existing, False
    purchase = BumpPackPurchase(
        user_id=user_id,
        pack_size=pack_size,
        price_cents=price_cents,
        stripe_session_id=stripe_session_id,
        bumps_remaining=pack_size,
    )
    db.session.add(purchase)
    db.session.flush()
    return purchase, True


def fulfill_single_bump(user, post, stripe_session_id):
    """Apply a paid single bump (webhook). Returns (bump, error)."""
    existing = Bump.query.filter_by(stripe_session_id=stripe_session_id).first()
    if existing is not None:
        return existing, None
    return apply_bump(
        user, post, 'paid',
        stripe_session_id=stripe_session_id,
        price_cents=BUMP_SINGLE_PRICE_CENTS,
        force=True,
    )


def fulfill_boost(user, post, days, stripe_session_id):
    """Create a boost (webhook). If a boost is already active on the
    post, the new purchase extends it — the new row covers the
    extension window, so performance stays per-purchase. Returns
    (boost, error)."""
    existing = Boost.query.filter_by(stripe_session_id=stripe_session_id).first()
    if existing is not None:
        return existing, None
    days = max(1, min(BOOST_MAX_DAYS, int(days or 1)))
    now = utc_now()
    starts = now
    current_until = _as_aware(post.boosted_until)
    if post.is_boosted and current_until:
        # Extend: the new boost starts when the current one ends.
        starts = current_until
    ends = starts + timedelta(days=days)
    boost = Boost(
        post_id=post.id,
        user_id=user.id,
        days=days,
        price_cents=days * BOOST_PRICE_PER_DAY_CENTS,
        stripe_session_id=stripe_session_id,
        views_at_boost=post.view_count or 0,
        likes_at_boost=post.like_count,
        comments_at_boost=post.comment_count,
        starts_at=starts,
        ends_at=ends,
    )
    db.session.add(boost)
    post.boosted_until = ends
    db.session.flush()
    return boost, None


def create_recurring_bump(user, post, stripe_subscription_id):
    """Create a recurring-bump row (webhook, after the $5/month
    subscription starts) and apply the first bump immediately when the
    post is eligible — the user just paid for it. Returns
    (recurring, first_bump_error)."""
    existing = RecurringBump.query.filter_by(
        post_id=post.id, status='active'
    ).first()
    if existing is not None:
        return existing, None
    now = utc_now()
    recurring = RecurringBump(
        post_id=post.id,
        user_id=user.id,
        stripe_subscription_id=stripe_subscription_id,
        status='active',
        next_bump_at=now + timedelta(days=RecurringBump.INTERVAL_DAYS),
    )
    db.session.add(recurring)
    db.session.flush()
    # First bump now (eligibility still applies — e.g. a post under
    # 24h old waits for the runner). The reason is returned so the
    # caller can tell the user the first bump is deferred, not applied.
    ok, reason = bump_eligibility(user, post)
    if ok:
        _bump, error = apply_bump(user, post, 'recurring')
        if error is None:
            recurring.last_bumped_at = now
        return recurring, error
    return recurring, reason


def cancel_recurring_bump(recurring):
    """Cancel a recurring bump at the end of its paid month (bumps
    keep running until then). Returns (ok, error). Does not commit —
    the caller commits."""
    from utils.payments import cancel_subscription_at_period_end
    if not recurring.stripe_subscription_id:
        recurring.status = 'canceled'
        return True, None
    ok, error = cancel_subscription_at_period_end(
        recurring.stripe_subscription_id
    )
    if not ok:
        return False, error
    recurring.cancel_at_period_end = True
    return True, None


def run_recurring_bumps():
    """Apply every due recurring bump (the `flask run-recurring-bumps`
    CLI command / a server cron calls this). A bump blocked by the
    limits (1/post/24h, 3/day) is retried tomorrow instead of failing
    the subscription. Returns a list of (recurring_id, ok, message).
    Does not commit — the caller commits."""
    now = utc_now()
    due = (
        RecurringBump.query
        .filter_by(status='active')
        .filter(RecurringBump.next_bump_at <= now)
        .all()
    )
    results = []
    for recurring in due:
        post = db.session.get(Post, recurring.post_id)
        user = recurring.user
        if post is None or post.is_hidden or user is None:
            recurring.next_bump_at = now + timedelta(days=RecurringBump.INTERVAL_DAYS)
            results.append((recurring.id, False, 'Post unavailable — skipped.'))
            continue
        ok, reason = bump_eligibility(user, post)
        if not ok:
            # Limits (not an error): retry tomorrow.
            recurring.next_bump_at = now + timedelta(days=1)
            results.append((recurring.id, False, f'Deferred: {reason}'))
            continue
        _bump, error = apply_bump(user, post, 'recurring')
        if error:
            recurring.next_bump_at = now + timedelta(days=1)
            results.append((recurring.id, False, f'Deferred: {error}'))
            continue
        recurring.last_bumped_at = now
        recurring.next_bump_at = now + timedelta(days=RecurringBump.INTERVAL_DAYS)
        results.append((recurring.id, True, 'Bumped.'))
    return results


def bump_performance_for_post(post_id):
    """Bump/boost performance rows for one post (Pro analytics §1.4):
    each bump/boost with the views/likes/comments gained while it ran
    plus referrals attributed to it (referrals for the linked need
    created during the window)."""
    from models.bounty import Bounty
    from models.referral import Referral

    post = db.session.get(Post, post_id)
    if post is None:
        return []
    rows = []
    for bump in Bump.query.filter_by(post_id=post_id).order_by(Bump.created_at.desc()).all():
        perf = dict(bump.performance)
        perf['kind'] = 'bump'
        perf['source'] = bump.source
        perf['started_at'] = bump.created_at
        perf['ended_at'] = bump.expires_at
        perf['referrals'] = _referrals_in_window(post, bump.created_at, bump.expires_at)
        rows.append(perf)
    for boost in Boost.query.filter_by(post_id=post_id).order_by(Boost.created_at.desc()).all():
        perf = dict(boost.performance)
        perf['kind'] = 'boost'
        perf['source'] = 'boost'
        perf['started_at'] = boost.starts_at
        perf['ended_at'] = boost.ends_at
        perf['referrals'] = _referrals_in_window(post, boost.starts_at, boost.ends_at)
        rows.append(perf)
    rows.sort(key=lambda r: r['started_at'] or utc_now(), reverse=True)
    return rows


def _referrals_in_window(post, start, end):
    """Referrals for the post's linked need created inside a window."""
    if not post or not post.bounty_id:
        return 0
    from models.referral import Referral
    start = _as_aware(start)
    end = _as_aware(end)
    return Referral.query.filter(
        Referral.bounty_id == post.bounty_id,
        Referral.created_at >= start,
        Referral.created_at < end,
    ).count()
