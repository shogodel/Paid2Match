"""Phase 3 tests: notifications, discovery, digest, trust visibility,
hiring layer, staged rewards, motion, skeletons, board cleanup."""
import os
from datetime import timedelta

import pytest

from extensions import db, mail
from models import utc_now
from models.bounty import Bounty, MIN_PAID_BOUNTY_CAD
from models.endorsement import Pledge
from models.follow import Follow
from models.message import Message
from models.notification import Notification
from models.post import Comment, Like, Post
from models.referral import Referral
from utils.notifications import notify, unread_count


def _make_post(app, author_id, body, post_type='update', created_at=None):
    with app.app_context():
        post = Post(author_id=author_id, post_type=post_type, body=body)
        if created_at is not None:
            post.created_at = created_at
        db.session.add(post)
        db.session.commit()
        db.session.refresh(post)
        return post


def _like(app, user_id, post_id):
    with app.app_context():
        db.session.add(Like(user_id=user_id, post_id=post_id))
        db.session.commit()


def _notify(app, recipient_id, type_, title, actor_id=None):
    with app.app_context():
        n = notify(recipient_id, type_, title, actor_id=actor_id)
        db.session.commit()
        return n


# ---------------------------------------------------------------- notifications


class TestNotificationsPanel:
    def test_bell_panel_renders(self, client, logged_in_user, app,
                                sample_user, second_user):
        _notify(app, sample_user.id, 'like', 'Second liked your post',
                actor_id=second_user.id)
        html = client.get('/social/feed').data.decode()
        assert 'notification-panel' in html
        assert 'notifications-bell' in html
        assert 'Mark all read' in html
        assert 'View all notifications' in html
        assert 'Second liked your post' in html

    def test_panel_groups_new_and_earlier(self, client, logged_in_user,
                                          app, sample_user, second_user):
        _notify(app, sample_user.id, 'like', 'New like here',
                actor_id=second_user.id)
        _notify(app, sample_user.id, 'follow',
                'Old follow here', actor_id=second_user.id)
        with app.app_context():
            old = Notification.query.filter_by(
                recipient_id=sample_user.id, type='follow').first()
            old.is_read = True
            db.session.commit()
        html = client.get('/social/feed').data.decode()
        assert 'New' in html
        assert 'Earlier' in html
        # The unread item comes before the read one (New group first).
        assert html.index('New like here') < html.index('Old follow here')

    def test_panel_json_endpoint(self, client, logged_in_user, app,
                                 sample_user, second_user):
        _notify(app, sample_user.id, 'like', 'JSON like',
                actor_id=second_user.id)
        r = client.get('/social/notifications/panel')
        assert r.status_code == 200
        data = r.get_json()
        assert data['unread'] == 1
        assert len(data['notifications']) == 1
        assert data['notifications'][0]['title'] == 'JSON like'
        assert data['notifications'][0]['icon'] == 'heart'

    def test_poll_returns_only_newer_unread(self, client, logged_in_user,
                                            app, sample_user, second_user):
        _notify(app, sample_user.id, 'like', 'Before poll',
                actor_id=second_user.id)
        since = utc_now().isoformat()
        _notify(app, sample_user.id, 'comment', 'After poll',
                actor_id=second_user.id)
        r = client.get('/social/notifications/poll',
                       query_string={'since': since})
        assert r.status_code == 200
        data = r.get_json()
        titles = [n['title'] for n in data['notifications']]
        assert 'After poll' in titles
        assert 'Before poll' not in titles
        assert data['unread'] == 2

    def test_read_all_json_clears_badge(self, client, logged_in_user, app,
                                        sample_user, second_user):
        _notify(app, sample_user.id, 'like', 'One',
                actor_id=second_user.id)
        assert unread_count(sample_user.id) == 1
        r = client.post('/social/notifications/read-all',
                        headers={'X-Requested-With': 'XMLHttpRequest'})
        assert r.status_code == 200
        assert r.get_json() == {'ok': True, 'unread': 0}
        assert unread_count(sample_user.id) == 0


class TestNotificationsPage:
    def test_filter_chips(self, client, logged_in_user, app, sample_user,
                          second_user):
        _notify(app, sample_user.id, 'like', 'Like note',
                actor_id=second_user.id)
        _notify(app, sample_user.id, 'comment', 'Comment note',
                actor_id=second_user.id)
        _notify(app, sample_user.id, 'follow', 'Follow note',
                actor_id=second_user.id)
        html = client.get('/social/notifications').data.decode()
        assert 'Likes' in html and 'Comments' in html
        assert 'Follows' in html and 'Referrals' in html
        assert 'Payouts' in html
        # The bell panel (base.html) shows all recent notifications,
        # so check only the page list — everything after the chips.
        likes = client.get(
            '/social/notifications?filter=likes').data.decode()
        likes = likes.split('notification-filters', 1)[1]
        assert 'Like note' in likes
        assert 'Comment note' not in likes
        assert 'Follow note' not in likes
        follows = client.get(
            '/social/notifications?filter=follows').data.decode()
        follows = follows.split('notification-filters', 1)[1]
        assert 'Follow note' in follows
        assert 'Like note' not in follows

    def test_referral_and_payout_filters(self, client, logged_in_user, app,
                                         sample_user, second_user):
        _notify(app, sample_user.id, 'referral_new', 'Referral note',
                actor_id=second_user.id)
        _notify(app, sample_user.id, 'payment_released', 'Payout note',
                actor_id=second_user.id)
        referrals = client.get(
            '/social/notifications?filter=referrals').data.decode()
        referrals = referrals.split('notification-filters', 1)[1]
        assert 'Referral note' in referrals
        assert 'Payout note' not in referrals
        payouts = client.get(
            '/social/notifications?filter=payouts').data.decode()
        payouts = payouts.split('notification-filters', 1)[1]
        assert 'Payout note' in payouts
        assert 'Referral note' not in payouts


# ---------------------------------------------------------------- search


class TestSearch:
    def test_grouped_results(self, client, logged_in_user, app, sample_user,
                             second_user, sample_bounty):
        _make_post(app, second_user.id,
                   'A post about zebra migrations in the wild.')
        html = client.get('/social/search?q=zebra').data.decode()
        assert 'Posts' in html
        assert 'zebra migrations' in html
        html = client.get('/social/search?q=Second').data.decode()
        assert 'People' in html
        assert 'Second User' in html
        html = client.get('/social/search?q=Python').data.decode()
        assert 'Opportunities' in html
        assert 'Senior Python Engineer' in html

    def test_empty_query_shows_prompt(self, client, logged_in_user):
        r = client.get('/social/search')
        assert r.status_code == 200
        html = r.data.decode()
        assert 'Search people, posts, and opportunities' in html

    def test_search_input_in_nav(self, client, logged_in_user):
        html = client.get('/social/feed').data.decode()
        assert 'nav-search' in html
        assert '/social/search' in html


# ---------------------------------------------------------------- trending


class TestTrendingFeed:
    def test_ordering_by_engagement_velocity(
            self, app, client, logged_in_user, sample_user, second_user,
            third_user):
        quiet = _make_post(app, second_user.id, 'Quiet post, no likes.')
        hot = _make_post(app, third_user.id, 'Hot post, many likes.')
        _like(app, sample_user.id, hot.id)
        _like(app, second_user.id, hot.id)
        html = client.get('/social/feed?tab=trending').data.decode()
        assert 'Hot post, many likes.' in html
        assert html.index('Hot post, many likes.') < html.index(
            'Quiet post, no likes.')

    def test_trending_differs_from_top(
            self, app, client, logged_in_user, sample_user, second_user,
            third_user):
        # A: 6 days old, 10 likes (from many users — reuse the three
        # users plus fresh Like rows are one-per-user, so use
        # comments too: 2 likes + 2 comments = velocity 6).
        old = _make_post(
            app, second_user.id, 'Old but engaged post.',
            created_at=utc_now() - timedelta(days=6))
        _like(app, sample_user.id, old.id)
        _like(app, third_user.id, old.id)
        with app.app_context():
            db.session.add(Comment(
                post_id=old.id, author_id=sample_user.id,
                body='Great point.'))
            db.session.add(Comment(
                post_id=old.id, author_id=third_user.id,
                body='Agreed.'))
            db.session.commit()
        # B: 1 hour old, no engagement.
        _make_post(app, third_user.id, 'Fresh but quiet post.',
                   created_at=utc_now() - timedelta(hours=1))
        trending = client.get('/social/feed?tab=trending').data.decode()
        top = client.get('/social/feed?tab=discover').data.decode()
        # Trending: engagement wins (old post first).
        assert trending.index('Old but engaged post.') < trending.index(
            'Fresh but quiet post.')
        # Top: recency wins (fresh post first — 47 recency points
        # beat the old post's engagement + 0 recency).
        assert top.index('Fresh but quiet post.') < top.index(
            'Old but engaged post.')

    def test_trending_excludes_old_posts_and_includes_own(
            self, app, client, logged_in_user, sample_user, second_user):
        _make_post(app, second_user.id, 'Too old to trend.',
                   created_at=utc_now() - timedelta(days=8))
        _make_post(app, sample_user.id, 'My own trending post.')
        html = client.get('/social/feed?tab=trending').data.decode()
        assert 'Too old to trend.' not in html
        assert 'My own trending post.' in html
        assert 'How Trending ranks posts:' in html


# ---------------------------------------------------------------- people to follow


class TestPeopleToFollow:
    def test_excludes_self_and_followed(
            self, app, sample_user, second_user, third_user):
        from utils.feed import suggested_connections
        with app.app_context():
            db.session.add(Follow(
                follower_id=sample_user.id, followed_id=second_user.id))
            db.session.commit()
        suggested = suggested_connections(sample_user.id)
        ids = [u.id for u in suggested]
        assert sample_user.id not in ids
        assert second_user.id not in ids
        assert third_user.id in ids

    def test_fallback_without_location(
            self, app, sample_user, second_user):
        # The sample user has no location — the old stub returned [];
        # the new function still suggests by karma/trust.
        from utils.feed import suggested_connections
        suggested = suggested_connections(sample_user.id)
        assert second_user.id in [u.id for u in suggested]


# ---------------------------------------------------------------- digest


class TestDigest:
    def test_build_digest_renders(self, app, sample_user, second_user):
        from utils.digest import build_digest
        _make_post(app, second_user.id, 'A digest-worthy post.')
        with app.test_request_context(base_url='https://paid2match.work'):
            from models.user import User
            user = db.session.get(User, sample_user.id)
            subject, body = build_digest(user)
        assert subject == 'Your week on Paid2Match'
        assert 'Top posts this week' in body
        assert 'A digest-worthy post.' in body
        assert 'New people to follow' in body
        assert 'Second User' in body
        assert 'Your week' in body
        assert 'Karma earned' in body

    def test_cli_skips_gracefully_without_mail(self, app, runner,
                                               sample_user):
        app.config['MAIL_SERVER'] = None
        result = runner.invoke(args=['send-weekly-digest'])
        assert result.exit_code == 0
        assert '0 sent' in result.output
        assert '1 skipped' in result.output

    def test_digest_sends_with_mail_configured(self, app, sample_user,
                                               second_user):
        from utils.digest import send_weekly_digest
        with mail.record_messages() as outbox:
            sent, skipped = send_weekly_digest()
        assert sent == 2
        assert skipped == 0
        assert len(outbox) == 2
        assert outbox[0].subject == 'Your week on Paid2Match'
        assert 'Your week' in outbox[0].body


# ---------------------------------------------------------------- trust visibility


class TestVouches:
    def test_public_profile_shows_vouches(self, client, logged_in_user, app,
                                          sample_user, second_user):
        from utils.endorsements import give_endorsement
        with app.app_context():
            from models.profile import Profile
            p = Profile.query.filter_by(user_id=second_user.id).first()
            p.karma = 300
            db.session.commit()
            _e, err = give_endorsement(
                second_user, sample_user, 'Python', 'Great to work with')
            assert err is None
            db.session.commit()
        html = client.get(
            f'/profile/{sample_user.handle}').data.decode()
        assert 'Vouches' in html
        assert 'Second User' in html
        assert 'Python' in html
        assert 'Great to work with' in html
        assert 'Endorsements' in html  # M3 vocabulary kept in subtext

    def test_own_profile_shows_vouches(self, client, logged_in_user, app,
                                       sample_user, second_user):
        from utils.endorsements import give_endorsement
        with app.app_context():
            from models.profile import Profile
            p = Profile.query.filter_by(user_id=second_user.id).first()
            p.karma = 300
            db.session.commit()
            _e, err = give_endorsement(second_user, sample_user, 'Python')
            assert err is None
            db.session.commit()
        html = client.get('/profile/').data.decode()
        assert 'Vouches' in html
        assert 'Second User' in html


class TestVerificationTiers:
    def test_tier_email_only(self, app, sample_user):
        from utils.verification_tiers import verification_tier
        tier, label, _signals = verification_tier(sample_user)
        assert tier == 1
        assert label == 'Email verified'

    def test_tier_not_verified(self, app, sample_user):
        from models.user import User
        from utils.verification_tiers import verification_tier
        with app.app_context():
            user = db.session.get(User, sample_user.id)
            user.email_verified = False
            db.session.commit()
        tier, _label, _signals = verification_tier(sample_user)
        assert tier == 0

    def test_tier_linked_account(self, app, sample_user):
        from models.linked_account import LinkedAccount
        from utils.verification_tiers import verification_tier
        with app.app_context():
            db.session.add(LinkedAccount(
                user_id=sample_user.id, platform='linkedin',
                status='verified', verification_method='oauth'))
            db.session.commit()
        tier, label, signals = verification_tier(sample_user)
        assert tier == 2
        assert label == 'Verified'
        assert signals['linked_verified']

    def test_tier_fully_verified(self, app, sample_user):
        from models.connect_account import ConnectAccount
        from models.linked_account import LinkedAccount
        from utils.verification_tiers import verification_tier
        with app.app_context():
            db.session.add(LinkedAccount(
                user_id=sample_user.id, platform='linkedin',
                status='verified', verification_method='oauth'))
            db.session.add(ConnectAccount(
                user_id=sample_user.id, stripe_account_id='acct_test',
                details_submitted=True, payouts_enabled=True))
            db.session.commit()
        tier, label, signals = verification_tier(sample_user)
        assert tier == 3
        assert label == 'Fully verified'
        assert signals['kyc']

    def test_badge_links_to_explainer(self, client, logged_in_user,
                                      sample_user):
        html = client.get('/profile/').data.decode()
        assert 'verification-badge' in html
        assert '/social/verification' in html
        page = client.get('/social/verification').data.decode()
        assert 'Tier 1' in page
        assert 'Tier 2' in page
        assert 'Tier 3' in page
        assert 'Your next step' in page


class TestReputationMetrics:
    def test_nothing_shown_without_data(self, client, logged_in_user,
                                        sample_user):
        html = client.get('/profile/').data.decode()
        assert 'Fill rate' not in html
        assert 'Responds' not in html

    def test_completed_referrals_shown(self, client, logged_in_user, app,
                                       sample_user, sample_bounty,
                                       sample_hunter, sample_pitch):
        from models.match import Match
        with app.app_context():
            db.session.add(Match(
                bounty_id=sample_bounty.id, pitch_id=sample_pitch.id,
                hunter_id=sample_hunter.id, employer_id=sample_user.id,
                hunter_confirmed=True, employer_confirmed=True))
            db.session.commit()
        html = client.get(
            f'/profile/{sample_hunter.handle}').data.decode()
        assert '1 completed referral' in html


class TestPledgeBadge:
    def test_board_card_shows_pledge(self, client, app, sample_user,
                                     sample_bounty):
        with app.app_context():
            db.session.add(Pledge(
                user_id=sample_user.id, company_name='Test Co'))
            db.session.commit()
        html = client.get('/needs/').data.decode()
        assert 'Pledged' in html

    def test_no_badge_without_pledge(self, client, sample_bounty):
        html = client.get('/needs/').data.decode()
        assert 'Pledged' not in html

    def test_has_active_pledge(self, app, sample_user):
        from utils.pledges import has_active_pledge
        assert not has_active_pledge(sample_user.id)
        with app.app_context():
            db.session.add(Pledge(
                user_id=sample_user.id, company_name='Test Co'))
            db.session.commit()
        assert has_active_pledge(sample_user.id)


# ---------------------------------------------------------------- hiring layer


class TestReferralThreads:
    def _referral(self, app, sample_user, sample_hunter, sample_bounty,
                  sample_pitch):
        with app.app_context():
            referral = Referral(
                bounty_id=sample_bounty.id,
                referrer_id=sample_hunter.id,
                referred_name='Candidate Person',
                referred_email='candidate@example.com',
                message='Great fit for this role.',
                status='consented',
                pitch_id=sample_pitch.id,
            )
            db.session.add(referral)
            db.session.commit()
            db.session.refresh(referral)
            return referral

    def test_thread_shows_messages(self, client, logged_in_user, app,
                                   sample_user, sample_hunter,
                                   sample_bounty, sample_pitch):
        referral = self._referral(app, sample_user, sample_hunter,
                                  sample_bounty, sample_pitch)
        with app.app_context():
            db.session.add(Message(
                sender_id=sample_hunter.id, recipient_id=sample_user.id,
                bounty_id=sample_bounty.id, pitch_id=sample_pitch.id,
                content='When can we talk about the candidate?'))
            db.session.add(Message(
                sender_id=sample_hunter.id, recipient_id=sample_user.id,
                content='Unrelated DM, not about this referral.'))
            db.session.commit()
        html = client.get(f'/referrals/{referral.id}').data.decode()
        assert 'Conversation about this referral' in html
        assert 'When can we talk about the candidate?' in html
        assert 'Unrelated DM, not about this referral.' not in html

    def test_posting_in_thread(self, client, logged_in_user, app,
                               sample_user, sample_hunter, sample_bounty,
                               sample_pitch):
        referral = self._referral(app, sample_user, sample_hunter,
                                  sample_bounty, sample_pitch)
        r = client.post(f'/referrals/{referral.id}/message', data={
            'content': 'Let us schedule a call Tuesday.',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'Let us schedule a call Tuesday.' in r.data.decode()
        with app.app_context():
            msg = Message.query.filter_by(
                pitch_id=sample_pitch.id,
                content='Let us schedule a call Tuesday.').first()
            assert msg is not None
            assert msg.recipient_id == sample_hunter.id
            assert msg.bounty_id == sample_bounty.id

    def test_referred_person_cannot_post(self, client, app, sample_user,
                                         sample_hunter, sample_bounty,
                                         sample_pitch, second_user,
                                         login_as):
        referral = self._referral(app, sample_user, sample_hunter,
                                  sample_bounty, sample_pitch)
        with app.app_context():
            referral.referred_user_id = second_user.id
            db.session.commit()
        login_as(client, 'second@example.com', 'SecondPass123!')
        r = client.post(f'/referrals/{referral.id}/message', data={
            'content': 'I should not be able to post here.',
        }, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            assert Message.query.filter_by(
                content='I should not be able to post here.').first() is None

    def test_bounty_detail_links_to_thread(self, client, logged_in_user,
                                           app, sample_user, sample_hunter,
                                           sample_bounty, sample_pitch):
        referral = self._referral(app, sample_user, sample_hunter,
                                  sample_bounty, sample_pitch)
        from models.pitch import Pitch
        with app.app_context():
            pitch = db.session.get(Pitch, sample_pitch.id)
            pitch.status = 'interested'
            db.session.commit()
        html = client.get(f'/needs/{sample_bounty.url_slug}').data.decode()
        assert f'/referrals/{referral.id}#thread' in html


class TestHiringStrip:
    def test_strip_on_profile_with_open_needs(self, client, logged_in_user,
                                              sample_bounty):
        html = client.get('/profile/').data.decode()
        assert 'hiring-strip' in html
        assert 'Senior Python Engineer Needed for Startup' in html
        assert 'View all' in html

    def test_no_strip_without_open_needs(self, client, logged_in_user,
                                         sample_user):
        html = client.get('/profile/').data.decode()
        assert 'hiring-strip' not in html

    def test_board_poster_filter(self, client, app, sample_user,
                                 second_user, sample_bounty):
        with app.app_context():
            other = Bounty(
                poster_id=second_user.id,
                title='A completely different need from Second',
                description='Another open need by another poster.',
                bounty_type='services',
                bounty_direction='seeking_opportunity',
                reward_amount=0, status='open',
                payment_status='released')
            db.session.add(other)
            db.session.commit()
        html = client.get(
            f'/needs/?poster={sample_user.id}').data.decode()
        assert 'Senior Python Engineer Needed for Startup' in html
        assert 'A completely different need from Second' not in html


# ---------------------------------------------------------------- bounty floor + staged rewards


class TestBountyFloor:
    def test_floor_constant(self):
        assert MIN_PAID_BOUNTY_CAD == 50

    def test_paid_below_floor_rejected(self, client, logged_in_user):
        r = client.post('/needs/post', data={
            'title': 'A paid need with too small a bounty',
            'description': 'This description is long enough to pass the fifty character minimum.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'seeking_opportunity',
            'reward_amount': '25',
        })
        assert r.status_code == 200  # Re-rendered with the error.
        assert b'at least $50' in r.data

    def test_free_zero_allowed(self, client, logged_in_user):
        r = client.post('/needs/post', data={
            'title': 'A free community need for testing',
            'description': 'This description is long enough to pass the fifty character minimum.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'seeking_opportunity',
            'reward_amount': '0',
            'is_free': '1',
        }, follow_redirects=False)
        assert r.status_code == 302

    def test_model_validation(self, app, sample_user):
        with app.app_context():
            with pytest.raises(ValueError):
                Bounty(
                    poster_id=sample_user.id,
                    title='Too small',
                    description='x',
                    bounty_type='recruitment',
                    bounty_direction='seeking_opportunity',
                    reward_amount=10,
                )


class TestStagedRewards:
    def test_declared_stages_shown_on_board_and_detail(
            self, client, app, sample_user, sample_bounty):
        with app.app_context():
            bounty = db.session.get(Bounty, sample_bounty.id)
            bounty.stage_submit_reward = 5
            bounty.stage_interview_reward = 50
            bounty.stage_hire_reward = 500
            db.session.commit()
        board = client.get('/needs/').data.decode()
        assert 'Staged rewards' in board
        assert 'Referral submitted CA$5.00' in board
        detail = client.get(
            f'/needs/{sample_bounty.url_slug}').data.decode()
        assert 'Staged rewards' in detail
        assert '$500' in detail
        assert 'not wired yet' in detail

    def test_stage_event_recorded_pending_no_payout(
            self, client, logged_in_hunter, app, sample_bounty,
            sample_hunter):
        from models.staged_reward import StageRewardEvent
        with app.app_context():
            bounty = db.session.get(Bounty, sample_bounty.id)
            bounty.stage_submit_reward = 5
            bounty.reward_amount = 500
            bounty.status = 'open'
            bounty.payment_status = 'secured'
            db.session.commit()
        client.post(f'/needs/{sample_bounty.id}/pitch', data={
            'candidate_teaser': (
                'I have 5 years of Python experience and have worked '
                'with Django and Flask extensively. Available now.'),
        }, follow_redirects=True)
        with app.app_context():
            event = StageRewardEvent.query.filter_by(
                bounty_id=sample_bounty.id, stage='submit').first()
            assert event is not None
            assert event.amount == 5
            # The only status that exists — no payout path.
            assert event.status == 'pending_stripe'
            assert StageRewardEvent.STATUSES == ['pending_stripe']


# ---------------------------------------------------------------- motion + skeletons + board


class TestMotionAndSkeletons:
    def test_reduced_motion_css_present(self):
        css = open(
            os.path.join(os.path.dirname(__file__), '..',
                         'static', 'css', 'main.css')).read()
        assert '@media (prefers-reduced-motion: reduce)' in css
        assert 'animation-duration: 0.01ms' in css
        # The motion pass durations.
        assert 'like-pop 180ms' in css
        assert 'save-settle 180ms' in css
        assert 'opacity 150ms' in css  # follow cross-fade
        assert 'opacity 160ms' in css  # dropdowns
        assert 'transform 220ms' in css  # modal in
        assert 'toast-in 250ms' in css
        assert 'composer-in 200ms' in css
        assert 'skeleton-shimmer 1.4s' in css
        assert 'tab-indicator' in css

    def test_skeleton_partial_and_js(self):
        partial = open(
            os.path.join(os.path.dirname(__file__), '..',
                         'templates', 'partials', 'skeleton.html')).read()
        assert 'skeleton_post' in partial
        assert 'skeleton_profile' in partial
        assert 'skeleton_notification' in partial
        js = open(
            os.path.join(os.path.dirname(__file__), '..',
                         'static', 'js', 'main.js')).read()
        assert 'skeletonPostCard' in js
        assert 'skeletonCommentRow' in js
        assert 'initTabIndicator' in js


class TestBoardCleanup:
    def test_chip_filters(self, client, app, sample_user, second_user,
                          sample_bounty):
        with app.app_context():
            paid = Bounty(
                poster_id=second_user.id,
                title='Paid need with a real bounty here',
                description='A paid recruitment need.',
                bounty_type='recruitment',
                bounty_direction='seeking_opportunity',
                reward_amount=1000, status='open',
                payment_status='secured')
            db.session.add(paid)
            db.session.commit()
        html = client.get('/needs/').data.decode()
        assert 'Free' in html  # chip row present
        free = client.get('/needs/?free=1').data.decode()
        assert 'Senior Python Engineer Needed for Startup' in free
        assert 'Paid need with a real bounty here' not in free
        services = client.get(
            '/needs/?bounty_type=services').data.decode()
        assert 'Senior Python Engineer Needed for Startup' not in services

    def test_reward_brand_colour(self, client, sample_bounty):
        html = client.get('/needs/').data.decode()
        assert 'bounty-reward' in html
        # Rewards are brand terracotta, never Bootstrap green.
        assert 'fw-bold text-success' not in html

    def test_sticky_cta_on_detail(self, client, logged_in_user, app,
                                  sample_user, second_user, sample_bounty,
                                  login_as):
        login_as(client, 'second@example.com', 'SecondPass123!')
        html = client.get(
            f'/needs/{sample_bounty.url_slug}').data.decode()
        assert 'bounty-sticky-cta' in html
        assert 'bounty-detail-sidebar' in html
        assert 'Refer someone' in html
