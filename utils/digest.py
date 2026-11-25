"""Weekly digest email (Phase 3).

One plain-text email per week per verified user: the week's top posts,
new people to follow, new referral needs in their spaces, and their
week's stats. Sent by the ``flask send-weekly-digest`` CLI command
(app.py) — the cron line lives in DEPLOYMENT.md.

Sending follows utils/email.py conventions exactly: ``send_simple_email``
logs a warning and returns False when MAIL_SERVER is unset, so on the
live container (no MAIL_* env vars) the command runs, reports 0 sent,
and does not crash. That is expected until SMTP is configured — the
digest must never invent mail credentials or work around the missing
config.

Content is English, like the other transactional emails.
"""
import logging
from datetime import timedelta

from flask import url_for
from sqlalchemy import desc, func

from extensions import db
from models import utc_now
from models.follow import Follow
from models.post import Like, Post
from models.referral import KarmaEvent
from models.space import SpaceMembership
from models.user import User
from utils.email import send_simple_email
from utils.feed import get_trending_feed, suggested_connections

logger = logging.getLogger(__name__)

WEEK_DAYS = 7


def _week_ago():
    return utc_now() - timedelta(days=WEEK_DAYS)


def _top_posts(user_id, limit=3):
    """The week's top posts by pure engagement velocity — the same
    ranking as the Trending tab, so the digest and the site agree."""
    result = get_trending_feed(user_id, per_page=limit)
    return [item['post'] for item in result['items']]


def _new_needs_in_spaces(user_id, limit=3):
    """Referral needs posted in the last 7 days in spaces the user is
    a member of. Needs are posts (post_type need_paid/need_free) with
    a bounty attached — and only publicly visible ones: an unsecured
    paid need is not public yet, so it must not leak into digests
    either."""
    from models.bounty import Bounty
    space_ids = {
        m.space_id
        for m in SpaceMembership.query.filter_by(user_id=user_id).all()
    }
    if not space_ids:
        return []
    posts = Post.query.join(
        Bounty, Post.bounty_id == Bounty.id
    ).filter(
        Post.space_id.in_(space_ids),
        Post.post_type.in_(['need_paid', 'need_free']),
        Post.is_hidden == False,  # noqa: E712
        Post.created_at >= _week_ago(),
        Bounty.publicly_visible_filter(),
    ).order_by(desc(Post.created_at)).limit(limit).all()
    return posts


def _week_stats(user_id):
    """The user's week in numbers: karma earned, new followers, and
    likes received on their posts. All from timestamped rows, so the
    numbers are the week, not all time."""
    week_ago = _week_ago()
    karma = db.session.query(
        func.coalesce(func.sum(KarmaEvent.amount), 0)
    ).filter(
        KarmaEvent.user_id == user_id,
        KarmaEvent.created_at >= week_ago,
    ).scalar()
    new_followers = Follow.query.filter(
        Follow.followed_id == user_id,
        Follow.created_at >= week_ago,
    ).count()
    my_post_ids = db.session.query(Post.id).filter(
        Post.author_id == user_id
    )
    likes = Like.query.filter(
        Like.post_id.in_(my_post_ids),
        Like.created_at >= week_ago,
    ).count()
    return {
        'karma_earned': int(karma or 0),
        'new_followers': new_followers,
        'likes_received': likes,
    }


def build_digest(user):
    """Build one user's digest. Returns (subject, body) — plain text,
    English. Sections with no content are skipped, so a quiet week
    still reads well."""
    profile = user.profiles.first()
    name = (
        profile.display_name
        if profile and profile.display_name
        else user.full_name
    )
    lines = [f"Hi {name},\n", "Here's your week on Paid2Match.\n"]

    top_posts = _top_posts(user.id)
    if top_posts:
        lines.append("Top posts this week")
        lines.append("-" * 21)
        for post in top_posts:
            author = post.author.full_name if post.author else ''
            snippet = (post.body or '').strip().replace('\n', ' ')
            if len(snippet) > 120:
                snippet = snippet[:117] + '...'
            lines.append(
                f"- {author}: {snippet} "
                f"({post.like_count} likes, {post.comment_count} comments)"
            )
            lines.append(
                f"  {url_for('social.post_detail', post_id=post.id, _external=True)}"
            )
        lines.append('')

    people = suggested_connections(user.id, limit=3)
    if people:
        lines.append('New people to follow')
        lines.append('-' * 20)
        for person in people:
            p = person.profiles.first()
            headline = p.headline if p and p.headline else ''
            line = f"- {person.full_name}"
            if headline:
                line += f" — {headline}"
            lines.append(line)
        lines.append(
            f"  {url_for('social.people', _external=True)}"
        )
        lines.append('')

    needs = _new_needs_in_spaces(user.id)
    if needs:
        lines.append('New referral needs in your spaces')
        lines.append('-' * 33)
        for post in needs:
            bounty = post.bounty
            reward = (
                'Free need'
                if (bounty.is_free or bounty.is_free_bounty)
                else f"${bounty.reward_amount:,.0f}"
                if bounty.reward_amount else 'Reward TBD'
            )
            lines.append(f"- {bounty.title} ({reward})")
            lines.append(
                f"  {url_for('bounties.detail', slug_code=bounty.url_slug, _external=True)}"
            )
        lines.append('')

    stats = _week_stats(user.id)
    lines.append('Your week')
    lines.append('-' * 9)
    lines.append(f"- Karma earned: {stats['karma_earned']:+d}")
    lines.append(f"- New followers: {stats['new_followers']}")
    lines.append(f"- Likes on your posts: {stats['likes_received']}")
    lines.append('')
    lines.append('See you on the feed:')
    lines.append(f"{url_for('social.feed', _external=True)}")
    lines.append('')
    lines.append('— The Paid2Match team')
    lines.append(
        'You get this email because you have a verified Paid2Match '
        'account. (There is no unsubscribe yet — reply to this email '
        'and we will stop sending it.)'
    )
    return ('Your week on Paid2Match', '\n'.join(lines))


def digest_recipients():
    """Active users with a verified email — the digest never goes to
    unverified addresses."""
    return User.query.filter(
        User.is_active == True,  # noqa: E712
        User.is_deleted == False,  # noqa: E712
        User.email_verified == True,  # noqa: E712
    ).all()


def send_weekly_digest():
    """Send the digest to every recipient. Returns (sent, skipped):
    skipped counts users whose send returned False (mail unconfigured
    or a send error) — never an exception. A user with nothing new
    still gets the digest (their week's stats are always there).

    The CLI runs without a request context, so _external URLs are
    built inside a test request context rooted at the site's base
    URL (P2M_BASE_URL, default https://paid2match.work) — the app
    context's URL adapter is fixed at push time, so setting
    SERVER_NAME here would be too late."""
    from flask import current_app
    import os
    base_url = os.environ.get('P2M_BASE_URL', 'https://paid2match.work')
    sent = 0
    skipped = 0
    with current_app.test_request_context(base_url=base_url):
        for user in digest_recipients():
            subject, body = build_digest(user)
            if send_simple_email(user.email, subject, body):
                sent += 1
            else:
                skipped += 1
    logger.info('Weekly digest: %d sent, %d skipped', sent, skipped)
    return sent, skipped
