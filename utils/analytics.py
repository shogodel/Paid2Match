"""Basic analytics — the free tier (§1.4). Everyone sees: post views,
likes/comments/shares (per post and totals), profile views (last 7 days,
count only — never viewer names), follower counts + new followers this
week, referral stats (made/accepted/completed, paid + free), earnings
(paid out / pending / lifetime), and karma + recent changes.

Pro analytics ($9/month, M3) add: members reached (unique viewers per
post), engagement rate with the 1–3–5% benchmarks, 90-day profile-view
history with viewer names (opt-in on the viewer's side — see
Profile.profile_view_names), search appearances, audience demographics,
referral funnel, bump performance, best time to post, and CSV export.

Earnings honesty: payouts flow through Stripe Connect (or a recorded
manual Interac payout) from M2 on, so "pending" means "confirmed
matches whose payout has not completed yet" and "paid out" counts
matches whose payout_status is 'completed'. The math here is
Match-based; Payout rows mirror the same statuses, so the two agree.
"""
import csv
import io
from collections import Counter
from datetime import timedelta, timezone

from sqlalchemy import func

from extensions import db
from models import utc_now
from models.follow import Follow
from models.match import Match
from models.pitch import Pitch
from models.post import Post
from models.post_view import PostView, SearchAppearance
from models.profile import Profile
from models.profile_view import ProfileView
from models.referral import KarmaEvent, Referral


def get_analytics(user_id):
    """Return the free-tier analytics dict for one user."""
    now = utc_now()
    week_ago = now - timedelta(days=7)

    posts = Post.query.filter_by(author_id=user_id, is_hidden=False).all()
    post_rows = [{
        'post': p,
        'views': p.view_count or 0,
        'likes': p.like_count,
        'comments': p.comment_count,
        'shares': p.share_count,
    } for p in posts]
    post_rows.sort(key=lambda r: r['views'], reverse=True)

    profile_views_7d = ProfileView.query.filter(
        ProfileView.profile_user_id == user_id,
        ProfileView.created_at >= week_ago,
    ).count()

    followers = Follow.query.filter_by(followed_id=user_id).count()
    following = Follow.query.filter_by(follower_id=user_id).count()
    new_followers_7d = Follow.query.filter(
        Follow.followed_id == user_id,
        Follow.created_at >= week_ago,
    ).count()

    referrals_made = Referral.query.filter_by(referrer_id=user_id).count()
    referrals_accepted = Referral.query.join(
        Pitch, Referral.pitch_id == Pitch.id
    ).filter(
        Referral.referrer_id == user_id,
        Pitch.status.in_(['interested', 'accepted']),
    ).count()
    referrals_completed = Referral.query.filter_by(
        referrer_id=user_id, status='completed'
    ).count()
    # Paid vs free, by the need's bounty.
    from models.bounty import Bounty
    completed_paid = Referral.query.join(
        Bounty, Referral.bounty_id == Bounty.id
    ).filter(
        Referral.referrer_id == user_id,
        Referral.status == 'completed',
        Bounty.reward_amount.isnot(None),
        Bounty.reward_amount > 0,
    ).count()

    # Earnings from completed matches (hunter side). payout_amount is the
    # full reward; the platform fee is what the platform keeps.
    matches = Match.query.filter_by(
        hunter_id=user_id, hunter_confirmed=True, employer_confirmed=True
    ).all()
    pending = 0.0
    paid_out = 0.0
    for m in matches:
        net = float(m.payout_amount or 0) - float(m.platform_fee or 0)
        if m.payout_status == 'completed':
            paid_out += net
        else:
            pending += net

    profile = Profile.query.filter_by(user_id=user_id).first()
    karma = (profile.karma or 0) if profile else 0
    helped = (profile.helped_count or 0) if profile else 0
    recent_karma = KarmaEvent.query.filter_by(user_id=user_id).order_by(
        KarmaEvent.created_at.desc()
    ).limit(10).all()

    return {
        'post_rows': post_rows,
        'totals': {
            'views': sum(r['views'] for r in post_rows),
            'likes': sum(r['likes'] for r in post_rows),
            'comments': sum(r['comments'] for r in post_rows),
            'shares': sum(r['shares'] for r in post_rows),
        },
        'profile_views_7d': profile_views_7d,
        'followers': followers,
        'following': following,
        'new_followers_7d': new_followers_7d,
        'referrals': {
            'made': referrals_made,
            'accepted': referrals_accepted,
            'completed': referrals_completed,
            'completed_paid': completed_paid,
            'completed_free': referrals_completed - completed_paid,
        },
        'earnings': {
            'pending': round(pending, 2),
            'paid_out': round(paid_out, 2),
            'lifetime': round(pending + paid_out, 2),
        },
        'karma': karma,
        'helped': helped,
        'recent_karma': recent_karma,
    }


# ---------------------------------------------------------------------------
# Pro analytics (M3, §1.4)
# ---------------------------------------------------------------------------

# Engagement-rate benchmarks, shown next to every rate (§1.4: 1–3%
# typical, 3–5% strong, 5%+ exceptional).
ENGAGEMENT_BENCHMARKS = {'typical': 0.01, 'strong': 0.03, 'exceptional': 0.05}


def _as_aware(dt):
    """Treat a naive datetime (SQLite stores naive) as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _members_reached(post_id):
    """Unique viewers of a post: distinct logged-in viewers plus
    anonymous session views (the anonymous part is an estimate —
    one browser session could be the same person twice, so the
    template labels reached as an estimate where anonymous views
    are included)."""
    logged_in = db.session.query(
        func.count(func.distinct(PostView.viewer_id))
    ).filter(
        PostView.post_id == post_id,
        PostView.viewer_id.isnot(None),
    ).scalar() or 0
    anonymous = PostView.query.filter_by(
        post_id=post_id, viewer_id=None
    ).count()
    return logged_in + anonymous


def _engagement_rate(engagements, reached):
    if not reached:
        return None
    return round(engagements / reached, 4)


def _profile_views_90d(user_id):
    """90-day profile-view history: total, daily trend (last 30 days),
    and named viewers — only viewers who opted in
    (Profile.profile_view_names)."""
    now = utc_now()
    start_90d = now - timedelta(days=90)
    start_30d = now - timedelta(days=30)
    views = ProfileView.query.filter(
        ProfileView.profile_user_id == user_id,
        ProfileView.created_at >= start_90d,
    ).all()
    trend = Counter()
    for view in views:
        created = _as_aware(view.created_at)
        if created and created >= start_30d:
            trend[created.date().isoformat()] += 1
    # Named viewers, opt-in only.
    viewer_ids = {v.viewer_id for v in views if v.viewer_id}
    named = []
    if viewer_ids:
        from models.user import User
        opted_in = {
            p.user_id
            for p in Profile.query.filter(
                Profile.user_id.in_(viewer_ids),
                Profile.profile_view_names.is_(True),
            ).all()
        }
        for viewer_id in sorted(viewer_ids & opted_in):
            viewer = db.session.get(User, viewer_id)
            if viewer:
                named.append(viewer.full_name)
    return {
        'total_90d': len(views),
        'trend_30d': sorted(trend.items()),
        'named_viewers': named,
    }


def _search_appearances(user_id):
    """Search appearances in the last 90 days + top queries."""
    start = utc_now() - timedelta(days=90)
    appearances = SearchAppearance.query.filter(
        SearchAppearance.searched_user_id == user_id,
        SearchAppearance.created_at >= start,
    ).all()
    queries = Counter(a.search_query for a in appearances if a.search_query)
    return {
        'total_90d': len(appearances),
        'top_queries': queries.most_common(5),
    }


def _demographics(user_id):
    """Follower breakdown by location and profile type — the only
    demographic dimensions the platform honestly has (no industry
    field exists; location + role type it is)."""
    from models.user import User
    followers = (
        User.query
        .join(Follow, Follow.follower_id == User.id)
        .join(Profile, Profile.user_id == User.id)
        .filter(Follow.followed_id == user_id)
        .all()
    )
    locations = Counter()
    roles = Counter()
    for follower in followers:
        profile = Profile.query.filter_by(user_id=follower.id).first()
        if profile:
            if profile.location:
                locations[profile.location.split(',')[0].strip()] += 1
            if profile.profile_type:
                roles[profile.profile_type] += 1
    return {
        'by_location': locations.most_common(5),
        'by_role_type': roles.most_common(),
        'total': len(followers),
    }


def _referral_funnel(user_id):
    """Need views → referrals → confirms → paid, per need and overall.
    Views come from the need post's view count; referrals from
    Referral rows; confirms from dual-confirmed matches; paid from
    matches whose payout completed."""
    from models.bounty import Bounty
    bounties = Bounty.query.filter_by(poster_id=user_id).all()
    rows = []
    totals = {'views': 0, 'referrals': 0, 'confirms': 0, 'paid': 0}
    for bounty in bounties:
        post = Post.query.filter_by(bounty_id=bounty.id).first()
        views = (post.view_count or 0) if post else 0
        referrals = Referral.query.filter_by(bounty_id=bounty.id).count()
        matches = Match.query.filter_by(bounty_id=bounty.id).all()
        confirms = sum(
            1 for m in matches
            if m.hunter_confirmed and m.employer_confirmed
        )
        paid = sum(1 for m in matches if m.payout_status == 'completed')
        rows.append({
            'bounty': bounty,
            'views': views,
            'referrals': referrals,
            'confirms': confirms,
            'paid': paid,
        })
        totals['views'] += views
        totals['referrals'] += referrals
        totals['confirms'] += confirms
        totals['paid'] += paid
    rows.sort(key=lambda r: r['views'], reverse=True)
    return {'rows': rows, 'totals': totals}


def _best_time_to_post(user_id):
    """The hour of day (UTC) when this user's audience engages most,
    from like/comment timestamps on their posts. Honest label: it
    measures engagement times, not logins — the platform does not
    track when people are online."""
    from models.post import Comment, Like
    post_ids = [
        p.id for p in Post.query.filter_by(author_id=user_id).all()
    ]
    if not post_ids:
        return None
    hours = Counter()
    for like in Like.query.filter(Like.post_id.in_(post_ids)).all():
        if like.created_at:
            hours[like.created_at.hour] += 1
    for comment in Comment.query.filter(Comment.post_id.in_(post_ids)).all():
        if comment.created_at:
            hours[comment.created_at.hour] += 1
    if not hours:
        return None
    best_hour, count = hours.most_common(1)[0]
    return {'hour': best_hour, 'engagements': count, 'by_hour': sorted(hours.items())}


def get_pro_analytics(user_id):
    """Return the Pro analytics dict for one user (§1.4)."""
    from utils.bumps import bump_performance_for_post

    posts = Post.query.filter_by(author_id=user_id, is_hidden=False).all()
    post_rows = []
    for post in posts:
        reached = _members_reached(post.id)
        engagements = post.like_count + post.comment_count + post.share_count
        post_rows.append({
            'post': post,
            'views': post.view_count or 0,
            'reached': reached,
            'engagement_rate': _engagement_rate(engagements, reached),
            'likes': post.like_count,
            'comments': post.comment_count,
            'shares': post.share_count,
            'bump_performance': bump_performance_for_post(post.id),
        })
    post_rows.sort(key=lambda r: r['views'], reverse=True)

    return {
        'post_rows': post_rows,
        'benchmarks': ENGAGEMENT_BENCHMARKS,
        'profile_views': _profile_views_90d(user_id),
        'search': _search_appearances(user_id),
        'demographics': _demographics(user_id),
        'funnel': _referral_funnel(user_id),
        'best_time': _best_time_to_post(user_id),
    }


def pro_analytics_csv(user_id):
    """CSV export of the Pro analytics: the per-post table, then the
    referral funnel. Returns the CSV text."""
    pro = get_pro_analytics(user_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Post', 'Type', 'Views', 'Members reached',
                     'Engagement rate', 'Likes', 'Comments', 'Shares'])
    for row in pro['post_rows']:
        post = row['post']
        rate = (
            f"{row['engagement_rate'] * 100:.2f}%"
            if row['engagement_rate'] is not None else ''
        )
        writer.writerow([
            post.body[:120].replace('\n', ' '),
            post.post_type,
            row['views'],
            row['reached'],
            rate,
            row['likes'],
            row['comments'],
            row['shares'],
        ])
    writer.writerow([])
    writer.writerow(['Referral funnel'])
    writer.writerow(['Need', 'Views', 'Referrals', 'Confirms', 'Paid'])
    for row in pro['funnel']['rows']:
        writer.writerow([
            row['bounty'].title,
            row['views'],
            row['referrals'],
            row['confirms'],
            row['paid'],
        ])
    totals = pro['funnel']['totals']
    writer.writerow(['TOTAL', totals['views'], totals['referrals'],
                     totals['confirms'], totals['paid']])
    return buf.getvalue()
