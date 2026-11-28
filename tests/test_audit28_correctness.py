"""Audit 2026-11-28 (correctness slice) probe + regression tests.

Scope: the tips + paid-communities build (main @ 04342c7 -> 6eea3f2).
Every bug found here has a test in this file; no fix without one.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import flask_migrate
import pytest
from sqlalchemy import inspect, text

from app import create_app
from extensions import db
from models import Space, SpaceMembership, Tip, utc_now
from models.post import Post
from tests.test_paid_spaces import (
    _invoice_event,
    _make_membership,
    _make_post,
    _make_space,
    _ready_connect,
)
from tests.test_m3_webhook import _post_event, _set_stripe_keys
from utils.tips import tips_received_summary


def _free_membership(user, space):
    m = SpaceMembership(user_id=user.id, space_id=space.id)
    db.session.add(m)
    db.session.commit()
    db.session.refresh(m)
    return m


# ---------------------------------------------------------------------------
# 1. Migration: single head, downgrade to perf_audit25_indexes, re-upgrade
# ---------------------------------------------------------------------------

class TestMigrationRoundTrip:
    def test_tips_migration_downgrades_and_reupgrades(self, tmp_path):
        db_file = tmp_path / 'audit28_mig.db'
        test_app = create_app('testing', config_overrides={
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_file}',
        })
        with test_app.app_context():
            flask_migrate.upgrade()
            assert db.session.execute(
                text('SELECT version_num FROM alembic_version')
            ).scalar() == 'tips_paid_communities'
            flask_migrate.downgrade(revision='perf_audit25_indexes')
            assert db.session.execute(
                text('SELECT version_num FROM alembic_version')
            ).scalar() == 'perf_audit25_indexes'
            inspector = inspect(db.engine)
            assert 'tips' not in inspector.get_table_names()
            assert 'space_payouts' not in inspector.get_table_names()
            assert 'owner_id' not in [
                c['name'] for c in inspector.get_columns('spaces')
            ]
            flask_migrate.upgrade()
            assert db.session.execute(
                text('SELECT version_num FROM alembic_version')
            ).scalar() == 'tips_paid_communities'
            assert 'tips' in inspect(db.engine).get_table_names()


# ---------------------------------------------------------------------------
# 2. Space.can_view matrix (incl. grandfathered free memberships)
# ---------------------------------------------------------------------------

class TestCanViewMatrix:
    def test_matrix(self, app, sample_user, second_user, sample_admin):
        free = _make_space(slug='free-m')
        paid = _make_space(owner=sample_user, paid=True, price_cents=999)
        # Free space: everyone, including anonymous.
        assert free.can_view(None) is True
        assert free.can_view(second_user) is True
        # Paid space: anonymous and strangers out; owner + admin in.
        assert paid.can_view(None) is False
        assert paid.can_view(second_user) is False
        assert paid.can_view(sample_user) is True
        assert paid.can_view(sample_admin) is True
        # Active paid member in; lapsed / past-due / expired out.
        _make_membership(second_user, paid, subscription_id='sub_m1')
        assert paid.can_view(second_user) is True

    def test_grandfathered_free_member_loses_access_when_space_goes_paid(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(slug='free-m2')
        _free_membership(second_user, space)
        _make_post(app, sample_user, space, body='Members-only after paywall.')
        # The space flips to paid (owner + price set, as admin does).
        space.owner_id = sample_user.id
        space.is_paid = True
        space.price_cents = 999
        db.session.commit()
        # A free membership is NOT a paid membership: no free ride.
        assert space.can_view(second_user) is False
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get(f'/social/spaces/{space.slug}')
        assert resp.status_code == 200
        assert b'Members-only after paywall.' not in resp.data
        assert b'This is a paid space' in resp.data

    def test_grandfathered_free_member_is_sent_to_checkout_not_already_member(
        self, app, client, sample_user, second_user, login_as
    ):
        _set_stripe_keys()
        space = _make_space(slug='free-m3')
        _free_membership(second_user, space)
        space.owner_id = sample_user.id
        space.is_paid = True
        space.price_cents = 999
        db.session.commit()
        login_as(client, second_user.email, 'TestPass123!')
        checkout = SimpleNamespace(
            url='https://checkout.stripe.com/paid-space'
        )
        with patch(
            'stripe.checkout.Session.create', return_value=checkout
        ):
            resp = client.post(f'/social/spaces/{space.slug}/join')
        assert resp.status_code == 302
        assert resp.headers['Location'] == (
            'https://checkout.stripe.com/paid-space'
        )

    def test_past_due_and_expired_members_cannot_view(
        self, app, sample_user, second_user
    ):
        paid = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_membership(
            second_user, paid, subscription_id='sub_m2', status='past_due',
            period_end=utc_now() + timedelta(days=10),
        )
        assert paid.can_view(second_user) is False
        paid2 = _make_space(
            owner=sample_user, paid=True, price_cents=999, slug='paid-m2',
        )
        _make_membership(
            second_user, paid2, subscription_id='sub_m3', status='active',
            period_end=utc_now() - timedelta(days=1),
        )
        assert paid2.can_view(second_user) is False


# ---------------------------------------------------------------------------
# 3. space_notifications must not mint a free membership on a paid space
# ---------------------------------------------------------------------------

class TestNotificationsBypass:
    def test_notifications_post_does_not_join_paid_space(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.post(
            f'/social/spaces/{space.slug}/notifications',
            data={'level': 'all'},
        )
        assert resp.status_code == 302
        assert SpaceMembership.query.filter_by(
            user_id=second_user.id, space_id=space.id
        ).first() is None
        assert space.can_view(second_user) is False

    def test_notifications_still_works_for_paid_member(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_membership(second_user, space, subscription_id='sub_m4')
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.post(
            f'/social/spaces/{space.slug}/notifications',
            data={'level': 'muted'},
        )
        assert resp.status_code == 302
        membership = SpaceMembership.query.filter_by(
            user_id=second_user.id, space_id=space.id
        ).first()
        assert membership is not None
        assert membership.notification_level == 'muted'


# ---------------------------------------------------------------------------
# 4. Paid-space posts must not leak into feeds / search / shares
# ---------------------------------------------------------------------------

class TestFeedLeak:
    def test_latest_feed_hides_paid_posts_from_non_members(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_post(app, sample_user, space, body='SecretLeakBody123')
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get('/social/feed?tab=latest')
        assert resp.status_code == 200
        assert b'SecretLeakBody123' not in resp.data

    def test_latest_feed_shows_paid_posts_to_members(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_post(app, sample_user, space, body='SecretLeakBody123')
        _make_membership(second_user, space, subscription_id='sub_m5')
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get('/social/feed?tab=latest')
        assert resp.status_code == 200
        assert b'SecretLeakBody123' in resp.data

    def test_search_shows_paid_posts_to_members(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_post(app, sample_user, space, body='SecretLeakBody123 hiddenphrase')
        _make_membership(second_user, space, subscription_id='sub_m5b')
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get('/social/search?q=SecretLeakBody123')
        assert resp.status_code == 200
        assert b'hiddenphrase' in resp.data

    def test_search_hides_paid_posts_from_non_members(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        # The search box echoes the query back, so the body carries
        # a second token the query does not contain.
        _make_post(app, sample_user, space, body='SecretLeakBody123 hiddenphrase')
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get('/social/search?q=SecretLeakBody123')
        assert resp.status_code == 200
        assert b'hiddenphrase' not in resp.data

    def test_search_hides_paid_posts_from_anonymous(
        self, app, client, sample_user
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_post(app, sample_user, space, body='SecretLeakBody123 hiddenphrase')
        resp = client.get('/social/search?q=SecretLeakBody123')
        assert resp.status_code == 200
        assert b'hiddenphrase' not in resp.data

    def test_latest_feed_hides_shares_of_paid_posts(
        self, app, client, sample_user, second_user, login_as
    ):
        from models.post import Share
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        post = _make_post(app, sample_user, space, body='SecretLeakBody123')
        # A member shares the paid post; a non-member must not see
        # the share (nor the body) in the Latest feed.
        _make_membership(second_user, space, subscription_id='sub_m6')
        share = Share(user_id=second_user.id, post_id=post.id)
        db.session.add(share)
        db.session.commit()
        db.session.delete(
            SpaceMembership.query.filter_by(
                user_id=second_user.id, space_id=space.id
            ).first()
        )
        db.session.commit()
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get('/social/feed?tab=latest')
        assert resp.status_code == 200
        assert b'SecretLeakBody123' not in resp.data


# ---------------------------------------------------------------------------
# 5. Leave / cancel stuck states for past-due subscriptions
# ---------------------------------------------------------------------------

class TestPastDueLeaveCancel:
    def test_leave_past_due_membership_kept(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_membership(
            second_user, space, subscription_id='sub_m7', status='past_due',
            period_end=utc_now() + timedelta(days=10),
        )
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.post(f'/social/spaces/{space.slug}/leave')
        assert resp.status_code == 302
        # The row survives: the Stripe subscription is still live
        # and would keep charging with no local record.
        assert SpaceMembership.query.filter_by(
            user_id=second_user.id, space_id=space.id
        ).first() is not None

    def test_cancel_allowed_for_past_due_membership(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        membership = _make_membership(
            second_user, space, subscription_id='sub_m8', status='past_due',
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

    def test_admin_cannot_free_space_with_past_due_subscription(
        self, app, client, sample_user, second_user, logged_in_admin
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_membership(
            second_user, space, subscription_id='sub_m9', status='past_due',
            period_end=utc_now() + timedelta(days=10),
        )
        resp = client.post(
            f'/admin/spaces/{space.id}/edit',
            data={'owner_email': sample_user.email, 'price': '9.99'},
        )
        assert resp.status_code == 302
        db.session.refresh(space)
        assert space.is_paid is True


# ---------------------------------------------------------------------------
# 6. Paid space with no price: no "$None", no 500 on subscribe
# ---------------------------------------------------------------------------

class TestMissingPrice:
    def test_pitch_page_never_shows_none_price(
        self, app, client, sample_user, second_user, login_as
    ):
        space = _make_space(owner=sample_user, paid=True, price_cents=None)
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get(f'/social/spaces/{space.slug}')
        assert resp.status_code == 200
        assert b'$None' not in resp.data
        assert b'None/month' not in resp.data

    def test_subscribe_with_no_price_redirects_not_500(
        self, app, client, sample_user, second_user, login_as
    ):
        _set_stripe_keys()
        space = _make_space(owner=sample_user, paid=True, price_cents=None)
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.post(f'/social/spaces/{space.slug}/subscribe')
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# 7. invoice.paid must not resurrect a canceled membership
# ---------------------------------------------------------------------------

class TestInvoiceDoesNotResurrect:
    def test_invoice_paid_keeps_canceled_membership_canceled(
        self, app, client, sample_user, second_user
    ):
        _set_stripe_keys()
        _ready_connect(sample_user)
        space = _make_space(owner=sample_user, paid=True, price_cents=1000)
        membership = _make_membership(
            second_user, space, subscription_id='sub_m10',
            status='canceled',
            period_end=utc_now() - timedelta(days=1),
        )
        transfer = SimpleNamespace(id='tr_space_9')
        with patch(
            'stripe.Transfer.create', return_value=transfer
        ):
            resp = _post_event(
                client, _invoice_event('in_space_9', 'sub_m10', 1000)
            )
        assert resp.status_code == 200
        db.session.refresh(membership)
        assert membership.status == 'canceled'
        assert membership.is_active is False
        assert space.can_view(second_user) is False


# ---------------------------------------------------------------------------
# 8. Tips summary: completed only (failed + pending excluded)
# ---------------------------------------------------------------------------

class TestTipsSummaryCompletedOnly:
    def test_failed_and_pending_tips_excluded(
        self, app, sample_user, sample_hunter
    ):
        from tests.test_tips import _make_tip
        _make_tip(
            app, sample_user, sample_hunter, 1000, status='completed'
        )
        _make_tip(
            app, sample_user, sample_hunter, 500, status='failed'
        )
        _make_tip(app, sample_user, sample_hunter, 2500)  # pending
        total, count = tips_received_summary(sample_hunter.id)
        assert (total, count) == (1000, 1)


# ---------------------------------------------------------------------------
# 9. Anonymous tip path: no button rendered, routes redirect to login
# ---------------------------------------------------------------------------

class TestAnonymousTipPath:
    def test_public_profile_hides_tip_button_from_anonymous(
        self, app, client, sample_hunter
    ):
        resp = client.get(
            f'/profile/{sample_hunter.id}', follow_redirects=True
        )
        assert resp.status_code == 200
        assert b'tip-modal-profile' not in resp.data

    def test_anonymous_tip_routes_redirect_to_login(
        self, app, client, sample_hunter
    ):
        resp = client.get(f'/tips/new/{sample_hunter.id}')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']
        resp = client.post(
            f'/tips/create/{sample_hunter.id}',
            data={'amount': '5', 'currency': 'CAD'},
        )
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']


# ---------------------------------------------------------------------------
# 10. i18n: real Babel parse — FR/ES fully translated, no fuzzy
# ---------------------------------------------------------------------------

class TestI18nCoverage:
    def test_fr_and_es_fully_translated_no_fuzzy(self):
        from babel.messages.pofile import read_po
        for lang in ('fr', 'es'):
            with open(
                f'translations/{lang}/LC_MESSAGES/messages.po',
                encoding='utf-8',
            ) as f:
                catalog = read_po(f)
            messages = [m for m in catalog if m.id]
            untranslated = [m for m in messages if not m.string]
            fuzzy = [m for m in messages if 'fuzzy' in m.flags]
            assert not untranslated, (
                f'{lang}: {len(untranslated)} untranslated'
            )
            assert not fuzzy, f'{lang}: {len(fuzzy)} fuzzy'

    def test_new_tip_and_space_strings_translated(self):
        from babel.messages.pofile import read_po
        for lang in ('fr', 'es'):
            with open(
                f'translations/{lang}/LC_MESSAGES/messages.po',
                encoding='utf-8',
            ) as f:
                catalog = read_po(f)
            by_id = {m.id: m for m in catalog if m.id}
            for msgid in (
                'You cannot tip yourself.',
                'The minimum tip is $2.',
                'This is a paid space',
                'Join to read the posts and take part in the conversation.',
                'This is a paid space — join it to post here.',
            ):
                assert msgid in by_id, f'{lang}: missing {msgid!r}'
                assert by_id[msgid].string, (
                    f'{lang}: untranslated {msgid!r}'
                )
