"""M3 webhook dispatch tests — checkout.session.completed events with
m3_kind metadata (bump pack / single bump / boost / Pro / recurring
bump) and customer.subscription.* events."""
from datetime import timedelta
from unittest.mock import patch

from extensions import db
from models import (
    Boost, Bump, BumpPackPurchase, ProSubscription, RecurringBump, utc_now,
)
from models.admin_settings import AdminSettings
from models.post import Post
from models.profile import Profile
from tests.test_bumps import _make_member, _old_post


def _set_stripe_keys():
    AdminSettings.set('STRIPE_SECRET_KEY', 'sk_test_fake')
    AdminSettings.set('STRIPE_PUBLISHABLE_KEY', 'pk_test_fake')
    AdminSettings.set('STRIPE_WEBHOOK_SECRET', 'whsec_fake')
    db.session.commit()


def _post_event(client, event):
    with patch('stripe.Webhook.construct_event', return_value=event):
        return client.post(
            '/bounties/stripe-webhook',
            data=b'{}',
            headers={'Stripe-Signature': 't=1,v1=fake'},
        )


def _checkout_event(session_id, metadata, subscription=None):
    session = {'id': session_id, 'metadata': metadata}
    if subscription:
        session['subscription'] = subscription
    return {
        'id': f'evt_{session_id}',
        'type': 'checkout.session.completed',
        'data': {'object': session},
    }


class TestM3CheckoutWebhook:
    def test_bump_pack_checkout(self, app, client, sample_user):
        _set_stripe_keys()
        resp = _post_event(client, _checkout_event('cs_wh_pack', {
            'm3_kind': 'bump_pack', 'user_id': sample_user.id,
            'pack_size': '5', 'price_cents': '800',
        }))
        assert resp.status_code == 200
        purchase = BumpPackPurchase.query.filter_by(
            stripe_session_id='cs_wh_pack'
        ).first()
        assert purchase is not None
        assert purchase.pack_size == 5
        assert purchase.bumps_remaining == 5
        # Duplicate delivery is a no-op.
        resp = _post_event(client, _checkout_event('cs_wh_pack', {
            'm3_kind': 'bump_pack', 'user_id': sample_user.id,
            'pack_size': '5', 'price_cents': '800',
        }))
        assert resp.status_code == 200
        assert BumpPackPurchase.query.filter_by(
            user_id=sample_user.id
        ).count() == 1

    def test_single_bump_checkout(self, app, client, sample_user, sample_hunter):
        _set_stripe_keys()
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        resp = _post_event(client, _checkout_event('cs_wh_single', {
            'm3_kind': 'bump_single', 'user_id': sample_user.id,
            'post_id': post.id,
        }))
        assert resp.status_code == 200
        assert Bump.query.filter_by(stripe_session_id='cs_wh_single').first() is not None
        assert db.session.get(Post, post.id).is_bumped is True

    def test_boost_checkout(self, app, client, sample_user, sample_hunter):
        _set_stripe_keys()
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        resp = _post_event(client, _checkout_event('cs_wh_boost', {
            'm3_kind': 'boost', 'user_id': sample_user.id,
            'post_id': post.id, 'days': '3',
        }))
        assert resp.status_code == 200
        boost = Boost.query.filter_by(stripe_session_id='cs_wh_boost').first()
        assert boost is not None
        assert boost.days == 3
        assert db.session.get(Post, post.id).is_boosted is True

    def test_pro_checkout(self, app, client, sample_user):
        _set_stripe_keys()
        resp = _post_event(client, _checkout_event('cs_wh_pro', {
            'm3_kind': 'pro', 'user_id': sample_user.id,
        }, subscription='sub_wh_pro'))
        assert resp.status_code == 200
        sub = ProSubscription.query.filter_by(user_id=sample_user.id).first()
        assert sub is not None
        assert sub.status == 'active'
        assert sub.stripe_subscription_id == 'sub_wh_pro'

    def test_recurring_bump_checkout(self, app, client, sample_user, sample_hunter):
        _set_stripe_keys()
        _make_member(sample_user, sample_hunter)
        post = _old_post(app, sample_user)
        resp = _post_event(client, _checkout_event('cs_wh_rec', {
            'm3_kind': 'recurring_bump', 'user_id': sample_user.id,
            'post_id': post.id,
        }, subscription='sub_wh_rec'))
        assert resp.status_code == 200
        recurring = RecurringBump.query.filter_by(
            stripe_subscription_id='sub_wh_rec'
        ).first()
        assert recurring is not None
        assert recurring.status == 'active'
        assert db.session.get(Post, post.id).is_bumped is True

    def test_unknown_user_returns_200(self, app, client):
        _set_stripe_keys()
        resp = _post_event(client, _checkout_event('cs_wh_nope', {
            'm3_kind': 'bump_pack', 'user_id': '00000000-0000-0000-0000-000000000000',
            'pack_size': '5', 'price_cents': '800',
        }))
        assert resp.status_code == 200  # logged, not retried
        assert BumpPackPurchase.query.count() == 0


class TestSubscriptionWebhook:
    def test_subscription_updated_and_deleted(
        self, app, client, sample_user
    ):
        _set_stripe_keys()
        sub = ProSubscription(
            user_id=sample_user.id, stripe_subscription_id='sub_wh_1',
            status='active',
        )
        db.session.add(sub)
        db.session.commit()
        event = {
            'id': 'evt_sub_upd', 'type': 'customer.subscription.updated',
            'data': {'object': {
                'id': 'sub_wh_1', 'status': 'active',
                'cancel_at_period_end': True,
                'current_period_start': 1790000000,
                'current_period_end': 1792592000,
            }},
        }
        resp = _post_event(client, event)
        assert resp.status_code == 200
        db.session.refresh(sub)
        assert sub.cancel_at_period_end is True
        assert sub.current_period_end is not None

        event = {
            'id': 'evt_sub_del', 'type': 'customer.subscription.deleted',
            'data': {'object': {'id': 'sub_wh_1', 'status': 'canceled'}},
        }
        resp = _post_event(client, event)
        assert resp.status_code == 200
        db.session.refresh(sub)
        assert sub.status == 'canceled'
