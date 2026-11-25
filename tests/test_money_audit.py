"""Money audit regression tests (2026-11-24) — M1..M13.

Every test proves one fixed bug from the money audit of the
Paid2Match codebase. All Stripe calls are mocked
(unittest.mock.patch on the stripe module) — no network calls, no
real keys, no real money movement.
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import stripe
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (
    AdminSettings, ConnectAccount, Match, Payout,
    ProSubscription, RecurringBump, utc_now,
)
from models.bounty import Bounty
from models.bounty_upgrade import BountyUpgrade
from models.dispute import Dispute
from models.notification import Notification
from models.pitch import Pitch
from models.stripe_event import StripeEventLog
from models.user import User
from tests.test_bumps import _make_member, _old_post
from utils.payments import (
    cancel_authorization,
    ensure_captured,
    handle_transfer_event,
    record_manual_payout,
    release_escrow,
    to_cents,
)
from utils.pro import create_pro_checkout


# ---------------------------------------------------------------------------
# Helpers (mirroring tests/test_payments.py)
# ---------------------------------------------------------------------------

def _set_stripe_keys():
    AdminSettings.set('STRIPE_SECRET_KEY', 'sk_test_fake')
    AdminSettings.set('STRIPE_PUBLISHABLE_KEY', 'pk_test_fake')
    AdminSettings.set('STRIPE_WEBHOOK_SECRET', 'whsec_fake')
    db.session.commit()


def _paid_bounty(app, poster, reward=1000, secured=True, currency='cad'):
    bounty = Bounty(
        poster_id=poster.id,
        title='Paid need',
        description='x' * 60,
        bounty_type='recruitment',
        bounty_direction='seeking_opportunity',
        reward_amount=reward,
        currency=currency,
        status='open',
        payment_status='secured' if secured else 'unsecured',
        stripe_payment_intent='pi_test_escrow' if secured else None,
        location='Remote',
    )
    db.session.add(bounty)
    db.session.commit()
    db.session.refresh(bounty)
    return bounty


def _confirmed_match(app, bounty, hunter, poster, fee=None):
    """A dual-confirmed match. fee=None leaves platform_fee /
    payout_amount unset (a legacy match with no locked fee)."""
    pitch = Pitch(bounty_id=bounty.id, hunter_id=hunter.id,
                  candidate_teaser='x' * 60, status='accepted')
    db.session.add(pitch)
    db.session.flush()
    match = Match(
        bounty_id=bounty.id, pitch_id=pitch.id,
        hunter_id=hunter.id, employer_id=poster.id,
        hunter_confirmed=True, employer_confirmed=True,
        payout_amount=(float(bounty.reward_amount or 0)
                       if fee is not None else None),
        platform_fee=fee,
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


def _post_event(client, event):
    with patch('stripe.Webhook.construct_event', return_value=event):
        return client.post(
            '/bounties/stripe-webhook',
            data=b'{}',
            headers={'Stripe-Signature': 't=1,v1=fake'},
        )


def _mock_checkout_session():
    return SimpleNamespace(id='cs_test_money', url='https://checkout.test/x')


# ---------------------------------------------------------------------------
# M2 — Stripe idempotency keys
# ---------------------------------------------------------------------------

class TestIdempotencyKeys:
    def test_transfer_uses_payout_idempotency_key(self, app, sample_user,
                                                  sample_hunter):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        _connect_account(app, sample_hunter)
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_1')) as transfer:
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok
        payout = Payout.query.filter_by(match_id=match.id).first()
        transfer.assert_called_once()
        assert transfer.call_args.kwargs['idempotency_key'] == (
            f'payout-{payout.id}'
        )

    def test_refund_uses_idempotency_key(self, client, logged_in_user,
                                         app, sample_user):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user)
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.Refund.create',
                   return_value=SimpleNamespace(id='re_1')) as refund:
            resp = client.post(f'/needs/{bounty.id}/cancel-refund',
                               follow_redirects=True)
        assert resp.status_code == 200
        refund.assert_called_once()
        # $1,000 minus Stripe's 2.9% + $0.30 = $970.70 → 97070 cents.
        assert refund.call_args.kwargs['amount'] == 97070
        assert refund.call_args.kwargs['idempotency_key'] == (
            'refund-pi_test_escrow-97070'
        )

    def test_secure_payment_checkout_uses_idempotency_key(
            self, client, logged_in_user, app, sample_user):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, secured=False)
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            resp = client.get(f'/needs/{bounty.id}/secure-payment')
        assert resp.status_code == 302
        create.assert_called_once()
        assert create.call_args.kwargs['idempotency_key'] == (
            f'escrow-{bounty.id}-unsecured'
        )

    def test_pro_checkout_uses_idempotency_key(self, app, sample_user):
        _set_stripe_keys()
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            url = create_pro_checkout(
                sample_user, 'https://x.test/ok', 'https://x.test/cancel')
        assert url == 'https://checkout.test/x'
        create.assert_called_once()
        assert create.call_args.kwargs['idempotency_key'] == (
            f'pro-{sample_user.id}'
        )

    def test_recurring_bump_subscribe_uses_idempotency_key(
            self, client, app, sample_user, sample_hunter, login_as):
        _set_stripe_keys()
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        login_as(client, 'testuser@example.com', 'TestPass123!')
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            resp = client.post(
                f'/payments/bumps/recurring/{post.id}/subscribe')
        assert resp.status_code == 302
        create.assert_called_once()
        assert create.call_args.kwargs['idempotency_key'] == (
            f'recurring-bump-{post.id}'
        )


# ---------------------------------------------------------------------------
# M7 — cent math (no float truncation)
# ---------------------------------------------------------------------------

class TestToCents:
    def test_to_cents_rounds_half_up(self):
        # The float trap: int(1029.9897 * 100) == 102998.
        assert int(1029.9897 * 100) == 102998
        assert to_cents(1029.9897) == 102999
        assert to_cents(30.8897) == 3089
        assert to_cents(77.8165) == 7782

    def test_secure_payment_charges_exact_cents(
            self, client, logged_in_user, app, sample_user):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, reward=999.99,
                              secured=False)
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            client.get(f'/needs/{bounty.id}/secure-payment')
        params = create.call_args.kwargs
        unit_amount = params['line_items'][0]['price_data']['unit_amount']
        # $999.99 + 3% = $1,029.9897 → 102999 cents, not 102998.
        assert unit_amount == 102999


# ---------------------------------------------------------------------------
# M3 — unique constraints + race fallbacks
# ---------------------------------------------------------------------------

class TestUniqueConstraintsAndRaces:
    def test_duplicate_match_rejected(self, app, sample_user,
                                      sample_hunter):
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        dup = Match(
            bounty_id=bounty.id, pitch_id=match.pitch_id,
            hunter_id=sample_hunter.id, employer_id=sample_user.id,
            payout_status='pending',
        )
        db.session.add(dup)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert Match.query.filter_by(pitch_id=match.pitch_id).count() == 1

    def test_duplicate_payout_rejected(self, app, sample_user,
                                       sample_hunter):
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        first = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                       bounty_id=bounty.id, gross_amount=1000,
                       platform_fee=200, amount=800, status='pending')
        db.session.add(first)
        db.session.commit()
        dup = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                     bounty_id=bounty.id, gross_amount=1000,
                     platform_fee=200, amount=800, status='pending')
        db.session.add(dup)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert Payout.query.filter_by(match_id=match.id,
                                      kind='referral').count() == 1

    def test_payout_creation_race_falls_back_to_winner(
            self, app, sample_user, sample_hunter):
        """Two simultaneous releases: the loser uses the winner's
        payout row instead of 500ing or double-paying."""
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        _connect_account(app, sample_hunter)
        winner = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='pending')
        db.session.add(winner)
        db.session.commit()
        # The initial lookups miss (the winner "hasn't committed
        # yet"), the flush collides, the re-lookups find the winner.
        mock_query = MagicMock()
        mock_query.filter_by.return_value.first.side_effect = [
            None, None, winner, None,
        ]
        real_flush = db.session().flush
        flush_calls = {'raised': False}

        def _flush_once(*args, **kwargs):
            # Raise only on the creation flush (a new Payout is
            # pending) — plain queries autoflush too, and those must
            # go through.
            if not flush_calls['raised'] and any(
                isinstance(obj, Payout) for obj in db.session.new
            ):
                flush_calls['raised'] = True
                raise IntegrityError('dup', {}, Exception('uq_payouts'))
            return real_flush(*args, **kwargs)

        with patch.object(Payout, 'query', mock_query), \
             patch.object(db.session(), 'flush', _flush_once), \
             patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_1')) as transfer:
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok
        assert Payout.query.filter_by(match_id=match.id,
                                      kind='referral').count() == 1
        transfer.assert_called_once()
        assert transfer.call_args.kwargs['idempotency_key'] == (
            f'payout-{winner.id}'
        )

    def test_payout_creation_reraises_when_no_winner(
            self, app, sample_user, sample_hunter):
        """When the flush fails for a reason other than a race (no
        winner row exists), the error propagates — it must not
        silently return (None, None) and crash release_escrow."""
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        mock_query = MagicMock()
        mock_query.filter_by.return_value.first.side_effect = [
            None, None, None,
        ]
        with patch.object(Payout, 'query', mock_query), \
             patch.object(
                 db.session(), 'flush',
                 side_effect=IntegrityError('boom', {}, Exception('boom'))):
            with pytest.raises(IntegrityError):
                release_escrow(match)
        db.session.rollback()

    def test_manual_payout_race_falls_back_to_winner(
            self, app, sample_user, sample_hunter, sample_admin):
        _set_stripe_keys()
        app.config['STRIPE_ENABLED'] = False  # skip escrow capture
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        winner = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='pending')
        db.session.add(winner)
        db.session.commit()
        mock_query = MagicMock()
        mock_query.filter_by.return_value.first.side_effect = [None, winner]
        real_flush = db.session().flush
        flush_calls = {'raised': False}

        def _flush_once(*args, **kwargs):
            # Raise only on the creation flush (a new Payout is
            # pending) — plain queries autoflush too, and those must
            # go through.
            if not flush_calls['raised'] and any(
                isinstance(obj, Payout) for obj in db.session.new
            ):
                flush_calls['raised'] = True
                raise IntegrityError('dup', {}, Exception('uq_payouts'))
            return real_flush(*args, **kwargs)

        with patch.object(Payout, 'query', mock_query), \
             patch.object(db.session(), 'flush', _flush_once):
            payout, _note = record_manual_payout(
                match, Decimal('800'), 'ref-1', sample_admin)
            db.session.commit()
        assert payout.id == winner.id
        assert Payout.query.filter_by(match_id=match.id,
                                      kind='referral').count() == 1

    def test_confirm_match_race_falls_back_to_winner(
            self, client, app, sample_user, sample_hunter, login_as):
        """Two simultaneous first-confirmations: the loser uses the
        winner's match instead of 500ing. The loser's commit fails
        on the real unique constraint (the winner is already in the
        DB); the route's first lookup is mocked to miss so the race
        path is exercised."""
        bounty = _paid_bounty(app, sample_user)
        pitch = Pitch(bounty_id=bounty.id, hunter_id=sample_hunter.id,
                      candidate_teaser='x' * 60, status='interested')
        db.session.add(pitch)
        db.session.commit()
        db.session.refresh(pitch)
        winner = Match(
            bounty_id=bounty.id, pitch_id=pitch.id,
            hunter_id=sample_hunter.id, employer_id=sample_user.id,
            payout_amount=1000, platform_fee=200,
            payout_status='pending',
        )
        db.session.add(winner)
        db.session.commit()
        # The route's first match lookup misses, the re-lookup after
        # the failed commit finds the winner.
        mock_query = MagicMock()
        mock_query.filter_by.return_value.first.side_effect = [
            None, winner,
        ]
        login_as(client, 'hunter@example.com', 'HunterPass123!')
        with patch.object(Match, 'query', mock_query):
            resp = client.post(
                f'/needs/pitches/{pitch.id}/confirm-match')
        assert resp.status_code in (200, 302)
        assert Match.query.filter_by(pitch_id=pitch.id).count() == 1
        # The route continued with the winner match (it set the
        # hunter's confirmation on it). The flag is in-memory here
        # because the mock returns the test session's object; in
        # production the re-query returns a request-session object
        # and the route's commit persists it.
        assert winner.hunter_confirmed is True


# ---------------------------------------------------------------------------
# M1 — cancel-refund must never refund a completed hire
# ---------------------------------------------------------------------------

class TestCancelRefundGuard:
    def test_cancel_refund_refuses_completed_bounty(
            self, client, logged_in_user, app, sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user)
        _confirmed_match(app, bounty, sample_hunter, sample_user, fee=200)
        bounty.status = 'completed'
        db.session.commit()
        with patch('stripe.PaymentIntent.retrieve') as retrieve, \
             patch('stripe.PaymentIntent.cancel') as cancel, \
             patch('stripe.Refund.create') as refund:
            resp = client.post(f'/needs/{bounty.id}/cancel-refund',
                               follow_redirects=True)
        assert resp.status_code == 200
        assert b'already has a completed hire' in resp.data
        retrieve.assert_not_called()
        cancel.assert_not_called()
        refund.assert_not_called()
        bounty = db.session.get(Bounty, bounty.id)
        assert bounty.payment_status == 'secured'
        assert bounty.status == 'completed'

    def test_cancel_refund_refuses_when_payout_exists(
            self, client, logged_in_user, app, sample_user, sample_hunter):
        """Even without a dual-confirmed match, an existing payout row
        means money already moved — no refund."""
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user)
        pitch = Pitch(bounty_id=bounty.id, hunter_id=sample_hunter.id,
                      candidate_teaser='x' * 60, status='accepted')
        db.session.add(pitch)
        db.session.flush()
        match = Match(
            bounty_id=bounty.id, pitch_id=pitch.id,
            hunter_id=sample_hunter.id, employer_id=sample_user.id,
            hunter_confirmed=True, employer_confirmed=False,
            payout_status='pending',
        )
        db.session.add(match)
        db.session.flush()
        payout = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='pending')
        db.session.add(payout)
        db.session.commit()
        with patch('stripe.Refund.create') as refund:
            resp = client.post(f'/needs/{bounty.id}/cancel-refund',
                               follow_redirects=True)
        assert b'already has a completed hire' in resp.data
        refund.assert_not_called()
        assert db.session.get(Bounty, bounty.id).status == 'open'


# ---------------------------------------------------------------------------
# M4 — fee locked at match creation
# ---------------------------------------------------------------------------

class TestFeeLock:
    def test_release_honors_fee_locked_at_match_creation(
            self, app, sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        _connect_account(app, sample_hunter)
        # The admin raises the fee AFTER the match was created.
        AdminSettings.set('PLATFORM_FEE_PERCENT', '30')
        db.session.commit()
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_1')):
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert float(payout.platform_fee) == 200.0  # locked 20%, not 30%
        assert float(payout.amount) == 800.0
        assert float(db.session.get(Match, match.id).platform_fee) == 200.0

    def test_manual_payout_honors_locked_fee(
            self, app, sample_user, sample_hunter, sample_admin):
        app.config['STRIPE_ENABLED'] = False  # skip escrow capture
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        AdminSettings.set('PLATFORM_FEE_PERCENT', '30')
        db.session.commit()
        payout, _note = record_manual_payout(
            match, Decimal('800'), 'ref-1', sample_admin)
        db.session.commit()
        assert float(payout.platform_fee) == 200.0
        assert float(payout.gross_amount) == 1000.0

    def test_legacy_match_without_locked_fee_recomputes(
            self, app, sample_user, sample_hunter):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=None)
        _connect_account(app, sample_hunter)
        AdminSettings.set('PLATFORM_FEE_PERCENT', '30')
        db.session.commit()
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_1')):
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert float(payout.platform_fee) == 300.0
        assert float(payout.amount) == 700.0


# ---------------------------------------------------------------------------
# M5 — open dispute freezes payouts
# ---------------------------------------------------------------------------

class TestDisputeFreeze:
    def _open_dispute(self, match, raised_by):
        dispute = Dispute(match_id=match.id, raised_by_id=raised_by.id,
                          reason='The candidate never started.',
                          status='open')
        db.session.add(dispute)
        db.session.commit()
        return dispute

    def test_open_dispute_blocks_release(self, app, sample_user,
                                         sample_hunter):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        _connect_account(app, sample_hunter)
        self._open_dispute(match, sample_user)
        with patch('stripe.Transfer.create') as transfer, \
             patch('stripe.PaymentIntent.retrieve') as retrieve:
            ok, msg = release_escrow(match)
            db.session.commit()
        assert not ok
        assert 'open dispute' in msg
        transfer.assert_not_called()
        retrieve.assert_not_called()
        assert Payout.query.filter_by(match_id=match.id).count() == 0

    def test_open_dispute_blocks_manual_payout(
            self, app, sample_user, sample_hunter, sample_admin):
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        self._open_dispute(match, sample_user)
        with pytest.raises(ValueError, match='open dispute'):
            record_manual_payout(match, Decimal('800'), 'ref-1',
                                 sample_admin)
        db.session.rollback()

    def test_resolved_dispute_allows_release(self, app, sample_user,
                                             sample_hunter):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        _connect_account(app, sample_hunter)
        dispute = self._open_dispute(match, sample_user)
        dispute.status = 'resolved'
        db.session.commit()
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_1')):
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok


# ---------------------------------------------------------------------------
# M6 — webhook event-id dedup
# ---------------------------------------------------------------------------

class TestWebhookDedup:
    def test_duplicate_extend_event_extends_once(
            self, client, app, sample_user):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, secured=False)
        starts = utc_now() - timedelta(days=1)
        expires = starts + timedelta(days=7)
        upgrade = BountyUpgrade(
            bounty_id=bounty.id, upgrade_type='highlight',
            duration_days=7, price_paid=7,
            stripe_session_id='cs_orig', is_active=True,
            starts_at=starts, expires_at=expires,
        )
        db.session.add(upgrade)
        db.session.commit()
        event = {
            'id': 'evt_ext_money_1', 'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_ext_money_1',
                'payment_intent': 'pi_ext_1',
                'metadata': {
                    'bounty_id': bounty.id,
                    'upgrade_session': 'true',
                    'upgrade_type': 'highlight',
                    'duration_days': '7',
                    'total_price': '7',
                    'extend_upgrade_id': upgrade.id,
                },
            }},
        }
        resp = _post_event(client, event)
        assert resp.status_code == 200
        resp = _post_event(client, event)
        assert resp.status_code == 200
        upgrade = db.session.get(BountyUpgrade, upgrade.id)
        # Extended by exactly 7 days once — not 14. (SQLite stores
        # datetimes naive, so compare naive.)
        expected = expires.replace(tzinfo=None) + timedelta(days=7)
        assert upgrade.expires_at == expected
        assert float(upgrade.price_paid) == 14.0
        assert StripeEventLog.query.filter_by(
            event_id='evt_ext_money_1').count() == 1

    def test_duplicate_bounty_checkout_notifies_once(
            self, client, app, sample_user):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, secured=False)
        event = {
            'id': 'evt_checkout_money_1',
            'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_checkout_money_1',
                'payment_intent': 'pi_checkout_1',
                'metadata': {
                    'bounty_id': bounty.id,
                    'transaction_fee': '30.0',
                },
            }},
        }
        assert _post_event(client, event).status_code == 200
        assert _post_event(client, event).status_code == 200
        bounty = db.session.get(Bounty, bounty.id)
        assert bounty.payment_status == 'secured'
        assert bounty.stripe_payment_intent == 'pi_checkout_1'
        notes = Notification.query.filter_by(
            recipient_id=sample_user.id, type='payment_secured').all()
        assert len(notes) == 1
        assert StripeEventLog.query.filter_by(
            event_id='evt_checkout_money_1').count() == 1

    def test_event_log_records_processed_events(
            self, client, app, sample_hunter):
        _set_stripe_keys()
        _connect_account(app, sample_hunter, ready=False)
        event = {
            'id': 'evt_log_money_1', 'type': 'account.updated',
            'data': {'object': {
                'id': 'acct_test_ready', 'charges_enabled': True,
                'payouts_enabled': True, 'details_submitted': True,
                'requirements': {'currently_due': []},
            }},
        }
        assert _post_event(client, event).status_code == 200
        row = StripeEventLog.query.filter_by(
            event_id='evt_log_money_1').first()
        assert row is not None
        assert row.event_type == 'account.updated'


# ---------------------------------------------------------------------------
# M9 — ensure_captured / cancel_authorization load the API key
# ---------------------------------------------------------------------------

class TestApiKeyLoading:
    def test_ensure_captured_loads_api_key(self, app):
        _set_stripe_keys()
        stripe.api_key = None
        try:
            with patch('stripe.PaymentIntent.retrieve',
                       return_value=_mock_pi()), \
                 patch('stripe.PaymentIntent.capture',
                       return_value=_mock_pi('succeeded')):
                ok, _err = ensure_captured('pi_test_escrow')
            assert ok
            assert stripe.api_key == 'sk_test_fake'
        finally:
            stripe.api_key = None

    def test_ensure_captured_fails_closed_without_keys(self, app):
        stripe.api_key = None
        try:
            with patch('stripe.PaymentIntent.retrieve') as retrieve:
                ok, err = ensure_captured('pi_test_escrow')
            assert not ok
            assert err == 'Stripe is not configured.'
            retrieve.assert_not_called()
        finally:
            stripe.api_key = None

    def test_cancel_authorization_loads_api_key(self, app):
        _set_stripe_keys()
        stripe.api_key = None
        try:
            with patch('stripe.PaymentIntent.retrieve',
                       return_value=_mock_pi()), \
                 patch('stripe.PaymentIntent.cancel') as cancel:
                ok, _err = cancel_authorization('pi_test_escrow')
            assert ok
            assert stripe.api_key == 'sk_test_fake'
            cancel.assert_called_once_with('pi_test_escrow')
        finally:
            stripe.api_key = None

    def test_cancel_authorization_fails_closed_without_keys(self, app):
        stripe.api_key = None
        try:
            with patch('stripe.PaymentIntent.retrieve') as retrieve:
                ok, err = cancel_authorization('pi_test_escrow')
            assert not ok
            assert err == 'Stripe is not configured.'
            retrieve.assert_not_called()
        finally:
            stripe.api_key = None


# ---------------------------------------------------------------------------
# M8 — delete-account stops the user's subscriptions
# ---------------------------------------------------------------------------

class TestDeleteAccountCancelsSubscriptions:
    def _paid_user(self, app, user):
        pro = ProSubscription(user_id=user.id,
                              stripe_subscription_id='sub_pro_money_1',
                              status='active')
        db.session.add(pro)
        post = _old_post(app, user)
        recurring = RecurringBump(
            post_id=post.id, user_id=user.id,
            stripe_subscription_id='sub_bump_money_1', status='active',
            next_bump_at=utc_now() + timedelta(days=3),
        )
        db.session.add(recurring)
        db.session.commit()
        return pro, recurring

    def test_delete_cancels_pro_and_recurring_bump(
            self, client, logged_in_user, app, sample_user):
        _set_stripe_keys()
        pro, recurring = self._paid_user(app, sample_user)
        with patch('stripe.Subscription.modify',
                   return_value=SimpleNamespace(id='sub_x')) as modify:
            resp = client.post('/auth/delete-account',
                               data={'confirmation': 'DELETE'},
                               follow_redirects=True)
        assert resp.status_code == 200
        assert db.session.get(
            ProSubscription, pro.id).cancel_at_period_end is True
        assert db.session.get(
            RecurringBump, recurring.id).cancel_at_period_end is True
        modified = {c.args[0] for c in modify.call_args_list}
        assert modified == {'sub_pro_money_1', 'sub_bump_money_1'}
        user = db.session.get(User, sample_user.id)
        assert user.is_deleted is True

    def test_delete_proceeds_when_stripe_fails(
            self, client, logged_in_user, app, sample_user):
        _set_stripe_keys()
        pro, _recurring = self._paid_user(app, sample_user)
        with patch('stripe.Subscription.modify',
                   side_effect=stripe.error.StripeError('boom')):
            resp = client.post('/auth/delete-account',
                               data={'confirmation': 'DELETE'},
                               follow_redirects=True)
        assert resp.status_code == 200
        user = db.session.get(User, sample_user.id)
        assert user.is_deleted is True
        # The cancel did not go through — the row still shows it,
        # so an admin can cancel it from the Stripe dashboard.
        assert db.session.get(
            ProSubscription, pro.id).cancel_at_period_end is not True


# ---------------------------------------------------------------------------
# M10 — CAD disclosure
# ---------------------------------------------------------------------------

class TestCadDisclosure:
    def test_secure_payment_description_names_cad(
            self, client, logged_in_user, app, sample_user):
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, reward=1000,
                              secured=False, currency='eur')
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            client.get(f'/needs/{bounty.id}/secure-payment')
        name = create.call_args.kwargs['line_items'][0]['price_data']['product_data']['name']
        assert '(CAD)' in name
        assert 'CA$' in name

    def test_detail_page_shows_cad_note_for_eur_bounty(
            self, client, logged_in_user, app, sample_user):
        bounty = _paid_bounty(app, sample_user, reward=1000,
                              secured=False, currency='eur')
        resp = client.get(f'/needs/{bounty.url_slug}')
        assert resp.status_code == 200
        assert b'Canadian dollars (CAD)' in resp.data

    def test_post_form_shows_cad_note(
            self, client, logged_in_user, app):
        resp = client.get('/needs/post')
        assert resp.status_code == 200
        assert b'Payments are processed in Canadian dollars (CAD)' in resp.data


# ---------------------------------------------------------------------------
# M11 — record_manual_payout refuses completed / in-flight payouts
# ---------------------------------------------------------------------------

class TestManualPayoutGuard:
    def _match_with_payout(self, app, sample_user, sample_hunter,
                           status):
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        payout = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status=status)
        db.session.add(payout)
        db.session.commit()
        return match

    def test_refuses_completed_payout(self, app, sample_user,
                                      sample_hunter, sample_admin):
        match = self._match_with_payout(app, sample_user, sample_hunter,
                                        'completed')
        with pytest.raises(ValueError, match='already completed'):
            record_manual_payout(match, Decimal('800'), 'ref-1',
                                 sample_admin)
        db.session.rollback()

    def test_refuses_processing_payout(self, app, sample_user,
                                       sample_hunter, sample_admin):
        match = self._match_with_payout(app, sample_user, sample_hunter,
                                        'processing')
        with pytest.raises(ValueError, match='already completed'):
            record_manual_payout(match, Decimal('800'), 'ref-1',
                                 sample_admin)
        db.session.rollback()


# ---------------------------------------------------------------------------
# M12 — transfer-failed copy: no false retry promise
# ---------------------------------------------------------------------------

class TestTransferFailedCopy:
    def test_transfer_failed_points_at_dashboard_retry(
            self, app, sample_user, sample_hunter):
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200)
        payout = Payout(match_id=match.id, referrer_id=sample_hunter.id,
                        bounty_id=bounty.id, gross_amount=1000,
                        platform_fee=200, amount=800, status='processing',
                        stripe_transfer_id='tr_fail_money_1')
        db.session.add(payout)
        db.session.commit()
        handle_transfer_event('transfer.failed', {'id': 'tr_fail_money_1'})
        db.session.commit()
        notes = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='payout_failed').all()
        assert len(notes) == 1
        assert 'retry it from your payouts page' in notes[0].body
        assert 'We will retry it' not in notes[0].body


# ---------------------------------------------------------------------------
# M13 — refund fee rates come from config
# ---------------------------------------------------------------------------

class TestRefundFeeConfig:
    def test_cancel_refund_uses_configured_fee_rates(
            self, client, logged_in_user, app, sample_user):
        _set_stripe_keys()
        app.config['STRIPE_FEE_PERCENT'] = 5
        app.config['STRIPE_FEE_FIXED'] = 0.50
        bounty = _paid_bounty(app, sample_user)
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.Refund.create',
                   return_value=SimpleNamespace(id='re_1')) as refund:
            resp = client.post(f'/needs/{bounty.id}/cancel-refund',
                               follow_redirects=True)
        assert resp.status_code == 200
        # $1,000 minus 5% + $0.50 = $949.50 → 94950 cents.
        assert refund.call_args.kwargs['amount'] == 94950
