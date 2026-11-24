"""M3 trust v2 tests — endorsements (gates + Trusted weighting),
pledge register (compliance, suspension, revocation), Ambassador
reachability, and the Ambassador flag weight."""
from datetime import timedelta

from extensions import db
from models import ConnectAccount, Match, Pledge, utc_now
from models.bounty import Bounty
from models.endorsement import Endorsement
from models.linked_account import LinkedAccount
from models.notification import Notification
from models.pitch import Pitch
from models.profile import Profile
from models.review import Review, maybe_reveal_reviews
from utils.endorsements import (
    can_endorse,
    endorsement_points,
    give_endorsement,
)
from utils.linked_accounts import get_or_create_link, mark_verified
from utils.pledges import (
    create_pledge,
    effective_pledge_status,
    ghosting_compliance,
    platform_ghosting_compliance,
    register_entries,
)
from utils.trust import compute_trust_score, trust_level


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_karma(user, amount):
    profile = Profile.query.filter_by(user_id=user.id).first()
    profile.karma = amount
    db.session.commit()
    return profile


def _free_bounty(poster):
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


def _confirmed_match(bounty, hunter, poster):
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


def _rate_both(match, hunter, poster, hunter_rates=(5, 5), poster_rates=(5, 5)):
    r1 = Review(
        match_id=match.id, reviewer_id=hunter.id, reviewee_id=poster.id,
        reviewee_role='poster', rating_primary=hunter_rates[0],
        rating_secondary=hunter_rates[1],
    )
    r2 = Review(
        match_id=match.id, reviewer_id=poster.id, reviewee_id=hunter.id,
        reviewee_role='referrer', rating_primary=poster_rates[0],
        rating_secondary=poster_rates[1],
    )
    db.session.add_all([r1, r2])
    db.session.commit()
    maybe_reveal_reviews(match.id)
    db.session.commit()


def _make_trusted(user, poster, matches=5):
    """Lift a user to Trusted: verified profile + 5 completed
    referrals, all rated 5/5 (identity 10 + transactions 40 +
    reliability 10 = 60)."""
    Profile.query.filter_by(user_id=user.id).first().verified = True
    db.session.commit()
    for _ in range(matches):
        match = _confirmed_match(_free_bounty(poster), user, poster)
        _rate_both(match, user, poster)
    level, _s, _b = trust_level(user)
    assert level == 'Trusted', f'expected Trusted, got {level}'
    return user


def _make_ambassador(user, poster):
    """Lift a user to Ambassador: 12+ months tenure, KYC, 15 completed
    referrals all rated 5/5, 500 karma, 2 verified linked accounts
    (identity 20 + transactions 40 + reliability 10 + community 5 +
    tenure 10 = 85)."""
    from models.user import User
    # Re-fetch: the fixture instance is detached, so setting
    # created_at on it would not persist for the request to see.
    user = db.session.get(User, user.id)
    user.created_at = utc_now() - timedelta(days=400)
    Profile.query.filter_by(user_id=user.id).first().verified = True
    _set_karma(user, 500)
    db.session.add(ConnectAccount(
        user_id=user.id, stripe_account_id='acct_amb_1',
        details_submitted=True, payouts_enabled=True,
    ))
    for platform in ('github', 'linkedin'):
        account = get_or_create_link(user.id, platform, 'bio_link')
        mark_verified(account)
    db.session.commit()
    for _ in range(15):
        match = _confirmed_match(_free_bounty(poster), user, poster)
        _rate_both(match, user, poster)
    level, score, _b = trust_level(user)
    assert level == 'Ambassador', f'expected Ambassador, got {level} ({score})'
    return user


def _pitch_on(bounty, hunter, days_ago, status='pending', responded_days_ago=None):
    pitch = Pitch(bounty_id=bounty.id, hunter_id=hunter.id,
                  candidate_teaser='x' * 60, status=status)
    db.session.add(pitch)
    db.session.flush()
    pitch.created_at = utc_now() - timedelta(days=days_ago)
    if responded_days_ago is not None:
        pitch.responded_at = utc_now() - timedelta(days=responded_days_ago)
    db.session.commit()
    db.session.refresh(pitch)
    return pitch


# ---------------------------------------------------------------------------
# Endorsements
# ---------------------------------------------------------------------------

class TestEndorsements:
    def test_karma_gate(self, app, sample_user, second_user):
        _set_karma(sample_user, 249)
        ok, reason = can_endorse(sample_user, second_user.id, 'Python')
        assert ok is False
        assert '250 karma' in reason
        _set_karma(sample_user, 250)
        ok, _reason = can_endorse(sample_user, second_user.id, 'Python')
        assert ok is True

    def test_no_self_endorsement(self, app, sample_user):
        _set_karma(sample_user, 250)
        ok, reason = can_endorse(sample_user, sample_user.id, 'Python')
        assert ok is False
        assert 'yourself' in reason

    def test_one_per_skill(self, app, sample_user, second_user):
        _set_karma(sample_user, 250)
        endorsement, error = give_endorsement(
            sample_user, second_user, 'Python'
        )
        assert error is None
        db.session.commit()
        ok, reason = can_endorse(sample_user, second_user.id, 'Python')
        assert ok is False
        assert 'already endorsed' in reason
        ok, _r = can_endorse(sample_user, second_user.id, 'JavaScript')
        assert ok is True

    def test_monthly_limit_five(self, app, sample_user, second_user, third_user, sample_hunter):
        _set_karma(sample_user, 250)
        endorsees = [second_user, third_user, sample_hunter, second_user, third_user]
        skills = ['S1', 'S2', 'S3', 'S4', 'S5']
        for endorsee, skill in zip(endorsees, skills):
            _e, error = give_endorsement(sample_user, endorsee, skill)
            assert error is None
        db.session.commit()
        ok, reason = can_endorse(sample_user, sample_hunter.id, 'S6')
        assert ok is False
        assert '5 endorsements' in reason

    def test_endorsement_notifies_endorsee(self, app, sample_user, second_user):
        _set_karma(sample_user, 250)
        _e, error = give_endorsement(
            sample_user, second_user, 'Python', 'Great referrer.'
        )
        assert error is None
        db.session.commit()
        note = Notification.query.filter_by(
            recipient_id=second_user.id, type='endorsement'
        ).first()
        assert note is not None
        assert 'Python' in note.title

    def test_newcomer_endorsement_weighs_one(self, app, sample_user, second_user):
        _set_karma(sample_user, 250)
        _e, _err = give_endorsement(sample_user, second_user, 'Python')
        db.session.commit()
        assert endorsement_points(second_user.id) == 1

    def test_trusted_endorsement_weighs_two(
        self, app, sample_user, second_user, sample_hunter
    ):
        _make_trusted(sample_user, sample_hunter)
        _set_karma(sample_user, 250)
        _e, _err = give_endorsement(sample_user, second_user, 'Python')
        db.session.commit()
        assert endorsement_points(second_user.id) == 2

    def test_endorsement_points_capped_at_five(
        self, app, sample_user, second_user, third_user, sample_hunter
    ):
        # Three Trusted endorsers = 6 raw points, capped at 5.
        _make_trusted(sample_user, sample_hunter)
        _make_trusted(third_user, sample_hunter)
        _make_trusted(sample_hunter, second_user)
        for endorser in (sample_user, third_user, sample_hunter):
            _set_karma(endorser, 250)
            _e, error = give_endorsement(endorser, second_user, 'Python')
            assert error is None
        db.session.commit()
        assert endorsement_points(second_user.id) == 5

    def test_endorsements_feed_community_points(
        self, app, sample_user, second_user, sample_hunter
    ):
        _make_trusted(sample_user, sample_hunter)
        _set_karma(sample_user, 250)
        _score, breakdown = compute_trust_score(second_user)
        assert breakdown['endorsement_points'] == 0
        _e, _err = give_endorsement(sample_user, second_user, 'Python')
        db.session.commit()
        _score, breakdown = compute_trust_score(second_user)
        assert breakdown['endorsement_points'] == 2
        assert breakdown['community'] == 2

    def test_mutual_endorsements_do_not_recurse(
        self, app, sample_user, second_user
    ):
        _set_karma(sample_user, 250)
        _set_karma(second_user, 250)
        _e1, _err1 = give_endorsement(sample_user, second_user, 'Python')
        _e2, _err2 = give_endorsement(second_user, sample_user, 'Python')
        db.session.commit()
        # Both directions compute without recursion.
        assert endorsement_points(sample_user.id) == 1
        assert endorsement_points(second_user.id) == 1
        trust_level(sample_user)
        trust_level(second_user)

    def test_endorse_route(self, app, client, sample_user, second_user, login_as):
        _set_karma(sample_user, 250)
        login_as(client, sample_user.email, 'TestPass123!')
        resp = client.post(
            f'/profile/users/{second_user.id}/endorse',
            data={'skill': 'Python', 'comment': 'Sharp and fast.'},
        )
        assert resp.status_code == 302
        assert Endorsement.query.filter_by(
            endorser_id=sample_user.id, endorsee_id=second_user.id,
            skill='Python',
        ).first() is not None
        # The endorsee's public profile shows it.
        resp = client.get(f'/profile/{second_user.handle}')
        assert resp.status_code == 200
        assert b'Python' in resp.data


# ---------------------------------------------------------------------------
# Pledges
# ---------------------------------------------------------------------------

class TestPledges:
    def test_create_and_register_lists_it(self, app, sample_user):
        pledge, error = create_pledge(sample_user, 'Acme Corp', 'https://acme.example')
        assert error is None
        db.session.commit()
        entries = register_entries()
        assert len(entries) == 1
        assert entries[0]['pledge'].company_name == 'Acme Corp'
        assert entries[0]['status'] == 'active'
        assert entries[0]['compliance_rate'] is None  # nothing due yet

    def test_one_active_pledge_per_user(self, app, sample_user):
        _p, _e = create_pledge(sample_user, 'Acme Corp')
        db.session.commit()
        _p2, error = create_pledge(sample_user, 'Acme Corp 2')
        assert error is not None
        assert 'already have an active pledge' in error

    def test_revoked_pledge_can_be_remade(self, app, sample_user):
        pledge, _e = create_pledge(sample_user, 'Acme Corp')
        db.session.commit()
        pledge.status = 'revoked'
        db.session.commit()
        assert register_entries() == []  # revoked pledges are not listed
        pledge2, error = create_pledge(sample_user, 'Acme Corp')
        assert error is None
        db.session.commit()
        assert Pledge.query.filter_by(user_id=sample_user.id).count() == 1
        assert register_entries()[0]['pledge'].id == pledge2.id

    def test_compliance_rate_from_sla_data(
        self, app, sample_user, sample_hunter, second_user
    ):
        bounty = _free_bounty(sample_user)
        # One answered on time (created 10d ago, answered 9d ago — the
        # 5-business-day deadline is ~7 calendar days out).
        _pitch_on(bounty, sample_hunter, 10, status='accepted', responded_days_ago=9)
        # One still pending past its deadline — breached.
        _pitch_on(bounty, second_user, 10, status='pending')
        rate, due, on_time = ghosting_compliance(sample_user.id)
        assert (rate, due, on_time) == (0.5, 2, 1)

    def test_open_breach_suspends_pledge(
        self, app, sample_user, sample_hunter
    ):
        pledge, _e = create_pledge(sample_user, 'Acme Corp')
        db.session.commit()
        bounty = _free_bounty(sample_user)
        _pitch_on(bounty, sample_hunter, 10, status='pending')
        assert effective_pledge_status(pledge) == 'suspended'
        entries = register_entries()
        assert entries[0]['status'] == 'suspended'

    def test_platform_compliance_rate(
        self, app, sample_user, sample_hunter, second_user
    ):
        bounty = _free_bounty(sample_user)
        _pitch_on(bounty, sample_hunter, 10, status='accepted', responded_days_ago=9)
        _pitch_on(bounty, second_user, 10, status='pending')
        rate, due, on_time = platform_ghosting_compliance()
        assert (rate, due, on_time) == (0.5, 2, 1)

    def test_pledge_page_renders(self, app, client, sample_user, login_as):
        pledge, _e = create_pledge(sample_user, 'Acme Corp')
        db.session.commit()
        login_as(client, sample_user.email, 'TestPass123!')
        resp = client.get('/social/pledges')
        assert resp.status_code == 200
        assert b'Acme Corp' in resp.data
        assert b'no ai screening' in resp.data.lower()

    def test_pledge_form_route(self, app, client, sample_user, login_as):
        login_as(client, sample_user.email, 'TestPass123!')
        resp = client.post(
            '/social/pledges/new',
            data={'company_name': 'Acme Corp', 'website': 'https://acme.example'},
        )
        assert resp.status_code == 302
        assert Pledge.query.filter_by(user_id=sample_user.id).first() is not None

    def test_admin_revoke(self, app, client, sample_user, sample_admin, login_as):
        pledge, _e = create_pledge(sample_user, 'Acme Corp')
        db.session.commit()
        login_as(client, sample_admin.email, 'AdminPass123!')
        resp = client.post(
            f'/admin/pledges/{pledge.id}/revoke',
            data={'reason': 'AI screening found.'},
        )
        assert resp.status_code == 302
        db.session.refresh(pledge)
        assert pledge.status == 'revoked'
        assert pledge.revoke_reason == 'AI screening found.'
        assert register_entries() == []
        note = Notification.query.filter_by(
            recipient_id=sample_user.id, type='pledge_revoked'
        ).first()
        assert note is not None


# ---------------------------------------------------------------------------
# Ambassador
# ---------------------------------------------------------------------------

class TestAmbassador:
    def test_ambassador_reachable(self, app, sample_user, sample_hunter):
        user = _make_ambassador(sample_user, sample_hunter)
        level, score, breakdown = trust_level(user)
        assert level == 'Ambassador'
        assert score >= 85
        assert breakdown['completed_referrals'] == 15
        assert breakdown['tenure_months'] >= 12

    def test_without_linked_accounts_stays_trusted(
        self, app, sample_user, sample_hunter
    ):
        # Everything except the 2 verified linked accounts: identity
        # drops to 15, score to 80 — below the Ambassador bar.
        from models.user import User
        user = db.session.get(User, sample_user.id)
        user.created_at = utc_now() - timedelta(days=400)
        Profile.query.filter_by(user_id=user.id).first().verified = True
        _set_karma(user, 500)
        db.session.add(ConnectAccount(
            user_id=user.id, stripe_account_id='acct_amb_2',
            details_submitted=True, payouts_enabled=True,
        ))
        db.session.commit()
        for _ in range(15):
            match = _confirmed_match(_free_bounty(sample_hunter), user, sample_hunter)
            _rate_both(match, user, sample_hunter)
        level, score, _b = trust_level(user)
        assert level == 'Trusted'
        assert score == 80

    def test_ambassador_flag_weighs_triple_and_auto_hides(
        self, app, client, sample_user, sample_hunter, sample_post, login_as
    ):
        from models.moderation import Report
        _make_ambassador(sample_hunter, sample_user)
        login_as(client, sample_hunter.email, 'HunterPass123!')
        resp = client.post(
            f'/social/report/post/{sample_post.id}',
            data={'reason': 'spam', 'details': 'Looks automated.'},
        )
        assert resp.status_code == 302
        report = Report.query.filter_by(
            reporter_id=sample_hunter.id, target_id=sample_post.id
        ).first()
        assert report is not None
        assert report.weight == 3
        # One Ambassador report meets the default threshold of 3 —
        # the post is auto-hidden pending review.
        from models.post import Post
        post = db.session.get(Post, sample_post.id)
        assert post.auto_hidden_at is not None
