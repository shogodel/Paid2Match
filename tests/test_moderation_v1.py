"""M3 moderation v1 tests — threshold auto-hide (incl. flag weights),
dismiss restores, upheld blocks restore, profile reports never
auto-hide, trust-level rate limits, 24h review SLA overdue flag."""
from datetime import timedelta

from extensions import db
from models import utc_now
from models.admin_settings import AdminSettings
from models.moderation import Report
from models.post import Comment, Post
from models.profile import Profile
from utils.moderation import (
    can_post_today,
    check_auto_hide,
    get_auto_hide_threshold,
    get_post_limit,
    maybe_restore_after_dismiss,
    open_report_weight,
)


def _report(reporter, target_type, target_id, weight=1, hours_ago=0):
    report = Report(
        reporter_id=reporter.id,
        target_type=target_type,
        target_id=target_id,
        reason='spam',
        weight=weight,
        status='open',
    )
    db.session.add(report)
    db.session.flush()
    if hours_ago:
        report.created_at = utc_now() - timedelta(hours=hours_ago)
    db.session.commit()
    db.session.refresh(report)
    return report


def _set_karma(user, amount):
    Profile.query.filter_by(user_id=user.id).first().karma = amount
    db.session.commit()


class TestAutoHide:
    def test_below_threshold_stays_visible(
        self, app, sample_user, second_user, sample_post
    ):
        _report(second_user, 'post', sample_post.id)
        _report(sample_user, 'post', sample_post.id)
        assert open_report_weight('post', sample_post.id) == 2
        assert check_auto_hide('post', sample_post.id) is False
        post = db.session.get(Post, sample_post.id)
        assert post.is_hidden is False
        assert post.auto_hidden_at is None

    def test_threshold_hides_post(
        self, app, sample_user, second_user, third_user, sample_post
    ):
        for reporter in (second_user, third_user, sample_user):
            _report(reporter, 'post', sample_post.id)
        assert open_report_weight('post', sample_post.id) == 3
        assert check_auto_hide('post', sample_post.id) is True
        post = db.session.get(Post, sample_post.id)
        assert post.is_hidden is True
        assert post.auto_hidden_at is not None

    def test_threshold_is_admin_configurable(
        self, app, sample_user, second_user, third_user, sample_post
    ):
        assert get_auto_hide_threshold() == 3
        AdminSettings.set('AUTO_HIDE_REPORT_THRESHOLD', '5')
        assert get_auto_hide_threshold() == 5
        for reporter in (second_user, third_user, sample_user):
            _report(reporter, 'post', sample_post.id)
        assert check_auto_hide('post', sample_post.id) is False

    def test_high_karma_report_weighs_double_via_route(
        self, app, client, sample_user, second_user, third_user, sample_post, login_as
    ):
        # 2500+ karma reports count double: two such reports meet the
        # default threshold of 3 (2 + 2 = 4).
        _set_karma(second_user, 2500)
        _set_karma(third_user, 2500)
        for user, password in (
            (second_user, 'SecondPass123!'),
            (third_user, 'ThirdPass123!'),
        ):
            login_as(client, user.email, password)
            resp = client.post(
                f'/social/report/post/{sample_post.id}',
                data={'reason': 'spam'},
            )
            assert resp.status_code == 302
        post = db.session.get(Post, sample_post.id)
        assert post.is_hidden is True
        assert post.auto_hidden_at is not None

    def test_comment_auto_hide(
        self, app, sample_user, second_user, third_user, sample_post
    ):
        comment = Comment(
            author_id=sample_user.id, post_id=sample_post.id, body='Hello.'
        )
        db.session.add(comment)
        db.session.commit()
        for reporter in (second_user, third_user, sample_user):
            _report(reporter, 'comment', comment.id)
        assert check_auto_hide('comment', comment.id) is True
        db.session.refresh(comment)
        assert comment.is_hidden is True

    def test_profile_reports_never_auto_hide(
        self, app, sample_user, second_user, third_user
    ):
        for reporter in (second_user, third_user, sample_user):
            _report(reporter, 'profile', sample_user.id)
        assert check_auto_hide('profile', sample_user.id) is False
        # The user's posts stay visible.
        posts = Post.query.filter_by(author_id=sample_user.id).all()
        assert all(p.is_hidden is False for p in posts)


class TestRestore:
    def _auto_hidden_post(self, sample_user, second_user, third_user, sample_post):
        reports = [
            _report(r, 'post', sample_post.id)
            for r in (second_user, third_user, sample_user)
        ]
        assert check_auto_hide('post', sample_post.id) is True
        db.session.commit()
        return reports

    def test_dismiss_all_restores(
        self, app, sample_user, second_user, third_user, sample_post
    ):
        reports = self._auto_hidden_post(
            sample_user, second_user, third_user, sample_post
        )
        # Dismissing two of three does not restore — one is still open.
        for report in reports[:2]:
            report.status = 'dismissed'
            db.session.commit()
            assert maybe_restore_after_dismiss('post', sample_post.id) is False
        reports[2].status = 'dismissed'
        db.session.commit()
        assert maybe_restore_after_dismiss('post', sample_post.id) is True
        post = db.session.get(Post, sample_post.id)
        assert post.is_hidden is False
        assert post.auto_hidden_at is None

    def test_upheld_report_blocks_restore(
        self, app, sample_user, second_user, third_user, sample_post
    ):
        reports = self._auto_hidden_post(
            sample_user, second_user, third_user, sample_post
        )
        reports[0].status = 'actioned'  # an admin upheld one report
        reports[1].status = 'dismissed'
        reports[2].status = 'dismissed'
        db.session.commit()
        assert maybe_restore_after_dismiss('post', sample_post.id) is False
        post = db.session.get(Post, sample_post.id)
        assert post.is_hidden is True

    def test_dismiss_via_admin_route_restores(
        self, app, client, sample_user, second_user, third_user, sample_post,
        sample_admin, login_as
    ):
        reports = self._auto_hidden_post(
            sample_user, second_user, third_user, sample_post
        )
        login_as(client, sample_admin.email, 'AdminPass123!')
        for report in reports:
            resp = client.post(
                f'/admin/reports/{report.id}/resolve',
                data={'action': 'dismiss'},
            )
            assert resp.status_code == 302
        post = db.session.get(Post, sample_post.id)
        assert post.is_hidden is False

    def test_admin_hide_clears_auto_hidden_marker(
        self, app, client, sample_user, second_user, third_user, sample_post,
        sample_admin, login_as
    ):
        reports = self._auto_hidden_post(
            sample_user, second_user, third_user, sample_post
        )
        login_as(client, sample_admin.email, 'AdminPass123!')
        # Uphold the first report (manual hide), dismiss the rest.
        resp = client.post(
            f'/admin/reports/{reports[0].id}/resolve',
            data={'action': 'hide'},
        )
        assert resp.status_code == 302
        post = db.session.get(Post, sample_post.id)
        assert post.is_hidden is True
        assert post.auto_hidden_at is None  # now a manual hide
        for report in reports[1:]:
            client.post(
                f'/admin/reports/{report.id}/resolve',
                data={'action': 'dismiss'},
            )
        post = db.session.get(Post, sample_post.id)
        assert post.is_hidden is True  # the manual hide stands


class TestRateLimits:
    def test_newcomer_limit_three(self, app, sample_user):
        assert get_post_limit(sample_user) == 3
        ok, limit, used = can_post_today(sample_user)
        assert (limit, used) == (3, 0)
        assert ok is True

    def test_newcomer_blocked_at_limit(
        self, app, client, sample_user, login_as
    ):
        login_as(client, sample_user.email, 'TestPass123!')
        for i in range(3):
            resp = client.post(
                '/social/posts/new',
                data={'post_type': 'update', 'body': f'Post {i}'},
            )
            assert resp.status_code == 302
        ok, limit, used = can_post_today(sample_user)
        assert (ok, limit, used) == (False, 3, 3)
        resp = client.post(
            '/social/posts/new',
            data={'post_type': 'update', 'body': 'Post 4'},
        )
        assert resp.status_code == 200  # re-rendered, not created
        assert Post.query.filter_by(author_id=sample_user.id).count() == 3

    def test_limits_are_admin_configurable(self, app, sample_user):
        AdminSettings.set('POST_LIMIT_NEWCOMER', '5')
        assert get_post_limit(sample_user) == 5


class TestReviewSla:
    def test_overdue_flag(self, app, sample_user, second_user, sample_post):
        fresh = _report(second_user, 'post', sample_post.id)
        assert fresh.is_overdue is False
        old = _report(sample_user, 'post', sample_post.id, hours_ago=25)
        assert old.is_overdue is True
        # Resolved reports are never overdue.
        old.status = 'dismissed'
        db.session.commit()
        assert old.is_overdue is False

    def test_overdue_sorts_first_in_admin_queue(
        self, app, client, sample_user, second_user, third_user,
        sample_post, sample_admin, login_as
    ):
        _report(second_user, 'post', sample_post.id)
        _report(third_user, 'post', sample_post.id, hours_ago=30)
        login_as(client, sample_admin.email, 'AdminPass123!')
        resp = client.get('/admin/reports')
        assert resp.status_code == 200
        assert b'Overdue' in resp.data
        assert b'1 report' in resp.data  # the overdue banner count
