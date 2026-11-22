"""M3 Pro analytics tests — gating, checkout fulfillment, subscription
lifecycle (cancel keeps access to period end), CSV export."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from extensions import db
from models import ProSubscription, RecurringBump, utc_now
from models.post_view import PostView, SearchAppearance
from models.profile_view import ProfileView
from utils.analytics import get_pro_analytics, pro_analytics_csv
from utils.pro import (
    cancel_pro_at_period_end,
    create_pro_checkout,
    fulfill_pro_checkout,
    handle_subscription_event,
    is_pro,
)


def _make_pro(user, subscription_id='sub_pro_1', period_end=None):
    sub = ProSubscription(
        user_id=user.id,
        stripe_subscription_id=subscription_id,
        status='active',
        current_period_end=period_end,
    )
    db.session.add(sub)
    db.session.commit()
    db.session.refresh(sub)
    return sub


def _as_ts(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


class TestIsPro:
    def test_no_subscription_not_pro(self, app, sample_user):
        assert is_pro(sample_user) is False

    def test_founding_referrer_is_pro(self, app, sample_user):
        sample_user.founding_referrer = True
        db.session.commit()
        assert is_pro(sample_user) is True

    def test_active_subscription_is_pro(self, app, sample_user):
        _make_pro(sample_user)
        assert is_pro(sample_user) is True

    def test_expired_period_not_pro(self, app, sample_user):
        _make_pro(
            sample_user,
            period_end=utc_now() - timedelta(days=1),
        )
        assert is_pro(sample_user) is False

    def test_canceled_subscription_not_pro(self, app, sample_user):
        sub = _make_pro(sample_user)
        sub.status = 'canceled'
        db.session.commit()
        assert is_pro(sample_user) is False

    def test_cancel_at_period_end_still_pro_until_period_end(
        self, app, sample_user
    ):
        sub = _make_pro(
            sample_user,
            period_end=utc_now() + timedelta(days=10),
        )
        sub.cancel_at_period_end = True
        db.session.commit()
        assert is_pro(sample_user) is True


class TestFulfillProCheckout:
    def test_checkout_creates_subscription(self, app, sample_user):
        session = {
            'id': 'cs_pro_1',
            'subscription': 'sub_pro_10',
            'customer': 'cus_1',
            'metadata': {'m3_kind': 'pro', 'user_id': sample_user.id},
        }
        sub = fulfill_pro_checkout(session)
        db.session.commit()
        assert sub is not None
        assert sub.status == 'active'
        assert sub.stripe_customer_id == 'cus_1'
        assert is_pro(sample_user) is True

    def test_checkout_idempotent_on_subscription_id(self, app, sample_user):
        session = {
            'id': 'cs_pro_2',
            'subscription': 'sub_pro_11',
            'metadata': {'m3_kind': 'pro', 'user_id': sample_user.id},
        }
        sub1 = fulfill_pro_checkout(session)
        db.session.commit()
        sub2 = fulfill_pro_checkout(session)
        db.session.commit()
        assert sub2.id == sub1.id
        assert ProSubscription.query.filter_by(user_id=sample_user.id).count() == 1

    def test_checkout_without_subscription_skipped(self, app, sample_user):
        session = {
            'id': 'cs_pro_3',
            'metadata': {'m3_kind': 'pro', 'user_id': sample_user.id},
        }
        assert fulfill_pro_checkout(session) is None
        assert ProSubscription.query.count() == 0


class TestSubscriptionEvents:
    def test_updated_syncs_period_and_cancel_flag(self, app, sample_user):
        sub = _make_pro(sample_user, subscription_id='sub_pro_20')
        start = utc_now()
        end = utc_now() + timedelta(days=30)
        handle_subscription_event(
            {
                'id': 'sub_pro_20',
                'status': 'active',
                'cancel_at_period_end': True,
                'current_period_start': _as_ts(start),
                'current_period_end': _as_ts(end),
            },
            'customer.subscription.updated',
        )
        db.session.commit()
        db.session.refresh(sub)
        assert sub.status == 'active'
        assert sub.cancel_at_period_end is True
        assert sub.current_period_end is not None
        # Still Pro — cancel takes effect at period end.
        assert is_pro(sample_user) is True

    def test_deleted_marks_canceled(self, app, sample_user):
        sub = _make_pro(sample_user, subscription_id='sub_pro_21')
        handle_subscription_event(
            {'id': 'sub_pro_21', 'status': 'canceled'},
            'customer.subscription.deleted',
        )
        db.session.commit()
        db.session.refresh(sub)
        assert sub.status == 'canceled'
        assert is_pro(sample_user) is False

    def test_unknown_subscription_ignored(self, app, sample_user):
        assert handle_subscription_event(
            {'id': 'sub_unknown', 'status': 'active'},
            'customer.subscription.updated',
        ) is None

    def test_recurring_bump_syncs_from_subscription_event(
        self, app, sample_user, sample_post
    ):
        recurring = RecurringBump(
            post_id=sample_post.id, user_id=sample_user.id,
            stripe_subscription_id='sub_rec_20', status='active',
            next_bump_at=utc_now() + timedelta(days=3),
        )
        db.session.add(recurring)
        db.session.commit()
        handle_subscription_event(
            {
                'id': 'sub_rec_20',
                'status': 'active',
                'cancel_at_period_end': True,
            },
            'customer.subscription.updated',
        )
        db.session.commit()
        db.session.refresh(recurring)
        assert recurring.cancel_at_period_end is True
        assert recurring.status == 'active'
        handle_subscription_event(
            {'id': 'sub_rec_20', 'status': 'canceled'},
            'customer.subscription.deleted',
        )
        db.session.commit()
        db.session.refresh(recurring)
        assert recurring.status == 'canceled'


class TestCancelPro:
    def test_cancel_sets_flag_when_stripe_agrees(self, app, sample_user):
        sub = _make_pro(sample_user)
        with patch(
            'utils.payments.cancel_subscription_at_period_end',
            return_value=(True, None),
        ):
            ok, error = cancel_pro_at_period_end(sub)
        assert (ok, error) == (True, None)
        assert sub.cancel_at_period_end is True

    def test_cancel_fails_when_stripe_fails(self, app, sample_user):
        sub = _make_pro(sample_user)
        with patch(
            'utils.payments.cancel_subscription_at_period_end',
            return_value=(False, 'boom'),
        ):
            ok, error = cancel_pro_at_period_end(sub)
        assert ok is False
        assert error == 'boom'
        assert sub.cancel_at_period_end is False


class TestProCheckoutSession:
    def test_subscription_mode_nine_dollars(self, app, sample_user):
        from models.admin_settings import AdminSettings
        AdminSettings.set('STRIPE_SECRET_KEY', 'sk_test_fake')
        db.session.commit()
        session = SimpleNamespace(url='https://checkout.stripe.com/pro')
        with patch(
            'stripe.checkout.Session.create', return_value=session
        ) as create:
            url = create_pro_checkout(
                sample_user, 'https://x/success', 'https://x/cancel'
            )
        assert url == 'https://checkout.stripe.com/pro'
        kwargs = create.call_args.kwargs
        assert kwargs['mode'] == 'subscription'
        line = kwargs['line_items'][0]
        assert line['price_data']['unit_amount'] == 900
        assert line['price_data']['currency'] == 'cad'
        assert line['price_data']['recurring'] == {'interval': 'month'}
        assert kwargs['metadata'] == {
            'm3_kind': 'pro', 'user_id': sample_user.id,
        }


class TestProGating:
    def test_free_user_sees_cta_not_pro_section(
        self, app, client, sample_user, login_as
    ):
        login_as(client, sample_user.email, 'TestPass123!')
        resp = client.get('/social/analytics')
        assert resp.status_code == 200
        assert b'Get Pro' in resp.data
        assert b'Download CSV' not in resp.data

    def test_pro_user_sees_pro_section(
        self, app, client, sample_user, sample_post, login_as
    ):
        _make_pro(sample_user)
        login_as(client, sample_user.email, 'TestPass123!')
        resp = client.get('/social/analytics')
        assert resp.status_code == 200
        assert b'Pro analytics' in resp.data
        assert b'Download CSV' in resp.data
        assert b'Members reached' in resp.data
        assert b'Referral funnel' in resp.data

    def test_export_blocked_for_free_user(
        self, app, client, sample_user, login_as
    ):
        login_as(client, sample_user.email, 'TestPass123!')
        resp = client.get('/social/analytics/export')
        assert resp.status_code in (302, 303)
        assert '/payments/pro' in resp.headers['Location']

    def test_export_csv_for_pro_user(
        self, app, client, sample_user, sample_post, login_as
    ):
        _make_pro(sample_user)
        login_as(client, sample_user.email, 'TestPass123!')
        resp = client.get('/social/analytics/export')
        assert resp.status_code == 200
        assert resp.mimetype == 'text/csv'
        text = resp.data.decode()
        assert 'Post,Type,Views,Members reached' in text
        assert 'Referral funnel' in text
        assert 'TOTAL' in text

    def test_pro_page_founding_free(
        self, app, client, sample_user, login_as
    ):
        from models.user import User
        # Re-fetch: the fixture instance is detached, so setting the
        # flag on it would not persist to the DB the request reads.
        user = db.session.get(User, sample_user.id)
        user.founding_referrer = True
        db.session.commit()
        login_as(client, sample_user.email, 'TestPass123!')
        resp = client.get('/payments/pro')
        assert resp.status_code == 200
        assert b'Pro is free for you' in resp.data


class TestProAnalyticsData:
    def test_members_reached_counts_distinct_viewers(
        self, app, sample_user, sample_post, second_user, third_user
    ):
        # Two logged-in viewers (one views twice) + one anonymous view.
        for viewer in (second_user, third_user):
            db.session.add(PostView(
                post_id=sample_post.id, viewer_id=viewer.id
            ))
        db.session.add(PostView(post_id=sample_post.id, viewer_id=second_user.id))
        db.session.add(PostView(post_id=sample_post.id, viewer_id=None))
        db.session.commit()
        pro = get_pro_analytics(sample_user.id)
        assert len(pro['post_rows']) == 1
        assert pro['post_rows'][0]['reached'] == 3  # 2 logged-in + 1 anonymous

    def test_engagement_rate_none_without_reach(
        self, app, sample_user, sample_post
    ):
        pro = get_pro_analytics(sample_user.id)
        assert pro['post_rows'][0]['engagement_rate'] is None

    def test_named_viewers_opt_in_only(
        self, app, sample_user, second_user, third_user
    ):
        from models.profile import Profile
        # second_user opts in, third_user does not.
        Profile.query.filter_by(user_id=second_user.id).first().profile_view_names = True
        for viewer in (second_user, third_user):
            db.session.add(ProfileView(
                profile_user_id=sample_user.id, viewer_id=viewer.id
            ))
        db.session.commit()
        pro = get_pro_analytics(sample_user.id)
        assert pro['profile_views']['total_90d'] == 2
        assert pro['profile_views']['named_viewers'] == [second_user.full_name]

    def test_search_appearances_counted(
        self, app, sample_user, second_user
    ):
        db.session.add(SearchAppearance(
            searched_user_id=sample_user.id, viewer_id=second_user.id,
            search_query='python',
        ))
        db.session.add(SearchAppearance(
            searched_user_id=sample_user.id, viewer_id=second_user.id,
            search_query='python',
        ))
        db.session.commit()
        pro = get_pro_analytics(sample_user.id)
        assert pro['search']['total_90d'] == 2
        assert pro['search']['top_queries'] == [('python', 2)]

    def test_csv_contents(self, app, sample_user, sample_post):
        text = pro_analytics_csv(sample_user.id)
        assert 'Post,Type,Views,Members reached' in text
        assert 'Hello network' in text  # the sample post body
        assert 'Referral funnel' in text
        assert 'TOTAL,0,0,0,0' in text
