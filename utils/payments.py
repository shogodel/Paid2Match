"""Payments service — Stripe Connect escrow, payouts and webhooks (M2).

Money flows through Stripe Connect in CAD:

* A poster "secures" a paid need with a Checkout session whose
  PaymentIntent uses manual capture — the money is authorized, not
  taken. That is the escrow.
* When both sides confirm the match, the PaymentIntent is captured and
  the referrer's share is transferred to their Connect Express account
  (``stripe.Transfer.create`` with ``source_transaction`` — the
  separate-charges-and-transfers flow). The platform fee is retained
  by transferring only the net amount: Stripe's Transfer API has no
  ``application_fee_amount`` parameter, and the referrer is not known
  at authorize time, so the fee cannot be set on the charge either.
  The fee is recorded on the Payout and Match rows instead.
* The referrer's Connect account pays out to their bank on Stripe's
  schedule; ``payout.*`` webhooks are informational for us.

M4 chains: when the winning referral came through someone's share,
the referrer's net splits 70/30 with the sharer (utils/chains.py).
The match then has TWO Payout rows — the referrer's ('referral') and
the sharer's ('chain_share') — each with its own transfer, status,
and Connect onboarding requirement. A payout whose recipient has not
finished onboarding waits without blocking the other payout. The
platform fee is recorded once, on the referrer payout.

Every function that touches the database does NOT commit — the caller
commits. That keeps route handlers and webhook handlers in control of
the transaction.

Stripe calls are made through the ``stripe`` module directly so tests
can patch them (``stripe.PaymentIntent.capture`` etc.) — no network
calls in tests.
"""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import stripe
from flask import current_app

from extensions import db
from models import (
    AdminSettings,
    ConnectAccount,
    Match,
    Payout,
    utc_now,
)
from utils.chains import compute_chain_split, resolve_chain_sharer
from utils.notifications import notify

logger = logging.getLogger(__name__)

# Paid2Match is a Canadian product — all money moves in CAD.
CURRENCY = 'cad'

# Platform fee: admin-configurable, default 20%. Founding referrers
# (admin-set flag) pay 15% for their first 12 months (the 12-month
# window is enforced by the admin removing the flag, not by code —
# see the M2 report).
PLATFORM_FEE_SETTING_KEY = 'PLATFORM_FEE_PERCENT'
DEFAULT_PLATFORM_FEE_PERCENT = 20
FOUNDING_REFERRER_FEE_PERCENT = 15
MAX_PLATFORM_FEE_PERCENT = 50


# ---------------------------------------------------------------------------
# Configuration / helpers
# ---------------------------------------------------------------------------

def stripe_enabled():
    """Whether payments are switched on (the kill-switch)."""
    return bool(current_app.config.get('STRIPE_ENABLED', True))


def _set_api_key():
    """Load the Stripe secret key into the client. Returns False when
    no key is configured."""
    secret = AdminSettings.get('STRIPE_SECRET_KEY')
    if not secret:
        return False
    stripe.api_key = secret
    return True


def _stripe_get(obj, key, default=None):
    """Read a field from a Stripe object, whether it is a StripeObject
    (attribute access), a plain dict, or a test double."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def to_cents(amount):
    """Convert a dollar amount to integer cents (half-up rounding)."""
    return int(
        (Decimal(str(amount)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    )


def get_platform_fee_percent():
    """The platform fee percentage from settings, clamped to 0–50."""
    raw = AdminSettings.get(PLATFORM_FEE_SETTING_KEY)
    try:
        value = int(raw) if raw is not None else DEFAULT_PLATFORM_FEE_PERCENT
    except (TypeError, ValueError):
        return DEFAULT_PLATFORM_FEE_PERCENT
    return max(0, min(MAX_PLATFORM_FEE_PERCENT, value))


def fee_rate_for(referrer):
    """The fee percentage that applies to one referrer: 15% for
    founding referrers, the configured platform fee otherwise."""
    if referrer is not None and getattr(referrer, 'founding_referrer', False):
        return FOUNDING_REFERRER_FEE_PERCENT
    return get_platform_fee_percent()


def compute_payout_amounts(reward, fee_percent):
    """Split a reward into (gross, platform fee, net to referrer)."""
    gross = Decimal(str(reward))
    fee = (gross * Decimal(fee_percent) / 100).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )
    return gross, fee, gross - fee


# ---------------------------------------------------------------------------
# Checkout (escrow authorization)
# ---------------------------------------------------------------------------

def create_checkout_session(success_url, cancel_url, amount_cents,
                            description, metadata=None, capture_method='automatic'):
    """Create a Stripe Checkout session (CAD) and return its URL, or
    None when Stripe is unavailable. ``capture_method='manual'``
    authorizes without capturing — the escrow flow for securing a
    need. Upgrades stay immediate-capture ('automatic')."""
    if not stripe_enabled():
        return None
    if not _set_api_key():
        return None
    params = {
        'payment_method_types': ['card'],
        'line_items': [{
            'price_data': {
                'currency': CURRENCY,
                'product_data': {'name': description},
                'unit_amount': int(amount_cents),
            },
            'quantity': 1,
        }],
        'mode': 'payment',
        'success_url': success_url,
        'cancel_url': cancel_url,
        'metadata': metadata or {},
    }
    if capture_method == 'manual':
        params['payment_intent_data'] = {'capture_method': 'manual'}
    try:
        session = stripe.checkout.Session.create(**params)
    except stripe.error.StripeError as exc:
        logger.error('Checkout session creation failed: %s', exc)
        return None
    return _stripe_get(session, 'url')


def create_subscription_checkout_session(success_url, cancel_url,
                                         amount_cents, description,
                                         metadata=None, interval='month'):
    """Create a Stripe Checkout session in subscription mode (M3: Pro
    $9/month, recurring bumps $5/month). The price is built inline
    (no pre-created Price in the Stripe dashboard), so the code works
    the moment the platform account is approved. Returns the Checkout
    URL, or None when Stripe is unavailable."""
    if not stripe_enabled():
        return None
    if not _set_api_key():
        return None
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': CURRENCY,
                    'product_data': {'name': description},
                    'unit_amount': int(amount_cents),
                    'recurring': {'interval': interval},
                },
                'quantity': 1,
            }],
            mode='subscription',
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata or {},
        )
    except stripe.error.StripeError as exc:
        logger.error('Subscription checkout creation failed: %s', exc)
        return None
    return _stripe_get(session, 'url')


def cancel_subscription_at_period_end(subscription_id):
    """Set cancel_at_period_end on a Stripe subscription (access runs
    to the end of the paid period). Returns (ok, error)."""
    if not stripe_enabled():
        return False, 'Payments are currently unavailable.'
    if not _set_api_key():
        return False, 'Stripe is not configured.'
    try:
        stripe.Subscription.modify(
            subscription_id,
            cancel_at_period_end=True,
        )
    except stripe.error.StripeError as exc:
        logger.error(
            'Subscription cancel failed for %s: %s', subscription_id, exc
        )
        return False, str(exc)
    return True, None


# ---------------------------------------------------------------------------
# Connect Express onboarding
# ---------------------------------------------------------------------------

def create_connect_account(user):
    """Create a Stripe Express account for a referrer and store the
    local mirror row. Returns (ConnectAccount, error)."""
    existing = ConnectAccount.query.filter_by(user_id=user.id).first()
    if existing:
        return existing, None
    if not stripe_enabled():
        return None, 'Payments are currently unavailable.'
    if not _set_api_key():
        return None, 'Stripe is not configured.'
    try:
        account = stripe.Account.create(
            type='express',
            country='CA',
            email=user.email,
            capabilities={
                'card_payments': {'requested': True},
                'transfers': {'requested': True},
            },
            metadata={'user_id': user.id},
        )
    except stripe.error.StripeError as exc:
        logger.error('Connect account creation failed for %s: %s', user.id, exc)
        return None, str(exc)
    connect = ConnectAccount(
        user_id=user.id,
        stripe_account_id=_stripe_get(account, 'id'),
        onboarding_status='pending',
    )
    db.session.add(connect)
    db.session.flush()
    return connect, None


def create_account_link(connect_account, return_url, refresh_url):
    """Create an onboarding link for a Connect account. Returns the
    URL, or None on failure."""
    if not _set_api_key():
        return None
    try:
        link = stripe.AccountLink.create(
            account=connect_account.stripe_account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type='account_onboarding',
        )
    except stripe.error.StripeError as exc:
        logger.error(
            'AccountLink creation failed for %s: %s',
            connect_account.stripe_account_id, exc,
        )
        return None
    return _stripe_get(link, 'url')


def _sync_connect_account(connect, account_obj):
    """Copy the live Stripe account state onto the local mirror row.
    Returns True when payouts became newly enabled."""
    was_ready = connect.is_ready
    connect.charges_enabled = bool(_stripe_get(account_obj, 'charges_enabled'))
    connect.payouts_enabled = bool(_stripe_get(account_obj, 'payouts_enabled'))
    connect.details_submitted = bool(_stripe_get(account_obj, 'details_submitted'))
    requirements = _stripe_get(account_obj, 'requirements') or {}
    due = _stripe_get(requirements, 'currently_due') or []
    connect.requirements_due = json.dumps(due) if due else None
    if connect.payouts_enabled:
        connect.onboarding_status = 'complete'
    elif connect.details_submitted and due:
        connect.onboarding_status = 'restricted'
    else:
        connect.onboarding_status = 'pending'
    db.session.flush()
    return connect.is_ready and not was_ready


def refresh_connect_account(connect):
    """Re-read a Connect account from Stripe and sync the local row.
    Returns (ok, newly_ready)."""
    if not _set_api_key():
        return False, False
    try:
        account = stripe.Account.retrieve(connect.stripe_account_id)
    except stripe.error.StripeError as exc:
        logger.error(
            'Connect account retrieve failed for %s: %s',
            connect.stripe_account_id, exc,
        )
        return False, False
    return True, _sync_connect_account(connect, account)


# ---------------------------------------------------------------------------
# PaymentIntent capture / cancel / refund
# ---------------------------------------------------------------------------

def ensure_captured(payment_intent_id):
    """Capture a PaymentIntent if it is still awaiting capture.
    Returns (ok, error). A PI that is already captured (pre-M2
    immediate-capture payments) counts as success."""
    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    except stripe.error.StripeError as exc:
        return False, str(exc)
    status = _stripe_get(intent, 'status')
    if status == 'succeeded':
        return True, None
    if status != 'requires_capture':
        return False, f'Payment is {status!r} — it cannot be captured.'
    try:
        stripe.PaymentIntent.capture(payment_intent_id)
    except stripe.error.StripeError as exc:
        return False, str(exc)
    return True, None


def cancel_authorization(payment_intent_id):
    """Cancel an uncaptured PaymentIntent (releases the hold on the
    poster's card). Returns (ok, error). A captured PI cannot be
    cancelled — refund it instead."""
    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    except stripe.error.StripeError as exc:
        return False, str(exc)
    status = _stripe_get(intent, 'status')
    if status == 'canceled':
        return True, None
    if status == 'succeeded':
        return False, 'Payment was already captured — refund it instead.'
    try:
        stripe.PaymentIntent.cancel(payment_intent_id)
    except stripe.error.StripeError as exc:
        return False, str(exc)
    return True, None


def payment_intent_status(payment_intent_id):
    """The current status of a PaymentIntent (e.g. 'requires_capture',
    'succeeded', 'canceled'), or None when Stripe cannot be reached."""
    if not _set_api_key():
        return None
    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    except stripe.error.StripeError:
        return None
    return _stripe_get(intent, 'status')


def create_refund(payment_intent_id, amount_cents):
    """Refund a captured PaymentIntent (partial refunds allowed).
    Returns (refund_id, error)."""
    if not _set_api_key():
        return None, 'Stripe is not configured.'
    try:
        refund = stripe.Refund.create(
            payment_intent=payment_intent_id,
            amount=int(amount_cents),
        )
    except stripe.error.StripeError as exc:
        return None, str(exc)
    return _stripe_get(refund, 'id'), None


# ---------------------------------------------------------------------------
# Escrow release (capture + transfer)
# ---------------------------------------------------------------------------

def _find_or_create_payouts(match):
    """The (referrer, chain) Payout rows for a match, creating them
    (with the fee locked in at the current rate) when they do not
    exist yet. The chain payout exists only when the referral came
    through a valid share (utils/chains.resolve_chain_sharer). The
    platform fee is recorded once, on the referrer payout; the chain
    payout's fee is 0. The split is decided once, at creation: a
    referrer payout created without a chain never gains one later
    (the referrer was already promised the full net).
    Returns (referrer_payout, chain_payout_or_None, created)."""
    referrer_payout = Payout.query.filter_by(
        match_id=match.id, kind='referral'
    ).first()
    chain_payout = Payout.query.filter_by(
        match_id=match.id, kind='chain_share'
    ).first()
    if referrer_payout is not None:
        return referrer_payout, chain_payout, False
    fee_pct = fee_rate_for(match.hunter)
    gross, fee, net = compute_payout_amounts(
        float(match.bounty.reward_amount or 0), fee_pct
    )
    sharer_id = resolve_chain_sharer(match)
    if sharer_id:
        referrer_amount, sharer_amount = compute_chain_split(net)
        chain_payout = Payout(
            match_id=match.id,
            referrer_id=sharer_id,
            bounty_id=match.bounty_id,
            kind='chain_share',
            gross_amount=sharer_amount,
            platform_fee=Decimal('0'),
            amount=sharer_amount,
            currency=CURRENCY,
            method='stripe',
            status='pending',
        )
        db.session.add(chain_payout)
    else:
        referrer_amount = net
    referrer_payout = Payout(
        match_id=match.id,
        referrer_id=match.hunter_id,
        bounty_id=match.bounty_id,
        kind='referral',
        gross_amount=gross,
        platform_fee=fee,
        amount=referrer_amount,
        currency=CURRENCY,
        method='stripe',
        status='pending',
    )
    db.session.add(referrer_payout)
    match.platform_fee = fee
    match.payout_amount = gross
    db.session.flush()
    return referrer_payout, chain_payout, True


def release_escrow(match):
    """Release escrow for a dual-confirmed match: capture the
    authorized PaymentIntent, then transfer each payout to its
    recipient's Connect account — the referrer's share, and the
    sharer's cut when the referral came through a share (M4 chains).
    Returns (ok, message). Idempotent — safe to call twice; a retry
    after a failed transfer never re-captures (``captured_at`` guards
    it) and never re-transfers a payout that already went out. A
    payout whose recipient has not finished Connect onboarding waits
    without blocking the other payout. Does not commit."""
    if not (match.hunter_confirmed and match.employer_confirmed):
        return False, 'Match is not confirmed by both sides yet.'
    bounty = match.bounty
    reward = float(bounty.reward_amount or 0) if bounty else 0
    if reward <= 0:
        if match.payout_status != 'completed':
            match.set_payout_status('completed')
        return True, 'Free need — nothing to pay out.'

    referrer_payout, chain_payout, created = _find_or_create_payouts(match)
    payouts = [p for p in (referrer_payout, chain_payout) if p is not None]
    to_transfer = [p for p in payouts if p.status in ('pending', 'failed')]
    if not to_transfer:
        if referrer_payout.status == 'completed':
            return True, 'Payout already completed.'
        return True, 'Payout already in progress.'

    # The referrer must have finished Stripe Connect onboarding
    # before their payout can move. (The sharer's onboarding is
    # checked separately below — it never blocks the referrer.)
    referrer_account = None
    if referrer_payout in to_transfer:
        referrer_account = ConnectAccount.query.filter_by(
            user_id=match.hunter_id
        ).first()
        if not referrer_account or not referrer_account.is_ready:
            referrer_payout.failure_reason = (
                'Awaiting referrer payout setup '
                '(Stripe Connect onboarding not complete).'
            )
            if created:
                notify(
                    match.hunter_id,
                    'payout_pending',
                    'Your payout is waiting on payout setup',
                    'Your referral was confirmed, but your payout is on hold '
                    'until you finish setting up Stripe payouts.',
                    link_url='/payments/connect',
                )
            return False, 'Referrer has not completed payout setup yet.'

    # The sharer's share waits on the sharer's own onboarding.
    sharer_waiting = False
    chain_account = None
    if chain_payout is not None and chain_payout in to_transfer:
        chain_account = ConnectAccount.query.filter_by(
            user_id=chain_payout.referrer_id
        ).first()
        if not chain_account or not chain_account.is_ready:
            chain_payout.failure_reason = (
                'Awaiting sharer payout setup '
                '(Stripe Connect onboarding not complete).'
            )
            if created:
                notify(
                    chain_payout.referrer_id,
                    'payout_pending',
                    'Your share payout is waiting on payout setup',
                    'A share of yours led to a paid hire, but your cut is '
                    'on hold until you finish setting up Stripe payouts.',
                    link_url='/payments/connect',
                )
            to_transfer.remove(chain_payout)
            sharer_waiting = True
            if not to_transfer:
                return False, 'Sharer has not completed payout setup yet.'

    if not stripe_enabled():
        for payout in to_transfer:
            payout.failure_reason = 'Payments are currently disabled.'
        return False, 'Payments are currently disabled.'
    if not _set_api_key():
        for payout in to_transfer:
            payout.failure_reason = 'Stripe is not configured.'
        return False, 'Stripe is not configured.'
    if not bounty.stripe_payment_intent:
        for payout in to_transfer:
            payout.failure_reason = 'No payment was secured for this need.'
            payout.set_status('failed')
        if referrer_payout in to_transfer:
            match.set_payout_status('failed')
        return False, 'No payment was secured for this need.'

    # Capture the authorization (no-op if already captured).
    ok, error = ensure_captured(bounty.stripe_payment_intent)
    if not ok:
        for payout in to_transfer:
            payout.failure_reason = f'Payment capture failed: {error}'
            payout.set_status('failed')
        if referrer_payout in to_transfer:
            match.set_payout_status('failed')
        return False, f'Payment capture failed: {error}'
    if referrer_payout.captured_at is None:
        referrer_payout.captured_at = utc_now()

    # Transfer each payout to its recipient's Connect account. The
    # platform fee stays in the platform balance — it is the
    # difference between the captured amount and the transfers.
    accounts = {referrer_payout.id: referrer_account}
    if chain_payout is not None:
        accounts[chain_payout.id] = chain_account
    succeeded = []
    first_error = None
    for payout in to_transfer:
        account = accounts.get(payout.id)
        if account is None:
            # Defensive: both accounts were fetched above for every
            # payout in to_transfer. This cannot happen.
            continue
        try:
            transfer = stripe.Transfer.create(
                amount=to_cents(payout.amount),
                currency=CURRENCY,
                destination=account.stripe_account_id,
                source_transaction=bounty.stripe_payment_intent,
                metadata={'match_id': match.id, 'payout_id': payout.id},
            )
        except stripe.error.StripeError as exc:
            # The money was captured but the transfer failed — the
            # platform holds the funds. Fail loudly so an admin
            # resolves it; a retry skips the capture thanks to
            # captured_at. The other payout still gets its chance.
            payout.failure_reason = f'Transfer failed after capture: {exc}'
            payout.set_status('failed')
            if payout is referrer_payout:
                match.set_payout_status('failed')
            if first_error is None:
                first_error = exc
            continue
        payout.stripe_transfer_id = _stripe_get(transfer, 'id')
        payout.failure_reason = None
        payout.set_status('processing')
        succeeded.append(payout)

    if not succeeded:
        return False, f'Transfer failed: {first_error}'
    if referrer_payout in succeeded:
        match.set_payout_status('processing')
    if sharer_waiting:
        return True, (
            'Payout in progress. The sharer share is waiting on '
            'their payout setup.'
        )
    return True, 'Payout in progress.'


def retry_pending_payouts(user_id):
    """Re-attempt release for all pending/failed Stripe payouts of one
    referrer (called when their Connect onboarding completes).
    Returns a list of (payout_id, ok, message). Does not commit."""
    account = ConnectAccount.query.filter_by(user_id=user_id).first()
    if not account or not account.is_ready:
        return []
    payouts = (
        Payout.query
        .filter_by(referrer_id=user_id, method='stripe')
        .filter(Payout.status.in_(['pending', 'failed']))
        .all()
    )
    results = []
    for payout in payouts:
        match = db.session.get(Match, payout.match_id)
        if not match or not (match.hunter_confirmed and match.employer_confirmed):
            continue
        ok, msg = release_escrow(match)
        results.append((payout.id, ok, msg))
    return results


# ---------------------------------------------------------------------------
# Manual (Interac) payouts — interim path before Connect is approved
# ---------------------------------------------------------------------------

def record_manual_payout(match, amount, reference, admin_user,
                         payout_kind='referral'):
    """Record an Interac payout the admin already made to the referrer
    (``payout_kind='referral'``) or to the sharer's cut of a
    two-level chain (``payout_kind='chain_share'`` — requires a chain
    sharer, else ValueError). Marks the payout completed and notifies
    the recipient. Also captures the escrow PaymentIntent when
    possible (so the platform receives the poster's money) — but a
    capture failure never blocks the record: the recipient was paid,
    and the capture can be retried separately. The match status
    mirrors the referrer payout — a chain payout never completes the
    match on its own. Returns (payout, capture_note). Does not
    commit."""
    if payout_kind not in ('referral', 'chain_share'):
        raise ValueError(f'unknown payout kind {payout_kind!r}')
    payout = Payout.query.filter_by(
        match_id=match.id, kind=payout_kind
    ).first()
    amount = Decimal(str(amount))
    if payout is None:
        if payout_kind == 'chain_share':
            sharer_id = resolve_chain_sharer(match)
            if sharer_id is None:
                raise ValueError('This match has no chain sharer to pay.')
            payout = Payout(
                match_id=match.id,
                referrer_id=sharer_id,
                bounty_id=match.bounty_id,
                kind='chain_share',
                gross_amount=amount,
                platform_fee=Decimal('0'),
                amount=amount,
                currency=CURRENCY,
                method='manual',
                status='pending',
            )
        else:
            fee_pct = fee_rate_for(match.hunter)
            gross, fee, _net = compute_payout_amounts(
                float(match.bounty.reward_amount or 0), fee_pct
            )
            payout = Payout(
                match_id=match.id,
                referrer_id=match.hunter_id,
                bounty_id=match.bounty_id,
                kind='referral',
                gross_amount=gross,
                platform_fee=fee,
                amount=amount,
                currency=CURRENCY,
                method='manual',
                status='pending',
            )
            match.platform_fee = fee
            match.payout_amount = gross
        db.session.add(payout)
        db.session.flush()
    payout.method = 'manual'
    payout.amount = amount
    payout.manual_reference = reference or None
    payout.manual_paid_at = utc_now()
    payout.recorded_by_id = admin_user.id
    payout.failure_reason = None
    payout.set_status('completed')
    if payout_kind == 'referral':
        match.set_payout_status('completed')

    # Best-effort escrow capture (skipped when payments are disabled —
    # that is the point of the interim path). The capture happens once
    # per match, whichever payout records it first.
    capture_note = None
    bounty = match.bounty
    already_captured = (
        Payout.query.filter_by(match_id=match.id)
        .filter(Payout.captured_at.isnot(None))
        .first()
        is not None
    )
    if (bounty and bounty.stripe_payment_intent and stripe_enabled()
            and _set_api_key() and not already_captured):
        ok, error = ensure_captured(bounty.stripe_payment_intent)
        if ok:
            payout.captured_at = utc_now()
        else:
            capture_note = (
                f'Note: escrow capture failed ({error}) — the recipient '
                'is paid, but the platform has not received the '
                'poster\u2019s money yet.'
            )

    if payout_kind == 'chain_share':
        notify(
            payout.referrer_id,
            'payment_released',
            'Your share payout was sent',
            f'Your cut of ${amount:,.2f} CAD for a share that led to a '
            'hire was sent by Interac.',
            link_url='/payments/payouts',
        )
    else:
        notify(
            match.hunter_id,
            'payment_released',
            'Your payout was sent',
            f'Your payout of ${amount:,.2f} CAD was sent by Interac.',
            link_url='/payments/payouts',
        )
    return payout, capture_note


# ---------------------------------------------------------------------------
# Connect webhook handlers
# ---------------------------------------------------------------------------

def on_connect_ready(connect):
    """Side effects when a referrer's Connect account becomes ready:
    notify them and retry their pending payouts. Does not commit."""
    notify(
        connect.user_id,
        'connect_ready',
        'Payout setup complete!',
        'Your Stripe payout account is ready — payouts can now be '
        'sent to you.',
        link_url='/payments/payouts',
    )
    retry_pending_payouts(connect.user_id)


def handle_account_updated(account_obj):
    """Sync a ConnectAccount from an ``account.updated`` event. Retries
    pending payouts when payouts become enabled. Returns the account,
    or None when it is not one of ours. Does not commit."""
    account_id = _stripe_get(account_obj, 'id')
    connect = ConnectAccount.query.filter_by(stripe_account_id=account_id).first()
    if connect is None:
        logger.info('account.updated for unknown Stripe account %s', account_id)
        return None
    newly_ready = _sync_connect_account(connect, account_obj)
    if newly_ready:
        on_connect_ready(connect)
    return connect


def handle_transfer_event(event_type, transfer_obj):
    """``transfer.created`` / ``transfer.paid`` / ``transfer.failed`` /
    ``transfer.reversed``. Marks the payout (and its match) completed
    or failed and notifies the referrer. Idempotent: a duplicate event
    is a no-op because the status is already there. Does not commit."""
    transfer_id = _stripe_get(transfer_obj, 'id')
    payout = Payout.query.filter_by(stripe_transfer_id=transfer_id).first()
    if payout is None:
        logger.info('transfer event %s for unknown transfer %s', event_type, transfer_id)
        return None
    if event_type == 'transfer.paid':
        if payout.status != 'completed':
            payout.set_status('completed')
            payout.failure_reason = None
            match = payout.match
            # The match status mirrors the referrer payout — a chain
            # payout completing never completes the match on its own.
            if (match is not None and payout.kind == 'referral'
                    and match.payout_status != 'completed'):
                match.set_payout_status('completed')
            if payout.kind == 'chain_share':
                notify(
                    payout.referrer_id,
                    'payment_released',
                    'Your share earned you money',
                    f'Your cut of ${payout.amount:,.2f} CAD for a share '
                    'that led to a hire is on its way to your Stripe '
                    'account.',
                    link_url='/payments/payouts',
                )
            else:
                notify(
                    payout.referrer_id,
                    'payment_released',
                    'Your payout was sent',
                    f'Your payout of ${payout.amount:,.2f} CAD is on its way '
                    'to your Stripe account.',
                    link_url='/payments/payouts',
                )
    elif event_type in ('transfer.failed', 'transfer.reversed'):
        if payout.status != 'failed':
            payout.set_status('failed')
            payout.failure_reason = (
                f'Stripe transfer {event_type.split(".")[1]}.'
            )
            match = payout.match
            if (match is not None and payout.kind == 'referral'
                    and match.payout_status != 'failed'):
                match.set_payout_status('failed')
            notify(
                payout.referrer_id,
                'payout_failed',
                'Your payout failed',
                'Stripe could not send your payout. We will retry it — '
                'no action needed from you.',
                link_url='/payments/payouts',
            )
    # transfer.created: the payout is already 'processing' — nothing to do.
    return payout


def handle_payout_event(event_type, payout_obj, connected_account_id=None):
    """``payout.paid`` / ``payout.failed`` — Stripe paying the
    referrer's bank from their Connect balance. Informational for us
    (the money was already released at ``transfer.paid``): record the
    last bank payout, and warn the referrer if their bank payout
    failed. Does not commit."""
    if not connected_account_id:
        return None
    connect = ConnectAccount.query.filter_by(
        stripe_account_id=connected_account_id
    ).first()
    if connect is None:
        return None
    if event_type == 'payout.paid':
        amount = _stripe_get(payout_obj, 'amount')
        if amount is not None:
            connect.last_payout_amount = Decimal(amount) / 100
        arrival = _stripe_get(payout_obj, 'arrival_date')
        if arrival:
            connect.last_payout_at = datetime.fromtimestamp(
                arrival, tz=timezone.utc
            ).replace(tzinfo=None)
        else:
            connect.last_payout_at = utc_now()
    elif event_type == 'payout.failed':
        failure_code = _stripe_get(payout_obj, 'failure_code') or 'unknown'
        notify(
            connect.user_id,
            'payout_failed',
            'Your bank payout failed',
            f'Stripe could not pay out to your bank account '
            f'({failure_code}). Please check your payout details in '
            'Stripe.',
            link_url='/payments/connect',
        )
    return connect
