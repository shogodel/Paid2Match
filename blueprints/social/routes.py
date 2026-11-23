"""Social routes (M1): feed, posts, follows, people, notifications,
analytics, moderation v0. M3 adds: bump/boost application, Pro
analytics + CSV export, the pledge register, moderation v1 (auto-hide,
rate limits), and trust-level badges on post cards. Phase 1 adds JSON
responses to the interaction routes (like/comment/share/follow) so the
feed works without full page reloads — plain form posts keep the old
redirect behaviour."""
import os
import uuid
from datetime import datetime, timezone

from flask import (
    Blueprint, Response, flash, jsonify, redirect, render_template, request,
    session, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import desc, or_
from werkzeug.utils import secure_filename

from blueprints.social.forms import (
    CommentForm, ConvertForm, PledgeForm, PostForm, ReportForm, ShareForm,
)
from extensions import db, get_or_404
from models.bounty import Bounty
from models.endorsement import Pledge
from models.follow import Follow
from models.moderation import Block, Report
from models.notification import Notification
from models.poll import create_poll, parse_poll_options
from models.post import Bookmark, Comment, Like, Post, Share
from models.post_view import PostView, SearchAppearance
from models.profile import Profile
from models.space import Space, SpaceMembership
from models.user import User
from utils.analytics import get_analytics, get_pro_analytics, pro_analytics_csv
from utils.bumps import (
    BOOST_MAX_DAYS,
    BOOST_PRICE_PER_DAY_CENTS,
    BUMP_SINGLE_PRICE_CENTS,
    RECURRING_BUMP_PRICE_CENTS,
    apply_bump,
    bump_eligibility,
    bump_performance_for_post,
    free_bump_balance,
    pack_bump_balance,
)
from models.bump import RecurringBump
from utils.feed import (
    PUBLISHED_DISCOVER_RULES, PUBLISHED_TRENDING_RULES,
    create_need_post, get_discover_feed, get_following_feed,
    get_latest_feed, get_trending_feed, hidden_author_ids,
    suggested_connections,
)
from utils.mentions import notify_mentions
from utils.onboarding import get_onboarding_steps
from utils.karma import KARMA_FLAG_WEIGHT_X2, KARMA_LIKE_RECEIVED, award_karma
from utils.moderation import can_post_today, check_auto_hide
from utils.notifications import notify, unread_count
from utils.pledges import (
    create_pledge,
    platform_ghosting_compliance,
    register_entries,
)
from utils.pro import is_pro
from utils.trust import is_top_helper, trust_level

social_bp = Blueprint('social', __name__)

POST_PHOTO_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
POST_UPLOAD_FOLDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'uploads', 'posts'
)


def _allowed_photo(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in POST_PHOTO_EXTENSIONS


def _save_post_photo(file):
    if file and file.filename and _allowed_photo(file.filename):
        filename = secure_filename(f"{current_user.id}_{uuid.uuid4().hex[:8]}_{file.filename}")
        os.makedirs(POST_UPLOAD_FOLDER, exist_ok=True)
        file.save(os.path.join(POST_UPLOAD_FOLDER, filename))
        return f"/uploads/posts/{filename}"
    return None


def _liked_and_shared_ids(post_ids):
    """Like/share state for the current user, for button rendering."""
    if not current_user.is_authenticated or not post_ids:
        return set(), set()
    liked = {
        like.post_id
        for like in Like.query.filter(
            Like.user_id == current_user.id,
            Like.post_id.in_(post_ids),
        ).all()
    }
    shared = {
        share.post_id
        for share in Share.query.filter(
            Share.user_id == current_user.id,
            Share.post_id.in_(post_ids),
        ).all()
    }
    return liked, shared


def _saved_ids(post_ids):
    """Saved (bookmarked) state for the current user (Phase 2)."""
    if not current_user.is_authenticated or not post_ids:
        return set()
    return {
        b.post_id
        for b in Bookmark.query.filter(
            Bookmark.user_id == current_user.id,
            Bookmark.post_id.in_(post_ids),
        ).all()
    }


def _poll_votes(post_ids):
    """Poll votes for the current user: {post_id: voted option_id}."""
    if not current_user.is_authenticated or not post_ids:
        return {}
    from models.poll import Poll, PollVote
    votes = PollVote.query.join(
        Poll, PollVote.poll_id == Poll.id
    ).filter(
        PollVote.user_id == current_user.id,
        Poll.post_id.in_(post_ids),
    ).all()
    poll_by_id = {
        poll.id: poll.post_id
        for poll in Poll.query.filter(Poll.post_id.in_(post_ids)).all()
    }
    return {
        poll_by_id[vote.poll_id]: vote.option_id
        for vote in votes
        if vote.poll_id in poll_by_id
    }


def _wants_json():
    """True when the client asked for JSON (fetch with an explicit
    Accept or X-Requested-With header). Plain form posts get the
    classic redirect + flash behaviour."""
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.accept_mimetypes.best == 'application/json'
    )


def _author_levels(posts):
    """Trust level per author id, for the badges on post cards (M3)."""
    levels = {}
    for post in posts:
        if post.author_id not in levels:
            author = db.session.get(User, post.author_id)
            levels[post.author_id] = (
                trust_level(author)[0] if author else 'Newcomer'
            )
    return levels


def _author_profiles(posts):
    """Profile per author id, for avatars/headlines on post cards."""
    profiles = {}
    for post in posts:
        if post.author_id not in profiles:
            profiles[post.author_id] = Profile.query.filter_by(
                user_id=post.author_id
            ).first()
    return profiles


def _comment_json(comment):
    """One comment as JSON for the inline comment box."""
    profile = Profile.query.filter_by(user_id=comment.author_id).first()
    author = comment.author
    return {
        'id': comment.id,
        'author_name': (
            profile.display_name if profile and profile.display_name
            else (author.full_name if author else 'User')
        ),
        'author_url': url_for(
            'profile.public_profile', user_id=comment.author_id
        ),
        'avatar_url': profile.avatar_url if profile else None,
        'body': comment.body,
        'reply_count': comment.replies.filter_by(is_hidden=False).count(),
        'created_at': (
            comment.created_at.isoformat() if comment.created_at else None
        ),
    }


# ---------------------------------------------------------------- feed

@social_bp.route('/feed')
@login_required
def feed():
    """GET /social/feed — user-chosen feeds (Bluesky model): Following
    (default, chronological, people you follow + you), Latest
    (chronological, the whole network), Top (Discover ranking — the
    tab=discover URL is kept for existing links), Trending (Phase 3:
    pure recent-engagement velocity over the last 7 days — see
    get_trending_feed for how it differs from Top). ?flair= filters by
    post-type taxonomy (discussion / win / ask / need / available /
    intro). No opaque algorithm: the picker is the feed choice."""
    tab = request.args.get('tab', 'following')
    flair = request.args.get('flair') or None
    if flair not in Post.FLAIR_TYPES:
        flair = None
    page = request.args.get('page', 1, type=int)
    if tab == 'discover':
        result = get_discover_feed(current_user.id, page=page, flair=flair)
    elif tab == 'latest':
        result = get_latest_feed(current_user.id, page=page, flair=flair)
    elif tab == 'trending':
        result = get_trending_feed(current_user.id, page=page, flair=flair)
    else:
        tab = 'following'
        result = get_following_feed(current_user.id, page=page, flair=flair)
    post_ids = [item['post'].id for item in result['items']]
    liked_ids, shared_ids = _liked_and_shared_ids(post_ids)
    posts = [item['post'] for item in result['items']]
    # Phase 1: the in-feed composer needs a blank post form and the
    # user's open bounties (attach-opportunity picker); the rails need
    # top helpers; the cards need author profiles for avatars.
    my_bounties = Bounty.query.filter_by(
        poster_id=current_user.id, status='open'
    ).order_by(desc(Bounty.created_at)).limit(10).all()
    top_helpers = Profile.query.filter(
        Profile.helped_count > 0
    ).order_by(desc(Profile.karma)).limit(3).all()
    newest = max(
        (item['sort_key'] for item in result['items']),
        default=None,
    )
    # Phase 2 onboarding: the welcome checklist shows until all three
    # steps are done, or the user dismisses it for the session.
    onboarding_steps = get_onboarding_steps(current_user)
    show_onboarding = (
        any(not step['done'] for step in onboarding_steps)
        and not session.get('onboarding_dismissed')
    )
    return render_template(
        'social/feed.html',
        tab=tab,
        flair=flair,
        flair_labels=Post.FLAIR_LABELS,
        feed=result,
        liked_ids=liked_ids,
        shared_ids=shared_ids,
        saved_ids=_saved_ids(post_ids),
        poll_votes=_poll_votes(post_ids),
        author_levels=_author_levels(posts),
        author_profiles=_author_profiles(posts),
        discover_rules=PUBLISHED_DISCOVER_RULES,
        trending_rules=PUBLISHED_TRENDING_RULES,
        suggested=suggested_connections(current_user.id),
        share_form=ShareForm(),
        composer_form=PostForm(),
        my_bounties=my_bounties,
        top_helpers=top_helpers,
        spaces=Space.query.order_by(Space.name).all(),
        onboarding_steps=onboarding_steps,
        show_onboarding=show_onboarding,
        feed_since=newest.isoformat() if newest else None,
    )


@social_bp.route('/onboarding/dismiss', methods=['POST'])
@login_required
def dismiss_onboarding():
    """POST /social/onboarding/dismiss — hide the welcome checklist
    for this session (it returns next session until all steps are
    done)."""
    session['onboarding_dismissed'] = True
    if _wants_json():
        return jsonify({'ok': True})
    return redirect(url_for('social.feed'))


@social_bp.route('/feed/new-count')
@login_required
def feed_new_count():
    """GET /social/feed/new-count?since=<iso> — how many following-feed
    posts are newer than the newest one the client already shows.
    Powers the 'New posts' pill; the pill never auto-inserts content
    above the user's reading position."""
    since_raw = request.args.get('since', '')
    try:
        since = datetime.fromisoformat(since_raw.replace('Z', '+00:00'))
    except ValueError:
        return jsonify({'count': 0})
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    followed_ids = {
        f.followed_id
        for f in Follow.query.filter_by(follower_id=current_user.id).all()
    }
    author_ids = followed_ids | {current_user.id}
    from utils.feed import _visible_posts_query
    count = _visible_posts_query(current_user.id).filter(
        Post.author_id.in_(author_ids),
        Post.created_at > since.replace(tzinfo=None),
    ).count()
    return jsonify({'count': count})


# ---------------------------------------------------------------- spaces (Phase 2)

def _notify_space_members(space, post):
    """Notify space members with notification level 'all' about a new
    post in their space (in-app only — 'space_post' is not an email
    type). 'mentions' members hear only when @mentioned; 'muted'
    members hear nothing. Does not commit — the caller commits."""
    if not space:
        return
    memberships = SpaceMembership.query.filter_by(
        space_id=space.id, notification_level='all',
    ).all()
    for membership in memberships:
        if membership.user_id == post.author_id:
            continue
        notify(
            membership.user_id, 'space_post',
            f'New post in {space.name}',
            post.body[:120], actor_id=post.author_id,
            link_url=url_for('social.space_detail', slug=space.slug),
        )


@social_bp.route('/spaces')
def spaces():
    """GET /social/spaces — the topic spaces directory."""
    all_spaces = Space.query.order_by(Space.name).all()
    memberships = {}
    if current_user.is_authenticated:
        memberships = {
            m.space_id: m
            for m in SpaceMembership.query.filter_by(
                user_id=current_user.id,
            ).all()
        }
    return render_template(
        'social/spaces.html',
        spaces=all_spaces,
        memberships=memberships,
    )


@social_bp.route('/spaces/<slug>')
def space_detail(slug):
    """GET /social/spaces/<slug> — a space's own feed (chronological),
    plus join/leave and the per-space notification setting."""
    space = Space.query.filter_by(slug=slug).first_or_404()
    page = request.args.get('page', 1, type=int)
    viewer_id = current_user.id if current_user.is_authenticated else None
    from utils.feed import _visible_posts_query
    pagination = _visible_posts_query(viewer_id).filter(
        Post.space_id == space.id,
    ).order_by(desc(Post.created_at)).paginate(
        page=page, per_page=20, error_out=False,
    )
    posts = pagination.items
    post_ids = [p.id for p in posts]
    liked_ids, shared_ids = _liked_and_shared_ids(post_ids)
    membership = None
    if current_user.is_authenticated:
        membership = SpaceMembership.query.filter_by(
            user_id=current_user.id, space_id=space.id,
        ).first()
    return render_template(
        'social/space_detail.html',
        space=space,
        posts=posts,
        pagination=pagination,
        membership=membership,
        liked_ids=liked_ids,
        shared_ids=shared_ids,
        saved_ids=_saved_ids(post_ids),
        poll_votes=_poll_votes(post_ids),
        author_levels=_author_levels(posts),
        author_profiles=_author_profiles(posts),
        share_form=ShareForm(),
    )


@social_bp.route('/spaces/<slug>/join', methods=['POST'])
@login_required
def join_space(slug):
    """POST /social/spaces/<slug>/join — join a space (default
    notification level: all posts). Idempotent."""
    space = Space.query.filter_by(slug=slug).first_or_404()
    if not SpaceMembership.query.filter_by(
        user_id=current_user.id, space_id=space.id,
    ).first():
        db.session.add(SpaceMembership(
            user_id=current_user.id, space_id=space.id,
        ))
        db.session.commit()
        flash(f'You joined {space.name}.', 'success')
    return redirect(url_for('social.space_detail', slug=slug))


@social_bp.route('/spaces/<slug>/leave', methods=['POST'])
@login_required
def leave_space(slug):
    """POST /social/spaces/<slug>/leave — leave a space."""
    space = Space.query.filter_by(slug=slug).first_or_404()
    membership = SpaceMembership.query.filter_by(
        user_id=current_user.id, space_id=space.id,
    ).first()
    if membership:
        db.session.delete(membership)
        db.session.commit()
        flash(f'You left {space.name}.', 'info')
    return redirect(url_for('social.space_detail', slug=slug))


@social_bp.route('/spaces/<slug>/notifications', methods=['POST'])
@login_required
def space_notifications(slug):
    """POST /social/spaces/<slug>/notifications — set the per-space
    notification level (all / mentions / muted). Joining is implied:
    setting a level on a space you haven't joined joins it."""
    space = Space.query.filter_by(slug=slug).first_or_404()
    level = request.form.get('level', 'all')
    if level not in SpaceMembership.NOTIFICATION_LEVELS:
        flash('Unknown notification setting.', 'warning')
        return redirect(url_for('social.space_detail', slug=slug))
    membership = SpaceMembership.query.filter_by(
        user_id=current_user.id, space_id=space.id,
    ).first()
    if not membership:
        membership = SpaceMembership(
            user_id=current_user.id, space_id=space.id,
        )
        db.session.add(membership)
    membership.notification_level = level
    db.session.commit()
    label = SpaceMembership.NOTIFICATION_LEVEL_LABELS[level]
    flash(f'Notifications for {space.name}: {label}.', 'success')
    return redirect(url_for('social.space_detail', slug=slug))


# ---------------------------------------------------------------- posts

@social_bp.route('/posts/new', methods=['GET', 'POST'])
@login_required
def new_post():
    """GET/POST /social/posts/new — create a post (or a referral need).
    The in-feed composer posts here with AJAX: on success it gets the
    rendered post card back and prepends it, no reload. A paid need
    still redirects to secure payment (returned as a URL for AJAX)."""
    form = PostForm()
    if request.method == 'POST' and form.validate_on_submit():
        # M3 moderation v1: trust-level daily post limit.
        ok, limit, used = can_post_today(current_user)
        if not ok:
            message = (
                f'You have used your {limit} posts for today '
                f'({used}/{limit}) — the daily limit grows as your trust '
                'level grows.'
            )
            if _wants_json():
                return jsonify({'ok': False, 'error': message}), 400
            flash(message, 'warning')
            return render_template(
                'social/post_form.html', form=form, post=None,
                spaces=Space.query.order_by(Space.name).all())
        photo_url = _save_post_photo(form.photo.data) if form.photo.data else None
        # Phase 2: the space must exist; an unknown id is ignored
        # (the post lands in the general feed instead of failing).
        space = None
        if form.space_id.data:
            space = db.session.get(Space, form.space_id.data)
        # Phase 2: poll options, one per line (2–6 options).
        poll_opts, poll_error = parse_poll_options(
            form.poll_options.data or ''
        )
        if poll_error:
            if _wants_json():
                return jsonify({'ok': False, 'error': poll_error}), 400
            flash(poll_error, 'warning')
            return render_template(
                'social/post_form.html', form=form, post=None,
                spaces=Space.query.order_by(Space.name).all())
        if form.post_type.data in ('need_free', 'need_paid'):
            reward = form.reward_amount.data if form.post_type.data == 'need_paid' else None
            bounty = Bounty(
                poster_id=current_user.id,
                title=form.title.data.strip(),
                description=form.body.data.strip(),
                bounty_type='recruitment',
                bounty_direction='seeking_opportunity',
                reward_amount=reward or None,
                location=form.location.data or None,
                status='open',
                payment_status='unsecured',
                is_free=form.post_type.data == 'need_free',
            )
            db.session.add(bounty)
            db.session.flush()
            post = create_need_post(bounty)
            post.photo_url = photo_url
            if space:
                post.space_id = space.id
            if poll_opts:
                create_poll(post, poll_opts)
            _notify_space_members(space, post)
            db.session.flush()
            notify_mentions(
                post.body, current_user.id, current_user.full_name,
                url_for('social.post_detail', post_id=post.id),
            )
            db.session.commit()
            if form.post_type.data == 'need_paid':
                secure_url = url_for('bounties.secure_payment', id=bounty.id)
                if _wants_json():
                    return jsonify({'ok': True, 'redirect': secure_url})
                flash('Referral need created! Secure payment to publish it.', 'success')
                return redirect(secure_url)
            if _wants_json():
                return jsonify(_new_post_json(post))
            flash('Community referral posted!', 'success')
            return redirect(url_for('social.post_detail', post_id=post.id))

        post = Post(
            author_id=current_user.id,
            post_type=form.post_type.data,
            body=form.body.data.strip(),
            photo_url=photo_url,
        )
        if space:
            post.space_id = space.id
        # Phase 1 composer: attach one of your open opportunities to a
        # plain post — the bounty card renders nested inside the post.
        # The bounty must be yours and open; anything else is ignored.
        if form.bounty_id.data:
            attach = db.session.get(Bounty, form.bounty_id.data)
            if (
                attach and attach.poster_id == current_user.id
                and attach.status == 'open'
            ):
                post.bounty_id = attach.id
        db.session.add(post)
        db.session.flush()
        if poll_opts:
            create_poll(post, poll_opts)
        _notify_space_members(space, post)
        notify_mentions(
            post.body, current_user.id, current_user.full_name,
            url_for('social.post_detail', post_id=post.id),
        )
        db.session.commit()
        if _wants_json():
            return jsonify(_new_post_json(post))
        flash('Posted!', 'success')
        return redirect(url_for('social.feed'))
    if request.method == 'POST' and _wants_json():
        return jsonify({'ok': False, 'errors': form.errors}), 400
    return render_template(
        'social/post_form.html', form=form, post=None,
        spaces=Space.query.order_by(Space.name).all())


def _new_post_json(post):
    """JSON payload for a composer-created post: the rendered card so
    the client can prepend it without a reload."""
    html = render_template(
        'social/_post_card.html',
        post=post,
        share=None,
        liked_ids=set(),
        shared_ids=set(),
        saved_ids=set(),
        poll_votes={},
        author_levels=_author_levels([post]),
        author_profiles=_author_profiles([post]),
        share_form=ShareForm(),
    )
    return {
        'ok': True,
        'post_id': post.id,
        'url': url_for('social.post_detail', post_id=post.id),
        'html': html,
    }


@social_bp.route('/posts/<post_id>')
def post_detail(post_id):
    """GET /social/posts/<id> — post detail + comments. Counts a view once
    per browser session (no refresh inflation)."""
    post = get_or_404(Post, post_id)
    if post.is_hidden and (
        not current_user.is_authenticated or current_user.id != post.author_id
    ):
        flash('This post is no longer available.', 'warning')
        return redirect(url_for('social.feed'))

    viewed = session.get('viewed_posts', [])
    if post_id not in viewed:
        post.view_count = (post.view_count or 0) + 1
        # M3: record the view for Pro "members reached" (same
        # once-per-session guard — no refresh inflation).
        db.session.add(PostView(
            post_id=post_id,
            viewer_id=current_user.id if current_user.is_authenticated else None,
        ))
        db.session.commit()
        viewed.append(post_id)
        session['viewed_posts'] = viewed[-100:]

    top_comments = Comment.query.filter_by(
        post_id=post_id, parent_id=None, is_hidden=False
    ).order_by(Comment.created_at).all()
    liked_ids, shared_ids = _liked_and_shared_ids([post_id])
    return render_template(
        'social/post_detail.html',
        post=post,
        top_comments=top_comments,
        comment_form=CommentForm(),
        share_form=ShareForm(),
        liked=post_id in liked_ids,
        shared=post_id in shared_ids,
        saved=post_id in _saved_ids([post_id]),
        poll_votes=_poll_votes([post_id]),
        author_trust=trust_level(post.author) if post.author else None,
        author_levels=_author_levels([post]),
        author_profiles=_author_profiles([post]),
    )


@social_bp.route('/posts/<post_id>/comments')
def post_comments(post_id):
    """GET /social/posts/<id>/comments — top-level comments as JSON for
    the inline comment box on post cards (latest 5; the full thread
    lives on the post detail page)."""
    post = get_or_404(Post, post_id)
    comments = Comment.query.filter_by(
        post_id=post.id, parent_id=None, is_hidden=False
    ).order_by(desc(Comment.created_at)).limit(5).all()
    return jsonify({
        'comments': [_comment_json(c) for c in reversed(comments)],
        'comment_count': post.comment_count,
        'detail_url': url_for('social.post_detail', post_id=post.id),
    })


@social_bp.route('/posts/<post_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_post(post_id):
    """GET/POST /social/posts/<id>/edit — edit your own post (text only;
    need posts keep their bounty — edit the need from its page)."""
    post = get_or_404(Post, post_id)
    if post.author_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('social.post_detail', post_id=post_id))
    form = PostForm(obj=post)
    if request.method == 'POST' and form.validate_on_submit():
        post.body = form.body.data.strip()
        if form.photo.data:
            photo_url = _save_post_photo(form.photo.data)
            if photo_url:
                post.photo_url = photo_url
        db.session.commit()
        flash('Post updated!', 'success')
        return redirect(url_for('social.post_detail', post_id=post_id))
    return render_template(
        'social/post_form.html', form=form, post=post,
        spaces=Space.query.order_by(Space.name).all())


@social_bp.route('/posts/<post_id>/delete', methods=['POST'])
@login_required
def delete_post(post_id):
    """POST /social/posts/<id>/delete — delete your own post."""
    post = get_or_404(Post, post_id)
    if post.author_id != current_user.id:
        if _wants_json():
            return jsonify({'ok': False, 'error': 'Access denied'}), 403
        flash('Access denied', 'danger')
        return redirect(url_for('social.post_detail', post_id=post_id))
    db.session.delete(post)
    db.session.commit()
    if _wants_json():
        return jsonify({'ok': True, 'post_id': post_id})
    flash('Post deleted.', 'info')
    return redirect(url_for('social.feed'))


@social_bp.route('/posts/<post_id>/like', methods=['POST'])
@login_required
def like_post(post_id):
    """POST /social/posts/<id>/like — toggle a like. A new like earns the
    author +10 karma (§4.2); unliking does not revoke it. AJAX callers
    get {liked, like_count} instead of a redirect."""
    post = get_or_404(Post, post_id)
    existing = Like.query.filter_by(user_id=current_user.id, post_id=post_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        if _wants_json():
            return jsonify({'liked': False, 'like_count': post.like_count})
        return redirect(request.referrer or url_for('social.feed'))
    db.session.add(Like(user_id=current_user.id, post_id=post_id))
    if post.author_id != current_user.id:
        award_karma(post.author_id, KARMA_LIKE_RECEIVED, 'like_received',
                    ref_type='post', ref_id=post_id)
        notify(
            post.author_id, 'like',
            f'{current_user.full_name} liked your post',
            post.body[:120], actor_id=current_user.id,
            link_url=url_for('social.post_detail', post_id=post_id),
        )
    db.session.commit()
    if _wants_json():
        return jsonify({'liked': True, 'like_count': post.like_count})
    return redirect(request.referrer or url_for('social.feed'))


@social_bp.route('/posts/<post_id>/comment', methods=['POST'])
@login_required
def comment_post(post_id):
    """POST /social/posts/<id>/comment — comment, or reply to a top-level
    comment (one level deep, §1.2). AJAX callers get the created
    comment as JSON instead of a redirect."""
    post = get_or_404(Post, post_id)
    form = CommentForm()
    if form.validate_on_submit():
        parent = None
        if form.parent_id.data:
            parent = db.session.get(Comment, form.parent_id.data)
            if not parent or parent.post_id != post_id or parent.parent_id is not None:
                if _wants_json():
                    return jsonify({
                        'ok': False,
                        'error': 'You can only reply to a top-level comment.',
                    }), 400
                flash('You can only reply to a top-level comment.', 'warning')
                return redirect(url_for('social.post_detail', post_id=post_id))
        comment = Comment(
            author_id=current_user.id,
            post_id=post_id,
            parent_id=parent.id if parent else None,
            body=form.body.data.strip(),
        )
        db.session.add(comment)
        db.session.flush()
        if post.author_id != current_user.id:
            notify(
                post.author_id, 'comment',
                f'{current_user.full_name} commented on your post',
                form.body.data.strip()[:120], actor_id=current_user.id,
                link_url=url_for('social.post_detail', post_id=post_id),
            )
        if parent and parent.author_id not in (current_user.id, post.author_id):
            notify(
                parent.author_id, 'comment',
                f'{current_user.full_name} replied to your comment',
                form.body.data.strip()[:120], actor_id=current_user.id,
                link_url=url_for('social.post_detail', post_id=post_id),
            )
        # Phase 2: @mentions in the comment notify the mentioned users
        # — except the post/parent authors, who already got a comment
        # notification for this same comment.
        notify_mentions(
            form.body.data.strip(), current_user.id, current_user.full_name,
            url_for('social.post_detail', post_id=post_id),
            exclude_ids={post.author_id}
            | ({parent.author_id} if parent else set()),
        )
        db.session.commit()
        if _wants_json():
            return jsonify({
                'ok': True,
                'comment': _comment_json(comment),
                'comment_count': post.comment_count,
            })
        flash('Comment posted!', 'success')
    elif _wants_json():
        return jsonify({'ok': False, 'errors': form.errors}), 400
    return redirect(url_for('social.post_detail', post_id=post_id))


@social_bp.route('/posts/<post_id>/share', methods=['POST'])
@login_required
def share_post(post_id):
    """POST /social/posts/<id>/share — reshare to your feed with a comment.
    One share per user per post; the share is attributed to you (§4.4).
    AJAX callers get {shared, share_count} instead of a redirect."""
    post = get_or_404(Post, post_id)
    form = ShareForm()
    if Share.query.filter_by(user_id=current_user.id, post_id=post_id).first():
        if _wants_json():
            return jsonify({
                'ok': False,
                'error': 'You already shared this post.',
                'shared': True,
                'share_count': post.share_count,
            }), 400
        flash('You already shared this post.', 'info')
        return redirect(request.referrer or url_for('social.feed'))
    if form.validate_on_submit():
        db.session.add(Share(
            user_id=current_user.id,
            post_id=post_id,
            comment=(form.comment.data or '').strip() or None,
        ))
        if post.author_id != current_user.id:
            notify(
                post.author_id, 'share',
                f'{current_user.full_name} shared your post',
                (form.comment.data or '').strip()[:120] or post.body[:120],
                actor_id=current_user.id,
                link_url=url_for('social.post_detail', post_id=post_id),
            )
        db.session.commit()
        if _wants_json():
            return jsonify({
                'ok': True,
                'shared': True,
                'share_count': post.share_count,
            })
        flash('Shared to your feed!', 'success')
    elif _wants_json():
        return jsonify({'ok': False, 'errors': form.errors}), 400
    return redirect(request.referrer or url_for('social.feed'))


@social_bp.route('/posts/<post_id>/convert', methods=['GET', 'POST'])
@login_required
def convert_post(post_id):
    """GET/POST /social/posts/<id>/convert — turn your post into a
    referral need (§1.1, §4.1). The post's text becomes the description."""
    post = get_or_404(Post, post_id)
    if post.author_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('social.post_detail', post_id=post_id))
    if post.is_need:
        flash('This post is already a referral need.', 'info')
        return redirect(url_for('social.post_detail', post_id=post_id))
    form = ConvertForm()
    if request.method == 'GET':
        form.title.data = post.body.strip().split('\n')[0][:200]
    if request.method == 'POST' and form.validate_on_submit():
        is_paid = form.need_kind.data == 'paid'
        bounty = Bounty(
            poster_id=current_user.id,
            title=form.title.data.strip(),
            description=post.body,
            bounty_type='recruitment',
            bounty_direction='seeking_opportunity',
            reward_amount=form.reward_amount.data if is_paid else None,
            location=form.location.data or None,
            status='open',
            payment_status='unsecured',
            is_free=not is_paid,
        )
        db.session.add(bounty)
        db.session.flush()
        post.bounty_id = bounty.id
        post.post_type = 'need_paid' if is_paid else 'need_free'
        db.session.commit()
        if is_paid:
            flash('Referral need created! Secure payment to publish it.', 'success')
            return redirect(url_for('bounties.secure_payment', id=bounty.id))
        flash('Your post is now a community referral!', 'success')
        return redirect(url_for('social.post_detail', post_id=post_id))
    return render_template('social/convert.html', form=form, post=post)


# ---------------------------------------------------------------- polls (Phase 2)

@social_bp.route('/polls/<poll_id>/vote', methods=['POST'])
@login_required
def vote_poll(poll_id):
    """POST /social/polls/<id>/vote — cast one vote (AJAX). One vote
    per user per poll: a second vote is idempotent — it returns the
    current results with already_voted set, and changes nothing.
    Returns the chart data (options with votes + percentages)."""
    from models.poll import Poll, PollOption, PollVote
    poll = get_or_404(Poll, poll_id)
    option_id = request.form.get('option_id')
    existing = PollVote.query.filter_by(
        poll_id=poll.id, user_id=current_user.id,
    ).first()
    if existing:
        return jsonify({
            'ok': True,
            'already_voted': True,
            'voted_option_id': existing.option_id,
            'total_votes': poll.total_votes,
            'options': poll.results(),
        })
    option = PollOption.query.filter_by(
        id=option_id, poll_id=poll.id,
    ).first()
    if not option:
        return jsonify({
            'ok': False,
            'error': 'Choose one of the poll options.',
        }), 400
    db.session.add(PollVote(
        poll_id=poll.id, option_id=option.id,
        user_id=current_user.id,
    ))
    db.session.commit()
    return jsonify({
        'ok': True,
        'already_voted': False,
        'voted_option_id': option.id,
        'total_votes': poll.total_votes,
        'options': poll.results(),
    })


# ---------------------------------------------------------------- saved (Phase 2)

@social_bp.route('/saved')
@login_required
def saved():
    """GET /social/saved — your saved posts, newest first."""
    page = request.args.get('page', 1, type=int)
    pagination = Bookmark.query.filter_by(
        user_id=current_user.id,
    ).order_by(desc(Bookmark.created_at)).paginate(
        page=page, per_page=20, error_out=False,
    )
    bookmarks = pagination.items
    posts = [b.post for b in bookmarks if b.post and not b.post.is_hidden]
    post_ids = [p.id for p in posts]
    liked_ids, shared_ids = _liked_and_shared_ids(post_ids)
    return render_template(
        'social/saved.html',
        posts=posts,
        pagination=pagination,
        liked_ids=liked_ids,
        shared_ids=shared_ids,
        saved_ids=set(post_ids),
        poll_votes=_poll_votes(post_ids),
        author_levels=_author_levels(posts),
        author_profiles=_author_profiles(posts),
        share_form=ShareForm(),
    )


@social_bp.route('/posts/<post_id>/save', methods=['POST'])
@login_required
def save_post(post_id):
    """POST /social/posts/<id>/save — toggle a bookmark (closes the
    Phase 1 Save deferral). AJAX callers get {saved} instead of a
    redirect; same optimistic-update pattern as like/follow."""
    post = get_or_404(Post, post_id)
    existing = Bookmark.query.filter_by(
        user_id=current_user.id, post_id=post_id,
    ).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        if _wants_json():
            return jsonify({'saved': False})
        return redirect(request.referrer or url_for('social.saved'))
    db.session.add(Bookmark(user_id=current_user.id, post_id=post_id))
    db.session.commit()
    if _wants_json():
        return jsonify({'saved': True})
    return redirect(request.referrer or url_for('social.saved'))


# ---------------------------------------------------------------- follows

@social_bp.route('/follow/<user_id>', methods=['POST'])
@login_required
def follow(user_id):
    """POST /social/follow/<id> — follow a user. AJAX callers get
    {following, followers_count} instead of a redirect. Following is
    idempotent: following twice still yields one Follow row."""
    target = get_or_404(User, user_id)
    if target.id == current_user.id:
        if _wants_json():
            return jsonify({
                'ok': False, 'error': 'You cannot follow yourself.',
            }), 400
        flash('You cannot follow yourself.', 'warning')
        return redirect(request.referrer or url_for('social.feed'))
    blocked = Block.query.filter_by(
        blocker_id=target.id, blocked_id=current_user.id, kind='block'
    ).first()
    if blocked:
        if _wants_json():
            return jsonify({
                'ok': False, 'error': 'You cannot follow this user.',
            }), 403
        flash('You cannot follow this user.', 'warning')
        return redirect(request.referrer or url_for('social.feed'))
    if Follow.query.filter_by(follower_id=current_user.id, followed_id=user_id).first():
        if _wants_json():
            return jsonify({
                'ok': True,
                'following': True,
                'followers_count': Follow.query.filter_by(
                    followed_id=user_id).count(),
            })
        flash('You already follow this user.', 'info')
        return redirect(request.referrer or url_for('social.feed'))
    db.session.add(Follow(follower_id=current_user.id, followed_id=user_id))
    notify(
        user_id, 'follow',
        f'{current_user.full_name} started following you',
        None, actor_id=current_user.id,
        link_url=url_for('profile.public_profile', user_id=current_user.id),
    )
    db.session.commit()
    if _wants_json():
        return jsonify({
            'ok': True,
            'following': True,
            'followers_count': Follow.query.filter_by(
                followed_id=user_id).count(),
        })
    flash(f'You are now following {target.full_name}.', 'success')
    return redirect(request.referrer or url_for('social.feed'))


@social_bp.route('/unfollow/<user_id>', methods=['POST'])
@login_required
def unfollow(user_id):
    """POST /social/unfollow/<id> — unfollow a user. AJAX callers get
    {following: false, followers_count} instead of a redirect."""
    follow = Follow.query.filter_by(
        follower_id=current_user.id, followed_id=user_id
    ).first()
    if follow:
        db.session.delete(follow)
        db.session.commit()
        if _wants_json():
            return jsonify({
                'ok': True,
                'following': False,
                'followers_count': Follow.query.filter_by(
                    followed_id=user_id).count(),
            })
        flash('Unfollowed.', 'info')
    elif _wants_json():
        return jsonify({
            'ok': True,
            'following': False,
            'followers_count': Follow.query.filter_by(
                followed_id=user_id).count(),
        })
    return redirect(request.referrer or url_for('social.feed'))


@social_bp.route('/users/<user_id>/followers')
def followers(user_id):
    """GET /social/users/<id>/followers — who follows this user."""
    user = get_or_404(User, user_id)
    follows = Follow.query.filter_by(followed_id=user_id).order_by(
        desc(Follow.created_at)
    ).all()
    return render_template(
        'social/follow_list.html', user=user, follows=follows, kind='followers'
    )


@social_bp.route('/users/<user_id>/following')
def following(user_id):
    """GET /social/users/<id>/following — who this user follows."""
    user = get_or_404(User, user_id)
    follows = Follow.query.filter_by(follower_id=user_id).order_by(
        desc(Follow.created_at)
    ).all()
    return render_template(
        'social/follow_list.html', user=user, follows=follows, kind='following'
    )


# ---------------------------------------------------------------- people

@social_bp.route('/people')
def people():
    """GET /social/people — searchable directory (§1.2) + suggested
    connections. Search by name, skill, or location."""
    query_text = (request.args.get('q') or '').strip()
    skill = (request.args.get('skill') or '').strip()
    location = (request.args.get('location') or '').strip()

    query = User.query.join(Profile, Profile.user_id == User.id).filter(
        User.is_active == True,  # noqa: E712
        User.is_deleted == False,  # noqa: E712
        Profile.privacy_level != 2,
    )
    if query_text:
        query = query.filter(or_(
            User.full_name.ilike(f'%{query_text}%'),
            Profile.display_name.ilike(f'%{query_text}%'),
        ))
    if skill:
        query = query.filter(Profile.skills.ilike(f'%{skill}%'))
    if location:
        query = query.filter(Profile.location.ilike(f'%{location}%'))
    users = query.order_by(desc(Profile.helped_count), desc(Profile.karma)).limit(50).all()

    # M3: record search appearances for Pro analytics — only when a
    # filter was actually used (browsing the directory is not a search).
    if query_text or skill or location:
        query_label = ', '.join(
            part for part in [query_text, skill, location] if part
        )
        viewer_id = current_user.id if current_user.is_authenticated else None
        for found in users:
            if found.id != viewer_id:
                db.session.add(SearchAppearance(
                    searched_user_id=found.id,
                    search_query=query_label,
                    viewer_id=viewer_id,
                ))
        db.session.commit()

    suggested = []
    following_ids = set()
    if current_user.is_authenticated:
        suggested = suggested_connections(current_user.id)
        following_ids = {
            f.followed_id
            for f in Follow.query.filter_by(follower_id=current_user.id).all()
        }
    # Phase 3: one verification badge per listed user (batched).
    from utils.verification_tiers import verification_tiers_for
    verification_tiers = verification_tiers_for([u.id for u in users])
    return render_template(
        'social/people.html',
        users=users,
        suggested=suggested,
        following_ids=following_ids,
        verification_tiers=verification_tiers,
        q=query_text, skill=skill, location=location,
    )


# ---------------------------------------------------------------- search

@social_bp.route('/search')
def search():
    """GET /social/search — nav-centre search: people, posts and
    opportunities in one box, grouped. Simple ILIKE matching across
    display names/headlines, post bodies, and bounty
    titles/descriptions — no external search engine. An empty query
    shows the page with a prompt, not an error. People results record
    SearchAppearance rows, like the people directory."""
    q = (request.args.get('q') or '').strip()
    people_results = []
    post_results = []
    bounty_results = []
    if q:
        pattern = f'%{q}%'
        people_results = User.query.join(
            Profile, Profile.user_id == User.id
        ).filter(
            User.is_active == True,  # noqa: E712
            User.is_deleted == False,  # noqa: E712
            Profile.privacy_level != 2,
            or_(
                User.full_name.ilike(pattern),
                Profile.display_name.ilike(pattern),
                Profile.headline.ilike(pattern),
            ),
        ).order_by(desc(Profile.helped_count), desc(Profile.karma)).limit(10).all()
        viewer_id = (
            current_user.id if current_user.is_authenticated else None
        )
        posts_q = Post.query.filter_by(is_hidden=False)
        hidden = hidden_author_ids(viewer_id)
        if hidden:
            posts_q = posts_q.filter(~Post.author_id.in_(hidden))
        post_results = posts_q.filter(
            Post.body.ilike(pattern)
        ).order_by(desc(Post.created_at)).limit(10).all()
        now = datetime.now(timezone.utc)
        bounty_results = Bounty.query.filter(
            Bounty.status == 'open',
            db.or_(
                Bounty.deadline.is_(None),
                Bounty.deadline > now,
            ),
            # Paid bounties stay hidden until payment is secured.
            Bounty.publicly_visible_filter(),
            or_(
                Bounty.title.ilike(pattern),
                Bounty.description.ilike(pattern),
            ),
        ).order_by(desc(Bounty.created_at)).limit(10).all()
        for found in people_results:
            if found.id != viewer_id:
                db.session.add(SearchAppearance(
                    searched_user_id=found.id,
                    search_query=q,
                    viewer_id=viewer_id,
                ))
        db.session.commit()
    post_ids = [p.id for p in post_results]
    liked_ids, shared_ids = _liked_and_shared_ids(post_ids)
    following_ids = set()
    if current_user.is_authenticated:
        following_ids = {
            f.followed_id
            for f in Follow.query.filter_by(
                follower_id=current_user.id
            ).all()
        }
    from utils.verification_tiers import verification_tiers_for
    verification_tiers = verification_tiers_for(
        [u.id for u in people_results]
    )
    return render_template(
        'social/search.html',
        q=q,
        people_results=people_results,
        post_results=post_results,
        bounty_results=bounty_results,
        liked_ids=liked_ids,
        shared_ids=shared_ids,
        saved_ids=_saved_ids(post_ids),
        poll_votes=_poll_votes(post_ids),
        author_levels=_author_levels(post_results),
        author_profiles=_author_profiles(post_results),
        following_ids=following_ids,
        verification_tiers=verification_tiers,
        share_form=ShareForm(),
    )


# ---------------------------------------------------------------- verification

@social_bp.route('/verification')
def verification():
    """GET /social/verification — what the verification badge means.
    One page explains all three tiers, what each one proves, and how
    to reach it; a logged-in viewer also sees their own tier and
    their next step. Tiers use existing signals only (email, linked
    accounts, admin verification, Stripe KYC) — there is no
    phone-verification backend."""
    from utils.verification_tiers import NEXT_STEP, verification_tier
    my_tier = None
    next_step = None
    if current_user.is_authenticated:
        my_tier = verification_tier(current_user)
        next_step = NEXT_STEP[my_tier[0]]
    return render_template(
        'social/verification.html',
        my_tier=my_tier,
        next_step=next_step,
    )


# ---------------------------------------------------------------- notifications

# Phase 3: the full notifications page filters by group. Types not in
# any group (messages, shares, space posts, reviews, disputes,
# verification, linked accounts, endorsements, pledge events) appear
# under All only — All is the default, so nothing is ever hidden.
NOTIFICATION_FILTERS = {
    'likes': ['like'],
    'comments': ['comment', 'mention'],
    'follows': ['follow'],
    'referrals': [
        'referral_new', 'referral_accepted', 'referral_rejected',
        'referral_invite', 'referral_consented', 'referral_declined',
        'referral_completed', 'share_led_to_hire', 'match_confirmed',
    ],
    'payouts': [
        'payment_secured', 'payment_released',
        'payout_pending', 'payout_failed', 'connect_ready',
    ],
}


@social_bp.route('/notifications')
@login_required
def notifications():
    """GET /social/notifications — notification center, with filter
    chips (All / Likes / Comments / Follows / Referrals / Payouts).
    Items are grouped New (unread) / Earlier (read)."""
    page = request.args.get('page', 1, type=int)
    filter_key = request.args.get('filter', 'all')
    if filter_key not in list(NOTIFICATION_FILTERS) + ['all']:
        filter_key = 'all'
    query = Notification.query.filter_by(recipient_id=current_user.id)
    if filter_key != 'all':
        query = query.filter(
            Notification.type.in_(NOTIFICATION_FILTERS[filter_key])
        )
    pagination = query.order_by(desc(Notification.created_at)).paginate(
        page=page, per_page=20, error_out=False
    )
    # Counts per chip (one query per group — cheap at this scale).
    filter_counts = {
        'all': Notification.query.filter_by(
            recipient_id=current_user.id).count(),
    }
    for key, types in NOTIFICATION_FILTERS.items():
        filter_counts[key] = Notification.query.filter_by(
            recipient_id=current_user.id,
        ).filter(Notification.type.in_(types)).count()
    # Actor profiles for the rows (one query — the page reuses the
    # same item partial as the panel).
    actor_profiles = {}
    actor_ids = {
        n.actor_id for n in pagination.items if n.actor_id
    }
    if actor_ids:
        actor_profiles = {
            p.user_id: p
            for p in Profile.query.filter(
                Profile.user_id.in_(actor_ids)
            ).all()
        }
    return render_template(
        'social/notifications.html',
        notifications=pagination.items,
        pagination=pagination,
        unread=unread_count(current_user.id),
        filter_key=filter_key,
        filter_counts=filter_counts,
        actor_profiles=actor_profiles,
    )


@social_bp.route('/notifications/panel')
@login_required
def notifications_panel():
    """GET /social/notifications/panel — the bell panel's content as
    JSON (latest 10 + unread count). The panel re-fetches on open so
    it is never stale; skeletons cover the fetch."""
    from utils.notifications import (
        notification_to_dict, recent_notifications,
    )
    items = recent_notifications(current_user.id, limit=10)
    return jsonify({
        'unread': unread_count(current_user.id),
        'notifications': [
            notification_to_dict(item['notification'])
            for item in items
        ],
    })


@social_bp.route('/notifications/poll')
@login_required
def notifications_poll():
    """GET /social/notifications/poll?since=<iso> — unread
    notifications created after ``since`` (max 5), for in-session
    toasts. The client polls while the tab is visible; the toast
    shows a View link to the notification's target."""
    from datetime import datetime as _dt
    from utils.notifications import notification_to_dict
    since_raw = request.args.get('since', '')
    try:
        since = _dt.fromisoformat(since_raw.replace('Z', '+00:00'))
    except ValueError:
        since = None
    query = Notification.query.filter_by(
        recipient_id=current_user.id, is_read=False,
    )
    if since is not None:
        query = query.filter(
            Notification.created_at > since.replace(tzinfo=None)
        )
    items = query.order_by(desc(Notification.created_at)).limit(5).all()
    return jsonify({
        'unread': unread_count(current_user.id),
        'notifications': [notification_to_dict(n) for n in items],
    })


@social_bp.route('/notifications/<notification_id>/read', methods=['POST'])
@login_required
def read_notification(notification_id):
    """POST /social/notifications/<id>/read — mark read, then follow the link."""
    notification = get_or_404(Notification, notification_id)
    if notification.recipient_id != current_user.id:
        if _wants_json():
            return jsonify({'ok': False, 'error': 'Access denied'}), 403
        flash('Access denied', 'danger')
        return redirect(url_for('social.notifications'))
    notification.is_read = True
    db.session.commit()
    if _wants_json():
        return jsonify({'ok': True, 'unread': unread_count(current_user.id)})
    if notification.link_url and notification.link_url.startswith('/'):
        return redirect(notification.link_url)
    return redirect(url_for('social.notifications'))


@social_bp.route('/notifications/read-all', methods=['POST'])
@login_required
def read_all_notifications():
    """POST /social/notifications/read-all — mark everything read.
    AJAX callers (the bell panel) get the new unread count (0)."""
    Notification.query.filter_by(
        recipient_id=current_user.id, is_read=False
    ).update({'is_read': True})
    db.session.commit()
    if _wants_json():
        return jsonify({'ok': True, 'unread': 0})
    return redirect(url_for('social.notifications'))


# ---------------------------------------------------------------- analytics

@social_bp.route('/analytics')
@login_required
def analytics():
    """GET /social/analytics — free-tier analytics (§1.4), plus the Pro
    section for subscribers."""
    level, score, breakdown = trust_level(current_user)
    pro = is_pro(current_user)
    return render_template(
        'social/analytics.html',
        analytics=get_analytics(current_user.id),
        trust_level=level,
        trust_score=score,
        trust_breakdown=breakdown,
        top_helper=is_top_helper(current_user.id),
        is_pro=pro,
        pro_analytics=get_pro_analytics(current_user.id) if pro else None,
    )


@social_bp.route('/analytics/export')
@login_required
def analytics_export():
    """GET /social/analytics/export — CSV export of the Pro analytics
    (§1.4). Pro only."""
    if not is_pro(current_user):
        flash('CSV export is a Pro feature — subscribe to Pro to download your analytics.', 'warning')
        return redirect(url_for('payments.pro_page'))
    csv_text = pro_analytics_csv(current_user.id)
    return Response(
        csv_text,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=paid2match-analytics.csv'},
    )


# ---------------------------------------------------------------- moderation v0

@social_bp.route('/report/<target_type>/<target_id>', methods=['GET', 'POST'])
@login_required
def report(target_type, target_id):
    """GET/POST /social/report/<type>/<id> — report a post, comment, or
    profile. One open report per reporter per target."""
    if target_type not in Report.TARGET_TYPES:
        flash('Invalid report target.', 'danger')
        return redirect(url_for('social.feed'))
    # The target must exist.
    target = None
    if target_type == 'post':
        target = db.session.get(Post, target_id)
    elif target_type == 'comment':
        target = db.session.get(Comment, target_id)
    else:
        target = db.session.get(User, target_id)
    if not target:
        flash('That no longer exists.', 'warning')
        return redirect(url_for('social.feed'))

    existing = Report.query.filter_by(
        reporter_id=current_user.id,
        target_type=target_type,
        target_id=target_id,
        status='open',
    ).first()
    if existing:
        flash('You have already reported this — it is in the review queue.', 'info')
        return redirect(request.referrer or url_for('social.feed'))

    form = ReportForm()
    if request.method == 'POST' and form.validate_on_submit():
        profile = Profile.query.filter_by(user_id=current_user.id).first()
        # Flag weight (§3.2/§4.2): Ambassadors count triple, 2,500+
        # karma counts double, everyone else counts once.
        level, _score, _breakdown = trust_level(current_user)
        if level == 'Ambassador':
            weight = 3
        elif profile and (profile.karma or 0) >= KARMA_FLAG_WEIGHT_X2:
            weight = 2
        else:
            weight = 1
        db.session.add(Report(
            reporter_id=current_user.id,
            target_type=target_type,
            target_id=target_id,
            reason=form.reason.data,
            details=(form.details.data or '').strip() or None,
            weight=weight,
        ))
        db.session.flush()
        # M3 moderation v1: enough weight auto-hides the content
        # pending review.
        auto_hidden = check_auto_hide(target_type, target_id)
        db.session.commit()
        if auto_hidden:
            flash(
                'Thanks — this content has been temporarily hidden '
                'pending review.',
                'success',
            )
        else:
            flash('Thanks — your report is in the review queue.', 'success')
        return redirect(request.referrer or url_for('social.feed'))
    return render_template(
        'social/report_form.html', form=form,
        target_type=target_type, target_id=target_id,
    )


@social_bp.route('/block/<user_id>', methods=['POST'])
@login_required
def block_user(user_id):
    """POST /social/block/<id>?kind=block|mute — block or mute a user.
    Blocking also unfollows in both directions."""
    kind = request.args.get('kind', 'block')
    if kind not in Block.KINDS:
        kind = 'block'
    target = get_or_404(User, user_id)
    if target.id == current_user.id:
        if _wants_json():
            return jsonify({
                'ok': False, 'error': 'You cannot block yourself.',
            }), 400
        flash('You cannot block yourself.', 'warning')
        return redirect(request.referrer or url_for('social.feed'))
    if not Block.query.filter_by(
        blocker_id=current_user.id, blocked_id=user_id, kind=kind
    ).first():
        db.session.add(Block(
            blocker_id=current_user.id, blocked_id=user_id, kind=kind
        ))
    if kind == 'block':
        Follow.query.filter(
            or_(
                (Follow.follower_id == current_user.id) & (Follow.followed_id == user_id),
                (Follow.follower_id == user_id) & (Follow.followed_id == current_user.id),
            )
        ).delete(synchronize_session=False)
    db.session.commit()
    if _wants_json():
        return jsonify({'ok': True, 'kind': kind, 'user_id': user_id})
    flash(f'{target.full_name} {kind}ed.', 'success')
    return redirect(request.referrer or url_for('social.feed'))


@social_bp.route('/unblock/<user_id>', methods=['POST'])
@login_required
def unblock_user(user_id):
    """POST /social/unblock/<id>?kind=block|mute — remove a block/mute."""
    kind = request.args.get('kind', 'block')
    block = Block.query.filter_by(
        blocker_id=current_user.id, blocked_id=user_id, kind=kind
    ).first()
    if block:
        db.session.delete(block)
        db.session.commit()
        flash('Removed.', 'info')
    return redirect(request.referrer or url_for('social.feed'))


# ---------------------------------------------------------------- bump/boost (M3, §1.3)

@social_bp.route('/posts/<post_id>/bump', methods=['GET', 'POST'])
@login_required
def bump_post(post_id):
    """GET/POST /social/posts/<post_id>/bump — bump this post with a
    free bump or a pack bump (paid bumps and boosts go through
    Checkout in the payments blueprint)."""
    post = get_or_404(Post, post_id)
    if post.author_id != current_user.id:
        flash('You can only bump your own posts.', 'danger')
        return redirect(url_for('social.post_detail', post_id=post_id))
    if request.method == 'POST':
        source = request.form.get('source')
        if source not in ('free', 'pack'):
            flash('Choose a free bump or a pack bump.', 'danger')
            return redirect(url_for('social.bump_post', post_id=post_id))
        bump, error = apply_bump(current_user, post, source)
        if error:
            flash(error, 'warning')
            return redirect(url_for('social.bump_post', post_id=post_id))
        db.session.commit()
        flash('Bumped! Your post is at the top of the feed for 24 hours.', 'success')
        return redirect(url_for('social.post_detail', post_id=post_id))
    eligible, reason = bump_eligibility(current_user, post)
    recurring = RecurringBump.query.filter_by(
        post_id=post.id, status='active'
    ).first()
    return render_template(
        'social/bump.html',
        post=post,
        eligible=eligible,
        reason=reason,
        free_balance=free_bump_balance(current_user.id),
        pack_balance=pack_bump_balance(current_user.id),
        performance=bump_performance_for_post(post.id),
        recurring=recurring,
        single_price_cents=BUMP_SINGLE_PRICE_CENTS,
        recurring_price_cents=RECURRING_BUMP_PRICE_CENTS,
        boost_price_per_day_cents=BOOST_PRICE_PER_DAY_CENTS,
        boost_max_days=BOOST_MAX_DAYS,
        author_levels=_author_levels([post]),
    )


# ---------------------------------------------------------------- pledge register (M3, §3.4)

@social_bp.route('/pledges')
def pledges():
    """GET /social/pledges — the public human-reviewed hiring pledge
    register: pledged employers, each with their no-ghosting
    compliance, and the platform-wide compliance rate."""
    rate, due, on_time = platform_ghosting_compliance()
    my_pledge = None
    if current_user.is_authenticated:
        my_pledge = Pledge.query.filter_by(user_id=current_user.id).first()
    return render_template(
        'social/pledges.html',
        entries=register_entries(),
        platform_rate=rate,
        platform_due=due,
        platform_on_time=on_time,
        pledge_text=Pledge.PLEDGE_TEXT,
        my_pledge=my_pledge,
    )


@social_bp.route('/pledges/new', methods=['GET', 'POST'])
@login_required
def new_pledge():
    """GET/POST /social/pledges/new — pledge human-reviewed hiring."""
    form = PledgeForm()
    if request.method == 'POST' and form.validate_on_submit():
        pledge, error = create_pledge(
            current_user,
            form.company_name.data,
            form.website.data,
        )
        if error:
            flash(error, 'warning')
            return redirect(url_for('social.pledges'))
        db.session.commit()
        flash(
            'Pledge published! Your company is now on the human-reviewed '
            'hiring register.',
            'success',
        )
        return redirect(url_for('social.pledges'))
    return render_template('social/pledge_form.html', form=form)
