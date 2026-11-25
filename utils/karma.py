"""Karma (M1, §4.2): a non-monetary reputation currency.

+50 per completed referral (paid or free — a completed referral is a
completed referral, §4.3 rule 1), +10 per like received on your
posts/comments, +10 when a share you made leads to a hire. Karma from
referrals is capped at +150/day. Karma is computed over a rolling
12-month window; Profile.karma is a cached sum, recomputed on every
award. Karma is never for sale and never convertible to cash.
"""
import uuid
from datetime import timedelta

from sqlalchemy import func

from extensions import db
from models import utc_now
from models.profile import Profile
from models.referral import KarmaEvent

KARMA_REFERRAL_COMPLETED = 50
KARMA_LIKE_RECEIVED = 10
KARMA_SHARE_LED_TO_HIRE = 10
REFERRAL_KARMA_DAILY_CAP = 150
KARMA_WINDOW_DAYS = 365

# Karma thresholds (§4.2) — privileges unlock as karma grows.
KARMA_DISCOVER = 100        # Post in Discover (not just Following)
KARMA_ENDORSE = 250         # Endorse other users (endorsements land in M3)
KARMA_ASK_DEADLINE = 500    # Community ask with a deadline (deadlines land later)
KARMA_TOP_HELPER = 1000     # "Top Helper" badge on profile
KARMA_FLAG_WEIGHT_X2 = 2500  # Reports count double
KARMA_MOD_QUEUE = 5000      # Moderation queue access


def _profile_for(user_id):
    return Profile.query.filter_by(user_id=user_id).first()


def recompute_karma(user_id):
    """Recompute the cached Profile.karma from the last 12 months of
    events. Returns the new total. Does not commit."""
    since = utc_now() - timedelta(days=KARMA_WINDOW_DAYS)
    total = db.session.query(
        func.coalesce(func.sum(KarmaEvent.amount), 0)
    ).filter(
        KarmaEvent.user_id == user_id,
        KarmaEvent.created_at >= since,
    ).scalar() or 0
    profile = _profile_for(user_id)
    if profile:
        profile.karma = max(total, 0)
    return max(total, 0)


def like_karma_ref(post_id, liker_id):
    """Stable reference id for a like's karma event: one (post, liker)
    pair maps to one UUID, so like -> unlike -> like cannot award
    karma twice. The Like row itself is deleted on unlike, so its id
    cannot be the key — the pair is the key. A uuid5 keeps the
    36-char shape of KarmaEvent.ref_id."""
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL, f'like:{post_id}:{liker_id}'
    ))


def award_karma(user_id, amount, reason, ref_type=None, ref_id=None):
    """Award karma to a user. Referral karma is capped at +150/day (§4.2).
    Idempotent when a ref is given: the same (user, reason, ref_type,
    ref_id) awards only once — a retry (webhook redelivery,
    double-submit, like -> unlike -> like) returns 0 instead of
    awarding again. Returns the amount actually awarded (0 if the cap
    was already hit or the award already exists).
    Does not commit — the caller commits."""
    if ref_type is not None and ref_id is not None:
        existing = KarmaEvent.query.filter_by(
            user_id=user_id, reason=reason,
            ref_type=ref_type, ref_id=ref_id,
        ).first()
        if existing is not None:
            return 0
    if reason == 'referral_completed':
        day_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        earned_today = db.session.query(
            func.coalesce(func.sum(KarmaEvent.amount), 0)
        ).filter(
            KarmaEvent.user_id == user_id,
            KarmaEvent.reason == 'referral_completed',
            KarmaEvent.created_at >= day_start,
        ).scalar() or 0
        amount = min(amount, max(REFERRAL_KARMA_DAILY_CAP - earned_today, 0))
        if amount <= 0:
            return 0
    db.session.add(KarmaEvent(
        user_id=user_id, amount=amount, reason=reason,
        ref_type=ref_type, ref_id=ref_id,
    ))
    db.session.flush()
    recompute_karma(user_id)
    return amount


def record_helped(user_id):
    """A completed referral: +1 to the public "Helped N people" counter
    and +1 free bump banked (§1.3 — bumps are spent in M3)."""
    profile = _profile_for(user_id)
    if profile:
        profile.helped_count = (profile.helped_count or 0) + 1
        profile.free_bumps = (profile.free_bumps or 0) + 1
