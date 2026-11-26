"""Trust score + levels (M3 subset of §3).

M3 completes the trust inputs: verified linked socials (+5 identity,
2+ links) and endorsements (+5 community, Trusted+ weigh double),
and Ambassador becomes reachable — 12 months, 15 completed
referrals, 90%+ positive ratings, Stripe KYC, and 2 verified linked
socials. Phone verification is still not built: the admin-verified
profile stands in for it as the identity proof (the Trusted gate
uses it).

M3 component mapping (§3.1):
- Verified identity (max 20): email verified +5, admin-verified
  profile +5, Stripe KYC (Connect payouts enabled) +5, 2+ verified
  linked socials +5.
- Transaction ratings (max 40): when revealed reviews exist, the
  average rating maps linearly ((avg - 1) / 4 x 40), 0 below 2
  completed referrals (Upwork's minimum-outcome rule). Until the
  first review is revealed, the M1 proxy stands (completed x 8,
  capped 40) so unrated users do not regress.
- Reliability (max 20): response rate to referrals received as a
  poster, x10, plus SLA compliance +10 (10 when no breach in the
  last 90 days, else 0).
- Community standing (max 10): karma bands, +1 per 100 karma (max 5)
  + endorsements, weighted (Trusted+ x2), max 5.
- Tenure (max 10): +1 per month on the platform.
- Penalties (§3.1): -10 per upheld report (12-month window), -5 per
  SLA breach (90-day window), -15 for a lost dispute (-7 after
  6 months, gone after 12). The spec lists both "Dispute lost -15"
  and "Dispute filed against you and upheld -10" — one event, so
  only -15 is applied, to the loser.

Level gates (§3.2): Member needs score 30 + email verified + 1
completed referral. Trusted needs score 60 + admin-verified profile
+ 5 completed + 85%+ positive ratings + 90%+ response rate (no
referrals received counts as passing — no opportunity to ghost).
Ambassador needs score 85 + Stripe KYC + 15 completed + 90%+
positive + 12 months + 2 verified linked socials.

Badge suspension: while a poster has any open SLA breach (a pitch
unanswered past its due date), the displayed level is capped at
Newcomer — the score itself is unchanged (the penalty is already
in it).
"""
from datetime import timedelta, timezone

from flask import g, has_request_context
from flask_babel import gettext as _, lazy_gettext as _l

from extensions import db
from models import utc_now
from models.bounty import Bounty
from models.connect_account import ConnectAccount
from models.dispute import Dispute
from models.match import Match
from models.moderation import Report
from models.pitch import Pitch
from models.post import Comment, Post
from models.profile import Profile
from models.review import revealed_reviews_for_user
from utils.sla import has_open_breach, sla_breach_count_90d

LEVELS = ['Newcomer', 'Member', 'Trusted', 'Ambassador']

# Display labels for trust levels (and the gamification ranks, which
# share the 'Newcomer' key). The LEVELS strings above are logic keys —
# code compares against them (e.g. level == 'Ambassador' for flag
# weight) — so they stay English everywhere in Python; only the
# rendered label is translated, via the level_label filter.
LEVEL_LABELS = {
    'Newcomer': _l('Newcomer'),
    'Member': _l('Member'),
    'Trusted': _l('Trusted'),
    'Ambassador': _l('Ambassador'),
    'Explorer': _l('Explorer'),
    'Contributor': _l('Contributor'),
}


def level_label(level):
    """The translated display label for a trust level / rank key.
    Unknown keys pass through unchanged."""
    return LEVEL_LABELS.get(level, level)


def _as_aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def completed_referrals_count(user_id):
    """Completed referrals (paid + free): matches confirmed by both sides."""
    return Match.query.filter_by(
        hunter_id=user_id, hunter_confirmed=True, employer_confirmed=True
    ).count()


def completed_referrals_counts(user_ids):
    """{user_id: completed referrals} for many users — one GROUP BY
    query instead of one COUNT per user (suggested_connections scores
    every candidate on the page)."""
    from sqlalchemy import func
    ids = list(user_ids)
    counts = {uid: 0 for uid in ids}
    if not ids:
        return counts
    for hunter_id, n in db.session.query(
        Match.hunter_id, func.count()
    ).filter(
        Match.hunter_id.in_(ids),
        Match.hunter_confirmed.is_(True),
        Match.employer_confirmed.is_(True),
    ).group_by(Match.hunter_id).all():
        counts[hunter_id] = n
    return counts


def response_rate(user_id):
    """Share of referrals received (as a poster) that got a response
    (interested or rejected). None when no referrals were received.

    Audit 2026-11-25: two COUNT queries (received, responded) in
    every trust computation — one conditional-aggregation query
    returns both numbers."""
    from sqlalchemy import case, func
    received, responded = db.session.query(
        func.count(Pitch.id),
        func.coalesce(func.sum(case(
            (Pitch.status.in_(['interested', 'rejected', 'accepted']), 1),
            else_=0,
        )), 0),
    ).join(
        Bounty, Pitch.bounty_id == Bounty.id
    ).filter(Bounty.poster_id == user_id).first()
    if received == 0:
        return None
    return responded / received


def average_response_time(user_id):
    """Median time (in hours) for the poster to answer pitches on
    their own bounties, plus how many answered pitches it came from:
    ``(median_hours, count)``, or ``None`` when no pitch was ever
    answered (never a fake zero).

    One query over the poster's bounties, mirroring response_rate():
    only pitches with status in ('interested', 'rejected',
    'accepted') count — pending/viewed pitches are still waiting
    and must not drag the number. The answer time is
    ``responded_at``, falling back to ``updated_at`` for pitches
    answered before M2 (same fallback as the SLA code). The median
    — not the mean — so one very slow pitch cannot wreck the
    number. There is no portable SQL median (Postgres has
    percentile_cont, SQLite has none), so the single query returns
    the answer/created timestamps and the median is taken in
    Python with statistics.median.

    This is a display metric only: it does not feed
    compute_trust_score / trust_level."""
    from sqlalchemy import func
    from statistics import median
    rows = db.session.query(
        func.coalesce(Pitch.responded_at, Pitch.updated_at),
        Pitch.created_at,
    ).join(
        Bounty, Pitch.bounty_id == Bounty.id
    ).filter(
        Bounty.poster_id == user_id,
        Pitch.status.in_(['interested', 'rejected', 'accepted']),
    ).all()
    hours = []
    for answered_at, created_at in rows:
        answered_at = _as_aware(answered_at)
        created_at = _as_aware(created_at)
        if answered_at is None or created_at is None:
            continue
        hours.append((answered_at - created_at).total_seconds() / 3600.0)
    if not hours:
        return None
    return float(median(hours)), len(hours)


def format_response_time(median_hours):
    """A median response time in hours as a short display string:
    under an hour -> "under an hour", hours -> "~N hours", a day
    or more -> "~N days". None in -> None out (no data, no claim).

    Single formatter for the response time everywhere it is shown
    (the Trust card and the reputation-metrics strip) — one string,
    both places. Previously utils/reputation.py had a second
    formatter ("≈ 6h" / "≈ 2d"); it was deleted in the
    consolidation and this one is the canonical formatter."""
    if median_hours is None:
        return None
    if median_hours < 1:
        return _('under an hour')
    if median_hours < 24:
        hours = round(median_hours)
        if hours == 1:
            return _('~1 hour')
        return _('~%(hours)d hours', hours=hours)
    days = round(median_hours / 24.0)
    if days == 1:
        return _('~1 day')
    return _('~%(days)d days', days=days)


def upheld_reports_count(user_id):
    """Reports upheld (actioned) against this user's content or profile
    in the last 12 months (§3.1 — penalties expire).

    Audit 2026-11-25: three COUNT queries (profile / post / comment
    targets) in every trust computation — one query with the same
    three disjoint target clauses returns the same total."""
    from sqlalchemy import and_, func, or_
    cutoff = utc_now() - timedelta(days=365)
    return Report.query.filter(
        Report.target_type.in_(['profile', 'post', 'comment']),
        Report.status == 'actioned',
        Report.created_at >= cutoff,
        or_(
            and_(
                Report.target_type == 'profile',
                Report.target_id == user_id,
            ),
            and_(
                Report.target_type == 'post',
                Report.target_id.in_(
                    db.session.query(Post.id).filter(
                        Post.author_id == user_id
                    )
                ),
            ),
            and_(
                Report.target_type == 'comment',
                Report.target_id.in_(
                    db.session.query(Comment.id).filter(
                        Comment.author_id == user_id
                    )
                ),
            ),
        ),
    ).count()


def ratings_summary(user_id, reviews=None):
    """(average rating 1–5, count, positive rate) over the user's
    revealed reviews — or (None, 0, None) when there are none. A
    review's rating is the mean of its two dimensions; positive means
    an average of 4.0 or better. ``reviews`` may be passed in when
    the caller already loaded them (the public profile page does —
    otherwise this re-queries the same rows)."""
    if reviews is None:
        reviews = revealed_reviews_for_user(user_id)
    if not reviews:
        return None, 0, None
    averages = [(r.rating_primary + r.rating_secondary) / 2 for r in reviews]
    avg = sum(averages) / len(averages)
    positive = sum(1 for a in averages if a >= 4.0) / len(averages)
    return round(avg, 2), len(averages), round(positive, 2)


def dispute_loss_penalty(user_id, now=None):
    """Penalty for disputes this user lost: -15 within 6 months of
    resolution, -7 between 6 and 12 months, 0 after a year. The loser
    is the respondent when the dispute was upheld, the raiser when it
    was dismissed.

    Audit 2026-11-25: this used to load EVERY resolved dispute in
    the database (plus one Match lookup each) on every trust
    computation — a full-table scan per author per feed render. It
    now joins Match and keeps only disputes where the user was the
    hunter or the employer (the loser is always one of the two), so
    the result is identical with one indexed query."""
    from sqlalchemy import or_
    now = _as_aware(now or utc_now())
    rows = (
        db.session.query(Dispute, Match)
        .join(Match, Dispute.match_id == Match.id)
        .filter(
            Dispute.status == 'resolved',
            Dispute.outcome.isnot(None),
            or_(
                Match.hunter_id == user_id,
                Match.employer_id == user_id,
            ),
        )
        .all()
    )
    penalty = 0
    for dispute, match in rows:
        if dispute.outcome == 'upheld':
            # The respondent (the other party) lost.
            loser = (
                match.employer_id
                if dispute.raised_by_id == match.hunter_id
                else match.hunter_id
            )
        elif dispute.outcome == 'dismissed':
            loser = dispute.raised_by_id
        else:
            continue
        if loser != user_id or dispute.resolved_at is None:
            continue
        age_days = (now - _as_aware(dispute.resolved_at)).days
        if age_days < 180:
            penalty += 15
        elif age_days < 365:
            penalty += 7
    return penalty


def _trust_cache():
    """Per-request cache for trust computations.

    A feed render scores every author on the page (Discover), the
    badges score them again, and endorsements score each endorser —
    without a cache the same user is computed 3+ times per request
    (measured: ~84 queries for one user, ~5,300 for a 120-post
    Discover render). The cache lives on flask.g, so it dies with
    the request; outside a request context (tests, CLI) there is no
    cache and every call computes fresh."""
    if not has_request_context():
        return None
    cache = g.get('_trust_cache')
    if cache is None:
        cache = g._trust_cache = {}
    return cache


# Sentinel: "no value was passed" — distinct from an explicitly
# passed None (the user really has no profile / Connect account).
# Audit 2026-11-25: the old `if profile is None` / `if connect is
# None` fallbacks could not tell the two apart, so a user with no
# Connect account was re-queried on every call.
_UNSET = object()


def identity_points(user, profile=_UNSET, connect=_UNSET,
                    linked_verified=None):
    """Verified-identity points (max 20). The profile, Connect
    account and verified-linked count may be passed in when the
    caller already loaded them (compute_trust_score does) so the
    same rows are not fetched three times per computation."""
    points = 0
    if user.email_verified:
        points += 5
    if profile is _UNSET:
        profile = Profile.query.filter_by(
            user_id=user.id, verified=True
        ).first()
    if profile is not None and profile.verified:
        points += 5
    if connect is _UNSET:
        connect = ConnectAccount.query.filter_by(user_id=user.id).first()
    if connect is not None and connect.kyc_passed:
        points += 5
    # M3: 2+ verified linked socials.
    if linked_verified is None:
        from utils.linked_accounts import verified_linked_count
        linked_verified = verified_linked_count(user.id)
    if linked_verified >= 2:
        points += 5
    return points


def has_kyc(user_id, connect=_UNSET):
    """Whether the user passed Stripe Connect KYC."""
    if connect is _UNSET:
        connect = ConnectAccount.query.filter_by(user_id=user_id).first()
    return bool(connect is not None and connect.kyc_passed)


def is_profile_verified(user_id, profile=_UNSET):
    """Whether the user has an admin-verified profile."""
    if profile is _UNSET:
        return Profile.query.filter_by(
            user_id=user_id, verified=True
        ).first() is not None
    return bool(profile is not None and profile.verified)


def compute_trust_score(user, include_endorsements=True):
    """Return (score 0–100, breakdown dict). Pure read — no writes.

    ``include_endorsements=False`` skips the endorsement component —
    used when weighting an endorser's level inside endorsement_points,
    so the computation cannot recurse (A endorses B, B endorses A).
    """
    cache = _trust_cache()
    cache_key = (user.id, include_endorsements)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    result = _compute_trust_score_uncached(user, include_endorsements)
    if cache is not None:
        cache[cache_key] = result
    return result


def _compute_trust_score_uncached(user, include_endorsements=True):
    """The trust computation itself (see compute_trust_score)."""
    profile = Profile.query.filter_by(user_id=user.id).first()
    karma = (profile.karma or 0) if profile else 0
    completed = completed_referrals_count(user.id)

    # Load the identity inputs once and share them: identity_points,
    # the KYC check and the verified-profile check each used to
    # re-query the same rows (3 Profile loads per computation).
    connect = ConnectAccount.query.filter_by(user_id=user.id).first()
    from utils.linked_accounts import verified_linked_count
    linked_verified = verified_linked_count(user.id)
    profile_verified = bool(profile is not None and profile.verified)
    kyc = bool(connect is not None and connect.kyc_passed)
    identity = identity_points(
        user, profile=profile, connect=connect,
        linked_verified=linked_verified,
    )

    ratings_avg, ratings_count, positive_rate = ratings_summary(user.id)
    if ratings_count > 0:
        transactions = round((ratings_avg - 1) / 4 * 40) if completed >= 2 else 0
    else:
        # No revealed reviews yet — keep the M1 completed-referrals
        # proxy so unrated users do not regress.
        transactions = min(40, completed * 8) if completed >= 2 else 0

    rate = response_rate(user.id)
    sla_90d = sla_breach_count_90d(user.id)
    reliability = (round(rate * 10) if rate is not None else 0)
    reliability += 10 if sla_90d == 0 else 0

    community = min(5, karma // 100)
    endorsement_pts = 0
    if include_endorsements:
        from utils.endorsements import endorsement_points
        endorsement_pts = endorsement_points(user.id)
        community += endorsement_pts
    community = min(10, community)
    created = _as_aware(user.created_at)
    months = max((utc_now() - created).days // 30, 0) if created else 0
    tenure = min(10, months)

    penalties = (
        upheld_reports_count(user.id) * 10
        + sla_90d * 5
        + dispute_loss_penalty(user.id)
    )

    score = max(0, min(100, identity + transactions + reliability + community + tenure - penalties))
    breakdown = {
        'identity': identity,
        'transactions': transactions,
        'reliability': reliability,
        'community': community,
        'tenure': tenure,
        'penalties': penalties,
        'completed_referrals': completed,
        'response_rate': rate,
        'karma': karma,
        'ratings_avg': ratings_avg,
        'ratings_count': ratings_count,
        'positive_rate': positive_rate,
        'sla_breaches': sla_90d,
        'profile_verified': profile_verified,
        'kyc': kyc,
        'linked_verified_count': linked_verified,
        'endorsement_points': endorsement_pts,
        'tenure_months': months,
    }
    return score, breakdown


def trust_level(user, include_endorsements=True):
    """Return (level name, score, breakdown). Gates per §3.2.

    While the user has an open SLA breach, the displayed level is
    capped at Newcomer (badge suspended)."""
    score, breakdown = compute_trust_score(
        user, include_endorsements=include_endorsements
    )
    # Copy: the breakdown may be the request-cached dict, and the
    # badge flag below must not leak into the cache.
    breakdown = dict(breakdown)
    completed = breakdown['completed_referrals']
    positive_rate = breakdown['positive_rate']
    rate = breakdown['response_rate']
    badge_suspended = has_open_breach(user.id)
    breakdown['badge_suspended'] = badge_suspended

    # The gates reuse the identity inputs compute_trust_score already
    # loaded (KYC, verified profile, linked count) instead of
    # re-querying them — they used to cost 3 more queries per level
    # check.
    if (
        score >= 85
        and breakdown.get('kyc')
        and completed >= 15
        and positive_rate is not None and positive_rate >= 0.90
        and breakdown['tenure_months'] >= 12
        and breakdown.get('linked_verified_count', 0) >= 2
    ):
        level = 'Ambassador'
    elif (
        score >= 60
        and breakdown['profile_verified']
        and completed >= 5
        and positive_rate is not None and positive_rate >= 0.85
        and (rate is None or rate >= 0.90)
    ):
        level = 'Trusted'
    elif score >= 30 and user.email_verified and completed >= 1:
        level = 'Member'
    else:
        level = 'Newcomer'
    if badge_suspended and level != 'Newcomer':
        level = 'Newcomer'
    return level, score, breakdown


def is_top_helper(user_id):
    """Top Helper badge (§4.2): 1,000+ karma."""
    from utils.karma import KARMA_TOP_HELPER
    profile = Profile.query.filter_by(user_id=user_id).first()
    return bool(profile and (profile.karma or 0) >= KARMA_TOP_HELPER)


def can_view_moderation_queue(user):
    """True if the user may VIEW the moderation queue: admin, moderator,
    or 5000+ karma (§3.4). Viewing is not acting — actions stay
    admin/moderator-only in the route."""
    if user.role in ('admin', 'moderator'):
        return True
    from utils.karma import KARMA_MOD_QUEUE
    profile = Profile.query.filter_by(user_id=user.id).first()
    return bool(profile and (profile.karma or 0) >= KARMA_MOD_QUEUE)
