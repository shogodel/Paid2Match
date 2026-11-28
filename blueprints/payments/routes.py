"""Payments routes — Stripe Connect onboarding + payout dashboard (M2),
bump/boost purchases + Pro subscriptions (M3)."""
from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from blueprints.payments import payments_bp
from extensions import db, get_or_404
from models import ConnectAccount, Payout
from models.bump import (
    BOOST_MAX_DAYS,
    BOOST_PRICE_PER_DAY_CENTS,
    BUMP_PACKS,
    BUMP_SINGLE_PRICE_CENTS,
    RECURRING_BUMP_PRICE_CENTS,
    RecurringBump,
)
from models.post import Post
from utils.bumps import (
    boost_eligibility,
    bump_eligibility,
    cancel_recurring_bump,
    free_bump_balance,
    pack_bump_balance,
)
from utils.earnings import get_earnings_summary
from utils.payments import (
    create_account_link,
    create_checkout_session,
    create_connect_account,
    create_subscription_checkout_session,
    idempotency_key_for,
    on_connect_ready,
    refresh_connect_account,
    release_escrow,
    stripe_enabled,
)
from utils.pro import (
    cancel_pro_at_period_end,
    create_pro_checkout,
    get_pro_subscription,
    is_pro,
)


def _return_url():
    return url_for('payments.connect_return', _external=True)


def _refresh_url():
    return url_for('payments.connect_refresh', _external=True)


@payments_bp.route('/connect')
@login_required
def connect_status():
    """GET /payments/connect - Show the referrer's payout setup status."""
    account = ConnectAccount.query.filter_by(user_id=current_user.id).first()
    payouts_pending = Payout.query.filter_by(
        referrer_id=current_user.id
    ).filter(Payout.status.in_(['pending', 'failed'])).count()
    return render_template(
        'payments/connect.html',
        account=account,
        payouts_pending=payouts_pending,
        payments_enabled=stripe_enabled(),
    )


@payments_bp.route('/connect/start', methods=['POST'])
@login_required
def connect_start():
    """POST /payments/connect/start - Create the Connect account (if
    needed) and redirect the referrer to Stripe's onboarding flow."""
    if not stripe_enabled():
        flash('Payments are currently unavailable.', 'warning')
        return redirect(url_for('payments.connect_status'))
    account, error = create_connect_account(current_user)
    if account is None:
        flash(f'Could not start payout setup: {error}', 'danger')
        return redirect(url_for('payments.connect_status'))
    db.session.commit()
    link = create_account_link(account, _return_url(), _refresh_url())
    if not link:
        flash('Could not start payout setup — please try again.', 'danger')
        return redirect(url_for('payments.connect_status'))
    return redirect(link)


@payments_bp.route('/connect/return')
@login_required
def connect_return():
    """GET /payments/connect/return - Stripe redirects here after
    onboarding. Refresh the account state from Stripe."""
    account = ConnectAccount.query.filter_by(user_id=current_user.id).first()
    if account is None:
        flash('No payout account found — please start setup again.', 'warning')
        return redirect(url_for('payments.connect_status'))
    ok, newly_ready = refresh_connect_account(account)
    if not ok:
        flash('Could not check your payout status — please try again.', 'warning')
        return redirect(url_for('payments.connect_status'))
    if newly_ready:
        on_connect_ready(account)
    db.session.commit()
    if account.is_ready:
        flash('Payout setup complete! Payouts can now be sent to you.', 'success')
    elif account.onboarding_status == 'restricted':
        flash('Stripe needs more information from you — please complete the remaining steps.', 'warning')
    else:
        flash('Payout setup is still in progress — please finish the steps in Stripe.', 'info')
    return redirect(url_for('payments.connect_status'))


@payments_bp.route('/connect/refresh')
@login_required
def connect_refresh():
    """GET /payments/connect/refresh - Stripe redirects here when an
    onboarding link expires. Create a fresh link."""
    account = ConnectAccount.query.filter_by(user_id=current_user.id).first()
    if account is None:
        flash('No payout account found — please start setup again.', 'warning')
        return redirect(url_for('payments.connect_status'))
    link = create_account_link(account, _return_url(), _refresh_url())
    if not link:
        flash('Could not restart payout setup — please try again.', 'danger')
        return redirect(url_for('payments.connect_status'))
    return redirect(link)


@payments_bp.route('/payouts')
@login_required
def payout_dashboard():
    """GET /payments/payouts - The referrer's payout dashboard: pending
    and paid payouts, amounts, dates and status. M4 extends it into
    the earnings dashboard: chain earnings, referrals by status, and
    per-need stats (utils/earnings.py)."""
    payouts = (
        Payout.query
        .filter_by(referrer_id=current_user.id)
        .order_by(Payout.created_at.desc())
        .all()
    )
    account = ConnectAccount.query.filter_by(user_id=current_user.id).first()
    earnings = get_earnings_summary(current_user.id)
    return render_template(
        'payments/payouts.html',
        payouts=payouts,
        account=account,
        total_paid=earnings['total_paid'],
        total_pending=earnings['total_pending'],
        earnings=earnings,
    )


@payments_bp.route('/payouts/<payout_id>/retry', methods=['POST'])
@login_required
def retry_payout(payout_id):
    """POST /payments/payouts/<payout_id>/retry - The referrer retries
    a failed or pending payout (e.g. after finishing Connect
    onboarding)."""
    payout = get_or_404(Payout, payout_id)
    if payout.referrer_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('payments.payout_dashboard'))
    if payout.status == 'completed':
        flash('This payout is already completed.', 'info')
        return redirect(url_for('payments.payout_dashboard'))
    if payout.status == 'processing':
        flash('This payout is already in progress.', 'info')
        return redirect(url_for('payments.payout_dashboard'))
    ok, msg = release_escrow(payout.match)
    db.session.commit()
    if ok:
        flash(f'Payout retry started: {msg}', 'success')
    else:
        flash(f'Payout could not be released: {msg}', 'warning')
    return redirect(url_for('payments.payout_dashboard'))


# ---------------------------------------------------------------------------
# Bumps, boosts, Pro (M3, §1.3–1.4)
# ---------------------------------------------------------------------------

def _checkout_base_url():
    return request.host_url.rstrip('/')


@payments_bp.route('/bumps')
@login_required
def bump_packs():
    """GET /payments/bumps — bump balances + pack options."""
    return render_template(
        'payments/bumps.html',
        free_balance=free_bump_balance(current_user.id),
        pack_balance=pack_bump_balance(current_user.id),
        packs=BUMP_PACKS,
        single_price_cents=BUMP_SINGLE_PRICE_CENTS,
        payments_enabled=stripe_enabled(),
    )


@payments_bp.route('/bumps/packs/buy', methods=['POST'])
@login_required
def buy_bump_pack():
    """POST /payments/bumps/packs/buy — buy a 5- or 10-pack via
    Checkout. The webhook credits the pack."""
    if not stripe_enabled():
        flash('Payments are currently unavailable.', 'warning')
        return redirect(url_for('payments.bump_packs'))
    pack_size = request.form.get('pack_size', type=int)
    if pack_size not in BUMP_PACKS:
        flash('Choose a 5-pack or a 10-pack.', 'danger')
        return redirect(url_for('payments.bump_packs'))
    price_cents = BUMP_PACKS[pack_size]
    base = _checkout_base_url()
    checkout_url = create_checkout_session(
        success_url=f'{base}{url_for("payments.bump_packs")}',
        cancel_url=f'{base}{url_for("payments.bump_packs")}',
        amount_cents=price_cents,
        description=f'Bump pack — {pack_size} bumps (CAD)',
        metadata={
            'm3_kind': 'bump_pack',
            'user_id': current_user.id,
            'pack_size': str(pack_size),
            'price_cents': str(price_cents),
        },
        # A double-click must return the same session, not a second
        # charge for a second pack (the webhook credits one pack per
        # session id).
        idempotency_key=idempotency_key_for(
            'bump-pack', current_user.id, pack_size,
        ),
    )
    if not checkout_url:
        flash('Stripe is not configured — please try again later.', 'danger')
        return redirect(url_for('payments.bump_packs'))
    return redirect(checkout_url)


@payments_bp.route('/bumps/single/<post_id>/buy', methods=['POST'])
@login_required
def buy_single_bump(post_id):
    """POST /payments/bumps/single/<post_id>/buy — buy one $2 bump
    for a post via Checkout. The webhook applies the bump."""
    post = get_or_404(Post, post_id)
    if post.author_id != current_user.id:
        flash('You can only bump your own posts.', 'danger')
        return redirect(url_for('social.post_detail', post_id=post_id))
    ok, reason = bump_eligibility(current_user, post)
    if not ok:
        flash(reason, 'warning')
        return redirect(url_for('social.bump_post', post_id=post_id))
    if not stripe_enabled():
        flash('Payments are currently unavailable.', 'warning')
        return redirect(url_for('social.bump_post', post_id=post_id))
    base = _checkout_base_url()
    checkout_url = create_checkout_session(
        success_url=f'{base}{url_for("social.post_detail", post_id=post_id)}',
        cancel_url=f'{base}{url_for("social.bump_post", post_id=post_id)}',
        amount_cents=BUMP_SINGLE_PRICE_CENTS,
        description='Single bump — 24h at the top of the feed (CAD)',
        metadata={
            'm3_kind': 'bump_single',
            'user_id': current_user.id,
            'post_id': post.id,
        },
        # See buy_bump_pack: a double-click returns the same session.
        idempotency_key=idempotency_key_for(
            'bump-single', post.id,
        ),
    )
    if not checkout_url:
        flash('Stripe is not configured — please try again later.', 'danger')
        return redirect(url_for('social.bump_post', post_id=post_id))
    return redirect(checkout_url)


@payments_bp.route('/bumps/boost/<post_id>/buy', methods=['POST'])
@login_required
def buy_boost(post_id):
    """POST /payments/bumps/boost/<post_id>/buy — buy a boost
    ($5/day, 1–7 days) via Checkout. The webhook starts the boost."""
    post = get_or_404(Post, post_id)
    if post.author_id != current_user.id:
        flash('You can only boost your own posts.', 'danger')
        return redirect(url_for('social.post_detail', post_id=post_id))
    ok, reason = boost_eligibility(current_user, post)
    if not ok:
        flash(reason, 'warning')
        return redirect(url_for('social.bump_post', post_id=post_id))
    days = request.form.get('days', type=int) or 1
    days = max(1, min(BOOST_MAX_DAYS, days))
    if not stripe_enabled():
        flash('Payments are currently unavailable.', 'warning')
        return redirect(url_for('social.bump_post', post_id=post_id))
    base = _checkout_base_url()
    checkout_url = create_checkout_session(
        success_url=f'{base}{url_for("social.post_detail", post_id=post_id)}',
        cancel_url=f'{base}{url_for("social.bump_post", post_id=post_id)}',
        amount_cents=days * BOOST_PRICE_PER_DAY_CENTS,
        description=f'Boost — {days} day(s) of extra Discover visibility (CAD)',
        metadata={
            'm3_kind': 'boost',
            'user_id': current_user.id,
            'post_id': post.id,
            'days': str(days),
        },
        # See buy_bump_pack: a double-click returns the same session.
        idempotency_key=idempotency_key_for(
            'boost', post.id, days,
        ),
    )
    if not checkout_url:
        flash('Stripe is not configured — please try again later.', 'danger')
        return redirect(url_for('social.bump_post', post_id=post_id))
    return redirect(checkout_url)


@payments_bp.route('/bumps/recurring/<post_id>/subscribe', methods=['POST'])
@login_required
def subscribe_recurring_bump(post_id):
    """POST /payments/bumps/recurring/<post_id>/subscribe — $5/month
    recurring bump (auto-bump every 3 days) via subscription Checkout.
    The webhook creates the recurring row and applies the first bump."""
    post = get_or_404(Post, post_id)
    if post.author_id != current_user.id:
        flash('You can only set up recurring bumps on your own posts.', 'danger')
        return redirect(url_for('social.post_detail', post_id=post_id))
    ok, reason = bump_eligibility(current_user, post)
    if not ok:
        flash(reason, 'warning')
        return redirect(url_for('social.bump_post', post_id=post_id))
    existing = RecurringBump.query.filter_by(post_id=post.id, status='active').first()
    if existing is not None:
        flash('This post already has a recurring bump.', 'info')
        return redirect(url_for('social.bump_post', post_id=post_id))
    if not stripe_enabled():
        flash('Payments are currently unavailable.', 'warning')
        return redirect(url_for('social.bump_post', post_id=post_id))
    base = _checkout_base_url()
    checkout_url = create_subscription_checkout_session(
        success_url=f'{base}{url_for("social.post_detail", post_id=post_id)}',
        cancel_url=f'{base}{url_for("social.bump_post", post_id=post_id)}',
        amount_cents=RECURRING_BUMP_PRICE_CENTS,
        description='Recurring bump — auto-bump every 3 days (CA$5/month)',
        metadata={
            'm3_kind': 'recurring_bump',
            'user_id': current_user.id,
            'post_id': post.id,
        },
        # One recurring bump per post: a double-click returns the
        # same session instead of a second subscription that would
        # keep charging with no local row. The key is bucketed by
        # the minute (see idempotency_key_for): a permanent key
        # would make a re-subscribe within Stripe's 24h idempotency
        # window return the stale completed session.
        idempotency_key=idempotency_key_for(
            'recurring-bump', post.id,
        ),
    )
    if not checkout_url:
        flash('Stripe is not configured — please try again later.', 'danger')
        return redirect(url_for('social.bump_post', post_id=post_id))
    return redirect(checkout_url)


@payments_bp.route('/bumps/recurring/<recurring_id>/cancel', methods=['POST'])
@login_required
def cancel_recurring_bump_route(recurring_id):
    """POST /payments/bumps/recurring/<id>/cancel — cancel at the end
    of the paid month (bumps keep running until then)."""
    recurring = get_or_404(RecurringBump, recurring_id)
    if recurring.user_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('social.feed'))
    if recurring.status != 'active':
        flash('This recurring bump is already canceled.', 'info')
        return redirect(url_for('social.post_detail', post_id=recurring.post_id))
    ok, error = cancel_recurring_bump(recurring)
    db.session.commit()
    if ok:
        flash('Recurring bump canceled — it runs until the end of the paid month.', 'success')
    else:
        flash(f'Could not cancel: {error}', 'danger')
    return redirect(url_for('social.post_detail', post_id=recurring.post_id))


@payments_bp.route('/pro')
@login_required
def pro_page():
    """GET /payments/pro — Pro analytics ($9/month): features, status,
    subscribe/cancel. Free for founding referrers (§1.4)."""
    subscription = get_pro_subscription(current_user.id)
    return render_template(
        'payments/pro.html',
        is_pro=is_pro(current_user),
        subscription=subscription,
        founding_free=bool(getattr(current_user, 'founding_referrer', False)),
        payments_enabled=stripe_enabled(),
    )


@payments_bp.route('/pro/subscribe', methods=['POST'])
@login_required
def subscribe_pro():
    """POST /payments/pro/subscribe — start Pro via subscription
    Checkout. The webhook activates the subscription."""
    if is_pro(current_user):
        flash('You already have Pro.', 'info')
        return redirect(url_for('payments.pro_page'))
    if not stripe_enabled():
        flash('Payments are currently unavailable.', 'warning')
        return redirect(url_for('payments.pro_page'))
    base = _checkout_base_url()
    checkout_url = create_pro_checkout(
        current_user,
        success_url=f'{base}{url_for("payments.pro_success")}',
        cancel_url=f'{base}{url_for("payments.pro_page")}',
    )
    if not checkout_url:
        flash('Stripe is not configured — please try again later.', 'danger')
        return redirect(url_for('payments.pro_page'))
    return redirect(checkout_url)


@payments_bp.route('/pro/success')
@login_required
def pro_success():
    """GET /payments/pro/success — return page after Pro checkout."""
    if is_pro(current_user):
        flash('Welcome to Pro! Your advanced analytics are live.', 'success')
    else:
        flash('Pro is activating — your subscription may still be processing.', 'info')
    return redirect(url_for('social.analytics'))


@payments_bp.route('/pro/cancel', methods=['POST'])
@login_required
def cancel_pro():
    """POST /payments/pro/cancel — cancel Pro at the end of the
    current period (access continues until then)."""
    subscription = get_pro_subscription(current_user.id)
    if subscription is None or not subscription.is_current:
        flash('You do not have an active Pro subscription.', 'info')
        return redirect(url_for('payments.pro_page'))
    if subscription.cancel_at_period_end:
        flash('Pro is already set to cancel at the end of the period.', 'info')
        return redirect(url_for('payments.pro_page'))
    ok, error = cancel_pro_at_period_end(subscription)
    db.session.commit()
    if ok:
        flash('Pro will cancel at the end of the current period — you keep access until then.', 'success')
    else:
        flash(f'Could not cancel Pro: {error}', 'danger')
    return redirect(url_for('payments.pro_page'))
