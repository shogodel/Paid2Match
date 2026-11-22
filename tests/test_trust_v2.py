"""M2 trust v2 tests — double-blind ratings, verification queue, SLA,
and the extended trust score (ratings / KYC / SLA / dispute inputs)."""
from datetime import datetime, timedelta, timezone

import pytest

from extensions import db
from models import ConnectAccount, Match, utc_now
from models.bounty import Bounty
from models.dispute import Dispute
from models.moderation import Report
from models.notification import Notification
from models.pitch import Pitch
from models.profile import Profile
from models.review import Review, maybe_reveal_reviews, revealed_reviews_for_user
from models.verification import VerificationRequest
from utils.sla import (
    add_business_days,
    has_open_breach,
    pitch_breached,
    pitch_due_at,
    sla_breach_count_90d,
    sla_breaches,
)
from utils.trust import (
    compute_trust_score,
    dispute_loss_penalty,
    ratings_summary,
    trust_level,
    upheld_reports_count,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_bounty(app, poster):
    bounty = Bounty(
        poster_id=poster.id, title='Free need', description='x' * 60,
        bounty_type='recruitment', bounty_direction='seeking_opportunity',
        reward_amount=None, status='open', payment_status='released',
        location='Remote',
    )
    db.session.add(bounty)
    db.session.commit()
    db.session.refresh(bounty)
    return bounty


def _confirmed_match(app, bounty, hunter, poster):
    pitch = Pitch(bounty_id=bounty.id, hunter_id=hunter.id,
                  candidate_teaser='x' * 60, status='accepted')
    db.session.add(pitch)
    db.session.flush()
    match = Match(
        bounty_id=bounty.id, pitch_id=pitch.id,
        hunter_id=hunter.id, employer_id=poster.id,
        hunter_confirmed=True, employer_confirmed=True,
        payout_status='completed',
    )
    db.session.add(match)
    db.session.commit()
    db.session.refresh(match)
    return match


def _review(match, reviewer, reviewee, role, primary, secondary, comment=None):
    review = Review(
        match_id=match.id, reviewer_id=reviewer.id, reviewee_id=reviewee.id,
        reviewee_role=role, rating_primary=primary,
        rating_secondary=secondary, comment=comment,
    )
    db.session.add(review)
    db.session.commit()
    db.session.refresh(review)
    return review


def _rate_both(match, hunter, poster, hunter_rates=(5, 5), poster_rates=(5, 5)):
    """Both sides rate; returns (hunter's review, poster's review)."""
    r1 = _review(match, hunter, poster, 'poster', *hunter_rates)
    r2 = _review(match, poster, hunter, 'referrer', *poster_rates)
    maybe_reveal_reviews(match.id)
    db.session.commit()
    return r1, r2


# ---------------------------------------------------------------------------
# Double-blind ratings
# ---------------------------------------------------------------------------

class TestDoubleBlindRatings:
    def test_single_review_not_revealed(self, app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        review = _review(match, sample_hunter, sample_user, 'poster', 5, 4)
        assert review.is_revealed() is False
        assert revealed_reviews_for_user(sample_user.id) == []
        assert ratings_summary(sample_user.id) == (None, 0, None)

    def test_both_reviews_reveal_each_other(self, app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        r1 = _review(match, sample_hunter, sample_user, 'poster', 5, 4)
        assert r1.is_revealed() is False
        r2 = _review(match, sample_user, sample_hunter, 'referrer', 5, 5)
        assert maybe_reveal_reviews(match.id) is True
        db.session.commit()
        assert db.session.get(Review, r1.id).revealed_at is not None
        assert db.session.get(Review, r2.id).revealed_at is not None
        assert db.session.get(Review, r1.id).is_revealed() is True
        assert len(revealed_reviews_for_user(sample_user.id)) == 1
        assert len(revealed_reviews_for_user(sample_hunter.id)) == 1

    def test_review_revealed_after_14_days(self, app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        review = _review(match, sample_hunter, sample_user, 'poster', 3, 3)
        review.created_at = utc_now() - timedelta(days=15)
        db.session.commit()
        assert db.session.get(Review, review.id).is_revealed() is True
        assert len(revealed_reviews_for_user(sample_user.id)) == 1

    def test_review_not_revealed_before_14_days(self, app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        review = _review(match, sample_hunter, sample_user, 'poster', 3, 3)
        review.created_at = utc_now() - timedelta(days=13)
        db.session.commit()
        assert db.session.get(Review, review.id).is_revealed() is False

    def test_ratings_summary_averages_and_positive_rate(self, app, sample_user,
                                                        sample_hunter, second_user):
        # Two 5-star reviews + one 2-star review for the hunter.
        for poster, rating in [(sample_user, 5), (second_user, 5)]:
            bounty = _free_bounty(app, poster)
            match = _confirmed_match(app, bounty, sample_hunter, poster)
            _rate_both(match, sample_hunter, poster, poster_rates=(rating, rating))
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _rate_both(match, sample_hunter, sample_user, poster_rates=(2, 2))
        avg, count, positive = ratings_summary(sample_hunter.id)
        assert count == 3
        assert avg == 4.0  # (5 + 5 + 2) / 3
        assert positive == 0.67  # 2 of 3 positive, rounded to 2 decimals

    def test_rate_route_requires_dual_confirmation(self, client, logged_in_hunter,
                                                   app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        pitch = Pitch(bounty_id=bounty.id, hunter_id=sample_hunter.id,
                      candidate_teaser='x' * 60, status='interested')
        db.session.add(pitch)
        db.session.flush()
        match = Match(bounty_id=bounty.id, pitch_id=pitch.id,
                      hunter_id=sample_hunter.id, employer_id=sample_user.id,
                      hunter_confirmed=True, employer_confirmed=False,
                      payout_status='pending')
        db.session.add(match)
        db.session.commit()
        resp = client.post(f'/bounties/matches/{match.id}/rate',
                           data={'rating_primary': '5', 'rating_secondary': '5'},
                           follow_redirects=True)
        assert b'both sides have confirmed' in resp.data
        assert Review.query.filter_by(match_id=match.id).count() == 0

    def test_rate_route_once_per_side(self, client, logged_in_hunter, app,
                                      sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        data = {'rating_primary': '5', 'rating_secondary': '4'}
        client.post(f'/bounties/matches/{match.id}/rate', data=data,
                    follow_redirects=True)
        resp = client.post(f'/bounties/matches/{match.id}/rate', data=data,
                           follow_redirects=True)
        assert b'already rated' in resp.data
        assert Review.query.filter_by(match_id=match.id).count() == 1

    def test_rate_route_reveals_when_both_rate(self, client, login_as, app,
                                               sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        data = {'rating_primary': '5', 'rating_secondary': '5',
                'comment': 'Great to work with!'}
        login_as(client, 'hunter@example.com', 'HunterPass123!')
        client.post(f'/bounties/matches/{match.id}/rate', data=data,
                    follow_redirects=True)
        # Only one side rated — the review is still hidden.
        resp = client.get(f'/bounties/matches/{match.id}/confirmed')
        assert b'Great to work with!' not in resp.data
        login_as(client, 'testuser@example.com', 'TestPass123!')
        resp = client.post(f'/bounties/matches/{match.id}/rate', data=data,
                           follow_redirects=True)
        assert b'now visible' in resp.data
        resp = client.get(f'/bounties/matches/{match.id}/confirmed')
        assert b'Great to work with!' in resp.data
        notes = Notification.query.filter_by(type='review_visible').all()
        assert len(notes) == 2  # both sides notified

    def test_cannot_rate_others_match(self, client, login_as, app, sample_user,
                                      sample_hunter, second_user):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        login_as(client, 'second@example.com', 'SecondPass123!')
        resp = client.post(f'/bounties/matches/{match.id}/rate',
                           data={'rating_primary': '5', 'rating_secondary': '5'},
                           follow_redirects=True)
        assert b'Access denied' in resp.data
        assert Review.query.filter_by(match_id=match.id).count() == 0


# ---------------------------------------------------------------------------
# Verification workflow
# ---------------------------------------------------------------------------

class TestVerification:
    def test_verify_request_creates_pending(self, client, logged_in_user, app,
                                            sample_user):
        resp = client.post('/profile/verify-request',
                           data={'note': 'My LinkedIn is linkedin.com/in/test'},
                           follow_redirects=True)
        assert b'Verification requested' in resp.data
        req = VerificationRequest.query.filter_by(user_id=sample_user.id).first()
        assert req is not None
        assert req.status == 'pending'
        assert 'LinkedIn' in req.note

    def test_verify_request_duplicate_blocked(self, client, logged_in_user, app,
                                              sample_user):
        client.post('/profile/verify-request', data={}, follow_redirects=True)
        resp = client.post('/profile/verify-request', data={}, follow_redirects=True)
        assert b'already have a pending' in resp.data
        assert VerificationRequest.query.filter_by(user_id=sample_user.id).count() == 1

    def test_verify_request_when_already_verified(self, client, logged_in_user,
                                                  app, sample_user):
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        profile.verified = True
        db.session.commit()
        resp = client.post('/profile/verify-request', data={}, follow_redirects=True)
        assert b'already verified' in resp.data
        assert VerificationRequest.query.filter_by(user_id=sample_user.id).count() == 0

    def test_admin_approve_sets_verified(self, client, logged_in_admin, app,
                                         sample_user):
        req = VerificationRequest(user_id=sample_user.id, note='Check me')
        db.session.add(req)
        db.session.commit()
        resp = client.post(f'/admin/verifications/{req.id}/approve',
                           follow_redirects=True)
        assert b'Verification approved' in resp.data
        assert db.session.get(VerificationRequest, req.id).status == 'approved'
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        assert profile.verified is True
        notes = Notification.query.filter_by(
            recipient_id=sample_user.id, type='verification_approved'
        ).all()
        assert len(notes) == 1

    def test_admin_reject_notifies_with_note(self, client, logged_in_admin, app,
                                             sample_user):
        req = VerificationRequest(user_id=sample_user.id)
        db.session.add(req)
        db.session.commit()
        resp = client.post(f'/admin/verifications/{req.id}/reject',
                           data={'admin_note': 'Could not confirm identity'},
                           follow_redirects=True)
        assert b'Verification rejected' in resp.data
        assert db.session.get(VerificationRequest, req.id).status == 'rejected'
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        assert profile.verified is False
        notes = Notification.query.filter_by(
            recipient_id=sample_user.id, type='verification_rejected'
        ).all()
        assert len(notes) == 1
        assert 'Could not confirm identity' in notes[0].body

    def test_admin_verifications_page_renders(self, client, logged_in_admin):
        resp = client.get('/admin/verifications')
        assert resp.status_code == 200
        assert b'Verification requests' in resp.data

    def test_verification_request_requires_login(self, client):
        resp = client.post('/profile/verify-request', data={},
                           follow_redirects=False)
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# No-ghosting SLA
# ---------------------------------------------------------------------------

class TestSLA:
    def test_add_business_days_skips_weekend(self, app):
        # 2026-11-20 is a Friday; +5 business days = Friday 2026-11-27.
        friday = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        assert add_business_days(friday, 5) == datetime(
            2026, 11, 27, 12, 0, tzinfo=timezone.utc
        )

    def test_pitch_due_at(self, app, sample_pitch):
        # created_at comes back naive from SQLite; pitch_due_at returns
        # an aware datetime.
        created = sample_pitch.created_at.replace(tzinfo=timezone.utc)
        due = pitch_due_at(sample_pitch)
        assert due == add_business_days(created, 5)
        assert due > created
        assert due.weekday() < 5

    def _old_pending_pitch(self, app, bounty, hunter, days_ago):
        pitch = Pitch(bounty_id=bounty.id, hunter_id=hunter.id,
                      candidate_teaser='x' * 60, status='pending')
        pitch.created_at = utc_now() - timedelta(days=days_ago)
        db.session.add(pitch)
        db.session.commit()
        db.session.refresh(pitch)
        return pitch

    def test_open_breach_detected(self, app, sample_user, sample_hunter,
                                  sample_bounty):
        self._old_pending_pitch(app, sample_bounty, sample_hunter, days_ago=10)
        assert has_open_breach(sample_user.id) is True
        breaches = sla_breaches(sample_user.id)
        assert len(breaches) == 1
        assert breaches[0]['open'] is True

    def test_no_breach_within_window(self, app, sample_user, sample_hunter,
                                     sample_bounty):
        self._old_pending_pitch(app, sample_bounty, sample_hunter, days_ago=2)
        assert has_open_breach(sample_user.id) is False
        assert sla_breaches(sample_user.id) == []

    def test_late_response_is_breach_but_not_open(self, app, sample_user,
                                                  sample_hunter, sample_bounty):
        pitch = self._old_pending_pitch(app, sample_bounty, sample_hunter,
                                        days_ago=10)
        pitch.status = 'interested'
        pitch.responded_at = utc_now()
        db.session.commit()
        assert pitch_breached(pitch) is True
        assert has_open_breach(sample_user.id) is False
        breaches = sla_breaches(sample_user.id)
        assert len(breaches) == 1
        assert breaches[0]['open'] is False

    def test_respond_to_pitch_sets_responded_at(self, client, logged_in_user,
                                                app, sample_pitch):
        resp = client.post(f'/bounties/pitches/{sample_pitch.id}/respond',
                           data={'response': 'interested'},
                           follow_redirects=True)
        assert resp.status_code == 200
        pitch = db.session.get(Pitch, sample_pitch.id)
        assert pitch.responded_at is not None
        assert pitch.status == 'interested'

    def test_sla_badge_suspension(self, app, sample_user, sample_hunter,
                                  sample_bounty, second_user):
        # Give the poster Member-level standing: 2 completed referrals
        # as a hunter (score 31: identity 5 + transactions 16 + SLA 10).
        for _ in range(2):
            bounty = _free_bounty(app, second_user)
            _confirmed_match(app, bounty, sample_user, second_user)
        level, _score, breakdown = trust_level(sample_user)
        assert level == 'Member'
        assert breakdown['badge_suspended'] is False
        # Now an open breach appears on one of their own needs.
        self._old_pending_pitch(app, sample_bounty, sample_hunter, days_ago=10)
        level, _score, breakdown = trust_level(sample_user)
        assert level == 'Newcomer'
        assert breakdown['badge_suspended'] is True

    def test_sla_penalty_in_score(self, app, sample_user, sample_hunter,
                                  sample_bounty):
        self._old_pending_pitch(app, sample_bounty, sample_hunter, days_ago=10)
        _score, breakdown = compute_trust_score(sample_user)
        assert breakdown['sla_breaches'] == 1
        assert breakdown['penalties'] >= 5
        # Reliability lost its +10 SLA-compliance half too.
        assert breakdown['reliability'] == 0

    def test_old_breach_does_not_count_90d(self, app, sample_user, sample_hunter,
                                           sample_bounty):
        pitch = self._old_pending_pitch(app, sample_bounty, sample_hunter,
                                        days_ago=200)
        pitch.status = 'rejected'
        pitch.responded_at = utc_now() - timedelta(days=200)
        db.session.commit()
        assert sla_breach_count_90d(sample_user.id) == 0


# ---------------------------------------------------------------------------
# Trust score v2
# ---------------------------------------------------------------------------

class TestTrustScoreV2:
    def test_verified_profile_adds_5_identity(self, app, sample_user):
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        profile.verified = True
        db.session.commit()
        _score, breakdown = compute_trust_score(sample_user)
        assert breakdown['identity'] == 10  # email 5 + verified profile 5

    def test_stripe_kyc_adds_5_identity(self, app, sample_user):
        account = ConnectAccount(
            user_id=sample_user.id, stripe_account_id='acct_kyc',
            onboarding_status='complete', charges_enabled=True,
            payouts_enabled=True, details_submitted=True,
        )
        db.session.add(account)
        db.session.commit()
        _score, breakdown = compute_trust_score(sample_user)
        assert breakdown['identity'] == 10  # email 5 + KYC 5

    def test_ratings_drive_transactions(self, app, sample_user, sample_hunter,
                                        second_user):
        # 2 completed referrals + revealed 5-star reviews → 40.
        for poster in (sample_user, second_user):
            bounty = _free_bounty(app, poster)
            match = _confirmed_match(app, bounty, sample_hunter, poster)
            _rate_both(match, sample_hunter, poster, poster_rates=(5, 5))
        _score, breakdown = compute_trust_score(sample_hunter)
        assert breakdown['transactions'] == 40
        assert breakdown['ratings_avg'] == 5.0
        assert breakdown['ratings_count'] == 2

    def test_low_ratings_drive_transactions_to_zero(self, app, sample_user,
                                                    sample_hunter, second_user):
        for poster in (sample_user, second_user):
            bounty = _free_bounty(app, poster)
            match = _confirmed_match(app, bounty, sample_hunter, poster)
            _rate_both(match, sample_hunter, poster, poster_rates=(1, 1))
        _score, breakdown = compute_trust_score(sample_hunter)
        assert breakdown['transactions'] == 0

    def test_ratings_need_two_completed(self, app, sample_user, sample_hunter):
        # One completed referral + a 5-star review → still 0 (Upwork rule).
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _rate_both(match, sample_hunter, sample_user, poster_rates=(5, 5))
        _score, breakdown = compute_trust_score(sample_hunter)
        assert breakdown['transactions'] == 0

    def test_proxy_kept_without_reviews(self, app, sample_user, sample_hunter,
                                        second_user, third_user):
        # 3 completed, no reviews → the M1 proxy (3 x 8 = 24).
        for poster in (sample_user, second_user, third_user):
            bounty = _free_bounty(app, poster)
            _confirmed_match(app, bounty, sample_hunter, poster)
        _score, breakdown = compute_trust_score(sample_hunter)
        assert breakdown['transactions'] == 24

    def test_trusted_level_reachable(self, app, sample_user, sample_hunter,
                                     second_user, third_user):
        # Score 60 (identity 10 + transactions 40 + reliability 10),
        # verified profile, 5 completed, all 5-star reviews.
        profile = Profile.query.filter_by(user_id=sample_hunter.id).first()
        profile.verified = True
        db.session.commit()
        posters = [sample_user, second_user, third_user, sample_user, second_user]
        for poster in posters:
            bounty = _free_bounty(app, poster)
            match = _confirmed_match(app, bounty, sample_hunter, poster)
            _rate_both(match, sample_hunter, poster, poster_rates=(5, 5))
        level, score, breakdown = trust_level(sample_hunter)
        assert score == 60
        assert breakdown['positive_rate'] == 1.0
        assert level == 'Trusted'

    def test_trusted_blocked_by_low_positive_rate(self, app, sample_user,
                                                  sample_hunter, second_user,
                                                  third_user):
        # Score 62 (identity 15 with KYC + transactions 37 + SLA 10),
        # but only 4 of 5 reviews positive (80% < 85%) — the gate fails.
        profile = Profile.query.filter_by(user_id=sample_hunter.id).first()
        profile.verified = True
        db.session.add(ConnectAccount(
            user_id=sample_hunter.id, stripe_account_id='acct_gate',
            onboarding_status='complete', charges_enabled=True,
            payouts_enabled=True, details_submitted=True,
        ))
        db.session.commit()
        posters = [sample_user, second_user, third_user, sample_user, second_user]
        ratings = [(5, 5), (5, 5), (5, 5), (5, 5), (3, 4)]
        for poster, rating in zip(posters, ratings):
            bounty = _free_bounty(app, poster)
            match = _confirmed_match(app, bounty, sample_hunter, poster)
            _rate_both(match, sample_hunter, poster, poster_rates=rating)
        level, score, breakdown = trust_level(sample_hunter)
        assert score == 62
        assert breakdown['positive_rate'] == 0.8
        assert level == 'Member'  # score is high, but the gate fails

    def test_ambassador_still_unreachable(self, app, sample_user, sample_hunter,
                                          second_user, third_user):
        # Even a perfect M2 user cannot be an Ambassador — 2 verified
        # linked socials is M3.
        profile = Profile.query.filter_by(user_id=sample_hunter.id).first()
        profile.verified = True
        db.session.commit()
        account = ConnectAccount(
            user_id=sample_hunter.id, stripe_account_id='acct_kyc2',
            onboarding_status='complete', charges_enabled=True,
            payouts_enabled=True, details_submitted=True,
        )
        db.session.add(account)
        db.session.commit()
        for i in range(15):
            poster = [sample_user, second_user, third_user][i % 3]
            bounty = _free_bounty(app, poster)
            match = _confirmed_match(app, bounty, sample_hunter, poster)
            _rate_both(match, sample_hunter, poster, poster_rates=(5, 5))
        level, _score, _breakdown = trust_level(sample_hunter)
        assert level == 'Trusted'

    def test_dispute_loss_penalty_upheld(self, app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        dispute = Dispute(match_id=match.id, raised_by_id=sample_hunter.id,
                          reason='Poster did not pay as agreed',
                          status='resolved', outcome='upheld',
                          resolved_at=utc_now())
        db.session.add(dispute)
        db.session.commit()
        # Upheld → the respondent (poster) lost.
        assert dispute_loss_penalty(sample_user.id) == 15
        assert dispute_loss_penalty(sample_hunter.id) == 0

    def test_dispute_loss_penalty_dismissed(self, app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        dispute = Dispute(match_id=match.id, raised_by_id=sample_hunter.id,
                          reason='Frivolous complaint',
                          status='resolved', outcome='dismissed',
                          resolved_at=utc_now())
        db.session.add(dispute)
        db.session.commit()
        # Dismissed → the raiser (hunter) lost.
        assert dispute_loss_penalty(sample_hunter.id) == 15
        assert dispute_loss_penalty(sample_user.id) == 0

    def test_dispute_penalty_decays_with_age(self, app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        dispute = Dispute(match_id=match.id, raised_by_id=sample_hunter.id,
                          reason='Old dispute', status='resolved',
                          outcome='upheld',
                          resolved_at=utc_now() - timedelta(days=200))
        db.session.add(dispute)
        db.session.commit()
        assert dispute_loss_penalty(sample_user.id) == 7  # 6–12 months
        dispute.resolved_at = utc_now() - timedelta(days=400)
        db.session.commit()
        assert dispute_loss_penalty(sample_user.id) == 0  # gone after 12 months

    def test_upheld_reports_expire_after_12_months(self, app, sample_user,
                                                   second_user):
        old = Report(reporter_id=second_user.id, target_type='profile',
                     target_id=sample_user.id, reason='spam', status='actioned')
        old.created_at = utc_now() - timedelta(days=400)
        db.session.add(old)
        db.session.commit()
        assert upheld_reports_count(sample_user.id) == 0
        fresh = Report(reporter_id=second_user.id, target_type='profile',
                       target_id=sample_user.id, reason='spam', status='actioned')
        db.session.add(fresh)
        db.session.commit()
        assert upheld_reports_count(sample_user.id) == 1
