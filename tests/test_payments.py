"""M2 payments tests — escrow, Connect, payouts, CAD, fail-closed.

All Stripe calls are mocked (unittest.mock.patch on the stripe module) —
no network calls, no real keys. Test-mode keys in AdminSettings are fake
strings; the code under test never reaches Stripe.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import stripe

from extensions import db
from models import AdminSettings, ConnectAccount, Match, Payout
from models.bounty import Bounty
from models.notification import Notification
from models.pitch import Pitch
from utils.payments import (
    compute_payout_amounts,
    create_account_link,
    create_checkout_session,
    create_connect_account,
    fee_rate_for,
    get_platform_fee_percent,
    handle_account_updated,
    handle_payout_event,
    handle_transfer_event,
    record_manual_payout,
    refresh_connect_account,
    release_escrow,
    to_cents,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_stripe_keys():
    AdminSettings.set('STRIPE_SECRET_KEY', 'sk_test_fake')
    AdminSettings.set('STRIPE_PUBLISHABLE_KEY', 'pk_test_fake')
    AdminSettings.set('STRIPE_WEBHOOK_SECRET', 'whsec_fake')
    db.session.commit()


def _secured_paid_bounty(app, poster, reward=1000):
    """A paid bounty whose payment is authorized (escrow). No nested
    app context — the test's own context is active, and a nested one
    would detach the returned instance."""
    bounty = Bounty(
        poster_id=poster.id,
        title='Paid need',
        description='x' * 60,
        bounty_type='recruitment',
        bounty_direction='seeking_opportunity',
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


def _confirmed_match(app, bounty, hunter, poster):
    """A dual-confirmed match on the bounty (payout still pending).
    The fee is locked the way confirm_match locks it at creation
    (fee_rate_for — 15% for founding referrers, else the configured
    rate), so the helper mirrors production: release_escrow honors
    the locked fee and never recomputes it."""
    pitch = Pitch(bounty_id=bounty.id, hunter_id=hunter.id,
                  candidate_teaser='x' * 60, status='accepted')
    db.session.add(pitch)
    db.session.flush()
    _gross, platform_fee, _net = compute_payout_amounts(
        float(bounty.reward_amount or 0), fee_rate_for(hunter)
    )
    match = Match(
        bounty_id=bounty.id, pitch_id=pitch.id,
        hunter_id=hunter.id, employer_id=poster.id,
        hunter_confirmed=True, employer_confirmed=True,
        payout_amount=float(bounty.reward_amount or 0),
        platform_fee=platform_fee,
        payout_status='pending',
    )
    db.session.add(match)
    db.session.commit()
    db.session.refresh(match)
    return match


def _connect_account(app, user, ready=True):
    account = ConnectAccount(
        user_id=user.id,
        stripe_account_id='acct_test_ready',
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


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------

class TestPlatformFee:
    def test_default_fee_is_20_percent(self, app):
        assert get_platform_fee_percent() == 20

    def test_fee_setting_overrides_default(self, app):
        AdminSettings.set('PLATFORM_FEE_PERCENT', '25')
        db.session.commit()
        assert get_platform_fee_percent() == 25

    def test_fee_setting_clamped_to_0_50(self, app):
        AdminSettings.set('PLATFORM_FEE_PERCENT', '99')
        db.session.commit()
        assert get_platform_fee_percent() == 50
        AdminSettings.set('PLATFORM_FEE_PERCENT', '-5')
        db.session.commit()
        assert get_platform_fee_percent() == 0

    def test_invalid_fee_setting_falls_back_to_default(self, app):
        AdminSettings.set('PLATFORM_FEE_PERCENT', 'not-a-number')
        db.session.commit()
        assert get_platform_fee_percent() == 20

    def test_founding_referrer_pays_15(self, app, sample_hunter):
        hunter = db.session.get(type(sample_hunter), sample_hunter.id)
        hunter.founding_referrer = True
        db.session.commit()
        assert fee_rate_for(hunter) == 15

    def test_standard_referrer_pays_configured_fee(self, app, sample_hunter):
        assert fee_rate_for(sample_hunter) == 20

    def test_compute_payout_amounts_1000(self, app):
        gross, fee, net = compute_payout_amounts(1000, 20)
        assert float(gross) == 1000.0
        assert float(fee) == 200.0
        assert float(net) == 800.0
        _g, fee15, net15 = compute_payout_amounts(1000, 15)
        assert float(fee15) == 150.0
        assert float(net15) == 850.0

    def test_to_cents(self, app):
        assert to_cents(800) == 80000
        assert to_cents('19.99') == 1999


# ---------------------------------------------------------------------------
# Checkout (CAD + manual capture)
# ---------------------------------------------------------------------------

class TestCheckout:
    def test_checkout_uses_cad_and_manual_capture(self, app):
        _set_stripe_keys()
        session = SimpleNamespace(url='https://checkout.stripe.com/test')
        with patch('stripe.checkout.Session.create', return_value=session) as create:
            url = create_checkout_session(
                'https://x/success', 'https://x/cancel', 103000,
                'Test need', metadata={'bounty_id': 'b1'},
                capture_method='manual',
            )
        assert url == 'https://checkout.stripe.com/test'
        kwargs = create.call_args.kwargs
        assert kwargs['line_items'][0]['price_data']['currency'] == 'cad'
        assert kwargs['payment_intent_data'] == {'capture_method': 'manual'}

    def test_checkout_automatic_by_default(self, app):
        _set_stripe_keys()
        session = SimpleNamespace(url='https://checkout.stripe.com/test')
        with patch('stripe.checkout.Session.create', return_value=session) as create:
            create_checkout_session(
                'https://x/success', 'https://x/cancel', 1000, 'Upgrade'
            )
        assert 'payment_intent_data' not in create.call_args.kwargs

    def test_checkout_returns_none_without_key(self, app):
        assert create_checkout_session(
            'https://x/success', 'https://x/cancel', 1000, 'Test'
        ) is None

    def test_checkout_returns_none_when_disabled(self, app):
        _set_stripe_keys()
        app.config['STRIPE_ENABLED'] = False
        assert create_checkout_session(
            'https://x/success', 'https://x/cancel', 1000, 'Test'
        ) is None


# ---------------------------------------------------------------------------
# Escrow release
# ---------------------------------------------------------------------------

class TestReleaseEscrow:
    def test_free_need_completes_without_stripe(self, app, sample_user, sample_hunter, sample_bounty):
        match = _confirmed_match(app, sample_bounty, sample_hunter, sample_user)
        ok, _msg = release_escrow(match)
        db.session.commit()
        assert ok is True
        assert db.session.get(Match, match.id).payout_status == 'completed'
        assert Payout.query.filter_by(match_id=match.id).first() is None

    def test_release_waits_for_connect_onboarding(self, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        ok, msg = release_escrow(match)
        db.session.commit()
        assert ok is False
        assert 'payout setup' in msg
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout is not None
        assert payout.status == 'pending'
        assert 'Awaiting referrer payout setup' in payout.failure_reason
        assert float(payout.amount) == 800.0
        assert float(payout.platform_fee) == 200.0
        # The referrer is notified (once — on the first attempt).
        notes = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='payout_pending'
        ).all()
        assert len(notes) == 1
        assert db.session.get(Match, match.id).payout_status == 'pending'

    def test_release_captures_and_transfers(self, app, sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _connect_account(app, sample_hunter)
        transfer = SimpleNamespace(id='tr_test_1')
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture') as capture, \
             patch('stripe.Transfer.create', return_value=transfer) as transfer_create:
            ok, msg = release_escrow(match)
            db.session.commit()
        assert ok is True
        assert msg == 'Payout in progress.'
        capture.assert_called_once_with('pi_test_escrow')
        transfer_create.assert_called_once()
        kwargs = transfer_create.call_args.kwargs
        assert kwargs['amount'] == 80000  # $800 net of a $1,000 reward
        assert kwargs['currency'] == 'cad'
        assert kwargs['destination'] == 'acct_test_ready'
        assert kwargs['source_transaction'] == 'pi_test_escrow'
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout.status == 'processing'
        assert payout.stripe_transfer_id == 'tr_test_1'
        assert payout.captured_at is not None
        assert db.session.get(Match, match.id).payout_status == 'processing'

    def test_release_skips_capture_when_already_captured(self, app, sample_user, sample_hunter):
        # Pre-M2 immediate-capture payments have a succeeded PI.
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _connect_account(app, sample_hunter)
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi('succeeded')), \
             patch('stripe.PaymentIntent.capture') as capture, \
             patch('stripe.Transfer.create', return_value=SimpleNamespace(id='tr_2')):
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok is True
        capture.assert_not_called()

    def test_release_founding_referrer_gets_85(self, app, sample_user, sample_hunter):
        _set_stripe_keys()
        hunter = db.session.get(type(sample_hunter), sample_hunter.id)
        hunter.founding_referrer = True
        db.session.commit()
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, hunter, sample_user)
        _connect_account(app, hunter)
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create', return_value=SimpleNamespace(id='tr_3')) as tc:
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok is True
        assert tc.call_args.kwargs['amount'] == 85000
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert float(payout.platform_fee) == 150.0

    def test_release_capture_failure_marks_failed(self, app, sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _connect_account(app, sample_hunter)
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture',
                   side_effect=stripe.error.StripeError('card declined')), \
             patch('stripe.Transfer.create') as tc:
            ok, msg = release_escrow(match)
            db.session.commit()
        assert ok is False
        assert 'capture failed' in msg.lower()
        tc.assert_not_called()
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout.status == 'failed'
        assert payout.captured_at is None
        assert db.session.get(Match, match.id).payout_status == 'failed'

    def test_release_transfer_failure_after_capture(self, app, sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _connect_account(app, sample_hunter)
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create',
                   side_effect=stripe.error.StripeError('no such account')):
            ok, msg = release_escrow(match)
            db.session.commit()
        assert ok is False
        assert 'Transfer failed' in msg
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout.status == 'failed'
        # The money WAS captured — the platform holds the funds.
        assert payout.captured_at is not None
        assert 'after capture' in payout.failure_reason

    def test_release_retry_after_transfer_failure_skips_recapture(self, app, sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _connect_account(app, sample_hunter)
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture') as capture, \
             patch('stripe.Transfer.create',
                   side_effect=stripe.error.StripeError('boom')):
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok is False
        assert capture.call_count == 1
        # Retry: transfer works this time; capture must NOT run again
        # (the PI is already captured — retrieve now says succeeded).
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi('succeeded')), \
             patch('stripe.PaymentIntent.capture') as capture2, \
             patch('stripe.Transfer.create', return_value=SimpleNamespace(id='tr_retry')):
            ok, _msg = release_escrow(db.session.get(Match, match.id))
            db.session.commit()
        assert ok is True
        capture2.assert_not_called()
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout.status == 'processing'

    def test_release_idempotent_when_completed(self, app, sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _connect_account(app, sample_hunter)
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create', return_value=SimpleNamespace(id='tr_4')) as tc:
            release_escrow(match)
            db.session.commit()
            payout = Payout.query.filter_by(match_id=match.id).first()
            payout.set_status('completed')
            db.session.commit()
            ok, msg = release_escrow(db.session.get(Match, match.id))
        assert ok is True
        assert 'already completed' in msg
        assert tc.call_count == 1

    def test_release_requires_dual_confirmation(self, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        pitch = Pitch(bounty_id=bounty.id, hunter_id=sample_hunter.id,
                      candidate_teaser='x' * 60, status='accepted')
        db.session.add(pitch)
        db.session.flush()
        match = Match(bounty_id=bounty.id, pitch_id=pitch.id,
                      hunter_id=sample_hunter.id, employer_id=sample_user.id,
                      hunter_confirmed=True, employer_confirmed=False,
                      payout_status='pending')
        db.session.add(match)
        db.session.commit()
        db.session.refresh(match)
        ok, _msg = release_escrow(match)
        assert ok is False
        assert Payout.query.filter_by(match_id=match.id).first() is None

    def test_release_payments_disabled(self, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _connect_account(app, sample_hunter)
        app.config['STRIPE_ENABLED'] = False
        ok, msg = release_escrow(match)
        db.session.commit()
        assert ok is False
        assert 'disabled' in msg
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout.status == 'pending'
        assert 'disabled' in payout.failure_reason

    def test_invalid_payout_status_rejected(self, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        with pytest.raises(ValueError):
            match.payout_status = 'pending_release'
        with pytest.raises(ValueError):
            Payout(match_id=match.id, referrer_id=sample_hunter.id,
                   bounty_id=bounty.id, gross_amount=1000, platform_fee=200,
                   amount=800, status='pending_release')


# ---------------------------------------------------------------------------
# Connect onboarding
# ---------------------------------------------------------------------------

class TestConnectOnboarding:
    def test_create_connect_account(self, app, sample_hunter):
        _set_stripe_keys()
        with patch('stripe.Account.create',
                   return_value=SimpleNamespace(id='acct_new_1')) as create:
            account, error = create_connect_account(sample_hunter)
            db.session.commit()
        assert error is None
        assert account.stripe_account_id == 'acct_new_1'
        assert account.onboarding_status == 'pending'
        assert account.is_ready is False
        kwargs = create.call_args.kwargs
        assert kwargs['type'] == 'express'
        assert kwargs['country'] == 'CA'

    def test_create_connect_account_returns_existing(self, app, sample_hunter):
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        with patch('stripe.Account.create') as create:
            account, error = create_connect_account(sample_hunter)
        assert error is None
        assert account.stripe_account_id == 'acct_test_ready'
        create.assert_not_called()

    def test_create_account_link(self, app, sample_hunter):
        _set_stripe_keys()
        account = _connect_account(app, sample_hunter, ready=False)
        with patch('stripe.AccountLink.create',
                   return_value=SimpleNamespace(url='https://connect.stripe.com/setup')) as create:
            url = create_account_link(account, 'https://x/return', 'https://x/refresh')
        assert url == 'https://connect.stripe.com/setup'
        assert create.call_args.kwargs['type'] == 'account_onboarding'

    def test_refresh_connect_account_syncs(self, app, sample_hunter):
        _set_stripe_keys()
        account = _connect_account(app, sample_hunter, ready=False)
        stripe_account = SimpleNamespace(
            id='acct_test_ready', charges_enabled=True, payouts_enabled=True,
            details_submitted=True, requirements=SimpleNamespace(currently_due=[]),
        )
        with patch('stripe.Account.retrieve', return_value=stripe_account):
            ok, newly_ready = refresh_connect_account(account)
            db.session.commit()
        assert ok is True
        assert newly_ready is True
        assert account.onboarding_status == 'complete'
        assert account.is_ready is True

    def test_refresh_tracks_requirements_due(self, app, sample_hunter):
        _set_stripe_keys()
        account = _connect_account(app, sample_hunter, ready=False)
        stripe_account = SimpleNamespace(
            id='acct_test_ready', charges_enabled=False, payouts_enabled=False,
            details_submitted=True,
            requirements=SimpleNamespace(currently_due=['individual.id_number']),
        )
        with patch('stripe.Account.retrieve', return_value=stripe_account):
            ok, newly_ready = refresh_connect_account(account)
            db.session.commit()
        assert ok is True
        assert newly_ready is False
        assert account.onboarding_status == 'restricted'
        assert account.requirements_list == ['individual.id_number']

    def test_connect_start_route_redirects_to_stripe(self, client, logged_in_hunter):
        _set_stripe_keys()
        with patch('stripe.Account.create',
                   return_value=SimpleNamespace(id='acct_route_1')), \
             patch('stripe.AccountLink.create',
                   return_value=SimpleNamespace(url='https://connect.stripe.com/setup/abc')):
            resp = client.post('/payments/connect/start', follow_redirects=False)
        assert resp.status_code == 302
        assert 'connect.stripe.com' in resp.headers['Location']

    def test_connect_status_page_renders(self, client, logged_in_hunter):
        resp = client.get('/payments/connect')
        assert resp.status_code == 200
        assert b'Payout setup' in resp.data


# ---------------------------------------------------------------------------
# Connect webhook handlers
# ---------------------------------------------------------------------------

class TestConnectWebhooks:
    def test_account_updated_retries_pending_payouts(self, app, sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        # First release attempt: onboarding incomplete → payout pending.
        _connect_account(app, sample_hunter, ready=False)
        ok, _msg = release_escrow(match)
        db.session.commit()
        assert ok is False
        assert Payout.query.filter_by(match_id=match.id).first().status == 'pending'
        # Now Stripe says the account is ready.
        event_obj = {
            'id': 'acct_test_ready', 'charges_enabled': True,
            'payouts_enabled': True, 'details_submitted': True,
            'requirements': {'currently_due': []},
        }
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create', return_value=SimpleNamespace(id='tr_w1')):
            handle_account_updated(event_obj)
            db.session.commit()
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout.status == 'processing'
        notes = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='connect_ready'
        ).all()
        assert len(notes) == 1

    def test_account_updated_unknown_account(self, app):
        assert handle_account_updated({'id': 'acct_unknown'}) is None

    def test_transfer_paid_completes_payout(self, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        payout = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='processing',
                        stripe_transfer_id='tr_paid_1')
        db.session.add(payout)
        match.set_payout_status('processing')
        db.session.commit()
        handle_transfer_event('transfer.paid', {'id': 'tr_paid_1'})
        db.session.commit()
        assert db.session.get(Payout, payout.id).status == 'completed'
        assert db.session.get(Match, match.id).payout_status == 'completed'
        notes = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='payment_released'
        ).all()
        assert len(notes) == 1

    def test_transfer_paid_idempotent(self, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        payout = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='completed',
                        stripe_transfer_id='tr_paid_2')
        db.session.add(payout)
        db.session.commit()
        handle_transfer_event('transfer.paid', {'id': 'tr_paid_2'})
        handle_transfer_event('transfer.paid', {'id': 'tr_paid_2'})
        db.session.commit()
        notes = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='payment_released'
        ).all()
        assert len(notes) == 0  # already completed — no duplicate notify

    def test_transfer_failed_marks_failed(self, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        payout = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='processing',
                        stripe_transfer_id='tr_fail_1')
        db.session.add(payout)
        db.session.commit()
        handle_transfer_event('transfer.failed', {'id': 'tr_fail_1'})
        db.session.commit()
        assert db.session.get(Payout, payout.id).status == 'failed'
        assert db.session.get(Match, match.id).payout_status == 'failed'
        notes = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='payout_failed'
        ).all()
        assert len(notes) == 1

    def test_transfer_created_is_noop(self, app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        payout = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='processing',
                        stripe_transfer_id='tr_created_1')
        db.session.add(payout)
        db.session.commit()
        handle_transfer_event('transfer.created', {'id': 'tr_created_1'})
        db.session.commit()
        assert db.session.get(Payout, payout.id).status == 'processing'

    def test_payout_paid_records_last_payout(self, app, sample_hunter):
        _connect_account(app, sample_hunter)
        handle_payout_event('payout.paid',
                            {'amount': 80000, 'arrival_date': 1793299200},
                            connected_account_id='acct_test_ready')
        db.session.commit()
        account = ConnectAccount.query.filter_by(user_id=sample_hunter.id).first()
        assert float(account.last_payout_amount) == 800.0
        assert account.last_payout_at is not None

    def test_payout_failed_notifies(self, app, sample_hunter):
        _connect_account(app, sample_hunter)
        handle_payout_event('payout.failed', {'failure_code': 'account_closed'},
                            connected_account_id='acct_test_ready')
        db.session.commit()
        notes = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='payout_failed'
        ).all()
        assert len(notes) == 1
        assert 'account_closed' in notes[0].body

    def test_webhook_route_dispatches_connect_events(self, client, app, sample_hunter):
        _set_stripe_keys()
        _connect_account(app, sample_hunter, ready=False)
        event = {
            'id': 'evt_1', 'type': 'account.updated',
            'data': {'object': {
                'id': 'acct_test_ready', 'charges_enabled': True,
                'payouts_enabled': True, 'details_submitted': True,
                'requirements': {'currently_due': []},
            }},
        }
        with patch('stripe.Webhook.construct_event', return_value=event):
            resp = client.post('/bounties/stripe-webhook',
                               data=b'{}',
                               headers={'Stripe-Signature': 't=1,v1=fake'})
        assert resp.status_code == 200
        account = ConnectAccount.query.filter_by(user_id=sample_hunter.id).first()
        assert account.onboarding_status == 'complete'

    def test_webhook_route_rejects_when_disabled(self, client, app):
        _set_stripe_keys()
        app.config['STRIPE_ENABLED'] = False
        resp = client.post('/bounties/stripe-webhook',
                           data=b'{}',
                           headers={'Stripe-Signature': 't=1,v1=fake'})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Payout dashboard
# ---------------------------------------------------------------------------

class TestPayoutDashboard:
    def test_dashboard_shows_payouts(self, client, logged_in_hunter, app,
                                     sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        payout = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='pending')
        db.session.add(payout)
        db.session.commit()
        resp = client.get('/payments/payouts')
        assert resp.status_code == 200
        assert b'800.00' in resp.data
        assert b'Pending' in resp.data

    def test_dashboard_requires_login(self, client):
        resp = client.get('/payments/payouts', follow_redirects=False)
        assert resp.status_code == 302

    def test_dashboard_hides_other_referrers_payouts(self, client, logged_in_user,
                                                      app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        payout = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='pending')
        db.session.add(payout)
        db.session.commit()
        resp = client.get('/payments/payouts')
        assert resp.status_code == 200
        assert b'800.00' not in resp.data

    def test_retry_payout_route(self, client, logged_in_hunter, app,
                                sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        payout = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='failed',
                        failure_reason='Transfer failed')
        db.session.add(payout)
        db.session.commit()
        _connect_account(app, sample_hunter)
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create', return_value=SimpleNamespace(id='tr_retry_route')):
            resp = client.post(f'/payments/payouts/{payout.id}/retry',
                               follow_redirects=True)
        assert resp.status_code == 200
        assert db.session.get(Payout, payout.id).status == 'processing'


# ---------------------------------------------------------------------------
# Manual payouts
# ---------------------------------------------------------------------------

class TestManualPayouts:
    def test_record_manual_payout(self, app, sample_user, sample_hunter, sample_admin):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi('succeeded')):
            payout, note = record_manual_payout(
                match, 800, 'Interac e-Transfer ref 123', sample_admin
            )
            db.session.commit()
        assert payout.status == 'completed'
        assert payout.method == 'manual'
        assert float(payout.amount) == 800.0
        assert payout.manual_reference == 'Interac e-Transfer ref 123'
        assert payout.recorded_by_id == sample_admin.id
        assert db.session.get(Match, match.id).payout_status == 'completed'
        notes = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='payment_released'
        ).all()
        assert len(notes) == 1

    def test_record_manual_payout_works_with_payments_disabled(self, app, sample_user,
                                                               sample_hunter, sample_admin):
        # The interim path exists precisely for when Stripe is off.
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        app.config['STRIPE_ENABLED'] = False
        payout, _note = record_manual_payout(match, 800, 'cash', sample_admin)
        db.session.commit()
        assert payout.status == 'completed'
        assert db.session.get(Match, match.id).payout_status == 'completed'

    def test_record_manual_payout_capture_failure_does_not_block(self, app, sample_user,
                                                                 sample_hunter, sample_admin):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        with patch('stripe.PaymentIntent.retrieve',
                   side_effect=stripe.error.StripeError('no such PI')):
            payout, note = record_manual_payout(match, 800, 'ref', sample_admin)
            db.session.commit()
        assert payout.status == 'completed'
        assert note is not None
        assert 'capture failed' in note

    def test_admin_record_manual_route(self, client, logged_in_admin, app,
                                       sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        resp = client.post(f'/admin/payouts/{match.id}/record-manual',
                           data={'amount': '800', 'reference': 'Interac 42'},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert b'Manual payout of $800.00 CAD recorded' in resp.data
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout.status == 'completed'
        assert payout.method == 'manual'

    def test_admin_record_manual_requires_confirmed_match(self, client, logged_in_admin,
                                                          app, sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
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
        db.session.refresh(match)
        resp = client.post(f'/admin/payouts/{match.id}/record-manual',
                           data={'amount': '800'}, follow_redirects=True)
        assert b'Only a dual-confirmed match' in resp.data
        assert Payout.query.filter_by(match_id=match.id).first() is None

    def test_admin_payouts_page_renders(self, client, logged_in_admin):
        resp = client.get('/admin/payouts')
        assert resp.status_code == 200
        assert b'Payouts' in resp.data


# ---------------------------------------------------------------------------
# Fail-closed payments-off behavior
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_secure_payment_fails_closed_when_disabled(self, client, logged_in_user,
                                                       app, sample_paid_bounty):
        app.config['STRIPE_ENABLED'] = False
        resp = client.get(f'/needs/{sample_paid_bounty.id}/secure-payment',
                          follow_redirects=True)
        assert b'Payments are currently unavailable' in resp.data
        bounty = db.session.get(Bounty, sample_paid_bounty.id)
        assert bounty.payment_status == 'unsecured'  # NOT marked secured

    def test_secure_third_party_fails_closed_when_disabled(self, client, logged_in_user,
                                                           app, sample_user):
        app.config['STRIPE_ENABLED'] = False
        bounty = Bounty(
            poster_id=sample_user.id, title='Third party need',
            description='x' * 60, bounty_type='recruitment',
            bounty_direction='seeking_opportunity', reward_amount=1000,
            status='open', payment_status='unsecured',
            payer_type='third_party', location='Remote',
        )
        db.session.add(bounty)
        db.session.commit()
        db.session.refresh(bounty)
        resp = client.get(f'/needs/{bounty.id}/secure-third-party',
                          follow_redirects=True)
        assert b'Payments are currently unavailable' in resp.data
        assert db.session.get(Bounty, bounty.id).payment_status == 'unsecured'

    def test_upgrade_fails_closed_when_disabled(self, client, logged_in_user,
                                                app, sample_bounty):
        app.config['STRIPE_ENABLED'] = False
        resp = client.post(f'/needs/{sample_bounty.id}/upgrade',
                           data={'upgrade_type': 'highlight', 'duration_days': '7'},
                           follow_redirects=True)
        assert b'Payments are currently unavailable' in resp.data
        from models.bounty_upgrade import BountyUpgrade
        assert BountyUpgrade.query.filter_by(bounty_id=sample_bounty.id).count() == 0

    def test_cancel_refund_refuses_secured_when_disabled(self, client, logged_in_user,
                                                         app, sample_user):
        app.config['STRIPE_ENABLED'] = False
        bounty = _secured_paid_bounty(app, sample_user)
        resp = client.post(f'/needs/{bounty.id}/cancel-refund',
                           follow_redirects=True)
        assert b'cannot be released right now' in resp.data
        bounty = db.session.get(Bounty, bounty.id)
        assert bounty.payment_status == 'secured'
        assert bounty.status == 'open'

    def test_cancel_refund_closes_unsecured_when_disabled(self, client, logged_in_user,
                                                          app, sample_paid_bounty):
        app.config['STRIPE_ENABLED'] = False
        resp = client.post(f'/needs/{sample_paid_bounty.id}/cancel-refund',
                           follow_redirects=True)
        assert resp.status_code == 200
        assert db.session.get(Bounty, sample_paid_bounty.id).status == 'closed'

    def test_cancel_refund_cancels_uncaptured_intent(self, client, logged_in_user,
                                                     app, sample_user):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        with patch('stripe.PaymentIntent.retrieve', return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.cancel') as cancel, \
             patch('stripe.Refund.create') as refund:
            resp = client.post(f'/needs/{bounty.id}/cancel-refund',
                               follow_redirects=True)
        assert resp.status_code == 200
        cancel.assert_called_once_with('pi_test_escrow')
        refund.assert_not_called()
        assert db.session.get(Bounty, bounty.id).payment_status == 'refunded'

    def test_cancel_refund_refunds_captured_intent(self, client, logged_in_user,
                                                   app, sample_user):
        _set_stripe_keys()
        bounty = _secured_paid_bounty(app, sample_user)
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.PaymentIntent.cancel') as cancel, \
             patch('stripe.Refund.create',
                   return_value=SimpleNamespace(id='re_1')) as refund:
            resp = client.post(f'/needs/{bounty.id}/cancel-refund',
                               follow_redirects=True)
        assert resp.status_code == 200
        cancel.assert_not_called()
        refund.assert_called_once()
        # $1,000 minus Stripe's 2.9% + $0.30 = $970.70 → 97070 cents.
        assert refund.call_args.kwargs['amount'] == 97070


# ---------------------------------------------------------------------------
# Confirm-match integration (escrow release via the route)
# ---------------------------------------------------------------------------

class TestConfirmMatchIntegration:
    def test_confirm_match_locks_in_20_percent_fee(self, client, login_as, app,
                                                   sample_user, sample_hunter):
        bounty = _secured_paid_bounty(app, sample_user)
        pitch = Pitch(bounty_id=bounty.id, hunter_id=sample_hunter.id,
                      candidate_teaser='x' * 60, status='interested')
        db.session.add(pitch)
        db.session.commit()
        db.session.refresh(pitch)
        login_as(client, 'hunter@example.com', 'HunterPass123!')
        client.post(f'/needs/pitches/{pitch.id}/confirm-match', follow_redirects=True)
        login_as(client, 'testuser@example.com', 'TestPass123!')
        client.post(f'/needs/pitches/{pitch.id}/confirm-match', follow_redirects=True)
        match = Match.query.filter_by(pitch_id=pitch.id).first()
        assert float(match.platform_fee) == 200.0  # 20% of $1,000, not 12%
        assert match.payout_status == 'pending'  # no Connect account yet
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout is not None
        assert float(payout.amount) == 800.0

    def test_confirm_match_free_need_completes_payout(self, client, login_as, app,
                                                      sample_bounty, sample_hunter):
        pitch = Pitch(bounty_id=sample_bounty.id, hunter_id=sample_hunter.id,
                      candidate_teaser='x' * 60, status='interested')
        db.session.add(pitch)
        db.session.commit()
        db.session.refresh(pitch)
        login_as(client, 'hunter@example.com', 'HunterPass123!')
        client.post(f'/needs/pitches/{pitch.id}/confirm-match', follow_redirects=True)
        login_as(client, 'testuser@example.com', 'TestPass123!')
        client.post(f'/needs/pitches/{pitch.id}/confirm-match', follow_redirects=True)
        match = Match.query.filter_by(pitch_id=pitch.id).first()
        assert match.payout_status == 'completed'
