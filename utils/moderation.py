"""Moderation v1 (M3): threshold auto-hide, trust-level rate limits,
24-hour review SLA.

- Auto-hide: when the total weight of open reports on a post or
  comment reaches the threshold (default 3, admin-configurable), the
  content is hidden pending review. Reports from high-karma users
  weigh more (weight 2 at 2,500+ karma, 3 for Ambassadors), so a
  trusted reporter's flag hides faster. Profile reports never
  auto-hide — hiding all of someone's posts on 3 reports is too blunt
  for an automatic action; a moderator decides.
- Restore: when the last open report on an auto-hidden target is
  dismissed, the content comes back — unless an earlier report was
  upheld (an admin hide stands).
- Rate limits: Newcomers post less often (default 3/day) than
  Member+ accounts (default 10/day); both admin-configurable.
"""
from sqlalchemy import func

from extensions import db
from models import utc_now
from models.admin_settings import AdminSettings
from models.moderation import Report
from models.post import Comment, Post
from utils.trust import trust_level

AUTO_HIDE_SETTING_KEY = 'AUTO_HIDE_REPORT_THRESHOLD'
DEFAULT_AUTO_HIDE_THRESHOLD = 3
POST_LIMIT_NEWCOMER_KEY = 'POST_LIMIT_NEWCOMER'
POST_LIMIT_MEMBER_KEY = 'POST_LIMIT_MEMBER'
DEFAULT_POST_LIMIT_NEWCOMER = 3
DEFAULT_POST_LIMIT_MEMBER = 10


def _int_setting(key, default, minimum=1):
    raw = AdminSettings.get(key)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def get_auto_hide_threshold():
    return _int_setting(AUTO_HIDE_SETTING_KEY, DEFAULT_AUTO_HIDE_THRESHOLD)


def get_post_limit(user):
    """Daily post limit for this user by trust level."""
    level, _s, _b = trust_level(user)
    if level == 'Newcomer':
        return _int_setting(POST_LIMIT_NEWCOMER_KEY, DEFAULT_POST_LIMIT_NEWCOMER)
    return _int_setting(POST_LIMIT_MEMBER_KEY, DEFAULT_POST_LIMIT_MEMBER)


def posts_today(user_id):
    """Posts this user created today (UTC day)."""
    day_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return Post.query.filter(
        Post.author_id == user_id,
        Post.created_at >= day_start,
    ).count()


def can_post_today(user):
    """(ok, limit, used) — has the user hit their daily post limit?"""
    limit = get_post_limit(user)
    used = posts_today(user.id)
    return used < limit, limit, used


def open_report_weight(target_type, target_id):
    """Total weight of open reports on a target."""
    return db.session.query(
        func.coalesce(func.sum(Report.weight), 0)
    ).filter(
        Report.target_type == target_type,
        Report.target_id == target_id,
        Report.status == 'open',
    ).scalar() or 0


def check_auto_hide(target_type, target_id):
    """Hide a post/comment whose open-report weight reached the
    threshold. Returns True when something was hidden. Does not commit
    — the caller commits."""
    if target_type not in ('post', 'comment'):
        return False
    if open_report_weight(target_type, target_id) < get_auto_hide_threshold():
        return False
    target = None
    if target_type == 'post':
        target = db.session.get(Post, target_id)
    else:
        target = db.session.get(Comment, target_id)
    if target is None or target.is_hidden:
        return False
    target.is_hidden = True
    target.auto_hidden_at = utc_now()
    db.session.flush()
    return True


def maybe_restore_after_dismiss(target_type, target_id):
    """Restore an auto-hidden post/comment when its last open report
    was dismissed — unless an earlier report was upheld (an admin
    hide stands). Returns True when something was restored. Does not
    commit — the caller commits."""
    if target_type not in ('post', 'comment'):
        return False
    target = None
    if target_type == 'post':
        target = db.session.get(Post, target_id)
    else:
        target = db.session.get(Comment, target_id)
    if target is None or not target.is_hidden or target.auto_hidden_at is None:
        return False
    open_left = Report.query.filter_by(
        target_type=target_type, target_id=target_id, status='open'
    ).count()
    if open_left > 0:
        return False
    upheld = Report.query.filter_by(
        target_type=target_type, target_id=target_id, status='actioned'
    ).count()
    if upheld > 0:
        return False
    target.is_hidden = False
    target.auto_hidden_at = None
    db.session.flush()
    return True
