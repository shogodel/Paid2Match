"""M4 tests — second vertical, referral chains, board search, earnings.

All Stripe calls are mocked (unittest.mock.patch on the stripe module) —
no network calls, no real keys.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import stripe
from decimal import Decimal

from extensions import db
from models import AdminSettings, ConnectAccount, Match, Payout
from models.bounty import Bounty
from models.pitch import Pitch
from models.post import Post, Share
from models.referral import Referral
from utils.chains import (
    compute_chain_split,
    resolve_chain_sharer,
    validate_share_for_referral,
)
from utils.earnings import get_earnings_summary
from utils.feed import create_need_post
from utils.payments import (
    handle_transfer_event,
    record_manual_payout,
    release_escrow,
    retry_pending_payouts,
)
from utils.referrals import complete_referral_for_match


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_stripe_keys():
    AdminSettings.set('STRIPE_SECRET_KEY', 'sk_test_fake')
    AdminSettings.set('STRIPE_PUBLISHABLE_KEY', 'pk_test_fake')
    AdminSettings.set('STRIPE_WEBHOOK_SECRET', 'whsec_fake')
    db.session.commit()


def _secured_paid_bounty(app, poster, reward=1000, bounty_type='recruitment',
                         category=None, skills=None, title='Paid need'):
    bounty = Bounty(
        poster_id=poster.id,
        title=title,
        description='A detailed description of the need, long enough to pass validation checks.',
        bounty_type=bounty_type,
        bounty_direction='seeking_opportunity',
        category=category,
        skills=skills,
        reward_amount=reward,
        status='open',
        payment_status='secured',
        stripe_payment_intent='pi_test_escrow',
        location='Remote',
    )
    db.session.add(bounty)
    db.session.commit()
    db.session.refresh(bounty)
    return bounty


def _need_post(app, bounty):
    post = create_need_post(bounty)
    db.session.commit()
    db.session.refresh(post)
    return post


def _share(app, user, post, comment='Sharing this — great opportunity!'):
    share = Share(user_id=user.id, post_id=post.id, comment=comment)
    db.session.add(share)
    db.session.commit()
    db.session.refresh(share)
    return share


def _confirmed_match(app, bounty, hunter, poster, via_share_id=None):
    pitch = Pitch(bounty_id=bounty.id, hunter_id=hunter.id,
                  candidate_teaser='x' * 60, status='accepted',
                  via_share_id=via_share_id)
    db.session.add(pitch)
    db.session.flush()
    match = Match(
        bounty_id=bounty.id, pitch_id=pitch.id,
        hunter_id=hunter.id, employer_id=poster.id,
        hunter_confirmed=True, employer_confirmed=True,
        payout_amount=float(bounty.reward_amount or 0),
        platform_fee=float(bounty.reward_amount or 0) * 0.20,
        payout_status='pending',
    )
    db.session.add(match)
    db.session.commit()
    db.session.refresh(match)
    return match


def _connect_account(app, user, stripe_id, ready=True):
    account = ConnectAccount(
        user_id=user.id,
        stripe_account_id=stripe_id,
        onboarding_status='complete' if ready else 'pending',
        charges_enabled=ready,
        payouts_enabled=ready,
        details_submitted=ready,
    )
    db.session.add(account)
    db.session.commit()
    return account


def _mock_pi(status='requires_capture'):
    return SimpleNamespace(id='pi_test_escrow', status=status)


def _post_bounty_data(**overrides):
    data = {
        'title': 'A Brand New Bounty For Testing Purposes',
        'description': 'This is a detailed description for a bounty that should pass all validations and be created successfully as an open bounty.',
        'bounty_type': 'recruitment',
        'bounty_direction': 'seeking_opportunity',
        'reward_amount': '0',
        'is_free': '1',
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Verticals
# ---------------------------------------------------------------------------

class TestVerticals:
    def test_services_is_a_valid_bounty_type(self, app, sample_user):
        bounty = Bounty(
            poster_id=sample_user.id, title='Need a plumber in Laval',
            description='x' * 60, bounty_type='services',
            bounty_direction='seeking_opportunity', reward_amount=500,
            status='open',
        )
        db.session.add(bounty)
        db.session.commit()
        assert bounty.type_label == 'Services'

    def test_other_bounty_types_stay_locked(self, app, sample_user):
        with pytest.raises(ValueError):
            Bounty(
                poster_id=sample_user.id, title='x' * 20,
                description='x' * 60, bounty_type='freelance',
                bounty_direction='seeking_opportunity',
            )

    def test_category_labels(self, app, sample_user):
        bounty = Bounty(
            poster_id=sample_user.id, title='Need a plumber in Laval',
            description='x' * 60, bounty_type='services',
            bounty_direction='seeking_opportunity',
            category='home_services',
        )
        assert bounty.category_label == 'Home services'
        bounty.category = None
        assert bounty.category_label is None

    def test_skill_list_parses_comma_separated(self, app, sample_user):
        bounty = Bounty(
            poster_id=sample_user.id, title='Need a plumber in Laval',
            description='x' * 60, bounty_type='services',
            bounty_direction='seeking_opportunity',
            skills='Plumbing,  Drainage,, Gas fitting ',
        )
        assert bounty.skill_list == ['Plumbing', 'Drainage', 'Gas fitting']
        bounty.skills = None
        assert bounty.skill_list == []

    def test_post_services_need_via_form(self, logged_in_user, app, sample_user):
        r = logged_in_user.post('/needs/post', data=_post_bounty_data(
            bounty_type='services', category='home_services',
            skills='Plumbing, Drainage',
            title='Need a plumber for a bathroom reno',
        ), follow_redirects=False)
        assert r.status_code == 302
        bounty = Bounty.query.filter_by(
            title='Need a plumber for a bathroom reno'
        ).first()
        assert bounty is not None
        assert bounty.bounty_type == 'services'
        assert bounty.category == 'home_services'
        assert bounty.skills == 'Plumbing, Drainage'

    def test_post_rejects_category_from_another_vertical(
            self, logged_in_user, app, sample_user):
        r = logged_in_user.post('/needs/post', data=_post_bounty_data(
            bounty_type='services', category='trades',
            title='Need a plumber for a bathroom reno',
        ))
        assert r.status_code == 200
        assert b'does not belong to the chosen vertical' in r.data
        assert Bounty.query.filter_by(
            title='Need a plumber for a bathroom reno'
        ).first() is None

    def test_edit_changes_vertical_category_skills(
            self, logged_in_user, app, sample_bounty):
        r = logged_in_user.post(
            f'/needs/{sample_bounty.id}/edit', data={
                'title': 'Updated Bounty Title Here',
                'description': 'Updated description for the bounty, long enough to pass.',
                'bounty_type': 'services',
                'category': 'creative_design',
                'skills': 'Logo design, Branding',
                'bounty_direction': 'seeking_opportunity',
                'reward_amount': '500',
                'location': 'Montreal',
            }, follow_redirects=False)
        assert r.status_code == 302
        bounty = db.session.get(Bounty, sample_bounty.id)
        assert bounty.bounty_type == 'services'
        assert bounty.category == 'creative_design'
        assert bounty.skills == 'Logo design, Branding'

    def test_services_board_lists_only_services(
            self, client, app, sample_user):
        _secured_paid_bounty(app, sample_user, title='Recruitment need here',
                             bounty_type='recruitment')
        _secured_paid_bounty(app, sample_user, title='Services need here',
                             bounty_type='services', category='home_services')
        r = client.get('/needs/services')
        assert r.status_code == 200
        assert b'Services need here' in r.data
        assert b'Recruitment need here' not in r.data
        r = client.get('/needs/recruitment')
        assert r.status_code == 200
        assert b'Recruitment need here' in r.data
        assert b'Services need here' not in r.data

    def test_board_vertical_filter(self, client, app, sample_user):
        _secured_paid_bounty(app, sample_user, title='Recruitment need here',
                             bounty_type='recruitment')
        _secured_paid_bounty(app, sample_user, title='Services need here',
                             bounty_type='services')
        r = client.get('/needs/?bounty_type=services')
        assert b'Services need here' in r.data
        assert b'Recruitment need here' not in r.data

    def test_board_category_filter(self, client, app, sample_user):
        _secured_paid_bounty(app, sample_user, title='Plumber needed now',
                             bounty_type='services', category='home_services')
        _secured_paid_bounty(app, sample_user, title='Designer needed now',
                             bounty_type='services', category='creative_design')
        r = client.get('/needs/?category=home_services')
        assert b'Plumber needed now' in r.data
        assert b'Designer needed now' not in r.data

    def test_second_vertical_holds_three_funded_needs(
            self, client, app, sample_user):
        # M4 exit criterion: the second vertical's first 3 funded needs.
        for title in ('Plumber needed in Laval', 'Electrician needed now',
                      'Designer for a logo project'):
            _secured_paid_bounty(app, sample_user, title=title,
                                 bounty_type='services',
                                 category='home_services')
        r = client.get('/needs/services')
        assert r.status_code == 200
        assert b'Plumber needed in Laval' in r.data
        assert b'Electrician needed now' in r.data
        assert b'Designer for a logo project' in r.data
        assert Bounty.query.filter_by(
            bounty_type='services', payment_status='secured'
        ).count() == 3


# ---------------------------------------------------------------------------
# Board search + skills filter
# ---------------------------------------------------------------------------

class TestBoardSearch:
    def test_keyword_search_matches_title(self, client, app, sample_user):
        _secured_paid_bounty(app, sample_user, title='Python engineer wanted')
        _secured_paid_bounty(app, sample_user, title='Plumber needed now')
        r = client.get('/needs/?q=Python')
        assert b'Python engineer wanted' in r.data
        assert b'Plumber needed now' not in r.data

    def test_keyword_search_matches_description(
            self, client, app, sample_user):
        bounty = _secured_paid_bounty(app, sample_user, title='Engineer wanted')
        bounty.description = 'We need someone fluent in Kubernetes for this role.'
        db.session.commit()
        _secured_paid_bounty(app, sample_user, title='Plumber needed now')
        r = client.get('/needs/?q=Kubernetes')
        assert b'Engineer wanted' in r.data
        assert b'Plumber needed now' not in r.data

    def test_skill_filter_matches_bounty_skills(
            self, client, app, sample_user):
        _secured_paid_bounty(app, sample_user, title='Python engineer wanted',
                             skills='Python, Django')
        _secured_paid_bounty(app, sample_user, title='Plumber needed now',
                             skills='Plumbing')
        r = client.get('/needs/?skill=python')
        assert b'Python engineer wanted' in r.data
        assert b'Plumber needed now' not in r.data

    def test_skill_filter_works_on_vertical_boards(
            self, client, app, sample_user):
        _secured_paid_bounty(app, sample_user, title='Python engineer wanted',
                             bounty_type='recruitment', skills='Python')
        _secured_paid_bounty(app, sample_user, title='Sales rep wanted here',
                             bounty_type='recruitment', skills='Sales')
        r = client.get('/needs/recruitment?skill=Python')
        assert b'Python engineer wanted' in r.data
        assert b'Sales rep wanted here' not in r.data

    def test_skills_field_is_saved_from_post_form(
            self, logged_in_user, app, sample_user):
        # The audit's discarded field (§3.10): the post form collected
        # skills but Bounty had no column — it is saved now.
        r = logged_in_user.post('/needs/post', data=_post_bounty_data(
            skills='Python, SQL, Leadership',
            title='Engineer with specific skills here',
        ), follow_redirects=False)
        assert r.status_code == 302
        bounty = Bounty.query.filter_by(
            title='Engineer with specific skills here'
        ).first()
        assert bounty is not None
        assert bounty.skills == 'Python, SQL, Leadership'
        assert bounty.skill_list == ['Python', 'SQL', 'Leadership']


# ---------------------------------------------------------------------------
# Chain attribution
# ---------------------------------------------------------------------------

class TestChainAttribution:
    def test_compute_chain_split_exact(self, app):
        referrer, sharer = compute_chain_split(800)
        assert referrer == Decimal('560.00')
        assert sharer == Decimal('240.00')
        # Odd amounts still sum exactly — the sharer gets the remainder.
        referrer, sharer = compute_chain_split('100.01')
        assert referrer == Decimal('70.01')
        assert sharer == Decimal('30.00')
        assert referrer + sharer == Decimal('100.01')

    def test_validate_share_valid(self, app, sample_user, second_user,
                                  sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        assert validate_share_for_referral(
            share.id, bounty, sample_hunter.id
        ) is share

    def test_validate_share_rejects_wrong_bounty(
            self, app, sample_user, second_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user, title='Need one here')
        other = _secured_paid_bounty(app, sample_user, title='Need two here')
        post = _need_post(app, other)
        share = _share(app, second_user, post)
        assert validate_share_for_referral(
            share.id, bounty, sample_hunter.id
        ) is None

    def test_validate_share_rejects_own_share_and_poster_share(
            self, app, sample_user, second_user):
        bounty = _secured_paid_bounty(app, sample_user)
        post = _need_post(app, bounty)
        own_share = _share(app, second_user, post)
        assert validate_share_for_referral(
            own_share.id, bounty, second_user.id
        ) is None
        poster_share = _share(app, sample_user, post)
        assert validate_share_for_referral(
            poster_share.id, bounty, second_user.id
        ) is None
        assert validate_share_for_referral(
            'nonexistent', bounty, second_user.id
        ) is None

    def test_resolve_chain_sharer_from_pitch(
            self, app, sample_user, second_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        assert resolve_chain_sharer(match) == second_user.id

    def test_resolve_chain_sharer_none_without_share(
            self, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        assert resolve_chain_sharer(match) is None

    def test_refer_route_stores_share_id(
            self, client, login_as, app, sample_user, second_user,
            sample_hunter, third_user):
        bounty = _secured_paid_bounty(app, sample_user)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        login_as(client, 'hunter@example.com', 'HunterPass123!')
        r = client.post(
            f'/referrals/new/{bounty.id}?share_id={share.id}', data={
                'connection_id': '',
                'referred_name': 'Third User',
                'referred_email': 'third@example.com',
                'message': 'Third is a great fit for this need — five years of experience.',
            }, follow_redirects=False)
        assert r.status_code == 302
        referral = Referral.query.filter_by(
            bounty_id=bounty.id, referred_email='third@example.com'
        ).first()
        assert referral is not None
        assert referral.via_share_id == share.id

    def test_refer_route_ignores_invalid_share_id(
            self, client, login_as, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        login_as(client, 'hunter@example.com', 'HunterPass123!')
        r = client.post(
            f'/referrals/new/{bounty.id}?share_id=nonexistent', data={
                'connection_id': '',
                'referred_name': 'Third User',
                'referred_email': 'third@example.com',
                'message': 'Third is a great fit for this need — five years of experience.',
            }, follow_redirects=False)
        assert r.status_code == 302
        referral = Referral.query.filter_by(
            bounty_id=bounty.id, referred_email='third@example.com'
        ).first()
        assert referral is not None
        assert referral.via_share_id is None

    def test_pitch_route_stores_share_id(
            self, client, login_as, app, sample_user, second_user,
            sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        login_as(client, 'hunter@example.com', 'HunterPass123!')
        r = client.post(f'/needs/{bounty.id}/pitch', data={
            'candidate_teaser': 'I am the right person for this need — five years of experience here.',
            'share_id': share.id,
        }, follow_redirects=False)
        assert r.status_code == 302
        pitch = Pitch.query.filter_by(
            bounty_id=bounty.id, hunter_id=sample_hunter.id
        ).first()
        assert pitch is not None
        assert pitch.via_share_id == share.id

    def test_consent_copies_share_to_pitch(
            self, client, login_as, app, sample_user, second_user,
            sample_hunter, third_user):
        bounty = _secured_paid_bounty(app, sample_user)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        referral = Referral(
            referrer_id=sample_hunter.id, bounty_id=bounty.id,
            referred_name='Third User', referred_email='third@example.com',
            referred_user_id=third_user.id,
            message='Third is a great fit for this need — five years of experience.',
            via_share_id=share.id,
        )
        db.session.add(referral)
        db.session.commit()
        login_as(client, 'third@example.com', 'ThirdPass123!')
        r = client.post(f'/referrals/{referral.id}/confirm',
                        follow_redirects=False)
        assert r.status_code == 302
        db.session.refresh(referral)
        assert referral.status == 'consented'
        pitch = db.session.get(Pitch, referral.pitch_id)
        assert pitch.via_share_id == share.id


# ---------------------------------------------------------------------------
# Chain payout splits (mocked Stripe)
# ---------------------------------------------------------------------------

class TestChainPayouts:
    def _release(self, match):
        transfers = [
            SimpleNamespace(id='tr_referrer'),
            SimpleNamespace(id='tr_sharer'),
        ]
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture') as capture, \
             patch('stripe.Transfer.create', side_effect=transfers) as tc:
            ok, msg = release_escrow(match)
            db.session.commit()
        return ok, msg, capture, tc

    def test_chain_split_pays_referrer_560_and_sharer_240(
            self, app, sample_user, second_user, sample_hunter):
        # $1,000 reward -> $200 platform, $560 referrer, $240 sharer.
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        _connect_account(app, sample_hunter, 'acct_referrer')
        _connect_account(app, second_user, 'acct_sharer')
        ok, msg, capture, tc = self._release(match)
        assert ok is True
        assert msg == 'Payout in progress.'
        capture.assert_called_once_with('pi_test_escrow')
        assert tc.call_count == 2
        calls = {c.kwargs['destination']: c.kwargs for c in tc.call_args_list}
        assert calls['acct_referrer']['amount'] == 56000
        assert calls['acct_sharer']['amount'] == 24000
        assert calls['acct_referrer']['currency'] == 'cad'
        assert calls['acct_sharer']['source_transaction'] == 'pi_test_escrow'
        referrer_payout = Payout.query.filter_by(
            match_id=match.id, kind='referral').first()
        chain_payout = Payout.query.filter_by(
            match_id=match.id, kind='chain_share').first()
        assert float(referrer_payout.amount) == 560.0
        assert float(referrer_payout.platform_fee) == 200.0
        assert float(referrer_payout.gross_amount) == 1000.0
        assert referrer_payout.status == 'processing'
        assert float(chain_payout.amount) == 240.0
        assert float(chain_payout.platform_fee) == 0.0
        assert chain_payout.referrer_id == second_user.id
        assert chain_payout.status == 'processing'
        assert db.session.get(Match, match.id).payout_status == 'processing'

    def test_no_chain_pays_full_net(self, app, sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _connect_account(app, sample_hunter, 'acct_referrer')
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_1')) as tc:
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok is True
        assert tc.call_count == 1
        assert tc.call_args.kwargs['amount'] == 80000
        assert Payout.query.filter_by(match_id=match.id).count() == 1
        assert Payout.query.filter_by(
            match_id=match.id, kind='chain_share').first() is None

    def test_chain_transfer_webhooks_complete_both_payouts(
            self, app, sample_user, second_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        _connect_account(app, sample_hunter, 'acct_referrer')
        _connect_account(app, second_user, 'acct_sharer')
        self._release(match)
        # The chain payout completing first does NOT complete the match.
        handle_transfer_event(
            'transfer.paid', SimpleNamespace(id='tr_sharer'))
        db.session.commit()
        chain = Payout.query.filter_by(
            match_id=match.id, kind='chain_share').first()
        assert chain.status == 'completed'
        assert db.session.get(Match, match.id).payout_status == 'processing'
        handle_transfer_event(
            'transfer.paid', SimpleNamespace(id='tr_referrer'))
        db.session.commit()
        referrer = Payout.query.filter_by(
            match_id=match.id, kind='referral').first()
        assert referrer.status == 'completed'
        assert db.session.get(Match, match.id).payout_status == 'completed'

    def test_chain_transfer_failure_does_not_fail_match(
            self, app, sample_user, second_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        _connect_account(app, sample_hunter, 'acct_referrer')
        _connect_account(app, second_user, 'acct_sharer')
        self._release(match)
        handle_transfer_event(
            'transfer.failed', SimpleNamespace(id='tr_sharer'))
        db.session.commit()
        chain = Payout.query.filter_by(
            match_id=match.id, kind='chain_share').first()
        assert chain.status == 'failed'
        # The match mirrors the referrer payout, still processing.
        assert db.session.get(Match, match.id).payout_status == 'processing'

    def test_sharer_not_ready_waits_without_blocking_referrer(
            self, app, sample_user, second_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        _connect_account(app, sample_hunter, 'acct_referrer')
        # The sharer has no Connect account at all.
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_referrer')) as tc:
            ok, msg = release_escrow(match)
            db.session.commit()
        assert ok is True
        assert 'sharer share is waiting' in msg
        assert tc.call_count == 1
        assert tc.call_args.kwargs['amount'] == 56000
        chain = Payout.query.filter_by(
            match_id=match.id, kind='chain_share').first()
        assert chain.status == 'pending'
        assert 'Awaiting sharer payout setup' in chain.failure_reason
        referrer = Payout.query.filter_by(
            match_id=match.id, kind='referral').first()
        assert referrer.status == 'processing'

    def test_sharer_retry_after_onboarding_releases_chain_cut(
            self, app, sample_user, second_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        _connect_account(app, sample_hunter, 'acct_referrer')
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_referrer')):
            release_escrow(match)
            db.session.commit()
        # The sharer finishes onboarding; the retry releases only the
        # chain cut (no second capture — the PI is already captured).
        _connect_account(app, second_user, 'acct_sharer')
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.PaymentIntent.capture') as capture, \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_sharer')) as tc:
            results = retry_pending_payouts(second_user.id)
            db.session.commit()
        assert len(results) == 1
        assert results[0][1] is True
        capture.assert_not_called()
        assert tc.call_count == 1
        assert tc.call_args.kwargs['amount'] == 24000
        assert tc.call_args.kwargs['destination'] == 'acct_sharer'
        chain = Payout.query.filter_by(
            match_id=match.id, kind='chain_share').first()
        assert chain.status == 'processing'

    def test_referrer_not_ready_blocks_whole_release(
            self, app, sample_user, second_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        _connect_account(app, second_user, 'acct_sharer')
        # The referrer has no Connect account.
        ok, msg = release_escrow(match)
        db.session.commit()
        assert ok is False
        assert 'Referrer has not completed payout setup' in msg
        assert Payout.query.filter_by(
            match_id=match.id, kind='referral').first().status == 'pending'
        assert Payout.query.filter_by(
            match_id=match.id, kind='chain_share').first().status == 'pending'

    def test_release_idempotent_with_chain(
            self, app, sample_user, second_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        _connect_account(app, sample_hunter, 'acct_referrer')
        _connect_account(app, second_user, 'acct_sharer')
        self._release(match)
        # Second call: both payouts are processing — nothing re-runs.
        with patch('stripe.PaymentIntent.retrieve') as retrieve, \
             patch('stripe.Transfer.create') as tc:
            ok, msg = release_escrow(db.session.get(Match, match.id))
        assert ok is True
        assert 'already in progress' in msg
        retrieve.assert_not_called()
        tc.assert_not_called()
        assert Payout.query.filter_by(match_id=match.id).count() == 2

    def test_manual_chain_payout(self, app, sample_user, second_user,
                                 sample_hunter, sample_admin):
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        payout, _note = record_manual_payout(
            match, 240, 'interac-ref-1', sample_admin,
            payout_kind='chain_share',
        )
        db.session.commit()
        assert payout.kind == 'chain_share'
        assert payout.referrer_id == second_user.id
        assert float(payout.amount) == 240.0
        assert payout.status == 'completed'
        # A chain payout never completes the match on its own.
        assert db.session.get(Match, match.id).payout_status == 'pending'

    def test_manual_chain_payout_without_sharer_raises(
            self, app, sample_user, sample_hunter, sample_admin):
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        with pytest.raises(ValueError, match='no chain sharer'):
            record_manual_payout(
                match, 240, 'ref', sample_admin, payout_kind='chain_share'
            )

    def test_win_post_shows_referrer_share_with_chain(
            self, app, sample_user, second_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        post = _need_post(app, bounty)
        share = _share(app, second_user, post)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        result = complete_referral_for_match(match)
        db.session.commit()
        assert 'Earned $560' in result['win_post'].body


# ---------------------------------------------------------------------------
# Earnings dashboard
# ---------------------------------------------------------------------------

class TestEarnings:
    def _payout(self, match, bounty, user, amount, status, kind='referral'):
        payout = Payout(
            match_id=match.id, referrer_id=user.id, bounty_id=bounty.id,
            kind=kind, gross_amount=1000, platform_fee=200,
            amount=amount, currency='cad', method='stripe', status=status,
        )
        db.session.add(payout)
        return payout

    def test_summary_totals(self, app, sample_user, second_user,
                            sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        self._payout(match, bounty, sample_hunter, 560, 'completed')
        self._payout(match, bounty, second_user, 240, 'completed',
                     kind='chain_share')
        db.session.commit()
        summary = get_earnings_summary(sample_hunter.id)
        assert summary['total_paid'] == 560.0
        assert summary['referral_paid'] == 560.0
        assert summary['chain_paid'] == 0.0
        sharer = get_earnings_summary(second_user.id)
        assert sharer['total_paid'] == 240.0
        assert sharer['chain_paid'] == 240.0
        assert sharer['referral_paid'] == 0.0

    def test_summary_pending_and_statuses(
            self, app, sample_user, sample_hunter, third_user):
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        self._payout(match, bounty, sample_hunter, 800, 'pending')
        referral = Referral(
            referrer_id=sample_hunter.id, bounty_id=bounty.id,
            referred_name='Third User', referred_email='third@example.com',
            message='x' * 60, status='completed',
        )
        db.session.add(referral)
        db.session.commit()
        summary = get_earnings_summary(sample_hunter.id)
        assert summary['total_pending'] == 800.0
        assert summary['referrals_by_status'] == {'completed': 1}
        assert summary['matches_total'] == 1
        assert summary['completed_matches'] == 1
        assert len(summary['per_need']) == 1
        row = summary['per_need'][0]
        assert row['bounty'].id == bounty.id
        assert row['referrals'] == 2  # the Referral + the direct Pitch
        assert row['matches'] == 1
        assert row['earned'] == 0.0  # payout is pending, not paid

    def test_per_need_earned_counts_completed_payouts(
            self, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        self._payout(match, bounty, sample_hunter, 800, 'completed')
        db.session.commit()
        summary = get_earnings_summary(sample_hunter.id)
        assert summary['per_need'][0]['earned'] == 800.0

    def test_payout_dashboard_shows_chain_earnings(
            self, client, login_as, app, sample_user, second_user,
            sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user, reward=1000)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        self._payout(match, bounty, second_user, 240, 'completed',
                     kind='chain_share')
        db.session.commit()
        login_as(client, 'second@example.com', 'SecondPass123!')
        r = client.get('/payments/payouts')
        assert r.status_code == 200
        assert b'Chain share' in r.data
        assert b'Earned from shares' in r.data
        assert b'240.00' in r.data
