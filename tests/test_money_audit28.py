"""Money audit 2026-11-28 — tips + paid communities.

All Stripe calls are mocked (unittest.mock.patch on the stripe
module) — no network calls, no real keys, no real money movement.
Follows the test_tips / test_paid_spaces mocking pattern.
"""
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import stripe

from extensions import db
from models import ConnectAccount, Tip, utc_now
from models.space_payout import SpacePayout
from tests.test_m3_webhook import _post_event, _set_stripe_keys
from tests.test_money_audit import _connect_account
from tests.test_paid_spaces import (
    _invoice_event,
    _make_membership,
    _make_space,
    _ready_connect,
)
from tests.test_tips import _make_tip, _post_event as _post_tip_event, _tip_checkout_event
from utils.paid_spaces import (
    create_paid_space_checkout,
    handle_space_invoice_paid,
    retry_pending_space_payouts,
    split_space_payment,
)
from utils.payments import on_connect_ready
from utils.pro import handle_subscription_event
from utils.tips import (
    compute_tip_split,
    create_tip_checkout,
    fulfill_tip_checkout,
    validate_tip_amount,
)


class TestFailedTipRetry:
    def test_failed_tip_retried_on_connect_ready(
        self, app, client, sample_user, sample_hunter
    ):
        """A tip whose transfer FAILED (create raised, no transfer id)
        is picked up again when onboarding completes — it must not sit
        'failed' forever with the money captured and no recovery path.
        The retry reuses the same idempotency key (no transfer was
        ever created, so a lost response must not double-pay)."""
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        with patch(
            'stripe.Transfer.create',
            side_effect=stripe.error.StripeError('boom'),
        ) as tc:
            resp = _post_tip_event(
                client, _tip_checkout_event('cs_a28_1', tip.id)
            )
        assert resp.status_code == 200
        assert tc.call_count == 1
        first_key = tc.call_args.kwargs['idempotency_key']
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'failed'
        assert tip.stripe_transfer_id is None
        # Onboarding completes (account already ready — the hook is
        # the retry path): the failed tip pays out under the same key.
        account = ConnectAccount.query.filter_by(
            user_id=sample_hunter.id
        ).first()
        with patch(
            'stripe.Transfer.create',
            return_value=SimpleNamespace(id='tr_a28_1b'),
        ) as tc2:
            on_connect_ready(account)
            db.session.commit()
        assert tc2.call_count == 1
        assert tc2.call_args.kwargs['idempotency_key'] == first_key
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'completed'
        assert tip.stripe_transfer_id == 'tr_a28_1b'

    def test_failed_tip_after_transfer_failed_event_retried(
        self, app, client, sample_user, sample_hunter
    ):
        """A tip completed, then Stripe reports the transfer failed:
        the retry (on connect-ready) must use a FRESH key scoped to
        the dead transfer id, or Stripe returns the cached dead
        transfer and no money moves."""
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        with patch(
            'stripe.Transfer.create',
            return_value=SimpleNamespace(id='tr_a28_2'),
        ):
            _post_tip_event(
                client, _tip_checkout_event('cs_a28_2', tip.id)
            )
        assert db.session.get(Tip, tip.id).status == 'completed'
        failed_event = {
            'id': 'evt_tr_a28_2_failed',
            'type': 'transfer.failed',
            'data': {'object': {'id': 'tr_a28_2'}},
        }
        assert _post_tip_event(client, failed_event).status_code == 200
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'failed'
        assert tip.stripe_transfer_id == 'tr_a28_2'
        account = ConnectAccount.query.filter_by(
            user_id=sample_hunter.id
        ).first()
        with patch(
            'stripe.Transfer.create',
            return_value=SimpleNamespace(id='tr_a28_2b'),
        ) as tc:
            on_connect_ready(account)
            db.session.commit()
        assert tc.call_count == 1
        assert tc.call_args.kwargs['idempotency_key'] == (
            f'tip-transfer-{tip.id}-tr_a28_2'
        )
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'completed'
        assert tip.stripe_transfer_id == 'tr_a28_2b'


class TestFailedSpacePayoutRetry:
    def test_failed_space_payout_retried_on_connect_ready(
        self, app, client, sample_user, second_user
    ):
        """A space payout whose transfer create raised is 'failed' —
        the next-invoice / connect-ready retry must pick it up, not
        only 'pending_stripe' rows, or the owner's share sits
        captured-but-unpaid forever."""
        _set_stripe_keys()
        _ready_connect(sample_user)
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        _make_membership(second_user, space, subscription_id='sub_a28_1')
        with patch(
            'stripe.Transfer.create',
            side_effect=stripe.error.StripeError('boom'),
        ):
            resp = _post_event(
                client, _invoice_event('in_a28_1', 'sub_a28_1', 1000),
            )
        assert resp.status_code == 200
        payout = SpacePayout.query.filter_by(
            stripe_invoice_id='in_a28_1'
        ).first()
        assert payout is not None
        assert payout.status == 'failed'
        assert payout.stripe_transfer_id is None
        with patch(
            'stripe.Transfer.create',
            return_value=SimpleNamespace(id='tr_a28_3b'),
        ) as tc:
            results = retry_pending_space_payouts(sample_user.id)
            db.session.commit()
        assert tc.call_count == 1
        assert tc.call_args.kwargs['idempotency_key'] == (
            f'space-payout-{payout.id}'
        )
        assert (payout.id, True, None) in results
        payout = db.session.get(SpacePayout, payout.id)
        assert payout.status == 'processing'
        assert payout.stripe_transfer_id == 'tr_a28_3b'

    def test_failed_space_payout_with_dead_transfer_uses_fresh_key(
        self, app, client, sample_user, second_user
    ):
        """Mirror of the audit27 payout lesson, for space payouts: a
        retry after a transfer id was recorded and then reported
        failed must use a FRESH key scoped to the dead transfer id —
        the old permanent key would return Stripe's cached response
        for the dead transfer and the code would record the dead id
        as a success while no money moves."""
        _set_stripe_keys()
        _ready_connect(sample_user)
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        _make_membership(second_user, space, subscription_id='sub_a28_2')
        with patch(
            'stripe.Transfer.create',
            return_value=SimpleNamespace(id='tr_a28_4'),
        ):
            _post_event(
                client, _invoice_event('in_a28_2', 'sub_a28_2', 1000),
            )
        payout = SpacePayout.query.filter_by(
            stripe_invoice_id='in_a28_2'
        ).first()
        assert payout.status == 'processing'
        assert payout.stripe_transfer_id == 'tr_a28_4'
        failed_event = {
            'id': 'evt_tr_a28_4_failed',
            'type': 'transfer.failed',
            'data': {'object': {'id': 'tr_a28_4'}},
        }
        assert _post_event(client, failed_event).status_code == 200
        payout = db.session.get(SpacePayout, payout.id)
        assert payout.status == 'failed'
        assert payout.stripe_transfer_id == 'tr_a28_4'
        with patch(
            'stripe.Transfer.create',
            return_value=SimpleNamespace(id='tr_a28_4b'),
        ) as tc:
            results = retry_pending_space_payouts(sample_user.id)
            db.session.commit()
        assert tc.call_count == 1
        assert tc.call_args.kwargs['idempotency_key'] == (
            f'space-payout-{payout.id}-tr_a28_4'
        )
        assert (payout.id, True, None) in results
        payout = db.session.get(SpacePayout, payout.id)
        assert payout.status == 'processing'
        assert payout.stripe_transfer_id == 'tr_a28_4b'


class TestFeeMath:
    def test_splits_sum_to_gross_on_odd_amounts(self, app):
        """Fee + recipient always equals the gross — no cent lost or
        created — on odd amounts, for both split functions."""
        for amount in (1, 199, 200, 201, 299, 555, 999, 1001, 999999):
            for fee_percent in (0, 20, 33, 50):
                fee, net = compute_tip_split(amount, fee_percent)
                assert fee + net == amount, (amount, fee_percent)
                assert fee >= 0 and net >= 0
                fee2, owner = split_space_payment(amount, fee_percent)
                assert fee2 + owner == amount, (amount, fee_percent)
        # Half-up rounding: $2.99 at 20% is 59.8 -> 60 fee, 239 net.
        assert compute_tip_split(299, 20) == (60, 239)
        assert split_space_payment(299, 20) == (60, 239)
        # $2.01 at 20% is 40.2 -> 40 fee, 161 net.
        assert compute_tip_split(201, 20) == (40, 161)
        # $5.55 at 20% is 111.0 exactly.
        assert compute_tip_split(555, 20) == (111, 444)


class TestTipCheckoutIdempotency:
    def test_checkout_key_stable_across_minute_boundary(
        self, app, sample_user, sample_hunter
    ):
        """A double-click straddling a minute boundary must NOT mint
        two Checkout sessions: the tip row is one row, so its
        idempotency key is per-tip, not per-minute. Two sessions
        would let the tipper pay twice for one tip while only one
        transfer ever happens (fulfillment is a no-op on the second
        session once the tip is completed)."""
        _set_stripe_keys()
        captured_keys = []

        def _fake_session(**kwargs):
            captured_keys.append(kwargs.get('idempotency_key'))
            return 'https://checkout.test/tip-a28'

        real_time = time.time
        calls = []

        def _fake_time():
            calls.append(1)
            if len(calls) == 1:
                return 60 * 1000 + 59  # last second of bucket 1000
            if len(calls) == 2:
                return 60 * 1000 + 60  # first second of bucket 1001
            return real_time()

        with patch(
            'utils.tips.create_checkout_session',
            side_effect=_fake_session,
        ), patch('time.time', side_effect=_fake_time):
            tip1, url1 = create_tip_checkout(
                sample_user, sample_hunter, 1000, 'CAD', None,
                'https://example.test/ok', 'https://example.test/cancel',
            )
            db.session.commit()
            tip2, url2 = create_tip_checkout(
                sample_user, sample_hunter, 1000, 'CAD', None,
                'https://example.test/ok', 'https://example.test/cancel',
            )
            db.session.commit()
        assert tip1.id == tip2.id
        assert url1 == url2
        assert len(captured_keys) == 2
        assert captured_keys[0] == captured_keys[1]
        assert captured_keys[0] == f'tip-checkout-{tip1.id}'

    def test_fulfill_tip_checkout_twice_transfers_once(
        self, app, sample_user, sample_hunter
    ):
        """Direct double fulfillment of the same session (bypassing
        the webhook event-log dedup) transfers exactly once."""
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        session = {
            'id': 'cs_a28_3',
            'payment_intent': 'pi_a28_3',
            'metadata': {'tip_id': tip.id},
        }
        with patch(
            'stripe.Transfer.create',
            return_value=SimpleNamespace(id='tr_a28_5'),
        ) as tc:
            fulfill_tip_checkout(session)
            db.session.commit()
            fulfill_tip_checkout(session)
            db.session.commit()
        assert tc.call_count == 1
        assert Tip.query.filter_by(recipient_id=sample_hunter.id).count() == 1
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'completed'
        assert tip.stripe_transfer_id == 'tr_a28_5'


class TestSpaceInvoiceIdempotency:
    def test_handle_invoice_paid_twice_transfers_once(
        self, app, sample_user, second_user
    ):
        """Direct double delivery of the same invoice (bypassing the
        webhook event-log dedup) records one payout and transfers
        once."""
        _set_stripe_keys()
        _ready_connect(sample_user)
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        _make_membership(second_user, space, subscription_id='sub_a28_3')
        invoice = {
            'id': 'in_a28_3',
            'subscription': 'sub_a28_3',
            'amount_paid': 1000,
            'currency': 'cad',
            'charge': 'ch_a28_3',
            'period_end': int(
                (utc_now() + timedelta(days=30)).timestamp()
            ),
        }
        with patch(
            'stripe.Transfer.create',
            return_value=SimpleNamespace(id='tr_a28_6'),
        ) as tc:
            handle_space_invoice_paid(invoice)
            db.session.commit()
            handle_space_invoice_paid(invoice)
            db.session.commit()
        assert tc.call_count == 1
        assert SpacePayout.query.filter_by(
            stripe_invoice_id='in_a28_3'
        ).count() == 1


class TestAmountValidation:
    def test_tip_amount_boundaries(self, app):
        assert validate_tip_amount(None)[0] is False
        assert validate_tip_amount('abc')[0] is False
        assert validate_tip_amount(0)[0] is False
        assert validate_tip_amount(-100)[0] is False
        assert validate_tip_amount(199)[0] is False
        assert validate_tip_amount(200)[0] is True
        assert validate_tip_amount(201)[0] is True
        # Stripe's per-charge maximum: above it the Checkout creation
        # fails server-side and the tipper gets a misleading
        # "Stripe is not configured" flash for an amount problem.
        assert validate_tip_amount(99_999_999)[0] is True
        assert validate_tip_amount(100_000_000)[0] is False


class TestAdminPriceValidation:
    def test_nan_price_rejected_without_500(
        self, logged_in_admin, client, app, second_user
    ):
        space = _make_space(slug='free-club-a28')
        resp = logged_in_admin.post(
            f'/admin/spaces/{space.id}/edit',
            data={
                'owner_email': second_user.email,
                'is_paid': 'on',
                'price': 'nan',
            },
        )
        # 'nan' parses as a float and to_cents() raises — the form
        # must reject it with a flash, not a 500.
        assert resp.status_code == 302
        db.session.refresh(space)
        assert space.is_paid is False
        assert space.price_cents is None

    def test_inf_and_huge_prices_rejected(
        self, logged_in_admin, client, app, second_user
    ):
        space = _make_space(slug='free-club-a28b')
        for bad_price in ('inf', '999999999'):
            resp = logged_in_admin.post(
                f'/admin/spaces/{space.id}/edit',
                data={
                    'owner_email': second_user.email,
                    'is_paid': 'on',
                    'price': bad_price,
                },
            )
            assert resp.status_code == 302, bad_price
            db.session.refresh(space)
            assert space.is_paid is False, bad_price
            assert space.price_cents is None, bad_price


class TestSubscriptionLifecycle28:
    def test_failed_renewal_past_due_loses_access(
        self, app, sample_user, second_user
    ):
        """A renewal whose payment fails (past_due) must not keep the
        member's access: the period just ended unpaid."""
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        membership = _make_membership(
            second_user, space, subscription_id='sub_a28_4',
            period_end=(utc_now() + timedelta(days=30)),
        )
        assert membership.is_active is True
        handle_subscription_event(
            {
                'id': 'sub_a28_4', 'status': 'past_due',
                'cancel_at_period_end': False,
                'current_period_end': int(
                    (utc_now() + timedelta(days=30)).timestamp()
                ),
            },
            'customer.subscription.updated',
        )
        db.session.commit()
        db.session.refresh(membership)
        assert membership.status == 'past_due'
        assert membership.is_active is False
        assert space.can_view(second_user) is False

    def test_unpaid_subscription_cancels_membership(
        self, app, sample_user, second_user
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        membership = _make_membership(
            second_user, space, subscription_id='sub_a28_5',
            period_end=(utc_now() + timedelta(days=30)),
        )
        handle_subscription_event(
            {'id': 'sub_a28_5', 'status': 'unpaid'},
            'customer.subscription.updated',
        )
        db.session.commit()
        db.session.refresh(membership)
        assert membership.status == 'canceled'
        assert membership.is_active is False


class TestCurrency:
    def test_paid_space_checkout_charges_cad(
        self, app, sample_user, second_user
    ):
        """The subscription Checkout is created in CAD whatever the
        space's record-only currency column says — no non-CAD amount
        may reach Stripe."""
        _set_stripe_keys()
        space = _make_space(owner=sample_user, paid=True, price_cents=1500)
        with patch(
            'stripe.checkout.Session.create',
            return_value=SimpleNamespace(url='https://checkout.test/space'),
        ) as sc:
            url = create_paid_space_checkout(
                second_user, space,
                'https://example.test/ok', 'https://example.test/cancel',
            )
        assert url == 'https://checkout.test/space'
        kwargs = sc.call_args.kwargs
        assert kwargs['mode'] == 'subscription'
        line = kwargs['line_items'][0]['price_data']
        assert line['currency'] == 'cad'
        assert line['unit_amount'] == 1500
        assert line['recurring']['interval'] == 'month'
