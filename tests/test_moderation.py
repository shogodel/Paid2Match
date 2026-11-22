"""M1 tests: moderation v0 — reports, block/mute, review queue."""
import pytest

from extensions import db
from models.follow import Follow
from models.moderation import Block, Report
from models.post import Comment, Post
from models.profile import Profile
from utils.feed import get_following_feed
from utils.trust import compute_trust_score


def _report(client, target_type, target_id, reason='spam', details=''):
    return client.post(f'/social/report/{target_type}/{target_id}', data={
        'reason': reason, 'details': details,
    }, follow_redirects=True)


class TestReports:
    def test_report_post(self, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        resp = _report(client, 'post', sample_post.id, details='This looks like spam.')
        assert resp.status_code == 200
        report = Report.query.filter_by(
            reporter_id=second_user.id, target_type='post', target_id=sample_post.id).first()
        assert report is not None
        assert report.status == 'open'
        assert report.weight == 1

    def test_report_dedupes_open_reports(self, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        _report(client, 'post', sample_post.id)
        _report(client, 'post', sample_post.id)
        assert Report.query.filter_by(
            reporter_id=second_user.id, target_type='post', target_id=sample_post.id).count() == 1

    def test_report_comment_and_profile(self, client, login_as, second_user, sample_post, sample_user):
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.post(f'/social/posts/{sample_post.id}/comment', data={
            'body': 'A comment worth reporting for this test, long enough.',
        })
        comment = Comment.query.filter_by(post_id=sample_post.id).first()
        _report(client, 'comment', comment.id, reason='harassment')
        _report(client, 'profile', sample_user.id, reason='fake')
        assert Report.query.filter_by(target_type='comment', target_id=comment.id).count() == 1
        assert Report.query.filter_by(target_type='profile', target_id=sample_user.id).count() == 1

    def test_high_karma_reporter_weight_two(self, client, login_as, second_user, sample_post):
        profile = Profile.query.filter_by(user_id=second_user.id).first()
        profile.karma = 2500
        db.session.commit()
        login_as(client, 'second@example.com', 'SecondPass123!')
        _report(client, 'post', sample_post.id)
        report = Report.query.filter_by(reporter_id=second_user.id).first()
        assert report.weight == 2

    def test_report_nonexistent_target_redirects(self, client, logged_in_user):
        resp = client.post('/social/report/post/does-not-exist', data={'reason': 'spam'},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert Report.query.count() == 0


class TestBlockMute:
    def test_block_hides_from_feed_and_unfollows(self, client, login_as, second_user,
                                                 sample_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.post(f'/social/follow/{sample_user.id}')
        assert Follow.query.filter_by(
            follower_id=second_user.id, followed_id=sample_user.id).first() is not None
        # Before the block, the post is in the feed.
        feed = get_following_feed(second_user.id)
        assert sample_post.id in [i['post'].id for i in feed['items']]
        client.post(f'/social/block/{sample_user.id}')
        # Block unfollowed both directions and hid the author.
        assert Follow.query.filter_by(
            follower_id=second_user.id, followed_id=sample_user.id).first() is None
        feed = get_following_feed(second_user.id)
        assert sample_post.id not in [i['post'].id for i in feed['items']]

    def test_mute_hides_from_feed_but_keeps_follow(self, client, login_as, second_user,
                                                   sample_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.post(f'/social/follow/{sample_user.id}')
        client.post(f'/social/block/{sample_user.id}?kind=mute')
        assert Block.query.filter_by(
            blocker_id=second_user.id, blocked_id=sample_user.id, kind='mute').first() is not None
        # Mute does not unfollow.
        assert Follow.query.filter_by(
            follower_id=second_user.id, followed_id=sample_user.id).first() is not None
        feed = get_following_feed(second_user.id)
        assert sample_post.id not in [i['post'].id for i in feed['items']]

    def test_cannot_block_self(self, client, logged_in_user, sample_user):
        client.post(f'/social/block/{sample_user.id}')
        assert Block.query.filter_by(blocker_id=sample_user.id).count() == 0


class TestReviewQueue:
    def _open_report(self, app, second_user, sample_post):
        report = Report(reporter_id=second_user.id, target_type='post',
                        target_id=sample_post.id, reason='spam')
        db.session.add(report)
        db.session.commit()
        return report.id

    def test_admin_can_view_queue(self, client, logged_in_admin, app, second_user, sample_post):
        self._open_report(app, second_user, sample_post)
        resp = client.get('/admin/reports')
        assert resp.status_code == 200
        assert b'Review queue' in resp.data

    def test_moderator_role_can_view_queue(self, client, login_as, sample_moderator,
                                           app, second_user, sample_post):
        self._open_report(app, second_user, sample_post)
        login_as(client, 'mod@example.com', 'ModPass123!')
        resp = client.get('/admin/reports')
        assert resp.status_code == 200

    def test_regular_user_cannot_view_queue(self, client, logged_in_user):
        resp = client.get('/admin/reports')
        assert resp.status_code == 302  # redirected away

    def test_5000_karma_user_can_view_but_not_act(self, client, logged_in_user, app,
                                                  sample_user, second_user, sample_post):
        report_id = self._open_report(app, second_user, sample_post)
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        profile.karma = 5000
        db.session.commit()
        resp = client.get('/admin/reports')
        assert resp.status_code == 200
        resp = client.post(f'/admin/reports/{report_id}/resolve',
                           data={'action': 'hide'}, follow_redirects=True)
        assert b'Only moderators' in resp.data
        assert db.session.get(Report, report_id).status == 'open'

    def test_admin_dismiss(self, client, logged_in_admin, app, second_user, sample_post):
        report_id = self._open_report(app, second_user, sample_post)
        client.post(f'/admin/reports/{report_id}/resolve', data={'action': 'dismiss'},
                    follow_redirects=True)
        report = db.session.get(Report, report_id)
        assert report.status == 'dismissed'
        assert report.reviewed_by_id is not None

    def test_admin_hide_upholds_and_hides_content(self, client, logged_in_admin, app,
                                                  second_user, sample_post):
        report_id = self._open_report(app, second_user, sample_post)
        client.post(f'/admin/reports/{report_id}/resolve', data={'action': 'hide'},
                    follow_redirects=True)
        assert db.session.get(Report, report_id).status == 'actioned'
        assert db.session.get(Post, sample_post.id).is_hidden is True

    def test_upheld_report_reduces_trust_score(self, app, sample_user, second_user, sample_post):
        before, _ = compute_trust_score(sample_user)
        report = Report(reporter_id=second_user.id, target_type='post',
                        target_id=sample_post.id, reason='spam', status='actioned')
        db.session.add(report)
        db.session.commit()
        after, breakdown = compute_trust_score(sample_user)
        assert breakdown['penalties'] == 10
        assert after == max(0, before - 10)
