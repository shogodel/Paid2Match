"""Paid communities (paywalled Spaces).

A space with an owner can be switched to paid by an admin (Admin >
Spaces). Members then pay a monthly price via Stripe Checkout
(subscription mode, reusing the Pro subscription-checkout pattern
in utils/payments.py). The platform keeps SPACE_PLATFORM_FEE_PERCENT
(admin-settable, default 20%) and the owner keeps the rest.

Money flow per paid invoice (``invoice.paid`` webhook):
the owner's share is transferred to their Connect Express account
with ``source_transaction`` set to the invoice's charge — the same
separate-charges-and-transfers flow as escrow release. When the
owner has no ready Connect account the share is recorded as a
SpacePayout with status 'pending_stripe' (the warm-intro-reward
pattern) and the owner is prompted to connect payouts; the next
invoice for that owner retries older pending rows.

Lifecycle is webhook-driven, not a cron: cancel-at-period-end keeps
the membership 'active' until the period ends, then
``customer.subscription.deleted`` flips it to 'canceled'.

Nothing here touches the hire/escrow/Pro/bump money paths.
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import stripe
from flask_babel import gettext as _

from extensions import db
from models import AdminSettings, ConnectAccount, Space, SpaceMembership
from models.space_payout import SpacePayout
from utils.notifications import notify
from utils.payments import (
    CURRENCY,
    _set_api_key,
    _stripe_get,
    cancel_subscription_at_period_end,
    create_subscription_checkout_session,
    idempotency_key_for,
    stripe_enabled,
)

logger = logging.getLogger(__name__)

SPACE_FEE_SETTING_KEY = 'SPACE_PLATFORM_FEE_PERCENT'
DEFAULT_SPACE_FEE_PERCENT = 20.0
MAX_SPACE_FEE_PERCENT = 50


def get_space_platform_fee_percent():
    """The paid-space platform fee percentage from settings, clamped
    to 0–50. Each fee has its own setting — this never reads
    PLATFORM_FEE_PERCENT (hires) or any tip fee."""
    raw = AdminSettings.get(SPACE_FEE_SETTING_KEY)
    try:
        value = float(raw) if raw is not None else DEFAULT_SPACE_FEE_PERCENT
    except (TypeError, ValueError):
        return DEFAULT_SPACE_FEE_PERCENT
    return max(0.0, min(float(MAX_SPACE_FEE_PERCENT), value))


def split_space_payment(gross_cents, fee_percent=None):
    """Split a monthly payment (cents) into (fee_cents, owner_cents).
    The fee rounds half-up; the owner keeps the remainder, so the
    two always sum to the gross."""
    if fee_percent is None:
        fee_percent = get_space_platform_fee_percent()
    fee = int(
        (Decimal(gross_cents) * Decimal(str(fee_percent)) / 100).quantize(
            Decimal('1'), rounding=ROUND_HALF_UP
        )
    )
    return fee, gross_cents - fee


def create_paid_space_checkout(user, space, success_url, cancel_url):
    """Create the paid-space subscription Checkout session. Returns
    the URL, or None when Stripe is unavailable. The space price is
    part of the idempotency key: a price change must never reuse a
    key minted for the old price."""
    return create_subscription_checkout_session(
        success_url=success_url,
        cancel_url=cancel_url,
        amount_cents=int(space.price_cents),
        description=_(
            'Paid2Match — %(name)s membership (%(price)s/month)',
            name=space.name,
            price=f'${space.price_cents / 100:.2f}',
        ),
        metadata={
            'm3_kind': 'paid_space',
            'user_id': user.id,
            'space_id': space.id,
        },
        idempotency_key=idempotency_key_for(
            'paid-space', user.id, space.id, space.price_cents
        ),
    )


def fulfill_paid_space_checkout(session_obj):
    """Create/update the local SpaceMembership from a completed
    checkout.session (webhook). Idempotent on the Stripe
    subscription id: a redelivered event finds the membership and
    only re-syncs it. Returns the membership, or None when the
    session is not a paid-space checkout. Does not commit."""
    metadata = _stripe_get(session_obj, 'metadata') or {}
    user_id = metadata.get('user_id') if isinstance(metadata, dict) else None
    space_id = metadata.get('space_id') if isinstance(metadata, dict) else None
    subscription_id = _stripe_get(session_obj, 'subscription')
    if not user_id or not space_id or not subscription_id:
        logger.warning('Paid-space checkout without user/space/subscription — skipping')
        return None
    space = db.session.get(Space, space_id)
    if space is None:
        logger.warning('Paid-space checkout for unknown space %s', space_id)
        return None
    membership = SpaceMembership.query.filter_by(
        stripe_subscription_id=subscription_id
    ).first()
    if membership is None:
        membership = SpaceMembership.query.filter_by(
            user_id=user_id, space_id=space_id
        ).first()
    if membership is None:
        membership = SpaceMembership(user_id=user_id, space_id=space_id)
        db.session.add(membership)
    membership.stripe_subscription_id = subscription_id
    membership.status = 'active'
    membership.cancel_at_period_end = False
    db.session.flush()
    return membership


def _sync_space_membership(membership, subscription_obj, event_type):
    """Copy a Stripe subscription's state onto a paid membership.
    'deleted' lapses access; anything else syncs status + period
    end. A cancel-at-period-end subscription stays 'active' until
    the period ends — the deleted event is what lapses it."""
    if event_type == 'customer.subscription.deleted':
        membership.status = 'canceled'
        membership.cancel_at_period_end = False
        db.session.flush()
        return membership
    status = _stripe_get(subscription_obj, 'status')
    if status in ('active', 'trialing'):
        membership.status = 'active'
    elif status == 'past_due':
        membership.status = 'past_due'
    elif status in ('canceled', 'unpaid', 'incomplete_expired', 'incomplete'):
        membership.status = 'canceled' if status != 'incomplete' else 'incomplete'
    membership.cancel_at_period_end = bool(
        _stripe_get(subscription_obj, 'cancel_at_period_end')
    )
    end = _stripe_get(subscription_obj, 'current_period_end')
    if end:
        membership.current_period_end = datetime.fromtimestamp(
            end, tz=timezone.utc
        ).replace(tzinfo=None)
    db.session.flush()
    return membership


def handle_space_subscription_event(subscription_obj, event_type):
    """Sync a SpaceMembership from a customer.subscription.* event.
    Returns the membership, or None when the subscription is not a
    paid-space one. Does not commit."""
    subscription_id = _stripe_get(subscription_obj, 'id')
    if not subscription_id:
        return None
    membership = SpaceMembership.query.filter_by(
        stripe_subscription_id=subscription_id
    ).first()
    if membership is None:
        return None
    return _sync_space_membership(membership, subscription_obj, event_type)


def cancel_space_subscription_at_period_end(membership):
    """Cancel a paid-space subscription at the end of its period
    (access continues until then). Returns (ok, error). Does not
    commit the DB change — the caller commits."""
    ok, error = cancel_subscription_at_period_end(
        membership.stripe_subscription_id
    )
    if not ok:
        return False, error
    membership.cancel_at_period_end = True
    return True, None


# ---------------------------------------------------------------------------
# Owner payout on invoice.paid
# ---------------------------------------------------------------------------

def _transfer_owner_share(payout):
    """Transfer one recorded owner share to the owner's Connect
    account. Returns (ok, error). The payout row must already exist
    (status 'pending_stripe'); on success it moves to 'processing'
    and records the transfer id — the transfer.paid webhook (the
    existing Payout handler path does not cover SpacePayout rows,
    see handle_space_transfer_event) completes it. Does not commit."""
    account = ConnectAccount.query.filter_by(user_id=payout.owner_id).first()
    if not account or not account.is_ready:
        payout.failure_reason = _(
            'Awaiting space owner payout setup '
            '(Stripe Connect onboarding not complete).'
        )
        return False, 'owner_connect_not_ready'
    if not stripe_enabled():
        payout.failure_reason = _('Payments are currently disabled.')
        return False, 'payments_disabled'
    if not _set_api_key():
        payout.failure_reason = _('Stripe is not configured.')
        return False, 'stripe_not_configured'
    try:
        transfer = stripe.Transfer.create(
            amount=int(payout.amount_cents),
            currency=payout.currency or CURRENCY,
            destination=account.stripe_account_id,
            # The invoice's charge is the source transaction, so the
            # transfer draws on the member's payment (the same
            # separate-charges-and-transfers flow as escrow release).
            source_transaction=payout.source_charge_id,
            metadata={
                'space_id': payout.space_id,
                'space_payout_id': payout.id,
            },
            # One payout row can only ever create one transfer: a
            # crash between the Stripe call and the DB commit retries
            # under the same key instead of paying twice.
            idempotency_key=f'space-payout-{payout.id}',
        )
    except stripe.error.StripeError as exc:
        payout.failure_reason = _('Transfer failed: %(error)s', error=str(exc))
        payout.status = 'failed'
        logger.error('Space payout transfer failed for %s: %s', payout.id, exc)
        return False, str(exc)
    payout.stripe_transfer_id = _stripe_get(transfer, 'id')
    payout.failure_reason = None
    payout.status = 'processing'
    return True, None


def retry_pending_space_payouts(owner_id):
    """Re-attempt the transfer for an owner's pending space payouts
    (called when a new invoice lands for that owner — i.e. after
    they may have finished Connect onboarding — and from the
    Connect-ready path). Returns a list of (payout_id, ok, message).
    Does not commit."""
    account = ConnectAccount.query.filter_by(user_id=owner_id).first()
    if not account or not account.is_ready:
        return []
    payouts = SpacePayout.query.filter_by(
        owner_id=owner_id, status='pending_stripe'
    ).all()
    results = []
    for payout in payouts:
        if not payout.source_charge_id:
            continue
        ok, msg = _transfer_owner_share(payout)
        results.append((payout.id, ok, msg))
    return results


def handle_space_invoice_paid(invoice_obj):
    """Split a paid invoice for a paid space: record the owner's
    share as a SpacePayout and transfer it when the owner can be
    paid, or leave it 'pending_stripe' and prompt them to connect
    payouts. Idempotent on the Stripe invoice id — a redelivered
    event finds the existing row and never transfers twice. Also
    retries the owner's older pending rows (they may have finished
    onboarding since). Returns the SpacePayout, or None when the
    invoice is not a paid-space one. Does not commit."""
    invoice_id = _stripe_get(invoice_obj, 'id')
    subscription_id = _stripe_get(invoice_obj, 'subscription')
    if not invoice_id or not subscription_id:
        return None
    existing = SpacePayout.query.filter_by(
        stripe_invoice_id=invoice_id
    ).first()
    if existing is not None:
        return existing
    membership = SpaceMembership.query.filter_by(
        stripe_subscription_id=subscription_id
    ).first()
    if membership is None:
        logger.info(
            'invoice.paid for unknown paid-space subscription %s',
            subscription_id,
        )
        return None
    space = db.session.get(Space, membership.space_id)
    if space is None or not space.is_paid or not space.owner_id:
        return None
    amount_paid = _stripe_get(invoice_obj, 'amount_paid')
    if amount_paid is None:
        amount_paid = _stripe_get(invoice_obj, 'amount_due') or 0
    gross_cents = int(amount_paid)
    if gross_cents <= 0:
        # A $0 invoice (e.g. a free trial period) owes nobody
        # anything — still sync the membership period below.
        fee_cents, owner_cents = 0, 0
    else:
        fee_cents, owner_cents = split_space_payment(gross_cents)
    payout = SpacePayout(
        space_id=space.id,
        owner_id=space.owner_id,
        member_id=membership.user_id,
        stripe_invoice_id=invoice_id,
        stripe_subscription_id=subscription_id,
        # The invoice's charge (or payment intent) is the transfer's
        # source transaction.
        source_charge_id=(
            _stripe_get(invoice_obj, 'charge')
            or _stripe_get(invoice_obj, 'payment_intent')
        ),
        gross_cents=gross_cents,
        platform_fee_cents=fee_cents,
        amount_cents=owner_cents,
        currency=(_stripe_get(invoice_obj, 'currency') or CURRENCY).lower(),
        status='pending_stripe',
    )
    db.session.add(payout)
    db.session.flush()

    # Keep the membership's period end in sync with the invoice
    # (the subscription.updated event normally does this, but the
    # invoice is the payment's own record).
    period_end = _stripe_get(invoice_obj, 'period_end')
    if period_end:
        membership.current_period_end = datetime.fromtimestamp(
            period_end, tz=timezone.utc
        ).replace(tzinfo=None)
    if membership.status != 'active':
        membership.status = 'active'
    db.session.flush()

    if gross_cents <= 0:
        payout.status = 'completed'
        return payout

    # Older pending rows first: the owner may have finished
    # onboarding since they were recorded.
    retry_pending_space_payouts(space.owner_id)
    if payout.status != 'pending_stripe':
        return payout
    if not payout.source_charge_id:
        payout.failure_reason = _(
            'No charge on the invoice to transfer from — the share '
            'is recorded and will be retried.'
        )
        notify(
            space.owner_id, 'payout_pending',
            _('Your space payout is waiting'),
            _(
                'A member paid for %(name)s, but the payment could '
                'not be transferred yet. Your share of '
                '%(amount)s is recorded.',
                name=space.name, amount=f'${payout.amount:.2f}',
            ),
            link_url='/payments/connect',
        )
        return payout
    ok, _error = _transfer_owner_share(payout)
    if not ok and _error == 'owner_connect_not_ready':
        notify(
            space.owner_id, 'payout_pending',
            _('Your space payout is waiting on payout setup'),
            _(
                'A member paid for %(name)s, but your share of '
                '%(amount)s is on hold until you finish setting up '
                'Stripe payouts.',
                name=space.name, amount=f'${payout.amount:.2f}',
            ),
            link_url='/payments/connect',
        )
    return payout


def handle_space_transfer_event(event_type, transfer_obj):
    """``transfer.paid`` / ``transfer.failed`` / ``transfer.reversed``
    for a space-owner transfer: complete or fail the SpacePayout
    row and notify the owner. Idempotent — a duplicate event is a
    no-op because the status is already there. Returns the
    SpacePayout, or None when the transfer is not a space payout.
    Does not commit."""
    transfer_id = _stripe_get(transfer_obj, 'id')
    payout = SpacePayout.query.filter_by(
        stripe_transfer_id=transfer_id
    ).first()
    if payout is None:
        return None
    if event_type == 'transfer.paid':
        if payout.status != 'completed':
            payout.status = 'completed'
            payout.failure_reason = None
            notify(
                payout.owner_id, 'payment_released',
                _('Your space payout was sent'),
                _(
                    'Your share of %(amount)s for %(name)s is on its '
                    'way to your Stripe account.',
                    amount=f'${payout.amount:.2f}',
                    name=payout.space.name if payout.space else '',
                ),
                link_url='/payments/payouts',
            )
    elif event_type in ('transfer.failed', 'transfer.reversed'):
        if payout.status != 'failed':
            payout.status = 'failed'
            payout.failure_reason = _(
                'Stripe transfer %(action)s.',
                action=event_type.split('.')[1],
            )
            notify(
                payout.owner_id, 'payout_failed',
                _('Your space payout failed'),
                _(
                    'Stripe could not send your share of %(amount)s '
                    'for %(name)s. Please check your payout details.',
                    amount=f'${payout.amount:.2f}',
                    name=payout.space.name if payout.space else '',
                ),
                link_url='/payments/connect',
            )
    return payout
