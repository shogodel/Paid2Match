"""M1 tests: trust score, levels, gates, Top Helper."""
import pytest
from datetime import datetime, timedelta, timezone

from extensions import db
from models.bounty import Bounty
from models.match import Match
from models.pitch import Pitch
from models.profile import Profile
from utils.trust import (
    completed_referrals_count, compute_trust_score, is_top_helper,
    response_rate, trust_level,
)


def _completed_match(hunter_id, employer_id, bounty_id, suffix=''):
    pitch = Pitch(bounty_id=bounty_id, hunter_id=hunter_id,
                  candidate_teaser='t' * 60, status='accepted')
    db.session.add(pitch)
    db.session.flush()
    match = Match(bounty_id=bounty_id, pitch_id=pitch.id,
                  hunter_id=hunter_id, employer_id=employer_id,
                  hunter_confirmed=True, employer_confirmed=True)
    db.session.add(match)
    db.session.commit()
    return match


class TestTrustScore:
    def test_new_verified_user_scores_five(self, app, sample_user):
        # M2: a new verified user now scores 15, not 5 — the reliability
        # component gained its SLA half (+10 with no breaches in the
        # last 90 days), on top of the +5 for a verified email.
        score, breakdown = compute_trust_score(sample_user)
        assert score == 15
        assert breakdown['identity'] == 5
        assert breakdown['reliability'] == 10
        assert breakdown['transactions'] == 0

    def test_unverified_user_scores_zero_identity(self, app, sample_user):
        from models.user import User
        user = db.session.get(User, sample_user.id)
        user.email_verified = False
        db.session.commit()
        _, breakdown = compute_trust_score(user)
        assert breakdown['identity'] == 0

    def test_transactions_need_two_completed(self, app, sample_user, sample_hunter, sample_bounty):
        _completed_match(sample_hunter.id, sample_user.id, sample_bounty.id)
        assert completed_referrals_count(sample_hunter.id) == 1
        _, breakdown = compute_trust_score(sample_hunter)
        assert breakdown['transactions'] == 0  # below the 2-outcome minimum
        _completed_match(sample_hunter.id, sample_user.id, sample_bounty.id)
        _, breakdown = compute_trust_score(sample_hunter)
        assert breakdown['transactions'] == 16  # +8 per completed referral

    def test_response_rate(self, app, sample_user, sample_hunter, sample_bounty):
        assert response_rate(sample_user.id) is None  # no referrals received
        pitch = Pitch(bounty_id=sample_bounty.id, hunter_id=sample_hunter.id,
                      candidate_teaser='r' * 60, status='pending')
        db.session.add(pitch)
        db.session.commit()
        assert response_rate(sample_user.id) == 0.0
        pitch.status = 'rejected'
        db.session.commit()
        assert response_rate(sample_user.id) == 1.0

    def test_tenure_counts_months(self, app, sample_user):
        from models.user import User
        user = db.session.get(User, sample_user.id)
        user.created_at = datetime.now(timezone.utc) - timedelta(days=95)
        db.session.commit()
        _, breakdown = compute_trust_score(user)
        assert breakdown['tenure'] == 3


class TestTrustLevels:
    def test_one_completed_referral_stays_newcomer(self, app, sample_user, sample_hunter, sample_bounty):
        # Member needs score >= 30; one completed referral can't reach it in M1.
        _completed_match(sample_hunter.id, sample_user.id, sample_bounty.id)
        level, score, _ = trust_level(sample_hunter)
        assert level == 'Newcomer'

    def test_member_gate(self, app, sample_user, sample_hunter, sample_bounty):
        # 2 completed referrals (+16) + 100% response rate (+10) + verified (+5) = 31.
        _completed_match(sample_hunter.id, sample_user.id, sample_bounty.id)
        _completed_match(sample_hunter.id, sample_user.id, sample_bounty.id)
        # Hunter also posts a need and responds to a referral on it.
        own = Bounty(poster_id=sample_hunter.id, title='Hunter own need here',
                     description='d' * 60, bounty_type='recruitment',
                     bounty_direction='seeking_opportunity', status='open')
        db.session.add(own)
        db.session.flush()
        pitch = Pitch(bounty_id=own.id, hunter_id=sample_user.id,
                      candidate_teaser='r' * 60, status='interested')
        db.session.add(pitch)
        db.session.commit()
        level, score, _ = trust_level(sample_hunter)
        assert score >= 30
        assert level == 'Member'

    def test_trusted_and_ambassador_unreachable_in_m1(self, app, sample_user, sample_hunter, sample_bounty):
        # Max out every M1 input: 5 completed referrals, full response
        # rate, 500 karma, 12 months tenure.
        for _ in range(5):
            _completed_match(sample_hunter.id, sample_user.id, sample_bounty.id)
        profile = Profile.query.filter_by(user_id=sample_hunter.id).first()
        profile.karma = 500
        sample_hunter.created_at = datetime.now(timezone.utc) - timedelta(days=365)
        db.session.commit()
        level, score, _ = trust_level(sample_hunter)
        assert score >= 60
        assert level == 'Member'  # Trusted needs phone verification (M2)

    def test_top_helper_badge(self, app, sample_user):
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        profile.karma = 999
        db.session.commit()
        assert is_top_helper(sample_user.id) is False
        profile.karma = 1000
        db.session.commit()
        assert is_top_helper(sample_user.id) is True
