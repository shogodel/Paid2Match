"""M1 tests: free referrals — attribution, consent flow, karma, completion."""
import pytest

from extensions import db
from models.match import Match
from models.pitch import Pitch
from models.post import Post, Share
from models.profile import Profile
from models.referral import KarmaEvent, Referral
from utils.feed import create_need_post
from utils.karma import award_karma
from utils.referrals import complete_referral_for_match

MESSAGE = (
    'I have worked with this person for years and they would be '
    'perfect for this role — reliable, skilled, and motivated.'
)


def _make_referral(client, bounty_id, name='Third User', email='third@example.com',
                   message=MESSAGE):
    return client.post(f'/referrals/new/{bounty_id}', data={
        'connection_id': '',
        'referred_name': name,
        'referred_email': email,
        'message': message,
    }, follow_redirects=True)


class TestReferralCreate:
    def test_create_referral(self, client, login_as, second_user, third_user, sample_bounty):
        login_as(client, 'second@example.com', 'SecondPass123!')
        resp = _make_referral(client, sample_bounty.id)
        assert resp.status_code == 200
        referral = Referral.query.filter_by(
            bounty_id=sample_bounty.id, referred_email='third@example.com').first()
        assert referral is not None
        assert referral.status == 'invited'
        assert referral.referrer_id == second_user.id
        assert referral.referred_user_id is not None  # matched existing user
        assert referral.invite_token

    def test_first_timestamp_wins(self, client, login_as, second_user, sample_hunter, sample_bounty):
        login_as(client, 'second@example.com', 'SecondPass123!')
        _make_referral(client, sample_bounty.id)
        # Another referrer tries the same person for the same need.
        login_as(client, 'hunter@example.com', 'HunterPass123!')
        resp = _make_referral(client, sample_bounty.id, name='Third User',
                              email='third@example.com')
        assert b'already referred' in resp.data
        assert Referral.query.filter_by(
            bounty_id=sample_bounty.id, referred_email='third@example.com').count() == 1

    def test_self_referral_rejected(self, client, login_as, second_user, sample_bounty):
        login_as(client, 'second@example.com', 'SecondPass123!')
        resp = _make_referral(client, sample_bounty.id, name='Second User',
                              email='second@example.com')
        assert b'cannot refer yourself' in resp.data
        assert Referral.query.filter_by(bounty_id=sample_bounty.id).count() == 0

    def test_cannot_refer_for_own_need(self, client, logged_in_user, sample_bounty):
        resp = _make_referral(client, sample_bounty.id)
        assert b'own need' in resp.data
        assert Referral.query.filter_by(bounty_id=sample_bounty.id).count() == 0

    def test_referral_message_too_short_rejected(self, client, login_as, second_user, sample_bounty):
        login_as(client, 'second@example.com', 'SecondPass123!')
        _make_referral(client, sample_bounty.id, message='Too short.')
        assert Referral.query.filter_by(bounty_id=sample_bounty.id).count() == 0


class TestConsentFlow:
    def _invited_referral(self, client, login_as, second_user, sample_bounty):
        login_as(client, 'second@example.com', 'SecondPass123!')
        _make_referral(client, sample_bounty.id)
        referral = Referral.query.filter_by(bounty_id=sample_bounty.id).first()
        return referral.id, referral.invite_token

    def test_consent_page_renders_logged_out(self, client, login_as, second_user, sample_bounty):
        _, token = self._invited_referral(client, login_as, second_user, sample_bounty)
        client.get('/auth/logout')
        resp = client.get(f'/referrals/invite/{token}')
        assert resp.status_code == 200
        assert b'Someone referred you' in resp.data

    def test_confirm_creates_pitch_and_notifies_poster(self, client, login_as, second_user,
                                                       third_user, sample_bounty, sample_user):
        referral_id, token = self._invited_referral(client, login_as, second_user, sample_bounty)
        login_as(client, 'third@example.com', 'ThirdPass123!')
        resp = client.post(f'/referrals/{referral_id}/confirm', follow_redirects=True)
        assert resp.status_code == 200
        referral = db.session.get(Referral, referral_id)
        assert referral.status == 'consented'
        assert referral.pitch_id is not None
        pitch = db.session.get(Pitch, referral.pitch_id)
        assert pitch.hunter_id == second_user.id
        assert pitch.bounty_id == sample_bounty.id
        # Poster got a referral_new notification.
        from models.notification import Notification
        assert Notification.query.filter_by(
            recipient_id=sample_user.id, type='referral_new').first() is not None

    def test_decline_notifies_referrer(self, client, login_as, second_user, third_user, sample_bounty):
        referral_id, _ = self._invited_referral(client, login_as, second_user, sample_bounty)
        login_as(client, 'third@example.com', 'ThirdPass123!')
        client.post(f'/referrals/{referral_id}/decline', follow_redirects=True)
        assert db.session.get(Referral, referral_id).status == 'declined'
        from models.notification import Notification
        assert Notification.query.filter_by(
            recipient_id=second_user.id, type='referral_declined').first() is not None

    def test_wrong_user_cannot_confirm(self, client, login_as, second_user, sample_bounty):
        referral_id, _ = self._invited_referral(client, login_as, second_user, sample_bounty)
        # The referrer (not the referred person) tries to confirm.
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.post(f'/referrals/{referral_id}/confirm', follow_redirects=True)
        assert db.session.get(Referral, referral_id).status == 'invited'


class TestEndToEndCompletion:
    def test_dual_confirm_completes_referral_and_awards_karma(
            self, client, login_as, second_user, third_user, sample_bounty, sample_user):
        # 1. Refer.
        login_as(client, 'second@example.com', 'SecondPass123!')
        _make_referral(client, sample_bounty.id)
        referral = Referral.query.filter_by(bounty_id=sample_bounty.id).first()
        referral_id = referral.id
        # 2. Consent.
        login_as(client, 'third@example.com', 'ThirdPass123!')
        client.post(f'/referrals/{referral_id}/confirm', follow_redirects=True)
        pitch_id = db.session.get(Referral, referral_id).pitch_id
        # 3. Poster accepts.
        login_as(client, 'testuser@example.com', 'TestPass123!')
        client.post(f'/bounties/pitches/{pitch_id}/respond',
                    data={'response': 'interested'}, follow_redirects=True)
        assert db.session.get(Pitch, pitch_id).status == 'interested'
        # 4. Dual confirmation.
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.post(f'/bounties/pitches/{pitch_id}/confirm-match', follow_redirects=True)
        login_as(client, 'testuser@example.com', 'TestPass123!')
        client.post(f'/bounties/pitches/{pitch_id}/confirm-match', follow_redirects=True)

        referral = db.session.get(Referral, referral_id)
        assert referral.status == 'completed'
        assert referral.completed_at is not None
        profile = Profile.query.filter_by(user_id=second_user.id).first()
        assert profile.karma == 50
        assert profile.helped_count == 1
        assert profile.free_bumps == 1
        # Win post auto-created for the referrer.
        win = Post.query.filter_by(author_id=second_user.id, post_type='win').first()
        assert win is not None
        assert 'no money involved' in win.body  # free bounty
        # Referrer got the completion notification.
        from models.notification import Notification
        assert Notification.query.filter_by(
            recipient_id=second_user.id, type='referral_completed').first() is not None


class TestKarmaRules:
    def test_referral_karma_daily_cap(self, app, sample_user):
        amounts = [
            award_karma(sample_user.id, 50, 'referral_completed', ref_type='match', ref_id=f'm{i}')
            for i in range(4)
        ]
        db.session.commit()
        assert amounts == [50, 50, 50, 0]  # cap is +150/day
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        assert profile.karma == 150

    def test_like_karma_not_capped_by_referral_cap(self, app, sample_user):
        for i in range(4):
            award_karma(sample_user.id, 50, 'referral_completed', ref_type='match', ref_id=f'm{i}')
        awarded = award_karma(sample_user.id, 10, 'like_received', ref_type='post', ref_id='p1')
        db.session.commit()
        assert awarded == 10  # the cap applies to referral karma only

    def test_circular_referral_earns_zero_karma(self, app, sample_user, second_user, third_user, sample_bounty):
        # A (second) refers B (third) -> completed.
        pitch1 = Pitch(bounty_id=sample_bounty.id, hunter_id=second_user.id,
                       candidate_teaser='x' * 60, status='accepted')
        db.session.add(pitch1)
        db.session.flush()
        match1 = Match(bounty_id=sample_bounty.id, pitch_id=pitch1.id,
                       hunter_id=second_user.id, employer_id=sample_user.id,
                       hunter_confirmed=True, employer_confirmed=True)
        db.session.add(match1)
        db.session.flush()
        ref1 = Referral(referrer_id=second_user.id, bounty_id=sample_bounty.id,
                        pitch_id=pitch1.id, referred_name='Third User',
                        referred_email='third@example.com',
                        referred_user_id=third_user.id, message='x' * 60,
                        status='consented')
        db.session.add(ref1)
        db.session.commit()
        result1 = complete_referral_for_match(match1)
        db.session.commit()
        assert result1['karma_awarded'] == 50

        # B (third) refers A (second) -> circular, zero karma.
        pitch2 = Pitch(bounty_id=sample_bounty.id, hunter_id=third_user.id,
                       candidate_teaser='y' * 60, status='accepted')
        db.session.add(pitch2)
        db.session.flush()
        match2 = Match(bounty_id=sample_bounty.id, pitch_id=pitch2.id,
                       hunter_id=third_user.id, employer_id=sample_user.id,
                       hunter_confirmed=True, employer_confirmed=True)
        db.session.add(match2)
        db.session.flush()
        ref2 = Referral(referrer_id=third_user.id, bounty_id=sample_bounty.id,
                        pitch_id=pitch2.id, referred_name='Second User',
                        referred_email='second@example.com',
                        referred_user_id=second_user.id, message='y' * 60,
                        status='consented')
        db.session.add(ref2)
        db.session.commit()
        result2 = complete_referral_for_match(match2)
        db.session.commit()
        assert result2['circular'] is True
        assert result2['karma_awarded'] == 0
        profile = Profile.query.filter_by(user_id=third_user.id).first()
        assert profile.karma == 0
        # Helped still counts — the person was genuinely helped.
        assert profile.helped_count == 1

    def test_completion_is_idempotent(self, app, sample_user, second_user, sample_bounty):
        pitch = Pitch(bounty_id=sample_bounty.id, hunter_id=second_user.id,
                      candidate_teaser='z' * 60, status='accepted')
        db.session.add(pitch)
        db.session.flush()
        match = Match(bounty_id=sample_bounty.id, pitch_id=pitch.id,
                      hunter_id=second_user.id, employer_id=sample_user.id,
                      hunter_confirmed=True, employer_confirmed=True)
        db.session.add(match)
        db.session.flush()
        ref = Referral(referrer_id=second_user.id, bounty_id=sample_bounty.id,
                       pitch_id=pitch.id, referred_name='Someone Else',
                       referred_email='someone@example.com',
                       message='z' * 60, status='consented')
        db.session.add(ref)
        db.session.commit()
        first = complete_referral_for_match(match)
        db.session.commit()
        second = complete_referral_for_match(match)
        db.session.commit()
        assert first['already_completed'] is False
        assert second['already_completed'] is True
        assert KarmaEvent.query.filter_by(
            user_id=second_user.id, reason='referral_completed').count() == 1

    def test_share_chain_credit(self, app, sample_user, second_user, third_user, sample_bounty):
        need_post = create_need_post(sample_bounty)
        db.session.commit()
        # Third user shared the need post.
        db.session.add(Share(user_id=third_user.id, post_id=need_post.id))
        db.session.commit()
        pitch = Pitch(bounty_id=sample_bounty.id, hunter_id=second_user.id,
                      candidate_teaser='w' * 60, status='accepted')
        db.session.add(pitch)
        db.session.flush()
        match = Match(bounty_id=sample_bounty.id, pitch_id=pitch.id,
                      hunter_id=second_user.id, employer_id=sample_user.id,
                      hunter_confirmed=True, employer_confirmed=True)
        db.session.add(match)
        db.session.flush()
        ref = Referral(referrer_id=second_user.id, bounty_id=sample_bounty.id,
                       pitch_id=pitch.id, referred_name='Someone Else',
                       referred_email='someone@example.com',
                       message='w' * 60, status='consented')
        db.session.add(ref)
        db.session.commit()
        result = complete_referral_for_match(match)
        db.session.commit()
        assert third_user.id in result['sharers_credited']
        profile = Profile.query.filter_by(user_id=third_user.id).first()
        assert profile.karma == 10
        from models.notification import Notification
        assert Notification.query.filter_by(
            recipient_id=third_user.id, type='share_led_to_hire').first() is not None
