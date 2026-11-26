"""Staked vouches — trust backed by money, not just clicks.

A voucher puts part of their balance behind a candidate; if the
candidate flakes, the voucher loses the stake. The stake is created
from a candidate's profile (a general stake) or from a pitch on a
need (the stake relates to that need). The need's poster — or an
admin — resolves an active stake once the outcome is known:
completed/hired -> released, flaked/no-show -> forfeited, need
cancelled -> returned.

RECORD ONLY, NO MONEY MOVES: there is no real balance to debit
and no Stripe calls anywhere here (same record-only pattern as
models/staged_reward.py). A stake is a RECORDED commitment
against the voucher's future referral earnings; resolution only
changes the record's status. The UI copy says so plainly and never
claims money moved.
"""
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, abort, flash, redirect, render_template, request,
    url_for,
)
from flask_babel import gettext as _
from flask_login import current_user, login_required
from sqlalchemy import desc

from extensions import db, get_or_404
from models.bounty import Bounty
from models.staked_vouch import RESOLUTIONS, StakedVouch
from models.user import User
from utils.currency import CURRENCIES, DEFAULT_CURRENCY


staked_vouches_bp = Blueprint('staked_vouches', __name__)


def _safe_next(value, default):
    """Same-origin redirect target only: a relative path (never
    protocol-relative //evil.com), or an absolute URL on this host
    (e.g. the referrer) reduced to its path."""
    if not value:
        return default
    # Browsers treat backslashes as slashes, so '/\evil.com' would
    # navigate to //evil.com — reject any backslash outright (the
    # same hole the auth/login and language-switcher validators
    # already close).
    if '\\' in value:
        return default
    if value.startswith('/') and not value.startswith('//'):
        return value
    from urllib.parse import urlparse
    parsed = urlparse(value)
    if parsed.netloc and parsed.netloc == request.host:
        path = parsed.path or '/'
        return path + (f'?{parsed.query}' if parsed.query else '')
    return default


def can_resolve(user, stake):
    """True when `user` may resolve this stake: an admin, or the
    poster of the need the stake relates to. A general stake (no
    need) can only be resolved by an admin — there is no poster
    to judge the outcome."""
    if user is None or not user.is_authenticated:
        return False
    if user.role == 'admin':
        return True
    if stake.bounty_id and stake.bounty:
        return stake.bounty.poster_id == user.id
    return False


@staked_vouches_bp.route('/')
@login_required
def mine():
    """GET /stakes/ — my stakes: the ones I placed (as voucher)
    and the ones placed on me (as candidate). Resolve buttons show
    on any active stake I may resolve (a need I posted, or any
    stake when I am an admin)."""
    placed = StakedVouch.query.filter_by(
        voucher_id=current_user.id
    ).order_by(desc(StakedVouch.created_at)).all()
    received = StakedVouch.query.filter_by(
        candidate_id=current_user.id
    ).order_by(desc(StakedVouch.created_at)).all()
    resolvable_ids = {
        s.id for s in placed + received
        if s.status == 'active' and can_resolve(current_user, s)
    }
    return render_template(
        'stakes/my_stakes.html',
        placed=placed,
        received=received,
        resolvable_ids=resolvable_ids,
    )


@staked_vouches_bp.route('/new')
@login_required
def new():
    """GET /stakes/new?candidate_id=<id>&bounty_id=<id> — the
    create-stake form. candidate_id is required; bounty_id is
    optional (a stake from a pitch carries the need, a stake from
    a profile does not)."""
    candidate_id = request.args.get('candidate_id')
    candidate = (
        db.session.get(User, candidate_id) if candidate_id else None
    )
    if not candidate:
        flash(_('Pick the person you want to stake on.'), 'danger')
        return redirect(url_for('social.people'))
    if candidate.id == current_user.id:
        flash(_('You cannot stake on yourself.'), 'danger')
        return redirect(url_for(
            'profile.public_profile',
            user_id=candidate.handle or candidate.id,
        ))
    bounty = None
    bounty_id = request.args.get('bounty_id')
    if bounty_id:
        bounty = db.session.get(Bounty, bounty_id)
        if not bounty:
            flash(_('That need no longer exists.'), 'danger')
            return redirect(url_for(
                'profile.public_profile',
                user_id=candidate.handle or candidate.id,
            ))
    existing = StakedVouch.active_for(
        current_user.id, candidate.id,
        bounty.id if bounty else None,
    )
    return render_template(
        'stakes/new.html',
        candidate=candidate,
        bounty=bounty,
        existing_stake=existing,
        currencies=CURRENCIES,
        default_currency=DEFAULT_CURRENCY,
    )


@staked_vouches_bp.route('/create', methods=['POST'])
@login_required
def create():
    """POST /stakes/create — record a stake. One handler for all
    three entry points: the dedicated form (/stakes/new), the
    inline form on a candidate's profile, and the inline form on
    a pitch row of a need page. Form fields: candidate_id,
    bounty_id (optional), amount, currency, next (redirect
    target)."""
    fallback = url_for('staked_vouches.mine')
    next_url = _safe_next(request.form.get('next'), fallback)

    candidate_id = request.form.get('candidate_id')
    candidate = (
        db.session.get(User, candidate_id) if candidate_id else None
    )
    if not candidate:
        flash(_('Pick the person you want to stake on.'), 'danger')
        return redirect(next_url)
    if candidate.id == current_user.id:
        flash(_('You cannot stake on yourself.'), 'danger')
        return redirect(next_url)

    bounty = None
    bounty_id = (request.form.get('bounty_id') or '').strip() or None
    if bounty_id:
        bounty = db.session.get(Bounty, bounty_id)
        if not bounty:
            flash(_('That need no longer exists.'), 'danger')
            return redirect(next_url)

    raw_amount = (request.form.get('amount') or '').strip()
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation:
        flash(_('Enter an amount greater than zero.'), 'danger')
        return redirect(next_url)
    if amount <= 0:
        flash(_('Enter an amount greater than zero.'), 'danger')
        return redirect(next_url)

    currency = (
        request.form.get('currency') or DEFAULT_CURRENCY
    ).strip().upper()
    if currency not in CURRENCIES:
        flash(_('Choose a supported currency.'), 'danger')
        return redirect(next_url)

    # One active stake per (voucher, candidate, bounty). The model
    # enforces this too (StakedVouch.create raises), but the route
    # checks first so the user gets a translated message.
    if StakedVouch.active_for(
        current_user.id, candidate.id, bounty.id if bounty else None
    ):
        flash(
            _('You already have an active stake on this person '
              'for this need.'),
            'warning',
        )
        return redirect(next_url)
    try:
        StakedVouch.create(
            voucher_id=current_user.id,
            candidate_id=candidate.id,
            amount=amount,
            currency=currency,
            bounty_id=bounty.id if bounty else None,
        )
    except ValueError:
        flash(
            _('That stake could not be recorded — check the amount '
              'and try again.'),
            'danger',
        )
        return redirect(next_url)
    db.session.commit()
    flash(
        _('You staked %(amount)s on %(name)s. The stake is recorded — no money moved.',
          amount=f'{amount} {currency}', name=candidate.full_name),
        'success',
    )
    return redirect(next_url)


@staked_vouches_bp.route('/<stake_id>/resolve', methods=['POST'])
@login_required
def resolve(stake_id):
    """POST /stakes/<id>/resolve — resolve an active stake. Only
    the need's poster (or an admin) may resolve; a general stake
    with no need is admin-only. Form field: outcome, one of
    'completed' (candidate completed / hired -> released),
    'flaked' (candidate flaked / no-show -> forfeited),
    'cancelled' (need cancelled -> returned). Resolution is
    record-only: it changes the stake's status, it never moves
    money."""
    stake = get_or_404(StakedVouch, stake_id)
    fallback = url_for('staked_vouches.mine')
    next_url = _safe_next(request.form.get('next'), fallback)
    if not can_resolve(current_user, stake):
        abort(403)
    outcome = request.form.get('outcome')
    if outcome not in RESOLUTIONS:
        flash(_('Choose an outcome to resolve the stake.'), 'danger')
        return redirect(next_url)
    if stake.status != 'active':
        flash(_('Only an active stake can be resolved.'), 'warning')
        return redirect(next_url)
    try:
        stake.resolve(outcome, current_user.id)
    except ValueError:
        flash(_('That stake could not be resolved.'), 'danger')
        return redirect(next_url)
    db.session.commit()
    if stake.status == 'released':
        flash(
            _('Stake released — the candidate came through.'),
            'success',
        )
    elif stake.status == 'forfeited':
        flash(
            _('Stake forfeited — the candidate flaked.'),
            'warning',
        )
    else:
        flash(
            _('Stake returned — the need was cancelled.'),
            'info',
        )
    return redirect(next_url)
