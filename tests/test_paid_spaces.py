"""Paid communities (paywalled Spaces) tests.

Covers: space access control (pitch page vs posts), the admin
owner/price flow, the fee split, checkout fulfillment, the
invoice.paid owner payout (transfer vs pending_stripe), the
subscription lifecycle (cancel keeps access to period end), and
the join/leave/cancel routes. All Stripe calls are mocked — no
network calls, no real keys, no real money movement.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from extensions import db
from models import AdminSettings, ConnectAccount, Space, SpaceMembership, utc_now
from models.post import Post
from models.space_payout import SpacePayout
from models.user import User
from tests.test_m3_webhook import _post_event, _set_stripe_keys
from utils.paid_spaces import (
    fulfill_paid_space_checkout,
    get_space_platform_fee_percent,
    handle_space_invoice_paid,
    split_space_payment,
)
from utils.pro import handle_subscription_event


def _make_space(owner=None, paid=False, price_cents=None, slug='paid-club'):
    space = Space(
        slug=slug, name='Paid Club', description='Members only.',
        icon='layers',
        owner_id=owner.id if owner else None,
        is_paid=paid, price_cents=price_cents,
    )
    db.session.add(space)
    db.session.commit()
    db.session.refresh(space)
    return space


def _make_membership(user, space, subscription_id='sub_space_1',
                     status='active', period_end=None):
    membership = SpaceMembership(
        user_id=user.id, space_id=space.id,
        stripe_subscription_id=subscription_id,
        status=status, current_period_end=period_end,
    )
    db.session.add(membership)
    db.session.commit()
    db.session.refresh(membership)
    return membership


def _make_post(app, author, space, body='Secret members-only post body.'):
    post = Post(author_id=author.id, post_type='update', body=body,
                space_id=space.id)
    db.session.add(post)
    db.session.commit()
    db.session.refresh(post)
    return post


def _ready_connect(user):
    connect = ConnectAccount(
        user_id=user.id, stripe_account_id='acct_owner_1',
        onboarding_status='complete',
        charges_enabled=True, payouts_enabled=True,
        details_submitted=True,
    )
    db.session.add(connect)
    db.session.commit()
    return connect


def _invoice_event(invoice_id, subscription_id, amount_paid=1000):
    return {
        'id': f'evt_{invoice_id}',
        'type': 'invoice.paid',
        'data': {'object': {
            'id': invoice_id,
            'subscription': subscription_id,
            'amount_paid': amount_paid,
            'currency': 'cad',
            'charge': 'ch_space_1',
            'period_end': int(
                (utc_now() + timedelta(days=30)).timestamp()
            ),
        }},
    }


class TestFeeSplit:
    def test_default_fee_is_20_owner_keeps_80(self, app):
        assert get_space_platform_fee_percent() == 20.0
        fee, owner = split_space_payment(1000)
        assert (fee, owner) == (200, 800)

    def test_fee_is_its_own_setting(self, app):
        AdminSettings.set('SPACE_PLATFORM_FEE_PERCENT', '30')
        AdminSettings.set('PLATFORM_FEE_PERCENT', '50')
        db.session.commit()
        assert get_space_platform_fee_percent() == 30.0
        fee, owner = split_space_payment(1000)
        assert (fee, owner) == (300, 700)

    def test_fee_clamped_and_bad_value_falls_back(self, app):
        AdminSettings.set('SPACE_PLATFORM_FEE_PERCENT', '99')
        db.session.commit()
        assert get_space_platform_fee_percent() == 50.0
        AdminSettings.set('SPACE_PLATFORM_FEE_PERCENT', 'not-a-number')
        db.session.commit()
        assert get_space_platform_fee_percent() == 20.0


class TestSpaceAccess:
    def test_free_space_open_to_everyone(self, app, sample_user, second_user):
        space = _make_space(slug='free-club')
        assert space.can_view(None) is True
        assert space.can_view(second_user) is True

    def test_paid_space_closed_to_non_members(self, app, sample_user,
                                              second_user):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        assert space.can_view(None) is False
        assert space.can_view(second_user) is False
        # Owner and admins can always see it.
        assert space.can_view(sample_user) is True

    def test_paid_space_open_to_active_member(self, app, sample_user,
                                              second_user):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_membership(second_user, space)
        assert space.can_view(second_user) is True

    def test_lapsed_member_loses_access(self, app, sample_user, second_user):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        membership = _make_membership(
            second_user, space, status='canceled',
            period_end=utc_now() - timedelta(days=1),
        )
        assert membership.is_active is False
        assert space.can_view(second_user) is False

    def test_free_membership_always_active(self, app, sample_user, second_user):
        space = _make_space(slug='free-club')
        membership = SpaceMembership(user_id=second_user.id, space_id=space.id)
        db.session.add(membership)
        db.session.commit()
        assert membership.is_active is True


class TestSpaceDetailGate:
    def test_non_member_sees_pitch_not_posts(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_post(app, sample_user, space)
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get(f'/social/spaces/{space.slug}')
        assert resp.status_code == 200
        assert b'Secret members-only post body.' not in resp.data
        assert b'This is a paid space' in resp.data
        assert b'$9.99' in resp.data

    def test_member_sees_posts(self, app, client, sample_user, second_user,
                                login_as):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_post(app, sample_user, space)
        _make_membership(second_user, space)
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get(f'/social/spaces/{space.slug}')
        assert resp.status_code == 200
        assert b'Secret members-only post body.' in resp.data

    def test_anonymous_sees_pitch(self, app, client, sample_user):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_post(app, sample_user, space)
        resp = client.get(f'/social/spaces/{space.slug}')
        assert resp.status_code == 200
        assert b'Secret members-only post body.' not in resp.data
        assert b'Log in to join' in resp.data

    def test_free_space_unchanged_for_non_member(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(slug='free-club')
        _make_post(app, sample_user, space)
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get(f'/social/spaces/{space.slug}')
        assert resp.status_code == 200
        assert b'Secret members-only post body.' in resp.data

    def test_post_detail_gated_for_non_member(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        post = _make_post(app, sample_user, space)
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get(f'/social/posts/{post.id}')
        assert resp.status_code == 302
        assert f'/social/spaces/{space.slug}' in resp.headers['Location']


class TestCheckoutFulfillment:
    def test_fulfill_creates_active_membership(self, app, sample_user,
                                               second_user):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        session = {
            'id': 'cs_space_1', 'subscription': 'sub_space_10',
            'metadata': {
                'm3_kind': 'paid_space', 'user_id': second_user.id,
                'space_id': space.id,
            },
        }
        membership = fulfill_paid_space_checkout(session)
        db.session.commit()
        assert membership is not None
        assert membership.status == 'active'
        assert membership.stripe_subscription_id == 'sub_space_10'
        assert space.can_view(second_user) is True

    def test_fulfill_idempotent_on_subscription_id(
        self, app, sample_user, second_user
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        session = {
            'id': 'cs_space_2', 'subscription': 'sub_space_11',
            'metadata': {
                'm3_kind': 'paid_space', 'user_id': second_user.id,
                'space_id': space.id,
            },
        }
        first = fulfill_paid_space_checkout(session)
        db.session.commit()
        second = fulfill_paid_space_checkout(session)
        db.session.commit()
        assert second.id == first.id
        assert SpaceMembership.query.filter_by(
            user_id=second_user.id, space_id=space.id,
        ).count() == 1

    def test_webhook_checkout_creates_membership(
        self, app, client, sample_user, second_user
    ):
        _set_stripe_keys()
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        event = {
            'id': 'evt_cs_space_3',
            'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_space_3', 'subscription': 'sub_space_12',
                'metadata': {
                    'm3_kind': 'paid_space', 'user_id': second_user.id,
                    'space_id': space.id,
                },
            }},
        }
        resp = _post_event(client, event)
        assert resp.status_code == 200
        membership = SpaceMembership.query.filter_by(
            stripe_subscription_id='sub_space_12'
        ).first()
        assert membership is not None
        assert membership.status == 'active'

    def test_join_paid_space_redirects_to_checkout(
        self, app, client, sample_user, second_user, login_as
    ):
        _set_stripe_keys()
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        login_as(client, second_user.email, 'TestPass123!')
        checkout = SimpleNamespace(
            url='https://checkout.stripe.com/paid-space'
        )
        with patch(
            'stripe.checkout.Session.create', return_value=checkout
        ) as create:
            resp = client.post(f'/social/spaces/{space.slug}/join')
        assert resp.status_code == 302
        assert resp.headers['Location'] == 'https://checkout.stripe.com/paid-space'
        kwargs = create.call_args.kwargs
        assert kwargs['mode'] == 'subscription'
        line = kwargs['line_items'][0]
        assert line['price_data']['unit_amount'] == 999
        assert line['price_data']['currency'] == 'cad'
        assert kwargs['metadata']['m3_kind'] == 'paid_space'
        assert kwargs['metadata']['space_id'] == space.id

    def test_join_free_space_still_joins_directly(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(slug='free-club')
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.post(f'/social/spaces/{space.slug}/join')
        assert resp.status_code == 302
        assert SpaceMembership.query.filter_by(
            user_id=second_user.id, space_id=space.id
        ).first() is not None


class TestInvoicePaidPayout:
    def test_owner_with_connect_gets_transfer(
        self, app, client, sample_user, second_user
    ):
        _set_stripe_keys()
        _ready_connect(sample_user)
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        _make_membership(second_user, space, subscription_id='sub_space_20')
        transfer = SimpleNamespace(id='tr_space_1')
        with patch(
            'stripe.Transfer.create', return_value=transfer
        ) as create:
            resp = _post_event(
                client, _invoice_event('in_space_1', 'sub_space_20', 1000)
            )
        assert resp.status_code == 200
        payout = SpacePayout.query.filter_by(
            stripe_invoice_id='in_space_1'
        ).first()
        assert payout is not None
        assert payout.gross_cents == 1000
        assert payout.platform_fee_cents == 200
        assert payout.amount_cents == 800
        assert payout.status == 'processing'
        assert payout.stripe_transfer_id == 'tr_space_1'
        kwargs = create.call_args.kwargs
        assert kwargs['amount'] == 800
        assert kwargs['destination'] == 'acct_owner_1'
        assert kwargs['source_transaction'] == 'ch_space_1'

    def test_owner_without_connect_gets_pending_stripe(
        self, app, client, sample_user, second_user
    ):
        _set_stripe_keys()
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        _make_membership(second_user, space, subscription_id='sub_space_21')
        with patch('stripe.Transfer.create') as create:
            resp = _post_event(
                client, _invoice_event('in_space_2', 'sub_space_21', 1000)
            )
        assert resp.status_code == 200
        create.assert_not_called()
        payout = SpacePayout.query.filter_by(
            stripe_invoice_id='in_space_2'
        ).first()
        assert payout is not None
        assert payout.status == 'pending_stripe'
        assert payout.amount_cents == 800
        # The owner is prompted to connect payouts.
        from models.notification import Notification
        note = Notification.query.filter_by(
            recipient_id=sample_user.id, type='payout_pending'
        ).first()
        assert note is not None

    def test_duplicate_invoice_paid_transfers_once(
        self, app, client, sample_user, second_user
    ):
        _set_stripe_keys()
        _ready_connect(sample_user)
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        _make_membership(second_user, space, subscription_id='sub_space_22')
        transfer = SimpleNamespace(id='tr_space_2')
        with patch(
            'stripe.Transfer.create', return_value=transfer
        ) as create:
            _post_event(
                client, _invoice_event('in_space_3', 'sub_space_22', 1000)
            )
            # Same Stripe event id redelivered: deduped by the event log.
            _post_event(
                client, _invoice_event('in_space_3', 'sub_space_22', 1000)
            )
            # Same invoice under a new event id: deduped by invoice id.
            event = _invoice_event('in_space_3', 'sub_space_22', 1000)
            event['id'] = 'evt_in_space_3_retry'
            _post_event(client, event)
        assert create.call_count == 1
        assert SpacePayout.query.filter_by(
            stripe_invoice_id='in_space_3'
        ).count() == 1

    def test_zero_invoice_owes_nothing(self, app, client, sample_user,
                                       second_user):
        _set_stripe_keys()
        _ready_connect(sample_user)
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        _make_membership(second_user, space, subscription_id='sub_space_23')
        with patch('stripe.Transfer.create') as create:
            resp = _post_event(
                client, _invoice_event('in_space_4', 'sub_space_23', 0)
            )
        assert resp.status_code == 200
        create.assert_not_called()
        payout = SpacePayout.query.filter_by(
            stripe_invoice_id='in_space_4'
        ).first()
        assert payout is not None
        assert payout.status == 'completed'
        assert payout.amount_cents == 0

    def test_pending_payout_retried_on_next_invoice(
        self, app, client, sample_user, second_user
    ):
        _set_stripe_keys()
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        _make_membership(second_user, space, subscription_id='sub_space_24')
        _post_event(client, _invoice_event('in_space_5', 'sub_space_24', 1000))
        pending = SpacePayout.query.filter_by(
            stripe_invoice_id='in_space_5'
        ).first()
        assert pending.status == 'pending_stripe'
        # Owner finishes Connect onboarding.
        _ready_connect(sample_user)
        transfer = SimpleNamespace(id='tr_space_5')
        with patch(
            'stripe.Transfer.create', return_value=transfer
        ) as create:
            _post_event(
                client, _invoice_event('in_space_6', 'sub_space_24', 1000)
            )
        # Both the old pending row and the new invoice transferred.
        assert create.call_count == 2
        db.session.refresh(pending)
        assert pending.status == 'processing'

    def test_transfer_paid_completes_payout(
        self, app, client, sample_user, second_user
    ):
        _set_stripe_keys()
        _ready_connect(sample_user)
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        _make_membership(second_user, space, subscription_id='sub_space_25')
        transfer = SimpleNamespace(id='tr_space_6')
        with patch('stripe.Transfer.create', return_value=transfer):
            _post_event(
                client, _invoice_event('in_space_7', 'sub_space_25', 1000)
            )
        event = {
            'id': 'evt_tr_space_6_paid',
            'type': 'transfer.paid',
            'data': {'object': {'id': 'tr_space_6'}},
        }
        resp = _post_event(client, event)
        assert resp.status_code == 200
        payout = SpacePayout.query.filter_by(
            stripe_invoice_id='in_space_7'
        ).first()
        assert payout.status == 'completed'


class TestSubscriptionLifecycle:
    def test_deleted_lapses_membership(self, app, sample_user, second_user):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        membership = _make_membership(
            second_user, space, subscription_id='sub_space_30',
            period_end=utc_now() + timedelta(days=30),
        )
        assert membership.is_active is True
        handle_subscription_event(
            {'id': 'sub_space_30', 'status': 'canceled'},
            'customer.subscription.deleted',
        )
        db.session.commit()
        db.session.refresh(membership)
        assert membership.status == 'canceled'
        assert membership.is_active is False
        assert space.can_view(second_user) is False

    def test_cancel_at_period_end_keeps_access(self, app, sample_user,
                                               second_user):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        membership = _make_membership(
            second_user, space, subscription_id='sub_space_31',
            period_end=utc_now() + timedelta(days=10),
        )
        handle_subscription_event(
            {
                'id': 'sub_space_31', 'status': 'active',
                'cancel_at_period_end': True,
                'current_period_end': int(
                    (utc_now() + timedelta(days=10)).timestamp()
                ),
            },
            'customer.subscription.updated',
        )
        db.session.commit()
        db.session.refresh(membership)
        assert membership.status == 'active'
        assert membership.cancel_at_period_end is True
        assert membership.is_active is True

    def test_cancel_route_sets_cancel_at_period_end(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        membership = _make_membership(
            second_user, space, subscription_id='sub_space_32',
            period_end=utc_now() + timedelta(days=10),
        )
        login_as(client, second_user.email, 'TestPass123!')
        with patch(
            'utils.paid_spaces.cancel_subscription_at_period_end',
            return_value=(True, None),
        ):
            resp = client.post(f'/social/spaces/{space.slug}/cancel')
        assert resp.status_code == 302
        db.session.refresh(membership)
        assert membership.cancel_at_period_end is True
        assert membership.is_active is True

    def test_leave_paid_active_membership_kept(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_membership(
            second_user, space, subscription_id='sub_space_33',
            period_end=utc_now() + timedelta(days=10),
        )
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.post(f'/social/spaces/{space.slug}/leave')
        assert resp.status_code == 302
        # The membership survives — leaving must not orphan a live
        # subscription that keeps charging.
        assert SpaceMembership.query.filter_by(
            user_id=second_user.id, space_id=space.id
        ).first() is not None

    def test_leave_free_space_still_works(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(slug='free-club')
        membership = SpaceMembership(
            user_id=second_user.id, space_id=space.id
        )
        db.session.add(membership)
        db.session.commit()
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.post(f'/social/spaces/{space.slug}/leave')
        assert resp.status_code == 302
        assert SpaceMembership.query.filter_by(
            user_id=second_user.id, space_id=space.id
        ).first() is None


class TestPostIntoPaidSpace:
    def test_non_member_cannot_post_into_paid_space(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.post('/social/posts/new', data={
            'post_type': 'update',
            'body': 'Trying to post into a paid space.',
            'space_id': space.id,
        })
        assert resp.status_code == 200
        assert Post.query.filter_by(space_id=space.id).count() == 0

    def test_member_can_post_into_paid_space(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_membership(second_user, space)
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.post('/social/posts/new', data={
            'post_type': 'update',
            'body': 'A members-only post.',
            'space_id': space.id,
        })
        assert resp.status_code == 302
        assert Post.query.filter_by(space_id=space.id).count() == 1


class TestAdminSpaces:
    def test_spaces_list_requires_admin(self, client, sample_user):
        resp = client.get('/admin/spaces')
        assert resp.status_code == 302

    def test_admin_can_set_owner_and_price(
        self, logged_in_admin, client, app, sample_user, second_user
    ):
        space = _make_space(slug='free-club')
        resp = logged_in_admin.post(
            f'/admin/spaces/{space.id}/edit',
            data={
                'owner_email': second_user.email,
                'is_paid': 'on',
                'price': '9.99',
            },
        )
        assert resp.status_code == 302
        db.session.refresh(space)
        assert space.owner_id == second_user.id
        assert space.is_paid is True
        assert space.price_cents == 999

    def test_paid_space_requires_owner(
        self, logged_in_admin, client, app
    ):
        space = _make_space(slug='free-club')
        resp = logged_in_admin.post(
            f'/admin/spaces/{space.id}/edit',
            data={'owner_email': '', 'is_paid': 'on', 'price': '9.99'},
        )
        assert resp.status_code == 302
        db.session.refresh(space)
        assert space.is_paid is False

    def test_paid_space_requires_positive_price(
        self, logged_in_admin, client, app, second_user
    ):
        space = _make_space(slug='free-club')
        resp = logged_in_admin.post(
            f'/admin/spaces/{space.id}/edit',
            data={
                'owner_email': second_user.email,
                'is_paid': 'on',
                'price': '0',
            },
        )
        assert resp.status_code == 302
        db.session.refresh(space)
        assert space.is_paid is False

    def test_unknown_owner_email_rejected(
        self, logged_in_admin, client, app
    ):
        space = _make_space(slug='free-club')
        resp = logged_in_admin.post(
            f'/admin/spaces/{space.id}/edit',
            data={
                'owner_email': 'nobody@example.com',
                'is_paid': 'on',
                'price': '9.99',
            },
        )
        assert resp.status_code == 302
        db.session.refresh(space)
        assert space.is_paid is False
        assert space.owner_id is None

    def test_admin_can_make_space_free_again(
        self, logged_in_admin, client, app, sample_user, second_user
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        resp = logged_in_admin.post(
            f'/admin/spaces/{space.id}/edit',
            data={'owner_email': sample_user.email, 'price': ''},
        )
        assert resp.status_code == 302
        db.session.refresh(space)
        assert space.is_paid is False

    def test_cannot_free_space_with_live_subscriptions(
        self, logged_in_admin, client, app, sample_user, second_user
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_membership(
            second_user, space, subscription_id='sub_space_40',
            period_end=utc_now() + timedelta(days=10),
        )
        resp = logged_in_admin.post(
            f'/admin/spaces/{space.id}/edit',
            data={'owner_email': sample_user.email, 'price': ''},
        )
        assert resp.status_code == 302
        db.session.refresh(space)
        assert space.is_paid is True

    def test_space_fee_setting_saved_from_settings(
        self, logged_in_admin, client, app
    ):
        resp = logged_in_admin.post('/admin/settings', data={
            'platform_fee_percent': '20',
            'space_platform_fee_percent': '25',
            'auto_hide_threshold': '3',
            'post_limit_newcomer': '3',
            'post_limit_member': '10',
        })
        assert resp.status_code == 302
        assert AdminSettings.get('SPACE_PLATFORM_FEE_PERCENT') == '25'
        assert get_space_platform_fee_percent() == 25.0
