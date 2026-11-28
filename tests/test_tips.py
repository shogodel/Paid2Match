"""Tips tests — send money to a user via Stripe Checkout.

All Stripe calls are mocked (unittest.mock.patch on the stripe
module) — no network calls, no real keys, no real money movement.
Follows the test_money_audit27 / test_m3_webhook mocking pattern.
"""
from types import SimpleNamespace
from unittest.mock import patch

import stripe

from extensions import db
from models import AdminSettings, Tip
from models.notification import Notification
from models.post import Post
from tests.test_money_audit import _connect_account, _set_stripe_keys
from utils.payments import on_connect_ready
from utils.tips import (
    compute_tip_split,
    get_tip_fee_percent,
    retry_pending_tips,
)


def _mock_checkout_session():
    return SimpleNamespace(id='cs_test_tip', url='https://checkout.test/tip')


def _post_event(client, event):
    with patch('stripe.Webhook.construct_event', return_value=event):
        return client.post(
            '/needs/stripe-webhook',
            data=b'{}',
            headers={'Stripe-Signature': 't=1,v1=fake'},
        )


def _tip_checkout_event(session_id, tip_id, payment_intent='pi_tip_x'):
    return {
        'id': f'evt_{session_id}',
        'type': 'checkout.session.completed',
        'data': {'object': {
            'id': session_id,
            'payment_intent': payment_intent,
            'metadata': {
                'm3_kind': 'tip',
                'tip_id': tip_id,
            },
        }},
    }


def _make_tip(app, tipper, recipient, amount_cents=1000, **kwargs):
    with app.app_context():
        tip = Tip(
            tipper_id=tipper.id,
            recipient_id=recipient.id,
            amount_cents=amount_cents,
            currency=kwargs.get('currency', 'CAD'),
            message=kwargs.get('message'),
            stripe_payment_intent_id=kwargs.get('payment_intent'),
            status=kwargs.get('status', 'pending'),
        )
        db.session.add(tip)
        db.session.commit()
        db.session.refresh(tip)
        return tip


class TestTipFee:
    def test_default_fee_is_20_percent(self, app):
        assert get_tip_fee_percent() == 20

    def test_fee_is_admin_settable_and_clamped(self, app):
        AdminSettings.set('TIP_PLATFORM_FEE_PERCENT', '30')
        db.session.commit()
        assert get_tip_fee_percent() == 30
        AdminSettings.set('TIP_PLATFORM_FEE_PERCENT', '99')
        db.session.commit()
        assert get_tip_fee_percent() == 50
        AdminSettings.set('TIP_PLATFORM_FEE_PERCENT', 'not-a-number')
        db.session.commit()
        assert get_tip_fee_percent() == 20

    def test_fee_is_independent_of_hire_fee(self, app):
        """Setting the tip fee must not move the hire fee, and
        vice versa — they are separate settings."""
        from utils.payments import get_platform_fee_percent
        AdminSettings.set('TIP_PLATFORM_FEE_PERCENT', '30')
        AdminSettings.set('PLATFORM_FEE_PERCENT', '10')
        db.session.commit()
        assert get_tip_fee_percent() == 30
        assert get_platform_fee_percent() == 10

    def test_split_rounds_half_up(self, app):
        assert compute_tip_split(1000, 20) == (200, 800)
        assert compute_tip_split(333, 20) == (67, 266)
        assert compute_tip_split(200, 20) == (40, 160)
        fee, net = compute_tip_split(1000, 0)
        assert (fee, net) == (0, 1000)


class TestTipRoutes:
    def test_self_tip_refused(self, app, client, sample_user, login_as):
        login_as(client, sample_user.email)
        resp = client.post(
            f'/tips/create/{sample_user.id}',
            data={'amount': '5', 'currency': 'CAD'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b'cannot tip yourself' in resp.data
        assert Tip.query.count() == 0

    def test_minimum_tip_enforced(self, app, client, sample_user,
                                  sample_hunter, login_as):
        login_as(client, sample_user.email)
        resp = client.post(
            f'/tips/create/{sample_hunter.id}',
            data={'amount': '1', 'currency': 'CAD'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b'minimum tip is $2' in resp.data.lower()
        assert Tip.query.count() == 0

    def test_create_tip_redirects_to_checkout(
            self, app, client, sample_user, sample_hunter, login_as):
        _set_stripe_keys()
        login_as(client, sample_user.email)
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as sc:
            resp = client.post(
                f'/tips/create/{sample_hunter.id}',
                data={'amount': '5', 'currency': 'CAD',
                      'message': 'Great post!'},
            )
        assert resp.status_code == 302
        assert resp.headers['Location'] == 'https://checkout.test/tip'
        tip = Tip.query.filter_by(recipient_id=sample_hunter.id).first()
        assert tip is not None
        assert tip.amount_cents == 500
        assert tip.message == 'Great post!'
        assert tip.status == 'pending'
        # Per-tip idempotency key on the Checkout creation (audit28:
        # minute-bucketed keys mint a second session on a double-click
        # straddling a minute boundary — one tip row, one key).
        key = sc.call_args.kwargs['idempotency_key']
        assert key == f'tip-checkout-{tip.id}'

    def test_double_click_reuses_pending_tip(
            self, app, client, sample_user, sample_hunter, login_as):
        """Two identical submissions in the same minute create ONE
        tip row (and one Stripe session), not two charges."""
        _set_stripe_keys()
        login_as(client, sample_user.email)
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()):
            client.post(
                f'/tips/create/{sample_hunter.id}',
                data={'amount': '5', 'currency': 'CAD'},
            )
            client.post(
                f'/tips/create/{sample_hunter.id}',
                data={'amount': '5', 'currency': 'CAD'},
            )
        assert Tip.query.filter_by(recipient_id=sample_hunter.id).count() == 1

    def test_new_tip_page_renders_picker(
            self, app, client, sample_user, sample_hunter, login_as):
        login_as(client, sample_user.email)
        resp = client.get(f'/tips/new/{sample_hunter.id}')
        assert resp.status_code == 200
        assert b'$5' in resp.data
        assert b'Minimum $2' in resp.data

    def test_anonymous_tip_redirects_to_login(
            self, app, client, sample_hunter):
        resp = client.post(
            f'/tips/create/{sample_hunter.id}',
            data={'amount': '5', 'currency': 'CAD'},
        )
        assert resp.status_code == 302
        assert '/auth/login' in resp.headers['Location']


class TestTipWebhook:
    def test_checkout_fulfills_tip_with_transfer(
            self, app, client, sample_user, sample_hunter):
        """A completed Checkout transfers the recipient's 80% share
        to their Connect account and notifies them."""
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        with patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_tip_1')) as tc:
            resp = _post_event(
                client, _tip_checkout_event('cs_tip_1', tip.id)
            )
        assert resp.status_code == 200
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'completed'
        assert tip.stripe_transfer_id == 'tr_tip_1'
        assert tip.platform_fee_cents == 200
        assert tip.net_cents == 800
        assert tip.stripe_payment_intent_id == 'pi_tip_x'
        assert tc.call_count == 1
        assert tc.call_args.kwargs['amount'] == 800
        assert tc.call_args.kwargs['currency'] == 'cad'
        assert tc.call_args.kwargs['destination'] == 'acct_test_ready'
        assert tc.call_args.kwargs['source_transaction'] == 'pi_tip_x'
        assert tc.call_args.kwargs['idempotency_key'] == (
            f'tip-transfer-{tip.id}'
        )
        note = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='tip_received'
        ).first()
        assert note is not None

    def test_tip_waits_when_recipient_has_no_connect(
            self, app, client, sample_user, sample_hunter):
        """Paid, but the recipient has not onboarded: the tip stays
        pending (money captured, payout waiting) and they are
        prompted to connect."""
        _set_stripe_keys()
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        with patch('stripe.Transfer.create') as tc:
            resp = _post_event(
                client, _tip_checkout_event('cs_tip_2', tip.id)
            )
        assert resp.status_code == 200
        assert tc.call_count == 0
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'pending'
        assert tip.stripe_payment_intent_id == 'pi_tip_x'
        note = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='tip_pending'
        ).first()
        assert note is not None

    def test_duplicate_webhook_delivery_is_noop(
            self, app, client, sample_user, sample_hunter):
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        event = _tip_checkout_event('cs_tip_3', tip.id)
        with patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_tip_3')) as tc:
            assert _post_event(client, event).status_code == 200
            assert _post_event(client, event).status_code == 200
        assert tc.call_count == 1
        assert Tip.query.filter_by(recipient_id=sample_hunter.id).count() == 1

    def test_transfer_failure_marks_tip_failed(
            self, app, client, sample_user, sample_hunter):
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        with patch('stripe.Transfer.create',
                   side_effect=stripe.error.StripeError('boom')):
            resp = _post_event(
                client, _tip_checkout_event('cs_tip_4', tip.id)
            )
        assert resp.status_code == 200
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'failed'
        assert 'Transfer failed' in (tip.failure_reason or '')

    def test_retry_after_failed_transfer_uses_fresh_key(
            self, app, client, sample_user, sample_hunter):
        """Mirror of the audit27 payout lesson: a retry after a
        transfer FAILED (transfer id recorded, Stripe later reports
        it failed) must use a FRESH idempotency key, or Stripe
        returns the cached response for the dead transfer and the
        code would record the old transfer id again while no money
        moves."""
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        with patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_tip_5')) as tc:
            _post_event(client, _tip_checkout_event('cs_tip_5', tip.id))
        assert tc.call_count == 1
        first_key = tc.call_args.kwargs['idempotency_key']
        assert first_key == f'tip-transfer-{tip.id}'
        assert db.session.get(Tip, tip.id).status == 'completed'
        # Stripe reports the transfer failed; the tip goes failed
        # (but keeps the dead transfer id).
        failed_event = {
            'id': 'evt_tr_tip_5_failed',
            'type': 'transfer.failed',
            'data': {'object': {'id': 'tr_tip_5'}},
        }
        assert _post_event(client, failed_event).status_code == 200
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'failed'
        assert tip.stripe_transfer_id == 'tr_tip_5'
        # The redelivered checkout event retries under a fresh key.
        with patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_tip_5b')) as tc2:
            event = _tip_checkout_event('cs_tip_5', tip.id)
            event['id'] = 'evt_cs_tip_5_retry'
            resp = _post_event(client, event)
        assert resp.status_code == 200
        assert tc2.call_count == 1
        assert tc2.call_args.kwargs['idempotency_key'] != first_key
        assert tc2.call_args.kwargs['idempotency_key'] == (
            f'tip-transfer-{tip.id}-tr_tip_5'
        )
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'completed'
        assert tip.stripe_transfer_id == 'tr_tip_5b'

    def test_retry_after_failed_create_keeps_same_key(
            self, app, client, sample_user, sample_hunter):
        """The mirror case: when Transfer.create itself raised (no
        transfer was ever created, no transfer id recorded), the
        retry MUST reuse the same key — otherwise a transfer that
        succeeded server-side but lost its response would be created
        twice."""
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        with patch('stripe.Transfer.create',
                   side_effect=stripe.error.StripeError('boom')) as tc:
            _post_event(client, _tip_checkout_event('cs_tip_5c', tip.id))
        assert tc.call_count == 1
        first_key = tc.call_args.kwargs['idempotency_key']
        assert db.session.get(Tip, tip.id).status == 'failed'
        assert db.session.get(Tip, tip.id).stripe_transfer_id is None
        with patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_tip_5c')) as tc2:
            event = _tip_checkout_event('cs_tip_5c', tip.id)
            event['id'] = 'evt_cs_tip_5c_retry'
            resp = _post_event(client, event)
        assert resp.status_code == 200
        assert tc2.call_count == 1
        assert tc2.call_args.kwargs['idempotency_key'] == first_key
        assert db.session.get(Tip, tip.id).status == 'completed'

    def test_transfer_failed_event_marks_tip_failed(
            self, app, client, sample_user, sample_hunter):
        """Stripe later reports the transfer failed: the tip goes
        failed (the payout handler ignores it — it is not a Payout)."""
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        with patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_tip_6')):
            _post_event(client, _tip_checkout_event('cs_tip_6', tip.id))
        assert db.session.get(Tip, tip.id).status == 'completed'
        event = {
            'id': 'evt_tr_tip_6_failed',
            'type': 'transfer.failed',
            'data': {'object': {'id': 'tr_tip_6'}},
        }
        assert _post_event(client, event).status_code == 200
        assert db.session.get(Tip, tip.id).status == 'failed'


class TestPendingTipPayoutOnConnect:
    def test_retry_pending_tips_when_onboarding_completes(
            self, app, sample_user, sample_hunter):
        """A tip paid while the recipient had no Connect account pays
        out when their onboarding completes (on_connect_ready hook)."""
        _set_stripe_keys()
        tip = _make_tip(
            app, sample_user, sample_hunter, 1000,
            payment_intent='pi_tip_pending',
        )
        account = _connect_account(app, sample_hunter, ready=False)
        # An unready account cannot pay anything out.
        assert retry_pending_tips(sample_hunter.id) == []
        assert db.session.get(Tip, tip.id).status == 'pending'
        # Onboarding completes: the hook retries the pending tip.
        account.charges_enabled = True
        account.payouts_enabled = True
        account.details_submitted = True
        account.onboarding_status = 'complete'
        db.session.commit()
        with patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_tip_7')) as tc:
            on_connect_ready(account)
            db.session.commit()
        assert tc.call_count == 1
        assert tc.call_args.kwargs['amount'] == 800
        tip = db.session.get(Tip, tip.id)
        assert tip.status == 'completed'
        assert tip.stripe_transfer_id == 'tr_tip_7'

    def test_unpaid_pending_tip_is_not_paid_out(
            self, app, sample_user, sample_hunter):
        """A pending tip with no payment intent is a Checkout that
        never completed — the sweep must not touch it."""
        _set_stripe_keys()
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        _connect_account(app, sample_hunter)
        assert retry_pending_tips(sample_hunter.id) == []
        assert db.session.get(Tip, tip.id).status == 'pending'


class TestTipDisplayAndAdmin:
    def test_profile_shows_total_tips_received(
            self, app, client, sample_user, sample_hunter, login_as):
        _make_tip(
            app, sample_user, sample_hunter, 1000, status='completed'
        )
        _make_tip(
            app, sample_user, sample_hunter, 500, status='completed'
        )
        _make_tip(app, sample_user, sample_hunter, 2500)  # pending: hidden
        login_as(client, sample_user.email)
        # The UUID URL 301s to the handle URL.
        resp = client.get(f'/profile/{sample_hunter.id}',
                          follow_redirects=True)
        assert resp.status_code == 200
        # Gross total of completed tips only: CA$15.00, 2 tips.
        assert b'CA$15.00' in resp.data
        assert b'2 tips' in resp.data
        # The Tip button renders for another user's profile.
        assert b'tip-modal-profile' in resp.data

    def test_post_card_shows_tip_button(
            self, app, client, sample_user, sample_hunter, login_as):
        with app.app_context():
            post = Post(author_id=sample_hunter.id, post_type='update',
                        body='Hello network.')
            db.session.add(post)
            db.session.commit()
            post_id = post.id
        login_as(client, sample_user.email)
        resp = client.get(f'/social/posts/{post_id}')
        assert resp.status_code == 200
        assert f'tip-modal-post-{post_id}'.encode() in resp.data

    def test_post_card_hides_tip_button_for_own_post(
            self, app, client, sample_user, login_as):
        with app.app_context():
            post = Post(author_id=sample_user.id, post_type='update',
                        body='My own post.')
            db.session.add(post)
            db.session.commit()
            post_id = post.id
        login_as(client, sample_user.email)
        resp = client.get(f'/social/posts/{post_id}')
        assert resp.status_code == 200
        assert f'tip-modal-post-{post_id}'.encode() not in resp.data

    def test_admin_tips_listing(
            self, app, client, sample_user, sample_hunter,
            logged_in_admin):
        _make_tip(
            app, sample_user, sample_hunter, 1000, status='completed',
            message='Thanks!',
        )
        resp = client.get('/admin/tips')
        assert resp.status_code == 200
        assert b'Thanks!' in resp.data
        assert b'CA$10.00' in resp.data
        resp = client.get('/admin/tips?status=pending')
        assert resp.status_code == 200
        assert b'Thanks!' not in resp.data

    def test_admin_tips_listing_requires_admin(
            self, app, client, sample_user, login_as):
        login_as(client, sample_user.email)
        resp = client.get('/admin/tips')
        assert resp.status_code == 302

    def test_admin_can_set_tip_fee(
            self, app, client, logged_in_admin):
        resp = client.post(
            '/admin/settings',
            data={'platform_fee_percent': '20', 'tip_fee_percent': '30'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert AdminSettings.get('TIP_PLATFORM_FEE_PERCENT') == '30'
        # The hire fee is untouched by the tip fee save.
        assert AdminSettings.get('PLATFORM_FEE_PERCENT') == '20'
