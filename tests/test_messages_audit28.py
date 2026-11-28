"""Audit 2026-11-28 (messaging slice) regression tests.

Each test pins a bug found by this audit in the tips + paid
communities build; they fail on the pre-fix code.

- Tip money notifications never emailed: 'tip_received',
  'tip_pending' and 'tip_failed' were missing from
  Notification.EMAIL_TYPES, while the equivalent payout events
  (payment_released / payout_pending / payout_failed) all email.
  A tip waiting on Connect setup only surfaced in the bell.
- Paid-space join/lapse never notified the owner:
  fulfill_paid_space_checkout created the membership silently
  (the owner only learned via invoice.paid, or not at all on a
  $0/trial invoice) and a lapsed membership flipped to
  'canceled' with no word to the owner.
- After a successful tip payment or paid-space checkout, Stripe
  returned the user to the same URL as a cancellation with no
  flash — success and cancel were indistinguishable (the member
  even landed on the pitch page, membership not yet created).
- Pitch-accept flash claimed "A message has been sent" even
  when the auto-DM was skipped for a blocked/deleted hunter
  (audit27 fix left the flash unconditional).
- The new notification types were missing from
  Notification.TYPES and had no icons (fell back to 'bell').
"""
from types import SimpleNamespace
from unittest.mock import patch

from extensions import db
from models.message import Message
from models.moderation import Block
from models.notification import Notification
from models.space import SpaceMembership
from tests.test_money_audit import _connect_account, _set_stripe_keys
from tests.test_paid_spaces import _make_membership, _make_space
from tests.test_tips import _make_tip, _post_event, _tip_checkout_event
from utils.notifications import (
    notification_icon,
    notify,
    unread_count,
)
from utils.paid_spaces import fulfill_paid_space_checkout
from utils.pro import handle_subscription_event


class TestTipNotificationTypes:
    def test_tip_types_registered_with_icons(self, app):
        for type_ in (
            'tip_received', 'tip_pending', 'tip_failed',
            'space_member_joined', 'space_member_left',
        ):
            assert type_ in Notification.TYPES
        assert notification_icon('tip_received') == 'wallet'
        assert notification_icon('tip_pending') == 'wallet'
        assert notification_icon('tip_failed') == 'wallet'
        assert notification_icon('space_member_joined') == 'users'
        assert notification_icon('space_member_left') == 'users'

    def test_tip_money_events_email_like_payouts(self, app):
        # The payout equivalents all email; tips are money events
        # too — a tip waiting on payout setup must reach the
        # recipient's inbox, not just the bell.
        for type_ in ('tip_received', 'tip_pending', 'tip_failed'):
            assert type_ in Notification.EMAIL_TYPES


class TestTipNotificationEmail:
    def test_tip_received_emails_recipient(
            self, app, client, sample_user, sample_hunter):
        _set_stripe_keys()
        _connect_account(app, sample_hunter)
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        with patch('stripe.Transfer.create',
                   return_value=SimpleNamespace(id='tr_tip_a28')), \
                patch('utils.notifications.send_simple_email',
                      return_value=True) as mail:
            resp = _post_event(
                client, _tip_checkout_event('cs_tip_a28', tip.id)
            )
        assert resp.status_code == 200
        note = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='tip_received'
        ).first()
        assert note is not None
        # In-app only, to the recipient — never to the tipper.
        assert Notification.query.filter_by(
            recipient_id=sample_user.id, type='tip_received'
        ).first() is None
        assert mail.call_count == 1
        assert mail.call_args.args[0] == sample_hunter.email

    def test_tip_pending_emails_recipient(
            self, app, client, sample_user, sample_hunter):
        _set_stripe_keys()
        tip = _make_tip(app, sample_user, sample_hunter, 1000)
        with patch('utils.notifications.send_simple_email',
                   return_value=True) as mail:
            resp = _post_event(
                client, _tip_checkout_event('cs_tip_a28b', tip.id)
            )
        assert resp.status_code == 200
        note = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='tip_pending'
        ).first()
        assert note is not None
        assert mail.call_count == 1
        assert mail.call_args.args[0] == sample_hunter.email


class TestTipUnreadCounts:
    def test_tip_notification_counts_and_clears(
            self, app, client, sample_user, sample_hunter, login_as):
        # A tip notification increments the unread count the bell
        # poll reads, and clears when read — same path as every
        # other notification type.
        with app.app_context():
            notify(sample_hunter.id, 'tip_received', 'You received a tip!')
            db.session.commit()
        assert unread_count(sample_hunter.id) == 1
        login_as(client, 'hunter@example.com')
        resp = client.get('/social/notifications/poll')
        assert resp.status_code == 200
        assert resp.get_json()['unread'] == 1
        assert len(resp.get_json()['notifications']) == 1
        client.post('/social/notifications/read-all')
        assert unread_count(sample_hunter.id) == 0


class TestSpaceJoinNotification:
    def test_join_notifies_owner_once(
            self, app, sample_user, second_user):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        session = {
            'id': 'cs_space_a28', 'subscription': 'sub_space_a28',
            'metadata': {
                'm3_kind': 'paid_space', 'user_id': second_user.id,
                'space_id': space.id,
            },
        }
        fulfill_paid_space_checkout(session)
        db.session.commit()
        # Redelivery must not re-notify.
        fulfill_paid_space_checkout(session)
        db.session.commit()
        notes = Notification.query.filter_by(
            recipient_id=sample_user.id, type='space_member_joined'
        ).all()
        assert len(notes) == 1
        assert notes[0].actor_id == second_user.id
        # Nobody else is notified — not the member, not other members.
        assert Notification.query.filter(
            Notification.type == 'space_member_joined',
            Notification.recipient_id != sample_user.id,
        ).count() == 0

    def test_lapse_notifies_owner_once(
            self, app, sample_user, second_user):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        membership = _make_membership(
            second_user, space, subscription_id='sub_space_a28b',
        )
        assert membership.is_active is True
        deleted = {'id': 'sub_space_a28b', 'status': 'canceled'}
        handle_subscription_event(deleted, 'customer.subscription.deleted')
        db.session.commit()
        handle_subscription_event(deleted, 'customer.subscription.deleted')
        db.session.commit()
        notes = Notification.query.filter_by(
            recipient_id=sample_user.id, type='space_member_left'
        ).all()
        assert len(notes) == 1
        assert notes[0].actor_id == second_user.id
        assert db.session.get(
            SpaceMembership, membership.id).status == 'canceled'

    def test_cancel_at_period_end_does_not_notify_yet(
            self, app, sample_user, second_user):
        # Cancelling keeps access until period end — the owner is
        # told when the membership actually lapses, not at the
        # cancel click.
        from datetime import timedelta

        from models import utc_now
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        _make_membership(
            second_user, space, subscription_id='sub_space_a28c',
        )
        handle_subscription_event(
            {
                'id': 'sub_space_a28c', 'status': 'active',
                'cancel_at_period_end': True,
                'current_period_end': int(
                    (utc_now() + timedelta(days=10)).timestamp()
                ),
            },
            'customer.subscription.updated',
        )
        db.session.commit()
        assert Notification.query.filter_by(
            recipient_id=sample_user.id, type='space_member_left'
        ).count() == 0


class TestCheckoutSuccessFlashes:
    def test_tip_success_route_flashes_confirmation(
            self, app, client, sample_user, sample_hunter, login_as):
        login_as(client, sample_user.email)
        resp = client.get(
            f'/tips/success/{sample_hunter.id}', follow_redirects=True
        )
        assert resp.status_code == 200
        assert b'Thanks' in resp.data
        assert b'tip is on its way' in resp.data

    def test_tip_create_uses_success_route(
            self, app, client, sample_user, sample_hunter, login_as):
        _set_stripe_keys()
        login_as(client, sample_user.email)
        with patch('stripe.checkout.Session.create',
                   return_value=SimpleNamespace(
                       id='cs_tip_a28c',
                       url='https://checkout.test/tip')) as sc:
            resp = client.post(
                f'/tips/create/{sample_hunter.id}',
                data={'amount': '5', 'currency': 'CAD'},
            )
        assert resp.status_code == 302
        kwargs = sc.call_args.kwargs
        assert '/tips/success/' in kwargs['success_url']
        assert '/tips/success/' not in kwargs['cancel_url']

    def test_space_subscribe_success_route_flashes(
            self, app, client, sample_user, second_user, login_as):
        space = _make_space(owner=sample_user, paid=True, price_cents=999)
        login_as(client, second_user.email, 'TestPass123!')
        resp = client.get(
            f'/social/spaces/{space.slug}/subscribe/success',
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b'Thanks for joining' in resp.data

    def test_space_join_uses_success_route(
            self, app, client, sample_user, second_user, login_as):
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
        kwargs = create.call_args.kwargs
        assert '/subscribe/success' in kwargs['success_url']
        assert '/subscribe/success' not in kwargs['cancel_url']


class TestPitchAcceptFlash:
    def test_blocked_hunter_flash_does_not_claim_message_sent(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, sample_pitch):
        with app.app_context():
            db.session.add(Block(
                blocker_id=sample_hunter.id,
                blocked_id=sample_user.id,
                kind='block'))
            db.session.commit()
        resp = logged_in_user.post(
            f'/needs/pitches/{sample_pitch.id}/respond',
            data={'response': 'interested'},
            follow_redirects=True)
        assert resp.status_code == 200
        assert b'A message has been sent' not in resp.data
        assert b'no message was sent' in resp.data
        with app.app_context():
            # The notification still fires — only the DM is skipped.
            assert Notification.query.filter_by(
                recipient_id=sample_hunter.id,
                type='referral_accepted',
            ).count() == 1
            assert Message.query.count() == 0

    def test_unblocked_hunter_flash_claims_message_sent(
            self, logged_in_user, client, app,
            sample_user, sample_hunter, sample_pitch):
        resp = logged_in_user.post(
            f'/needs/pitches/{sample_pitch.id}/respond',
            data={'response': 'interested'},
            follow_redirects=True)
        assert resp.status_code == 200
        assert b'A message has been sent' in resp.data
        with app.app_context():
            assert Message.query.count() == 1
