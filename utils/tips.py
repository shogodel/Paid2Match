"""Tips service — send money to a user (Tips workstream).

Money flow (all Stripe calls mocked in tests — never hit the real
Stripe API):

* The tipper picks an amount (min $2) and pays via Stripe Checkout
  (payment mode, immediate capture) — the same
  ``create_checkout_session`` pattern as the bump/Pro checkouts.
  The Checkout session carries ``m3_kind='tip'`` + the tip id in
  its metadata so the webhook can fulfill it.
* On ``checkout.session.completed`` the webhook calls
  ``fulfill_tip_checkout`` (additive branch in the webhook
  dispatch — no existing branch is touched): the tip's fee split
  is locked in, and the recipient's share is transferred to their
  Stripe Connect account from the Checkout charge
  (``stripe.Transfer.create`` with ``source_transaction`` — the
  separate-charges-and-transfers flow, same as the escrow release).
* If the recipient has no ready Connect account yet, the tip stays
  'pending' (same pending-until-onboarded pattern as a hire payout
  waiting on onboarding) and the recipient is prompted to connect.
  When their onboarding completes, ``on_connect_ready`` retries
  pending tips via ``retry_pending_tips`` — the same hook that
  retries pending payouts.

Idempotency (audit 2026-11-27 lessons):
* Checkout creation uses a per-tip key (``tip-checkout-<tip id>``)
  — a double-click returns the same Stripe session, and so does a
  double-click straddling a minute boundary (a minute-bucketed key
  would mint a second session there, letting the tipper pay twice
  for one tip row while fulfillment only ever transfers once).
  A deliberate re-tip creates a NEW tip row (and a new key) — the
  recent-pending reuse window below is only 60 seconds.
* A double-clicked tip form also reuses the pending Tip row created
  moments ago (``find_recent_pending_tip``), so one session never
  leaves two pending rows behind.
* Each transfer carries ``tip-transfer-<tip id>`` — with one
  exception (mirroring release_escrow): a tip whose recorded
  transfer FAILED gets a fresh key scoped to the dead transfer id,
  so the retry does not return Stripe's cached response for the
  dead transfer.
* Fulfillment is idempotent on tip status: a redelivered webhook
  event is a no-op for a completed tip (and the webhook's own
  StripeEventLog dedup catches most redeliveries first).

Every function that touches the database does NOT commit — the
caller commits. That keeps route handlers and webhook handlers in
control of the transaction.
"""
import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

import stripe
from flask_babel import gettext as _

from extensions import db
from models import AdminSettings, ConnectAccount, utc_now
from models.tip import Tip
from utils.currency import format_money
from utils.notifications import notify
from utils.payments import (
    CURRENCY,
    STRIPE_MAX_CENTS,
    _set_api_key,
    _stripe_get,
    create_checkout_session,
    stripe_enabled,
)

logger = logging.getLogger(__name__)

# Minimum tip: $2 (200 cents).
TIP_MIN_CENTS = 200
# Quick amounts on the picker, in cents.
TIP_QUICK_AMOUNTS_CENTS = [200, 500, 1000, 2500]
TIP_MESSAGE_MAX_LENGTH = 280

# Platform fee on tips: a SEPARATE setting from the hire platform
# fee (PLATFORM_FEE_PERCENT) — the two rates are set independently
# by the admin. Default 20%, clamped to 0–50 like the hire fee.
TIP_FEE_SETTING_KEY = 'TIP_PLATFORM_FEE_PERCENT'
DEFAULT_TIP_FEE_PERCENT = 20
MAX_TIP_FEE_PERCENT = 50

# A pending tip created this recently by the same tipper for the
# same recipient and amount is a double-clicked form, not a second
# tip — the route reuses it (and the minute-bucketed idempotency
# key returns the same Stripe session for it).
RECENT_PENDING_TIP_SECONDS = 60


def get_tip_fee_percent():
    """The tip platform fee percentage from settings, clamped to 0–50."""
    raw = AdminSettings.get(TIP_FEE_SETTING_KEY)
    try:
        value = int(raw) if raw is not None else DEFAULT_TIP_FEE_PERCENT
    except (TypeError, ValueError):
        return DEFAULT_TIP_FEE_PERCENT
    return max(0, min(MAX_TIP_FEE_PERCENT, value))


def compute_tip_split(amount_cents, fee_percent):
    """Split a tip into (platform fee, net to recipient), in cents.
    The fee rounds half-up to the nearest cent; the recipient gets
    the rest, so fee + net always equals the gross."""
    fee = int(
        (Decimal(str(amount_cents)) * Decimal(str(fee_percent)) / 100)
        .quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    )
    return fee, int(amount_cents) - fee


def validate_tip_amount(amount_cents):
    """(ok, error) — the amount must be an integer of at least $2 and
    no more than Stripe's per-charge maximum (above it the Checkout
    creation fails server-side and the tipper would get a misleading
    'Stripe is not configured' flash for an amount problem)."""
    if amount_cents is None:
        return False, _('Enter an amount.')
    try:
        amount_cents = int(amount_cents)
    except (TypeError, ValueError):
        return False, _('Enter a valid amount.')
    if amount_cents < TIP_MIN_CENTS:
        return False, _('The minimum tip is $2.')
    if amount_cents > STRIPE_MAX_CENTS:
        return False, _('Enter a valid amount.')
    return True, None


def tips_received_summary(user_id):
    """(total_cents, count) of COMPLETED tips received by a user.

    The total is the gross amount tipped — the charge is always in
    CAD (Stripe charges CAD whatever display currency was picked),
    so summing cents across display currencies is still a CAD
    total. The profile shows this gross total: the social signal
    is what people tipped, and the fee is platform-internal (the
    admin listing shows the fee/net breakdown)."""
    from sqlalchemy import func

    total, count = (
        db.session.query(
            func.coalesce(func.sum(Tip.amount_cents), 0),
            func.count(Tip.id),
        )
        .filter_by(recipient_id=user_id, status='completed')
        .one()
    )
    return int(total), int(count)


def find_recent_pending_tip(tipper_id, recipient_id, amount_cents):
    """A pending tip from this tipper to this recipient for this
    amount, created within the last minute — a double-clicked form
    submission, not a second tip. Returns the Tip or None."""
    since = utc_now() - timedelta(seconds=RECENT_PENDING_TIP_SECONDS)
    return (
        Tip.query
        .filter_by(
            tipper_id=tipper_id,
            recipient_id=recipient_id,
            amount_cents=int(amount_cents),
            status='pending',
        )
        .filter(Tip.stripe_payment_intent_id.is_(None))
        .filter(Tip.created_at >= since)
        .order_by(Tip.created_at.desc())
        .first()
    )


def create_tip_checkout(tipper, recipient, amount_cents, currency,
                        message, success_url, cancel_url):
    """Create the pending Tip row and its Stripe Checkout session.
    Returns (tip, checkout_url) — checkout_url is None when Stripe
    is unavailable (the pending row stays: a retry within a minute
    reuses it instead of leaving a second orphan row). The caller
    commits. The tipper must not be the recipient (the route
    enforces that too — this is defense in depth)."""
    if tipper.id == recipient.id:
        raise ValueError('You cannot tip yourself.')
    ok, error = validate_tip_amount(amount_cents)
    if not ok:
        raise ValueError(error)
    tip = find_recent_pending_tip(
        tipper.id, recipient.id, int(amount_cents)
    )
    if tip is None:
        tip = Tip(
            tipper_id=tipper.id,
            recipient_id=recipient.id,
            amount_cents=int(amount_cents),
            currency=currency,
            message=message or None,
            status='pending',
        )
        db.session.add(tip)
        db.session.flush()
    checkout_url = create_checkout_session(
        success_url=success_url,
        cancel_url=cancel_url,
        amount_cents=int(amount_cents),
        # The charge is always in CAD (utils.payments.CURRENCY) —
        # the display currency is record-only, no FX (same pattern
        # as bounties). The description says so.
        description=(
            f'Tip for {recipient.full_name or recipient.email} '
            f'(CAD {int(amount_cents) / 100:.2f})'
        ),
        metadata={
            'm3_kind': 'tip',
            'tip_id': tip.id,
            'tipper_id': tipper.id,
            'recipient_id': recipient.id,
            'amount_cents': str(int(amount_cents)),
        },
        # A double-click returns the same session, not a second
        # charge (the webhook fulfills one tip per session id).
        # Per-tip key, not minute-bucketed: a double-click straddling
        # a minute boundary must still return the same session — a
        # second session would let the tipper pay twice for one tip
        # row while fulfillment only ever transfers once.
        idempotency_key=f'tip-checkout-{tip.id}',
    )
    return tip, checkout_url


def _transfer_tip(tip, account):
    """Transfer a paid tip's recipient share to a ready Connect
    account. Returns (ok, message). Sets the tip completed (and
    notifies the recipient) on success, failed (with a reason) when
    the transfer raises. Does not commit."""
    fee_cents, net_cents = compute_tip_split(
        tip.amount_cents, get_tip_fee_percent()
    )
    tip.platform_fee_cents = fee_cents
    if not stripe_enabled():
        tip.failure_reason = 'Payments are currently disabled.'
        tip.status = 'failed'
        return False, 'Payments are currently disabled.'
    if not _set_api_key():
        tip.failure_reason = 'Stripe is not configured.'
        tip.status = 'failed'
        return False, 'Stripe is not configured.'
    if not tip.stripe_payment_intent_id:
        tip.failure_reason = 'No payment was completed for this tip.'
        tip.status = 'failed'
        return False, 'No payment was completed for this tip.'
    # Idempotency key: one tip row can only ever create one
    # transfer — with one exception (mirroring release_escrow): a
    # tip whose recorded transfer FAILED gets a fresh key scoped to
    # the dead transfer id, so the retry does not return Stripe's
    # cached response for the dead transfer inside its 24h window.
    transfer_key = f'tip-transfer-{tip.id}'
    if tip.stripe_transfer_id:
        transfer_key = f'tip-transfer-{tip.id}-{tip.stripe_transfer_id}'
    try:
        transfer = stripe.Transfer.create(
            amount=net_cents,
            currency=CURRENCY,
            destination=account.stripe_account_id,
            source_transaction=tip.stripe_payment_intent_id,
            metadata={'tip_id': tip.id},
            idempotency_key=transfer_key,
        )
    except stripe.error.StripeError as exc:
        # The money was captured but the transfer failed — the
        # platform holds the funds. Fail loudly (admin listing
        # shows it) so it gets resolved; a retry uses a fresh key.
        tip.failure_reason = f'Transfer failed after capture: {exc}'
        tip.status = 'failed'
        logger.error('Tip transfer failed for %s: %s', tip.id, exc)
        return False, f'Transfer failed: {exc}'
    tip.stripe_transfer_id = _stripe_get(transfer, 'id')
    tip.failure_reason = None
    tip.status = 'completed'
    tip.completed_at = utc_now()
    notify(
        tip.recipient_id,
        'tip_received',
        _('You received a tip!'),
        _('You received a tip of %(amount)s.',
          amount=format_money(net_cents / 100, 'CAD')),
        actor_id=tip.tipper_id,
        link_url='/payments/payouts',
    )
    return True, 'Tip sent.'


def fulfill_tip_checkout(session_obj):
    """Fulfill a completed tip Checkout session (webhook). Idempotent
    on tip status: a redelivered event for a completed tip is a
    no-op. A tip whose recipient has no ready Connect account stays
    'pending' — the money is captured, the payout waits until they
    connect (retry_pending_tips). Returns the Tip, or None when the
    session carries no tip id we know. Does not commit."""
    metadata = _stripe_get(session_obj, 'metadata') or {}
    tip_id = metadata.get('tip_id') if isinstance(metadata, dict) else None
    tip = db.session.get(Tip, tip_id) if tip_id else None
    if tip is None:
        logger.warning('Tip checkout without a known tip_id — skipping')
        return None
    if tip.status == 'completed':
        return tip
    first_fulfillment = tip.stripe_payment_intent_id is None
    tip.stripe_session_id = _stripe_get(session_obj, 'id')
    tip.stripe_payment_intent_id = _stripe_get(session_obj, 'payment_intent')
    account = ConnectAccount.query.filter_by(
        user_id=tip.recipient_id
    ).first()
    if not account or not account.is_ready:
        # Paid, but the recipient cannot be paid out yet — wait
        # without failing (same as a hire payout waiting on
        # onboarding). Prompt them to connect, once: only on the
        # first fulfillment, so a redelivery does not re-notify.
        if first_fulfillment:
            notify(
                tip.recipient_id,
                'tip_pending',
                _('A tip is waiting for you'),
                _('Someone tipped you, but the money is on hold until '
                  'you finish setting up Stripe payouts.'),
                actor_id=tip.tipper_id,
                link_url='/payments/connect',
            )
        return tip
    if tip.status == 'failed':
        # A previous transfer attempt failed — a redelivered webhook
        # is a legitimate retry (fresh key, see _transfer_tip).
        logger.info('Retrying failed tip transfer for %s', tip.id)
    _transfer_tip(tip, account)
    return tip


def retry_pending_tips(user_id):
    """Re-attempt the transfer for all unpaid tips owed to one
    recipient (called when their Connect onboarding completes).
    Only tips that were actually PAID (a payment intent is
    recorded) can move — a pending tip with no payment intent is a
    Checkout that never completed, not money waiting. 'failed' tips
    are included too: a transfer that failed after capture has no
    other recovery path (the webhook already returned 200, so
    Stripe never redelivers it) — without this, captured money
    would sit unpaid forever. The per-tip idempotency key in
    _transfer_tip (fresh when a dead transfer id is recorded) keeps
    the retry from double-paying. Returns a list of
    (tip_id, ok, message). Does not commit."""
    account = ConnectAccount.query.filter_by(user_id=user_id).first()
    if not account or not account.is_ready:
        return []
    tips = (
        Tip.query
        .filter_by(recipient_id=user_id)
        .filter(Tip.status.in_(['pending', 'failed']))
        .filter(Tip.stripe_payment_intent_id.isnot(None))
        .order_by(Tip.created_at)
        .all()
    )
    results = []
    for tip in tips:
        ok, msg = _transfer_tip(tip, account)
        results.append((tip.id, ok, msg))
    return results


def handle_tip_transfer_event(event_type, transfer_obj):
    """``transfer.paid`` / ``transfer.failed`` / ``transfer.reversed``
    for a tip transfer. The tip is normally already 'completed'
    (the transfer succeeded synchronously at fulfillment) — this
    confirms it, and catches the failure case: a transfer that
    Stripe later reports failed marks the tip failed so the admin
    listing shows it. Idempotent: a duplicate event is a no-op
    because the status is already there. Returns the Tip, or None
    when the transfer is not one of ours. Does not commit."""
    transfer_id = _stripe_get(transfer_obj, 'id')
    tip = Tip.query.filter_by(stripe_transfer_id=transfer_id).first()
    if tip is None:
        return None
    if event_type == 'transfer.paid':
        if tip.status != 'completed':
            tip.status = 'completed'
            tip.completed_at = tip.completed_at or utc_now()
            tip.failure_reason = None
    elif event_type in ('transfer.failed', 'transfer.reversed'):
        if tip.status != 'failed':
            tip.status = 'failed'
            tip.failure_reason = (
                f'Stripe transfer {event_type.split(".")[1]}.'
            )
            notify(
                tip.recipient_id,
                'tip_failed',
                _('Your tip payout failed'),
                _('Stripe could not send a tip payout to you. Please '
                  'check your payout details.'),
                link_url='/payments/connect',
            )
    # transfer.created: the tip is already 'completed' — nothing to do.
    return tip
