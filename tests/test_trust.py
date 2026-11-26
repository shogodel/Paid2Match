"""M1 tests: trust score, levels, gates, Top Helper."""
import pytest
from datetime import datetime, timedelta, timezone

from extensions import db
from models.bounty import Bounty
from models.match import Match
from models.pitch import Pitch
from models.profile import Profile
from utils.reputation import median_response_time, reputation_metrics
from utils.trust import (
    average_response_time, completed_referrals_count,
    compute_trust_score, format_response_time, is_top_helper,
    response_rate, trust_level,
)


def _timed_pitch(bounty_id, hunter_id, status, created_at,
                 responded_at=None, updated_at=None):
    """A pitch with explicit timestamps, so response-time tests use
    known deltas instead of wall-clock timing."""
    pitch = Pitch(
        bounty_id=bounty_id, hunter_id=hunter_id,
        candidate_teaser='t' * 60, status=status,
        created_at=created_at, responded_at=responded_at,
        updated_at=updated_at if updated_at is not None
        else (responded_at or created_at),
    )
    db.session.add(pitch)
    return pitch


# Fixed base time (UTC) for response-time deltas.
_BASE = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class TestAverageResponseTime:
    def test_no_pitches_returns_none(self, app, sample_user, sample_bounty):
        assert average_response_time(sample_user.id) is None

    def test_median_odd_count(self, app, sample_user, sample_hunter, sample_bounty):
        # Deltas 2h, 6h, 10h across all three answered statuses.
        for hours, status in [(2, 'interested'), (6, 'rejected'), (10, 'accepted')]:
            _timed_pitch(sample_bounty.id, sample_hunter.id, status,
                         _BASE, _BASE + timedelta(hours=hours))
        db.session.commit()
        assert average_response_time(sample_user.id) == (6.0, 3)

    def test_median_even_count_resists_slow_outlier(self, app, sample_user, sample_hunter, sample_bounty):
        # Even count: median is the mean of the two middle values
        # (4h + 6h) / 2 = 5h. The 100h pitch would drag a mean to
        # 28h — the median must not move.
        for hours in [2, 4, 6, 100]:
            _timed_pitch(sample_bounty.id, sample_hunter.id, 'interested',
                         _BASE, _BASE + timedelta(hours=hours))
        db.session.commit()
        assert average_response_time(sample_user.id) == (5.0, 4)

    def test_null_responded_at_falls_back_to_updated_at(self, app, sample_user, sample_hunter, sample_bounty):
        # Pre-M2 pitch: responded_at NULL, updated_at 8h after creation.
        _timed_pitch(sample_bounty.id, sample_hunter.id, 'rejected',
                     _BASE, responded_at=None,
                     updated_at=_BASE + timedelta(hours=8))
        db.session.commit()
        assert average_response_time(sample_user.id) == (8.0, 1)

    def test_unanswered_pitches_excluded(self, app, sample_user, sample_hunter, sample_bounty):
        _timed_pitch(sample_bounty.id, sample_hunter.id, 'interested',
                     _BASE, _BASE + timedelta(hours=4))
        # Still waiting — must not count, even with a recent updated_at.
        _timed_pitch(sample_bounty.id, sample_hunter.id, 'pending',
                     _BASE, updated_at=_BASE + timedelta(hours=200))
        _timed_pitch(sample_bounty.id, sample_hunter.id, 'viewed',
                     _BASE, updated_at=_BASE + timedelta(hours=200))
        db.session.commit()
        assert average_response_time(sample_user.id) == (4.0, 1)

    def test_other_users_bounties_excluded(self, app, sample_user, sample_hunter, sample_bounty):
        _timed_pitch(sample_bounty.id, sample_hunter.id, 'interested',
                     _BASE, _BASE + timedelta(hours=4))
        others = Bounty(poster_id=sample_hunter.id, title='Not my need here',
                        description='d' * 60, bounty_type='recruitment',
                        bounty_direction='seeking_opportunity', status='open')
        db.session.add(others)
        db.session.flush()
        _timed_pitch(others.id, sample_user.id, 'interested',
                     _BASE, _BASE + timedelta(hours=100))
        db.session.commit()
        assert average_response_time(sample_user.id) == (4.0, 1)

    def test_format_response_time(self, app):
        assert format_response_time(None) is None
        assert format_response_time(0.5) == 'under an hour'
        assert format_response_time(0.99) == 'under an hour'
        assert format_response_time(1) == '~1 hour'
        assert format_response_time(6) == '~6 hours'
        assert format_response_time(5.6) == '~6 hours'
        assert format_response_time(12) == '~12 hours'
        assert format_response_time(23.9) == '~24 hours'
        # A day or more renders as days, not a big hour count.
        assert format_response_time(24) == '~1 day'
        assert format_response_time(49) == '~2 days'
        assert format_response_time(72) == '~3 days'


class TestResponseTimeConsolidation:
    """utils/reputation.py's median_response_time must stay a thin
    wrapper over utils.trust — one computation, one formatter, one
    string on every surface (Trust card and reputation-metrics
    strip)."""

    def test_wrapper_matches_trust(self, app, sample_user, sample_hunter,
                                   sample_bounty):
        for hours in [2, 6, 10]:
            _timed_pitch(sample_bounty.id, sample_hunter.id, 'interested',
                         _BASE, _BASE + timedelta(hours=hours))
        db.session.commit()
        avg = average_response_time(sample_user.id)
        assert avg == (6.0, 3)
        assert median_response_time(sample_user.id) == '~6 hours'
        assert median_response_time(sample_user.id) == (
            format_response_time(avg[0]))

    def test_wrapper_days_branch(self, app, sample_user, sample_hunter,
                                 sample_bounty):
        _timed_pitch(sample_bounty.id, sample_hunter.id, 'interested',
                     _BASE, _BASE + timedelta(hours=49))
        db.session.commit()
        assert median_response_time(sample_user.id) == '~2 days'

    def test_wrapper_no_data_returns_none(self, app, sample_user,
                                          sample_bounty):
        assert median_response_time(sample_user.id) is None
        assert reputation_metrics(sample_user.id)['response_time'] is None

    def test_reputation_metrics_uses_same_string(
            self, app, sample_user, sample_hunter, sample_bounty):
        for hours in [2, 6, 10]:
            _timed_pitch(sample_bounty.id, sample_hunter.id, 'interested',
                         _BASE, _BASE + timedelta(hours=hours))
        db.session.commit()
        assert reputation_metrics(sample_user.id)['response_time'] == (
            '~6 hours')



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
