"""M1 tests: basic analytics (free tier) + view counting."""
import pytest
from datetime import datetime, timedelta, timezone

from extensions import db
from models.follow import Follow
from models.post import Like, Post, Share
from models.profile_view import ProfileView
from models.referral import Referral
from utils.analytics import get_analytics


class TestPostViews:
    def test_view_counted_once_per_session(self, client, sample_post):
        post_id = sample_post.id
        client.get(f'/social/posts/{post_id}')
        client.get(f'/social/posts/{post_id}')
        client.get(f'/social/posts/{post_id}')
        assert db.session.get(Post, post_id).view_count == 1

    def test_view_counted_again_in_new_session(self, client, app, sample_post):
        post_id = sample_post.id
        client.get(f'/social/posts/{post_id}')
        # A fresh client = a fresh session.
        other = app.test_client()
        other.get(f'/social/posts/{post_id}')
        assert db.session.get(Post, post_id).view_count == 2


class TestProfileViews:
    def test_profile_view_recorded_once_per_session(self, client, login_as, second_user, sample_user):
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.get(f'/profile/{sample_user.id}')
        client.get(f'/profile/{sample_user.id}')
        assert ProfileView.query.filter_by(
            profile_user_id=sample_user.id, viewer_id=second_user.id).count() == 1

    def test_own_profile_view_not_recorded(self, client, logged_in_user, sample_user):
        client.get(f'/profile/{sample_user.id}')
        assert ProfileView.query.filter_by(profile_user_id=sample_user.id).count() == 0

    def test_profile_views_7d_counts_recent_only(self, app, sample_user, second_user):
        recent = ProfileView(profile_user_id=sample_user.id, viewer_id=second_user.id)
        db.session.add(recent)
        db.session.commit()
        old = ProfileView(profile_user_id=sample_user.id, viewer_id=second_user.id)
        db.session.add(old)
        db.session.commit()
        old.created_at = datetime.now(timezone.utc) - timedelta(days=10)
        db.session.commit()
        analytics = get_analytics(sample_user.id)
        assert analytics['profile_views_7d'] == 1


class TestAnalyticsNumbers:
    def test_post_rows_and_totals(self, app, sample_user, second_user, sample_post):
        # Fixture objects are detached — re-fetch before mutating.
        post = db.session.get(Post, sample_post.id)
        db.session.add(Like(user_id=second_user.id, post_id=post.id))
        db.session.add(Share(user_id=second_user.id, post_id=post.id))
        post.view_count = 7
        db.session.commit()
        analytics = get_analytics(sample_user.id)
        assert analytics['totals'] == {'views': 7, 'likes': 1, 'comments': 0, 'shares': 1}
        assert len(analytics['post_rows']) == 1
        assert analytics['post_rows'][0]['views'] == 7

    def test_follower_counts(self, app, sample_user, second_user, third_user):
        db.session.add(Follow(follower_id=second_user.id, followed_id=sample_user.id))
        db.session.add(Follow(follower_id=third_user.id, followed_id=sample_user.id))
        db.session.add(Follow(follower_id=sample_user.id, followed_id=second_user.id))
        db.session.commit()
        analytics = get_analytics(sample_user.id)
        assert analytics['followers'] == 2
        assert analytics['following'] == 1
        assert analytics['new_followers_7d'] == 2

    def test_referral_stats_and_earnings(self, app, sample_user, second_user, sample_bounty, sample_paid_bounty):
        from models.match import Match
        from models.pitch import Pitch
        # One completed free referral + one completed paid referral.
        for bounty, email in [(sample_bounty, 'free@example.com'),
                              (sample_paid_bounty, 'paid@example.com')]:
            pitch = Pitch(bounty_id=bounty.id, hunter_id=second_user.id,
                          candidate_teaser='e' * 60, status='accepted')
            db.session.add(pitch)
            db.session.flush()
            match = Match(bounty_id=bounty.id, pitch_id=pitch.id,
                          hunter_id=second_user.id, employer_id=sample_user.id,
                          hunter_confirmed=True, employer_confirmed=True,
                          payout_amount=5000 if bounty.reward_amount else None,
                          platform_fee=600 if bounty.reward_amount else None,
                          # M2: 'pending_release' was never a valid
                          # status — the validator now rejects it.
                          payout_status='processing')
            db.session.add(match)
            referral = Referral(referrer_id=second_user.id, bounty_id=bounty.id,
                                pitch_id=pitch.id, referred_name='Someone',
                                referred_email=email, message='e' * 60,
                                status='completed')
            db.session.add(referral)
        db.session.commit()
        analytics = get_analytics(second_user.id)
        assert analytics['referrals']['made'] == 2
        assert analytics['referrals']['completed'] == 2
        assert analytics['referrals']['completed_paid'] == 1
        assert analytics['referrals']['completed_free'] == 1
        # Pending = net of the paid match (5000 - 600); free match nets 0.
        assert analytics['earnings']['pending'] == 4400.0
        assert analytics['earnings']['paid_out'] == 0.0

    def test_analytics_page_renders(self, client, logged_in_user, sample_post):
        resp = client.get('/social/analytics')
        assert resp.status_code == 200
        assert b'My analytics' in resp.data
        assert b'Trust level' in resp.data
        assert b'Karma' in resp.data
