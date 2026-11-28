"""Money audit regression tests (2026-11-27) — audit27-money slice.

All Stripe calls are mocked (unittest.mock.patch on the stripe
module) — no network calls, no real keys, no real money movement.
"""
from types import SimpleNamespace
from unittest.mock import patch

from extensions import db
from models import AdminSettings, ConnectAccount, Match, Payout
from models.bounty import Bounty
from models.pitch import Pitch
from tests.test_bumps import _make_member, _old_post
from tests.test_money_audit import (
    _confirmed_match,
    _connect_account,
    _mock_pi,
    _paid_bounty,
    _set_stripe_keys,
)
from utils.payments import handle_transfer_event, release_escrow
from utils.pro import create_pro_checkout


def _mock_checkout_session():
    return SimpleNamespace(id='cs_test_money27', url='https://checkout.test/x')


class TestTransferRetryIdempotency:
    def test_retry_after_transfer_failure_uses_fresh_key(
            self, app, sample_user, sample_hunter):
        """A transfer that failed (webhook transfer.failed) must be
        retried under a FRESH idempotency key. Reusing
        ``payout-<id>`` returns Stripe's cached response for the dead
        transfer inside its 24h window — the code would record the
        old transfer id again and mark the payout 'processing' while
        no money moves."""
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, reward=1000)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200.0)
        _connect_account(app, sample_hunter)
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_first')) as tc:
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok is True
        first_key = tc.call_args.kwargs['idempotency_key']
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout.stripe_transfer_id == 'tr_first'
        assert payout.status == 'processing'
        # Stripe reports the transfer failed; the payout goes failed.
        handle_transfer_event(
            'transfer.failed', SimpleNamespace(id='tr_first'))
        db.session.commit()
        assert db.session.get(Payout, payout.id).status == 'failed'
        # The referrer retries from the payouts page.
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_second')) as tc2:
            ok, _msg = release_escrow(db.session.get(Match, match.id))
            db.session.commit()
        assert ok is True
        assert tc2.call_count == 1
        assert tc2.call_args.kwargs['idempotency_key'] != first_key
        assert db.session.get(Payout, payout.id).stripe_transfer_id == (
            'tr_second'
        )

    def test_retry_after_failed_create_keeps_same_key(
            self, app, sample_user, sample_hunter):
        """The mirror case: when Transfer.create itself raised (no
        transfer was ever created, no transfer id recorded), the
        retry MUST reuse the same key — otherwise a transfer that
        succeeded server-side but lost its response would be created
        twice."""
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, reward=1000)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 fee=200.0)
        _connect_account(app, sample_hunter)
        import stripe as stripe_mod
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi()), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create',
                   side_effect=stripe_mod.error.StripeError('boom')):
            ok, _msg = release_escrow(match)
            db.session.commit()
        assert ok is False
        payout = Payout.query.filter_by(match_id=match.id).first()
        assert payout.status == 'failed'
        assert payout.stripe_transfer_id is None
        with patch('stripe.PaymentIntent.retrieve',
                   return_value=_mock_pi('succeeded')), \
             patch('stripe.PaymentIntent.capture'), \
             patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_retry')) as tc:
            ok, _msg = release_escrow(db.session.get(Match, match.id))
            db.session.commit()
        assert ok is True
        assert tc.call_args.kwargs['idempotency_key'] == (
            f'payout-{payout.id}'
        )


class TestSubscriptionCheckoutKeys:
    def test_pro_checkout_key_changes_across_minutes(
            self, app, sample_user):
        """Pro uses a permanent ``pro-<user>`` key: cancel and
        re-subscribe within Stripe's 24h idempotency window returns
        the stale completed session instead of a fresh checkout. The
        key must be bucketed like every other checkout flow."""
        _set_stripe_keys()
        import time as _time
        now = _time.time()
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create, \
             patch('time.time', return_value=now):
            create_pro_checkout(
                sample_user, 'https://x.test/ok', 'https://x.test/cancel')
            first_key = create.call_args.kwargs['idempotency_key']
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create, \
             patch('time.time', return_value=now):
            create_pro_checkout(
                sample_user, 'https://x.test/ok', 'https://x.test/cancel')
            assert create.call_args.kwargs['idempotency_key'] == first_key
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create, \
             patch('time.time', return_value=now + 120.0):
            create_pro_checkout(
                sample_user, 'https://x.test/ok', 'https://x.test/cancel')
            assert create.call_args.kwargs['idempotency_key'] != first_key

    def test_recurring_bump_key_changes_across_minutes(
            self, client, app, sample_user, sample_hunter, login_as):
        """Same permanent-key problem on the recurring-bump checkout
        (``recurring-bump-<post>``): re-subscribe within 24h of a
        cancel returns the stale session."""
        _set_stripe_keys()
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        login_as(client, 'testuser@example.com', 'TestPass123!')
        import time as _time
        now = _time.time()
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create, \
             patch('time.time', return_value=now):
            resp = client.post(
                f'/payments/bumps/recurring/{post.id}/subscribe')
            assert resp.status_code == 302
            first_key = create.call_args.kwargs['idempotency_key']
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create, \
             patch('time.time', return_value=now + 120.0):
            resp = client.post(
                f'/payments/bumps/recurring/{post.id}/subscribe')
            assert resp.status_code == 302
            assert create.call_args.kwargs['idempotency_key'] != first_key


class TestCloseReuseCaptured:
    def test_close_reuse_refused_when_payment_captured(
            self, client, logged_in_user, app, sample_user):
        """'Close Without Refund' tells the poster nothing was
        charged and the authorization will expire — false when the
        PaymentIntent was already captured (e.g. a pre-M2
        immediate-capture payment with no match). A captured payment
        must go through the refund path, not silently stay with the
        platform."""
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, reward=1000)
        with patch('blueprints.bounties.routes.payment_intent_status',
                   return_value='succeeded'):
            resp = client.post(
                f'/needs/{bounty.id}/close',
                data={'close_action': 'reuse'},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert '/close' in resp.headers['Location']
        assert db.session.get(Bounty, bounty.id).status == 'open'
