"""Money audit regression tests (2026-11-25) — audit25-money.

Every test proves one fixed bug from the second money audit. All
Stripe calls are mocked (unittest.mock.patch on the stripe module)
— no network calls, no real keys, no real money movement.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from extensions import db
from models import utc_now
from models.bounty_upgrade import BountyUpgrade
from models.user import User
from tests.test_bumps import _make_member, _old_post
from tests.test_money_audit import (
    _mock_checkout_session,
    _paid_bounty,
    _set_stripe_keys,
)
from utils.payments import fee_rate_for, founding_benefit_active
from utils.pro import create_pro_checkout, is_pro


def _naive(dt):
    return dt.replace(tzinfo=None) if dt is not None else None


def _edit_data(**overrides):
    data = {
        'title': 'Paid need updated title',
        'bounty_type': 'recruitment',
        'category': '',
        'bounty_direction': 'seeking_opportunity',
        'description': 'x' * 60,
        'reward_amount': '1000',
        'currency': 'CAD',
        'location': 'Remote',
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Close page: refund actually runs (was a 405) and reuse closes
# ---------------------------------------------------------------------------

class TestClosePage:
    def test_close_refund_runs_refund(self, client, logged_in_user,
                                      sample_user, app):
        """POST /needs/<id>/close with close_action=refund used to
        redirect (GET) to the POST-only cancel-refund route — the
        browser got a 405, the refund never ran and the need stayed
        open. Now the close route calls cancel_refund directly."""
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, reward=1000, secured=True)
        with patch('utils.payments.stripe.PaymentIntent.retrieve',
                   return_value=SimpleNamespace(
                       id='pi_test_escrow', status='requires_capture')), \
             patch('utils.payments.stripe.PaymentIntent.cancel',
                   return_value=SimpleNamespace(
                       id='pi_test_escrow', status='canceled')):
            resp = client.post(f'/needs/{bounty.id}/close',
                               data={'close_action': 'refund'},
                               follow_redirects=True)
        assert resp.status_code == 200
        db.session.refresh(bounty)
        assert bounty.status == 'closed'
        assert bounty.payment_status == 'refunded'

    def test_close_reuse_actually_closes(self, client, logged_in_user,
                                          sample_user, app):
        """The reuse branch never committed, so the need stayed open
        while the page claimed it was closed. Now the close lands,
        and the copy no longer promises the payment carries over to
        a new need (no such mechanism exists)."""
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, reward=1000, secured=True)
        resp = client.post(f'/needs/{bounty.id}/close',
                           data={'close_action': 'reuse'})
        assert resp.status_code == 302
        db.session.refresh(bounty)
        assert bounty.status == 'closed'

    def test_close_page_refund_matches_route(self, client, logged_in_user,
                                             sample_user, app):
        """The close page quoted Total Refund = reward − 3% − Stripe
        fee ($940.70 on $1,000) while the refund route returns reward
        − Stripe fee only ($970.70) — the 3% transaction fee was
        deducted twice (once as the top-up the poster paid, once
        from the reward). The page now shows the total paid
        ($1,030.00) minus both fees = the route's $970.70."""
        bounty = _paid_bounty(app, sample_user, reward=1000, secured=True)
        resp = client.get(f'/needs/{bounty.id}/close')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'CA$1,030.00' in body
        assert 'CA$970.70' in body
        assert 'CA$940.70' not in body


# ---------------------------------------------------------------------------
# Edit lock: reward/currency frozen while escrow is secured
# ---------------------------------------------------------------------------

class TestEditSecuredBounty:
    def test_edit_secured_reward_refused(self, client, logged_in_user,
                                         sample_user, app):
        """A secured $1,000 CAD bounty could be edited down to €50
        while CA$1,030 of escrow sat behind it — capture, refund
        and match-fee math would all disagree with the need."""
        bounty = _paid_bounty(app, sample_user, reward=1000, secured=True)
        resp = client.post(f'/needs/{bounty.id}/edit',
                           data=_edit_data(reward_amount='50',
                                           currency='EUR'))
        assert resp.status_code == 200  # re-rendered with the error
        db.session.refresh(bounty)
        assert float(bounty.reward_amount) == 1000
        assert bounty.currency.upper() == 'CAD'

    def test_edit_secured_other_fields_allowed(self, client, logged_in_user,
                                               sample_user, app):
        """The lock covers only reward + currency: title/description
        edits on a secured need still work."""
        bounty = _paid_bounty(app, sample_user, reward=1000, secured=True)
        resp = client.post(f'/needs/{bounty.id}/edit',
                           data=_edit_data(title='Still secured title here'))
        assert resp.status_code == 302
        db.session.refresh(bounty)
        assert bounty.title == 'Still secured title here'
        assert float(bounty.reward_amount) == 1000

    def test_edit_unsecured_reward_allowed(self, client, logged_in_user,
                                           sample_user, app):
        bounty = _paid_bounty(app, sample_user, reward=1000, secured=False)
        resp = client.post(f'/needs/{bounty.id}/edit',
                           data=_edit_data(reward_amount='750'))
        assert resp.status_code == 302
        db.session.refresh(bounty)
        assert float(bounty.reward_amount) == 750


# ---------------------------------------------------------------------------
# Escrow idempotency key covers the amount
# ---------------------------------------------------------------------------

class TestEscrowKeyCoversAmount:
    def test_resecure_after_reward_edit_gets_fresh_key(
            self, client, logged_in_user, app, sample_user):
        """The escrow key was per bounty + payment state only, so
        editing an unsecured need's reward and securing again
        reused the key with a different amount — Stripe rejects a
        reused key with different parameters and the poster got a
        misleading 'Stripe is not configured' error. The amount is
        now in the key; an unchanged re-secure keeps the same key."""
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, reward=1000, secured=False)
        keys = []
        with patch('stripe.checkout.Session.create',
                   side_effect=lambda **kw: keys.append(
                       kw.get('idempotency_key')) or _mock_checkout_session()):
            client.get(f'/needs/{bounty.id}/secure-payment')
            bounty.reward_amount = 2000
            db.session.commit()
            client.get(f'/needs/{bounty.id}/secure-payment')
            client.get(f'/needs/{bounty.id}/secure-payment')
        assert keys[0] == (
            f'escrow-{bounty.id}-unsecured-103000-{sample_user.id}'
        )
        assert keys[1] == (
            f'escrow-{bounty.id}-unsecured-206000-{sample_user.id}'
        )
        assert keys[1] != keys[0]
        assert keys[2] == keys[1]


# ---------------------------------------------------------------------------
# Checkout idempotency keys: bump pack / single bump / boost / upgrade
# ---------------------------------------------------------------------------

class TestCheckoutIdempotencyKeys:
    def test_bump_pack_uses_idempotency_key(self, client, logged_in_user,
                                            app, sample_user):
        _set_stripe_keys()
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            resp = client.post('/payments/bumps/packs/buy',
                               data={'pack_size': '5'})
        assert resp.status_code == 302
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        assert kwargs['idempotency_key'].startswith(
            f'bump-pack-{sample_user.id}-5-')
        name = kwargs['line_items'][0]['price_data']['product_data']['name']
        assert 'CAD' in name

    def test_double_click_bump_pack_returns_same_key(
            self, client, logged_in_user, app, sample_user):
        """Two rapid buys of the same pack share the key, so the
        second returns the first session instead of a second charge."""
        _set_stripe_keys()
        keys = []
        with patch('stripe.checkout.Session.create',
                   side_effect=lambda **kw: keys.append(
                       kw.get('idempotency_key')) or _mock_checkout_session()):
            client.post('/payments/bumps/packs/buy', data={'pack_size': '5'})
            client.post('/payments/bumps/packs/buy', data={'pack_size': '5'})
        assert len(keys) == 2
        assert keys[0] == keys[1]

    def test_single_bump_and_boost_use_idempotency_keys(
            self, client, app, sample_user, sample_hunter, login_as):
        _set_stripe_keys()
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        login_as(client, 'testuser@example.com', 'TestPass123!')
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            resp = client.post(
                f'/payments/bumps/single/{post.id}/buy')
        assert resp.status_code == 302
        kwargs = create.call_args.kwargs
        assert kwargs['idempotency_key'].startswith(
            f'bump-single-{post.id}-')
        name = kwargs['line_items'][0]['price_data']['product_data']['name']
        assert 'CAD' in name
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            resp = client.post(
                f'/payments/bumps/boost/{post.id}/buy',
                data={'days': '2'})
        assert resp.status_code == 302
        kwargs = create.call_args.kwargs
        assert kwargs['idempotency_key'].startswith(f'boost-{post.id}-2-')
        name = kwargs['line_items'][0]['price_data']['product_data']['name']
        assert 'CAD' in name

    def test_upgrade_and_extend_use_idempotency_keys(
            self, client, logged_in_user, app, sample_user):
        """Upgrades had no key at all: a double-click created two
        Checkout sessions and the webhook created two upgrades for
        two charges."""
        _set_stripe_keys()
        bounty = _paid_bounty(app, sample_user, reward=1000, secured=False)
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            resp = client.post(f'/needs/{bounty.id}/upgrade',
                               data={'upgrade_type': 'highlight',
                                     'duration_days': '7'})
        assert resp.status_code == 302
        kwargs = create.call_args.kwargs
        assert kwargs['idempotency_key'].startswith(
            f'upgrade-{bounty.id}-highlight-7-')
        name = kwargs['line_items'][0]['price_data']['product_data']['name']
        assert 'CAD' in name
        upgrade = BountyUpgrade(
            bounty_id=bounty.id, upgrade_type='highlight',
            duration_days=7, price_paid=7.00, is_active=True,
            starts_at=_naive(utc_now()),
            expires_at=_naive(utc_now()) + timedelta(days=7),
        )
        db.session.add(upgrade)
        db.session.commit()
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            resp = client.post(f'/needs/{bounty.id}/upgrade/extend',
                               data={'upgrade_type': 'highlight',
                                     'duration_days': '3'})
        assert resp.status_code == 302
        kwargs = create.call_args.kwargs
        assert kwargs['idempotency_key'].startswith(
            f'upgrade-extend-{upgrade.id}-3-')

    def test_pro_and_recurring_descriptions_say_cad(self, app, sample_user):
        """The Pro and recurring-bump Checkout descriptions showed a
        bare $ — the charge is CAD, so the description says so."""
        _set_stripe_keys()
        with patch('stripe.checkout.Session.create',
                   return_value=_mock_checkout_session()) as create:
            create_pro_checkout(sample_user, 'https://x.test/ok',
                                'https://x.test/cancel')
        name = create.call_args.kwargs['line_items'][0]['price_data'][
            'product_data']['name']
        assert 'CA$9' in name


# ---------------------------------------------------------------------------
# Founding 12-month window is enforced in code
# ---------------------------------------------------------------------------

class TestFoundingWindow:
    def test_fee_rate_expires_after_12_months(self, app, sample_hunter):
        sample_hunter.founding_referrer = True
        sample_hunter.founding_referrer_since = _naive(utc_now())
        db.session.commit()
        assert fee_rate_for(sample_hunter) == 15
        sample_hunter.founding_referrer_since = (
            _naive(utc_now()) - timedelta(days=400))
        db.session.commit()
        assert fee_rate_for(sample_hunter) == 20

    def test_fee_rate_grandfathered_without_start_date(
            self, app, sample_hunter):
        """A flag with no recorded grant date predates window
        tracking — the benefit stays, exactly as before."""
        sample_hunter.founding_referrer = True
        sample_hunter.founding_referrer_since = None
        db.session.commit()
        assert founding_benefit_active(sample_hunter) is True
        assert fee_rate_for(sample_hunter) == 15

    def test_pro_free_expires_with_window(self, app, sample_user):
        sample_user.founding_referrer = True
        sample_user.founding_referrer_since = _naive(utc_now())
        db.session.commit()
        assert is_pro(sample_user) is True
        sample_user.founding_referrer_since = (
            _naive(utc_now()) - timedelta(days=400))
        db.session.commit()
        assert is_pro(sample_user) is False

    def test_admin_toggle_starts_and_clears_window(
            self, client, logged_in_admin, app, sample_user):
        """Granting the flag stamps the start date and the flash
        names the expiry; removing it clears the date so a later
        re-grant starts a fresh 12 months."""
        resp = client.post(
            f'/admin/users/{sample_user.id}/founding-referrer',
            follow_redirects=True)
        assert resp.status_code == 200
        assert '15% fee until' in resp.get_data(as_text=True)
        user = db.session.get(User, sample_user.id)
        assert user.founding_referrer is True
        assert user.founding_referrer_since is not None
        resp = client.post(
            f'/admin/users/{sample_user.id}/founding-referrer')
        user = db.session.get(User, sample_user.id)
        assert user.founding_referrer is False
        assert user.founding_referrer_since is None

    def test_founding_window_column_exists(self):
        assert 'founding_referrer_since' in User.__table__.columns
