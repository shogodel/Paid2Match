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

from flask_babel import lazy_gettext as _l

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


def response_rate(user_id):
    """Share of referrals received (as a poster) that got a response
    (interested or rejected). None when no referrals were received."""
    received = Pitch.query.join(
        Bounty, Pitch.bounty_id == Bounty.id
    ).filter(Bounty.poster_id == user_id).count()
    if received == 0:
        return None
    responded = Pitch.query.join(
        Bounty, Pitch.bounty_id == Bounty.id
    ).filter(
        Bounty.poster_id == user_id,
        # M3 fix: 'accepted' is a response too — a poster who accepts
        # referrals was scoring a 0% response rate, which blocked the
        # Trusted/Ambassador gates that require >= 90%.
        Pitch.status.in_(['interested', 'rejected', 'accepted']),
    ).count()
    return responded / received


def upheld_reports_count(user_id):
    """Reports upheld (actioned) against this user's content or profile
    in the last 12 months (§3.1 — penalties expire)."""
    cutoff = utc_now() - timedelta(days=365)
    count = Report.query.filter_by(
        target_type='profile', target_id=user_id, status='actioned'
    ).filter(Report.created_at >= cutoff).count()
    count += Report.query.join(
        Post, Report.target_id == Post.id
    ).filter(
        Report.target_type == 'post',
        Report.status == 'actioned',
        Post.author_id == user_id,
        Report.created_at >= cutoff,
    ).count()
    count += Report.query.join(
        Comment, Report.target_id == Comment.id
    ).filter(
        Report.target_type == 'comment',
        Report.status == 'actioned',
        Comment.author_id == user_id,
        Report.created_at >= cutoff,
    ).count()
    return count


def ratings_summary(user_id):
    """(average rating 1–5, count, positive rate) over the user's
    revealed reviews — or (None, 0, None) when there are none. A
    review's rating is the mean of its two dimensions; positive means
    an average of 4.0 or better."""
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
    was dismissed."""
    now = _as_aware(now or utc_now())
    disputes = Dispute.query.filter(
        Dispute.status == 'resolved',
        Dispute.outcome.isnot(None),
    ).all()
    penalty = 0
    for dispute in disputes:
        match = db.session.get(Match, dispute.match_id)
        if match is None:
            continue
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


def identity_points(user):
    """Verified-identity points (max 20)."""
    points = 0
    if user.email_verified:
        points += 5
    verified_profile = Profile.query.filter_by(
        user_id=user.id, verified=True
    ).first()
    if verified_profile is not None:
        points += 5
    connect = ConnectAccount.query.filter_by(user_id=user.id).first()
    if connect is not None and connect.kyc_passed:
        points += 5
    # M3: 2+ verified linked socials.
    from utils.linked_accounts import verified_linked_count
    if verified_linked_count(user.id) >= 2:
        points += 5
    return points


def has_kyc(user_id):
    """Whether the user passed Stripe Connect KYC."""
    connect = ConnectAccount.query.filter_by(user_id=user_id).first()
    return bool(connect is not None and connect.kyc_passed)


def is_profile_verified(user_id):
    """Whether the user has an admin-verified profile."""
    return Profile.query.filter_by(
        user_id=user_id, verified=True
    ).first() is not None


def compute_trust_score(user, include_endorsements=True):
    """Return (score 0–100, breakdown dict). Pure read — no writes.

    ``include_endorsements=False`` skips the endorsement component —
    used when weighting an endorser's level inside endorsement_points,
    so the computation cannot recurse (A endorses B, B endorses A).
    """
    profile = Profile.query.filter_by(user_id=user.id).first()
    karma = (profile.karma or 0) if profile else 0
    completed = completed_referrals_count(user.id)

    identity = identity_points(user)

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
        'profile_verified': is_profile_verified(user.id),
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
    completed = breakdown['completed_referrals']
    positive_rate = breakdown['positive_rate']
    rate = breakdown['response_rate']
    badge_suspended = has_open_breach(user.id)
    breakdown['badge_suspended'] = badge_suspended

    from utils.linked_accounts import verified_linked_count
    if (
        score >= 85
        and has_kyc(user.id)
        and completed >= 15
        and positive_rate is not None and positive_rate >= 0.90
        and breakdown['tenure_months'] >= 12
        and verified_linked_count(user.id) >= 2
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
