"""Social routes (M1): feed, posts, follows, people, notifications,
analytics, moderation v0. M3 adds: bump/boost application, Pro
analytics + CSV export, the pledge register, moderation v1 (auto-hide,
rate limits), and trust-level badges on post cards."""
import os
import uuid

from flask import (
    Blueprint, Response, flash, redirect, render_template, request, session,
    url_for,
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
from models.post import Comment, Like, Post, Share
from models.post_view import PostView, SearchAppearance
from models.profile import Profile
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
    PUBLISHED_DISCOVER_RULES, create_need_post, get_discover_feed,
    get_following_feed, suggested_connections,
)
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


# ---------------------------------------------------------------- feed

@social_bp.route('/feed')
@login_required
def feed():
    """GET /social/feed — Following (default) or Discover tab."""
    tab = request.args.get('tab', 'following')
    page = request.args.get('page', 1, type=int)
    if tab == 'discover':
        result = get_discover_feed(current_user.id, page=page)
    else:
        tab = 'following'
        result = get_following_feed(current_user.id, page=page)
    post_ids = [item['post'].id for item in result['items']]
    liked_ids, shared_ids = _liked_and_shared_ids(post_ids)
    return render_template(
        'social/feed.html',
        tab=tab,
        feed=result,
        liked_ids=liked_ids,
        shared_ids=shared_ids,
        author_levels=_author_levels([item['post'] for item in result['items']]),
        discover_rules=PUBLISHED_DISCOVER_RULES,
        suggested=suggested_connections(current_user.id),
        share_form=ShareForm(),
    )


# ---------------------------------------------------------------- posts

@social_bp.route('/posts/new', methods=['GET', 'POST'])
@login_required
def new_post():
    """GET/POST /social/posts/new — create a post (or a referral need)."""
    form = PostForm()
    if request.method == 'POST' and form.validate_on_submit():
        # M3 moderation v1: trust-level daily post limit.
        ok, limit, used = can_post_today(current_user)
        if not ok:
            flash(
                f'You have used your {limit} posts for today '
                f'({used}/{limit}) — the daily limit grows as your trust '
                'level grows.',
                'warning',
            )
            return render_template('social/post_form.html', form=form, post=None)
        photo_url = _save_post_photo(form.photo.data) if form.photo.data else None
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
            db.session.commit()
            if form.post_type.data == 'need_paid':
                flash('Referral need created! Secure payment to publish it.', 'success')
                return redirect(url_for('bounties.secure_payment', id=bounty.id))
            flash('Community referral posted!', 'success')
            return redirect(url_for('social.post_detail', post_id=post.id))

        post = Post(
            author_id=current_user.id,
            post_type=form.post_type.data,
            body=form.body.data.strip(),
            photo_url=photo_url,
        )
        db.session.add(post)
        db.session.commit()
        flash('Posted!', 'success')
        return redirect(url_for('social.feed'))
    return render_template('social/post_form.html', form=form, post=None)


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
        author_trust=trust_level(post.author) if post.author else None,
        author_levels=_author_levels([post]),
    )


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
    return render_template('social/post_form.html', form=form, post=post)


@social_bp.route('/posts/<post_id>/delete', methods=['POST'])
@login_required
def delete_post(post_id):
    """POST /social/posts/<id>/delete — delete your own post."""
    post = get_or_404(Post, post_id)
    if post.author_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('social.post_detail', post_id=post_id))
    db.session.delete(post)
    db.session.commit()
    flash('Post deleted.', 'info')
    return redirect(url_for('social.feed'))


@social_bp.route('/posts/<post_id>/like', methods=['POST'])
@login_required
def like_post(post_id):
    """POST /social/posts/<id>/like — toggle a like. A new like earns the
    author +10 karma (§4.2); unliking does not revoke it."""
    post = get_or_404(Post, post_id)
    existing = Like.query.filter_by(user_id=current_user.id, post_id=post_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
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
    return redirect(request.referrer or url_for('social.feed'))


@social_bp.route('/posts/<post_id>/comment', methods=['POST'])
@login_required
def comment_post(post_id):
    """POST /social/posts/<id>/comment — comment, or reply to a top-level
    comment (one level deep, §1.2)."""
    post = get_or_404(Post, post_id)
    form = CommentForm()
    if form.validate_on_submit():
        parent = None
        if form.parent_id.data:
            parent = db.session.get(Comment, form.parent_id.data)
            if not parent or parent.post_id != post_id or parent.parent_id is not None:
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
        db.session.commit()
        flash('Comment posted!', 'success')
    return redirect(url_for('social.post_detail', post_id=post_id))


@social_bp.route('/posts/<post_id>/share', methods=['POST'])
@login_required
def share_post(post_id):
    """POST /social/posts/<id>/share — reshare to your feed with a comment.
    One share per user per post; the share is attributed to you (§4.4)."""
    post = get_or_404(Post, post_id)
    form = ShareForm()
    if Share.query.filter_by(user_id=current_user.id, post_id=post_id).first():
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
        flash('Shared to your feed!', 'success')
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


# ---------------------------------------------------------------- follows

@social_bp.route('/follow/<user_id>', methods=['POST'])
@login_required
def follow(user_id):
    """POST /social/follow/<id> — follow a user."""
    target = get_or_404(User, user_id)
    if target.id == current_user.id:
        flash('You cannot follow yourself.', 'warning')
        return redirect(request.referrer or url_for('social.feed'))
    blocked = Block.query.filter_by(
        blocker_id=target.id, blocked_id=current_user.id, kind='block'
    ).first()
    if blocked:
        flash('You cannot follow this user.', 'warning')
        return redirect(request.referrer or url_for('social.feed'))
    if Follow.query.filter_by(follower_id=current_user.id, followed_id=user_id).first():
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
    flash(f'You are now following {target.full_name}.', 'success')
    return redirect(request.referrer or url_for('social.feed'))


@social_bp.route('/unfollow/<user_id>', methods=['POST'])
@login_required
def unfollow(user_id):
    """POST /social/unfollow/<id> — unfollow a user."""
    follow = Follow.query.filter_by(
        follower_id=current_user.id, followed_id=user_id
    ).first()
    if follow:
        db.session.delete(follow)
        db.session.commit()
        flash('Unfollowed.', 'info')
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
    return render_template(
        'social/people.html',
        users=users,
        suggested=suggested,
        following_ids=following_ids,
        q=query_text, skill=skill, location=location,
    )


# ---------------------------------------------------------------- notifications

@social_bp.route('/notifications')
@login_required
def notifications():
    """GET /social/notifications — notification center."""
    page = request.args.get('page', 1, type=int)
    pagination = Notification.query.filter_by(
        recipient_id=current_user.id
    ).order_by(desc(Notification.created_at)).paginate(
        page=page, per_page=20, error_out=False
    )
    return render_template(
        'social/notifications.html',
        notifications=pagination.items,
        pagination=pagination,
        unread=unread_count(current_user.id),
    )


@social_bp.route('/notifications/<notification_id>/read', methods=['POST'])
@login_required
def read_notification(notification_id):
    """POST /social/notifications/<id>/read — mark read, then follow the link."""
    notification = get_or_404(Notification, notification_id)
    if notification.recipient_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('social.notifications'))
    notification.is_read = True
    db.session.commit()
    if notification.link_url and notification.link_url.startswith('/'):
        return redirect(notification.link_url)
    return redirect(url_for('social.notifications'))


@social_bp.route('/notifications/read-all', methods=['POST'])
@login_required
def read_all_notifications():
    """POST /social/notifications/read-all — mark everything read."""
    Notification.query.filter_by(
        recipient_id=current_user.id, is_read=False
    ).update({'is_read': True})
    db.session.commit()
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
