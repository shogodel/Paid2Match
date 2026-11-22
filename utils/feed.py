"""Feed ranking (M1, §1.1; M3 bump/boost, §1.3).

Following: chronological, from people you follow (plus your own posts).
No algorithmic black box — this is a deliberate human-first choice. A
bump refreshes a post's effective timestamp for 24 hours (it returns
to the top), but bumped posts are capped at 1 in 5 — if everyone
bumps, nobody is at the top.

Discover: engagement + recency + trust-weighted, with the ranking rules
published on the page (PUBLISHED_DISCOVER_RULES). A bump does not
affect Discover ranking beyond the recency it creates; a boost adds a
fixed lift and is labeled "Boosted". Referral needs are capped at
about 1 in 3 posts — if the feed becomes a job board, the social
network has failed. The cap is applied by interleaving: needs are
slotted at most every third position; excess needs still appear, after
the interleaved portion, in rank order.

Feed items are dicts: {'kind': 'post'|'share', 'post': Post,
'share': Share|None, 'score': float|None, 'sort_key': datetime}.
"""
from datetime import timezone

from extensions import db
from models import utc_now
from models.follow import Follow
from models.moderation import Block
from models.post import Post, Share
from utils.trust import compute_trust_score

PUBLISHED_DISCOVER_RULES = (
    "Discover ranks posts by engagement (likes, comments and shares), "
    "how recent they are, and the author's trust score. Referral needs "
    "are capped at about 1 in 3 posts, so the feed stays social — not a "
    "job board. Your own posts don't appear in Discover. A bumped post "
    "counts as freshly posted for recency; a boosted post gets extra "
    "visibility and is always labeled Boosted."
)

# A post's recency bonus decays to zero after 48 hours.
RECENCY_WINDOW_HOURS = 48


def _as_aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def hidden_author_ids(viewer_id):
    """Authors whose content the viewer must not see: people the viewer
    blocked or muted, plus people who blocked the viewer."""
    if not viewer_id:
        return set()
    hidden = {
        b.blocked_id
        for b in Block.query.filter_by(blocker_id=viewer_id).all()
    }
    hidden |= {
        b.blocker_id
        for b in Block.query.filter_by(blocked_id=viewer_id, kind='block').all()
    }
    return hidden


def _visible_posts_query(viewer_id):
    query = Post.query.filter_by(is_hidden=False)
    hidden = hidden_author_ids(viewer_id)
    if hidden:
        query = query.filter(~Post.author_id.in_(hidden))
    return query


def _paginate(items, page, per_page):
    total = len(items)
    start = (page - 1) * per_page
    end = start + per_page
    return {
        'items': items[start:end],
        'page': page,
        'per_page': per_page,
        'total': total,
        'pages': (total + per_page - 1) // per_page,
        'has_prev': page > 1,
        'has_next': end < total,
    }


def get_following_feed(viewer_id, page=1, per_page=20):
    """Chronological feed: posts and shares from followed users + self.
    A bumped post sorts by its bump time (top for 24h); bumped posts
    are capped at 1 in 5 items so the feed never becomes an auction."""
    followed_ids = {
        f.followed_id
        for f in Follow.query.filter_by(follower_id=viewer_id).all()
    }
    author_ids = followed_ids | {viewer_id}

    posts = _visible_posts_query(viewer_id).filter(
        Post.author_id.in_(author_ids)
    ).all()
    items = [
        {'kind': 'post', 'post': p, 'share': None, 'score': None,
         'sort_key': p.effective_at or _as_aware(utc_now())}
        for p in posts
    ]

    hidden = hidden_author_ids(viewer_id)
    shares = Share.query.join(Post, Share.post_id == Post.id).filter(
        Share.user_id.in_(author_ids),
        Post.is_hidden == False,  # noqa: E712 (SQLAlchemy boolean comparison)
    )
    if hidden:
        shares = shares.filter(~Post.author_id.in_(hidden))
    for share in shares.all():
        items.append({
            'kind': 'share', 'post': share.post, 'share': share,
            'score': None, 'sort_key': _as_aware(share.created_at),
        })

    items.sort(key=lambda i: i['sort_key'] or _as_aware(utc_now()), reverse=True)
    items = apply_bump_cap(items)
    return _paginate(items, page, per_page)


def apply_bump_cap(items):
    """Cap bumped posts at 1 in 5 in a sorted feed, preserving sort
    order: a bumped post keeps its position (a fresh bump still sorts
    first), but a bumped post is deferred to the end when placing it
    would put more than 1 bumped post in the first 5 items, more than
    2 in the first 10, and so on. Deferred posts still appear, later.
    Shares of bumped posts are not bumped — only the post itself."""
    from models.bump import BUMP_FEED_CAP_DENOMINATOR
    out = []
    deferred = []
    bumped_placed = 0
    for item in items:
        is_bumped = item['kind'] == 'post' and item['post'].is_bumped
        if not is_bumped:
            out.append(item)
            continue
        # Placing this bumped post now would make it the (n+1)-th
        # bumped post among len(out)+1 items: allowed only if that
        # stays within 1 per denominator (1st bumped may sit anywhere
        # in the first 5, the 2nd no earlier than position 6, ...).
        allowed = (
            (bumped_placed + 1)
            <= (len(out) + 1 + BUMP_FEED_CAP_DENOMINATOR - 1)
            // BUMP_FEED_CAP_DENOMINATOR
        )
        if allowed:
            out.append(item)
            bumped_placed += 1
        else:
            deferred.append(item)
    out.extend(deferred)
    return out


def discover_score(post):
    """Engagement + recency + trust. Engagement: likes x2, comments x3,
    shares x2. Recency: up to 48 points, decaying to zero over 48 hours
    from the post's effective time (a bump refreshes it — the only
    Discover effect a bump has). Trust: author's trust score / 10
    (0–10). A boost adds a fixed lift (BOOST_DISCOVER_LIFT)."""
    from models.bump import BOOST_DISCOVER_LIFT
    now = utc_now()
    created = post.effective_at or now
    hours_old = max((now - created).total_seconds() / 3600.0, 0)
    engagement = post.like_count * 2 + post.comment_count * 3 + post.share_count * 2
    recency = max(0, RECENCY_WINDOW_HOURS - hours_old)
    trust = compute_trust_score(post.author)[0] / 10.0 if post.author else 0
    score = engagement + recency + trust
    if post.is_boosted:
        score += BOOST_DISCOVER_LIFT
    return score


def apply_need_cap(ranked_posts):
    """Interleave ranked posts so referral needs appear at most ~1 in 3.
    Needs and non-needs each keep their rank order. Leftover needs are
    appended at the end (they still appear, just later)."""
    needs = [p for p in ranked_posts if p.is_need]
    others = [p for p in ranked_posts if not p.is_need]
    out = []
    ni = oi = 0
    while oi < len(others) or ni < len(needs):
        for _ in range(2):
            if oi < len(others):
                out.append(others[oi])
                oi += 1
        if ni < len(needs):
            out.append(needs[ni])
            ni += 1
        if oi >= len(others) and ni < len(needs):
            # No non-needs left to interleave with — drain the needs.
            out.extend(needs[ni:])
            break
    return out


def get_discover_feed(viewer_id, page=1, per_page=20):
    """Ranked feed for everyone else's posts (own posts excluded)."""
    posts = _visible_posts_query(viewer_id).filter(
        Post.author_id != viewer_id
    ).all()
    ranked = sorted(posts, key=discover_score, reverse=True)
    capped = apply_need_cap(ranked)
    items = [
        {'kind': 'post', 'post': p, 'share': None,
         'score': discover_score(p), 'sort_key': _as_aware(p.created_at)}
        for p in capped
    ]
    return _paginate(items, page, per_page)


def create_need_post(bounty):
    """Create the feed Post for a newly posted referral need (§1.1: every
    referral need is a post). The post body is the need's description;
    the title lives on the bounty and is shown as the card heading.
    Does not commit — the caller commits."""
    post_type = 'need_free' if (bounty.is_free or bounty.is_free_bounty) else 'need_paid'
    post = Post(
        author_id=bounty.poster_id,
        post_type=post_type,
        body=bounty.description,
        bounty_id=bounty.id,
    )
    db.session.add(post)
    return post


def suggested_connections(user_id, limit=5):
    """People who refer in your area (§1.2): users whose profile location
    matches yours (same city/region substring), ordered by completed
    referrals, excluding people you already follow and yourself."""
    from models.profile import Profile
    from models.user import User
    from utils.trust import completed_referrals_count

    my_profile = Profile.query.filter_by(user_id=user_id).first()
    if not my_profile or not my_profile.location:
        return []
    # Match on the first location segment ("Laval, QC" -> "Laval").
    city = my_profile.location.split(',')[0].strip()
    if not city:
        return []
    followed_ids = {
        f.followed_id
        for f in Follow.query.filter_by(follower_id=user_id).all()
    }
    followed_ids.add(user_id)
    hidden = hidden_author_ids(user_id)
    candidates = User.query.join(
        Profile, Profile.user_id == User.id
    ).filter(
        Profile.location.ilike(f'%{city}%'),
        User.is_active == True,  # noqa: E712
        User.is_deleted == False,  # noqa: E712
        ~User.id.in_(followed_ids),
    ).all()
    candidates = [u for u in candidates if u.id not in hidden]
    candidates.sort(key=lambda u: completed_referrals_count(u.id), reverse=True)
    return candidates[:limit]
