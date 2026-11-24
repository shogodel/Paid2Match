"""Warm-intro bounties: pay for an introduction, not just a hire.

A poster offers a reward for a warm introduction to a named
person or company; anyone who can make that intro claims it; the
poster confirms the intro happened, and the reward is RECORDED
as owed (IntroRewardEvent, status 'pending_stripe' — see
models/intro_bounty.py). No Stripe call happens anywhere here
and no money moves.
"""
from datetime import datetime, timezone

from flask import (
    Blueprint, abort, flash, redirect, render_template, url_for,
)
from flask_babel import gettext as _, lazy_gettext as _l
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import (
    DecimalField, SelectField, StringField, SubmitField, TextAreaField,
)
from wtforms.validators import (
    DataRequired, InputRequired, Length, NumberRange,
)

from extensions import db, get_or_404
from models.intro_bounty import (
    IntroBounty, IntroClaim, IntroRewardEvent,
)
from utils.currency import CURRENCIES

intro_bounties_bp = Blueprint('intro_bounties', __name__)


class IntroBountyForm(FlaskForm):
    """Offer a reward for a warm introduction."""
    target_name = StringField(
        _l('Who do you want to be introduced to?'),
        validators=[DataRequired(), Length(min=2, max=200)],
    )
    target_org = StringField(
        _l('Organization (optional)'),
        validators=[Length(max=200)],
    )
    context = TextAreaField(
        _l('What is the introduction for?'),
        validators=[DataRequired(), Length(max=2000)],
    )
    amount = DecimalField(
        _l('Reward amount'),
        validators=[
            # InputRequired, not DataRequired: DataRequired treats a
            # submitted 0 as missing and reports 'This field is
            # required', hiding the real NumberRange message below.
            InputRequired(),
            NumberRange(
                min=0.01,
                message=_l('The reward must be more than zero.'),
            ),
        ],
    )
    currency = SelectField(
        _l('Currency'),
        choices=[(code, code) for code in CURRENCIES],
        default='CAD',
    )
    submit = SubmitField(_l('Post intro bounty'))


class IntroClaimForm(FlaskForm):
    """Offer to make the introduction."""
    note = TextAreaField(
        _l('How do you know the target? (optional)'),
        validators=[Length(max=1000)],
    )
    submit = SubmitField(_l('I can make this intro'))


@intro_bounties_bp.route('/')
def index():
    """GET /intros — open intro bounties, newest first. Claimed,
    completed and cancelled bounties never appear here."""
    bounties = IntroBounty.query.filter_by(
        status='open'
    ).order_by(IntroBounty.created_at.desc()).all()
    return render_template(
        'intro_bounties/list.html',
        intro_bounties=bounties,
    )


@intro_bounties_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new():
    """GET/POST /intros/new — post an intro bounty. The amount
    must be greater than zero and the currency one of CAD/EUR/USD
    (default CAD); the model validators are the backstop."""
    form = IntroBountyForm()
    if form.validate_on_submit():
        bounty = IntroBounty(
            poster_id=current_user.id,
            target_name=form.target_name.data.strip(),
            target_org=(form.target_org.data or '').strip() or None,
            context=form.context.data.strip(),
            amount=form.amount.data,
            currency=form.currency.data or 'CAD',
            status='open',
        )
        db.session.add(bounty)
        db.session.commit()
        flash(
            _('Your intro bounty is live.'),
            'success',
        )
        return redirect(
            url_for('intro_bounties.detail', intro_id=bounty.id)
        )
    return render_template('intro_bounties/new.html', form=form)


@intro_bounties_bp.route('/<intro_id>')
def detail(intro_id):
    """GET /intros/<id> — the bounty and its claims. The poster
    sees every claim; anyone else sees only their own."""
    bounty = get_or_404(IntroBounty, intro_id)
    is_poster = (
        current_user.is_authenticated
        and current_user.id == bounty.poster_id
    )
    if is_poster:
        claims = bounty.claims.order_by(
            IntroClaim.created_at.desc()
        ).all()
    elif current_user.is_authenticated:
        claims = bounty.claims.filter_by(
            claimer_id=current_user.id
        ).order_by(IntroClaim.created_at.desc()).all()
    else:
        claims = []
    my_pending_claim = None
    if current_user.is_authenticated and not is_poster:
        my_pending_claim = IntroClaim.pending_for(
            bounty.id, current_user.id
        )
    claim_form = IntroClaimForm()
    return render_template(
        'intro_bounties/detail.html',
        bounty=bounty,
        claims=claims,
        is_poster=is_poster,
        my_pending_claim=my_pending_claim,
        claim_form=claim_form,
    )


@intro_bounties_bp.route('/<intro_id>/claim', methods=['POST'])
@login_required
def claim(intro_id):
    """POST /intros/<id>/claim — "I can make this intro". The
    first claim moves the bounty to 'claimed'; it stays 'claimed'
    while any claim is pending. A claimer cannot claim their own
    bounty, and one pending claim per (bounty, claimer)."""
    bounty = get_or_404(IntroBounty, intro_id)
    if bounty.poster_id == current_user.id:
        flash(
            _('You cannot claim your own intro bounty.'),
            'danger',
        )
        return redirect(
            url_for('intro_bounties.detail', intro_id=bounty.id)
        )
    if bounty.status not in ('open', 'claimed'):
        flash(
            _('This intro bounty is no longer open.'),
            'danger',
        )
        return redirect(
            url_for('intro_bounties.detail', intro_id=bounty.id)
        )
    if IntroClaim.pending_for(bounty.id, current_user.id):
        flash(
            _('You already have a pending claim on this intro bounty.'),
            'info',
        )
        return redirect(
            url_for('intro_bounties.detail', intro_id=bounty.id)
        )
    form = IntroClaimForm()
    if not form.validate_on_submit():
        flash(
            _('Could not submit your claim. Please try again.'),
            'danger',
        )
        return redirect(
            url_for('intro_bounties.detail', intro_id=bounty.id)
        )
    claim = IntroClaim(
        intro_bounty_id=bounty.id,
        claimer_id=current_user.id,
        note=(form.note.data or '').strip() or None,
        status='pending',
    )
    db.session.add(claim)
    bounty.status = 'claimed'
    db.session.commit()
    flash(
        _('Your claim was sent to the poster.'),
        'success',
    )
    return redirect(
        url_for('intro_bounties.detail', intro_id=bounty.id)
    )


def _poster_only_claim(intro_id, claim_id):
    """Load the bounty + claim for a poster decision. 404s on a
    missing row or a claim that does not belong to the bounty;
    403s when the caller is not the poster."""
    bounty = get_or_404(IntroBounty, intro_id)
    claim = get_or_404(IntroClaim, claim_id)
    if claim.intro_bounty_id != bounty.id:
        abort(404)
    if bounty.poster_id != current_user.id:
        abort(403)
    return bounty, claim


@intro_bounties_bp.route(
    '/<intro_id>/claims/<claim_id>/confirm', methods=['POST']
)
@login_required
def confirm_claim(intro_id, claim_id):
    """POST /intros/<id>/claims/<claim_id>/confirm — poster only:
    the intro was made. The claim is confirmed, the bounty is
    completed, and the reward is RECORDED (IntroRewardEvent,
    status 'pending_stripe') — no Stripe call, no money moves."""
    bounty, claim = _poster_only_claim(intro_id, claim_id)
    if claim.status != 'pending':
        flash(
            _('This claim has already been decided.'),
            'info',
        )
        return redirect(
            url_for('intro_bounties.detail', intro_id=bounty.id)
        )
    claim.status = 'confirmed'
    claim.decided_at = datetime.now(timezone.utc)
    bounty.status = 'completed'
    IntroRewardEvent.record_for(claim)
    db.session.commit()
    flash(
        _('Intro confirmed. The reward is recorded as owed and '
          'will be paid once payouts are available.'),
        'success',
    )
    return redirect(
        url_for('intro_bounties.detail', intro_id=bounty.id)
    )


@intro_bounties_bp.route(
    '/<intro_id>/claims/<claim_id>/reject', methods=['POST']
)
@login_required
def reject_claim(intro_id, claim_id):
    """POST /intros/<id>/claims/<claim_id>/reject — poster only:
    this claimer will not make the intro. When no pending claims
    remain, the bounty reopens."""
    bounty, claim = _poster_only_claim(intro_id, claim_id)
    if claim.status != 'pending':
        flash(
            _('This claim has already been decided.'),
            'info',
        )
        return redirect(
            url_for('intro_bounties.detail', intro_id=bounty.id)
        )
    claim.status = 'rejected'
    claim.decided_at = datetime.now(timezone.utc)
    # Reopen only a claimed bounty: a completed bounty keeps a
    # leftover pending claim's rejection from resurrecting it.
    if bounty.status == 'claimed' and not bounty.has_pending_claims:
        bounty.status = 'open'
    db.session.commit()
    flash(
        _('Claim rejected.'),
        'success',
    )
    return redirect(
        url_for('intro_bounties.detail', intro_id=bounty.id)
    )


@intro_bounties_bp.route('/<intro_id>/cancel', methods=['POST'])
@login_required
def cancel(intro_id):
    """POST /intros/<id>/cancel — poster only, while the bounty
    is open or claimed with no confirmed claim. Pending claims
    are rejected along with the cancellation, so no claim is
    left actionable on a cancelled bounty."""
    bounty = get_or_404(IntroBounty, intro_id)
    if bounty.poster_id != current_user.id:
        abort(403)
    if not bounty.can_be_cancelled:
        flash(
            _('This intro bounty can no longer be cancelled.'),
            'danger',
        )
        return redirect(
            url_for('intro_bounties.detail', intro_id=bounty.id)
        )
    now = datetime.now(timezone.utc)
    for pending in bounty.pending_claims:
        pending.status = 'rejected'
        pending.decided_at = now
    bounty.status = 'cancelled'
    db.session.commit()
    flash(
        _('Intro bounty cancelled.'),
        'success',
    )
    return redirect(url_for('intro_bounties.index'))
