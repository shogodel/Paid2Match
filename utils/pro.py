"""Pro analytics subscription service (M3, §1.4).

Pro is $9/month via Stripe Checkout (subscription mode). Founding
referrers get it free for 12 months — the window runs from when the
admin granted the flag (User.founding_referrer_since) and is
enforced by utils.payments.founding_benefit_active, same as the
15% fee.

Canceling keeps access until the end of the current period: Stripe
keeps the subscription 'active' with cancel_at_period_end=True, and
the subscription webhook flips it to 'canceled' at period end. The
webhook is the source of truth for status and period dates; the
checkout handler only creates the local row (period dates arrive with
the subscription events that follow).
"""
import logging
from datetime import datetime, timezone

import stripe

from extensions import db
from models import utc_now
from models.subscription import PRO_PRICE_CENTS, ProSubscription
from utils.payments import (
    CURRENCY,
    _set_api_key,
    _stripe_get,
    create_subscription_checkout_session,
    founding_benefit_active,
    idempotency_key_for,
    stripe_enabled,
)

logger = logging.getLogger(__name__)


def is_pro(user):
    """Whether the user has live Pro access: a founding referrer
    inside the 12-month window, or an active Pro subscription whose
    period has not ended."""
    if user is None:
        return False
    if founding_benefit_active(user):
        return True
    sub = ProSubscription.query.filter_by(user_id=user.id).first()
    return bool(sub and sub.is_current)


def get_pro_subscription(user_id):
    return ProSubscription.query.filter_by(user_id=user_id).first()


def create_pro_checkout(user, success_url, cancel_url):
    """Create the Pro subscription Checkout session. Returns the URL,
    or None when Stripe is unavailable."""
    return create_subscription_checkout_session(
        success_url=success_url,
        cancel_url=cancel_url,
        amount_cents=PRO_PRICE_CENTS,
        description='Paid2Match Pro — advanced analytics (CA$9/month)',
        metadata={'m3_kind': 'pro', 'user_id': user.id},
        # One Pro subscription per user: a double-click returns the
        # same session instead of creating a second subscription
        # that would keep charging with no local row. The key is
        # bucketed by the minute (see idempotency_key_for): a
        # permanent key would make a re-subscribe within Stripe's
        # 24h idempotency window return the stale completed session.
        idempotency_key=idempotency_key_for('pro', user.id),
    )


def fulfill_pro_checkout(session_obj):
    """Create/update the local ProSubscription from a completed
    checkout.session (webhook). Idempotent on the Stripe subscription
    id. Returns the ProSubscription, or None when the session carries
    no subscription (should not happen for subscription-mode
    checkouts). Does not commit."""
    metadata = _stripe_get(session_obj, 'metadata') or {}
    user_id = metadata.get('user_id') if isinstance(metadata, dict) else None
    subscription_id = _stripe_get(session_obj, 'subscription')
    if not user_id or not subscription_id:
        logger.warning('Pro checkout without user_id/subscription — skipping')
        return None
    sub = ProSubscription.query.filter_by(
        stripe_subscription_id=subscription_id
    ).first()
    if sub is None:
        sub = ProSubscription.query.filter_by(user_id=user_id).first()
    if sub is None:
        sub = ProSubscription(
            user_id=user_id,
            stripe_subscription_id=subscription_id,
        )
        db.session.add(sub)
    else:
        sub.stripe_subscription_id = subscription_id
    sub.stripe_customer_id = _stripe_get(session_obj, 'customer')
    sub.status = 'active'
    sub.cancel_at_period_end = False
    db.session.flush()
    return sub


def _sync_subscription_row(sub, subscription_obj):
    """Copy a Stripe subscription's state onto a local row."""
    status = _stripe_get(subscription_obj, 'status')
    if status in ('active', 'trialing'):
        sub.status = 'active'
    elif status == 'past_due':
        sub.status = 'past_due'
    elif status in ('canceled', 'unpaid', 'incomplete_expired', 'incomplete'):
        sub.status = 'canceled' if status != 'incomplete' else 'incomplete'
    sub.cancel_at_period_end = bool(
        _stripe_get(subscription_obj, 'cancel_at_period_end')
    )
    start = _stripe_get(subscription_obj, 'current_period_start')
    end = _stripe_get(subscription_obj, 'current_period_end')
    if start:
        sub.current_period_start = datetime.fromtimestamp(start, tz=timezone.utc).replace(tzinfo=None)
    if end:
        sub.current_period_end = datetime.fromtimestamp(end, tz=timezone.utc).replace(tzinfo=None)
    db.session.flush()
    return sub


def handle_subscription_event(subscription_obj, event_type):
    """Sync a ProSubscription and/or RecurringBump from a
    customer.subscription.* event. ``customer.subscription.deleted``
    marks the local rows canceled. Returns the ProSubscription (or
    None when the subscription is not one of ours). Does not commit."""
    from models.bump import RecurringBump

    subscription_id = _stripe_get(subscription_obj, 'id')
    if not subscription_id:
        return None
    sub = ProSubscription.query.filter_by(
        stripe_subscription_id=subscription_id
    ).first()
    if sub is not None:
        if event_type == 'customer.subscription.deleted':
            sub.status = 'canceled'
            sub.cancel_at_period_end = False
            db.session.flush()
        else:
            _sync_subscription_row(sub, subscription_obj)
    recurring = RecurringBump.query.filter_by(
        stripe_subscription_id=subscription_id
    ).first()
    if recurring is not None:
        if event_type == 'customer.subscription.deleted':
            recurring.status = 'canceled'
            recurring.cancel_at_period_end = False
        else:
            status = _stripe_get(subscription_obj, 'status')
            recurring.cancel_at_period_end = bool(
                _stripe_get(subscription_obj, 'cancel_at_period_end')
            )
            if status in ('active', 'trialing'):
                recurring.status = 'active'
            elif status == 'past_due':
                recurring.status = 'past_due'
            elif status in ('canceled', 'unpaid', 'incomplete_expired'):
                recurring.status = 'canceled'
        db.session.flush()
    # Paid spaces: a subscription that belongs to a space membership
    # syncs the same way (deleted lapses access at period end).
    # Additive — Pro and recurring-bump rows above are untouched.
    from utils.paid_spaces import handle_space_subscription_event
    handle_space_subscription_event(subscription_obj, event_type)
    return sub


def cancel_pro_at_period_end(sub):
    """Cancel a Pro subscription at the end of its period (access
    continues until then). Returns (ok, error). Does not commit the
    DB change — the caller commits."""
    from utils.payments import cancel_subscription_at_period_end
    ok, error = cancel_subscription_at_period_end(sub.stripe_subscription_id)
    if not ok:
        return False, error
    sub.cancel_at_period_end = True
    return True, None
