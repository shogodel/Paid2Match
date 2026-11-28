"""Tips routes — send money to a user via Stripe Checkout.

* GET /tips/new/<user_id> — the amount picker page (the standalone
  fallback; the profile and post cards open the same picker in a
  modal that posts to /tips/create/<user_id>).
* POST /tips/create/<user_id> — validate, create the Tip + Checkout
  session, redirect the tipper to Stripe. The webhook fulfills it.

Rules (enforced here, not just in the template): no self-tips, a
$2 minimum, a 280-character message cap, CAD/EUR/USD display
currency (record-only — Stripe always charges CAD, same pattern as
bounties). Tips are final — no refund path exists anywhere.
"""
from decimal import InvalidOperation

from flask import abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required

from blueprints.tips import tips_bp
from extensions import db, get_or_404
from models import Post, User
from utils.currency import CURRENCIES, validate_currency
from utils.payments import stripe_enabled, to_cents
from utils.tips import (
    TIP_MESSAGE_MAX_LENGTH,
    TIP_MIN_CENTS,
    TIP_QUICK_AMOUNTS_CENTS,
    create_tip_checkout,
    validate_tip_amount,
)


@tips_bp.route('/success/<user_id>', methods=['GET'])
@login_required
def tip_success(user_id):
    """GET /tips/success/<user_id> — return page after a tip
    Checkout (mirrors /payments/pro/success). Audit 2026-11-28:
    the success and cancel URLs were the same profile/post URL
    with no flash, so after paying the tipper saw the same page
    as after cancelling and could not tell the tip went through.
    The webhook fulfills the tip; this only confirms the payment
    completed."""
    recipient = get_or_404(User, user_id)
    flash(_('Thanks — your tip is on its way!'), 'success')
    return redirect(
        url_for('profile.public_profile', user_id=recipient.id)
    )


def _return_target(recipient, post_id):
    """(success_url, cancel_url) for a tip: back to the post when
    the tip started on one (and the post really is the recipient's),
    else back to the recipient's public profile."""
    post = None
    if post_id:
        post = db.session.get(Post, post_id)
        if post is None or post.author_id != recipient.id:
            post = None
    base = request.host_url.rstrip('/')
    if post is not None:
        url = f'{base}{url_for("social.post_detail", post_id=post.id)}'
        return url, url
    url = f'{base}{url_for("profile.public_profile", user_id=recipient.id)}'
    return url, url


def _parse_tip_form(recipient):
    """Validate the tip form. Returns (amount_cents, currency,
    message, post_id, error) — error is None on success."""
    amount_raw = (request.form.get('amount') or '').strip()
    if not amount_raw:
        return None, None, None, None, _('Enter an amount.')
    try:
        amount_cents = to_cents(amount_raw)
    except (TypeError, ValueError, InvalidOperation):
        return None, None, None, None, _('Enter a valid amount.')
    ok, error = validate_tip_amount(amount_cents)
    if not ok:
        return None, None, None, None, error
    try:
        currency = validate_currency(
            (request.form.get('currency') or 'CAD').strip()
        )
    except ValueError:
        return None, None, None, None, _(
            'Currency must be CAD, EUR or USD.'
        )
    message = (request.form.get('message') or '').strip()
    if len(message) > TIP_MESSAGE_MAX_LENGTH:
        return None, None, None, None, _(
            'Your message must be %(max)s characters or fewer.',
            max=TIP_MESSAGE_MAX_LENGTH,
        )
    post_id = (request.form.get('post_id') or '').strip() or None
    return amount_cents, currency, message, post_id, None


@tips_bp.route('/new/<user_id>', methods=['GET'])
@login_required
def new_tip(user_id):
    """GET /tips/new/<user_id> — the amount picker page."""
    recipient = get_or_404(User, user_id)
    if recipient.is_deleted:
        abort(404)
    if recipient.id == current_user.id:
        flash(_('You cannot tip yourself.'), 'warning')
        return redirect(
            url_for('profile.public_profile', user_id=recipient.id)
        )
    return render_template(
        'tips/new.html',
        recipient=recipient,
        quick_amounts=TIP_QUICK_AMOUNTS_CENTS,
        min_cents=TIP_MIN_CENTS,
        currencies=CURRENCIES,
    )


@tips_bp.route('/create/<user_id>', methods=['POST'])
@login_required
def create_tip(user_id):
    """POST /tips/create/<user_id> — create the Tip + Checkout
    session and send the tipper to Stripe."""
    recipient = get_or_404(User, user_id)
    if recipient.is_deleted:
        abort(404)
    success_url, cancel_url = _return_target(
        recipient, (request.form.get('post_id') or '').strip() or None
    )
    back = redirect(success_url)
    if recipient.id == current_user.id:
        flash(_('You cannot tip yourself.'), 'warning')
        return back
    amount_cents, currency, message, post_id, error = _parse_tip_form(
        recipient
    )
    if error:
        flash(error, 'danger')
        return back
    if not stripe_enabled():
        flash(_('Payments are currently unavailable.'), 'warning')
        return back
    success_url, cancel_url = _return_target(recipient, post_id)
    # Success goes through the confirmation route (flash); cancel
    # goes back to where the tip started (audit 2026-11-28).
    success_url = (
        f'{request.host_url.rstrip("/")}'
        f'{url_for("tips.tip_success", user_id=recipient.id)}'
    )
    try:
        _tip, checkout_url = create_tip_checkout(
            current_user, recipient, amount_cents, currency, message,
            success_url, cancel_url,
        )
    except ValueError as exc:
        flash(str(exc), 'danger')
        return back
    db.session.commit()
    if not checkout_url:
        flash(
            _('Stripe is not configured — please try again later.'),
            'danger',
        )
        return back
    return redirect(checkout_url)
