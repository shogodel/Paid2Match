"""Referral routes (M1, §4): refer someone, consent flow, my referrals.

Flow: referrer creates the referral (status 'invited') -> the referred
person gets an invite link (email, or the link shared manually) and
consents (status 'consented'; a Pitch is created so the existing
pitch/match machinery takes over) -> poster reviews -> dual confirmation
completes the match -> referral 'completed', karma + helped awarded
(see utils/referrals.py).
"""
from flask import (
    Blueprint, flash, redirect, render_template, request, session, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError

from blueprints.referrals.forms import ReferForm
from extensions import db, get_or_404
from models.bounty import Bounty
from models.follow import Follow
from models.pitch import Pitch
from models.referral import Referral
from models.user import User
from utils.chains import validate_share_for_referral
from utils.email import send_referral_invite_email
from utils.notifications import notify

referrals_bp = Blueprint('referrals', __name__)


def _connection_choices(user_id):
    followed = Follow.query.filter_by(follower_id=user_id).all()
    users = []
    for f in followed:
        user = db.session.get(User, f.followed_id)
        if user:
            users.append(user)
    choices = [('', '— type a name + email instead —')]
    choices += [(u.id, f'{u.full_name} ({u.email})') for u in users]
    return choices


@referrals_bp.route('/new/<bounty_id>', methods=['GET', 'POST'])
@login_required
def new_referral(bounty_id):
    """GET/POST /referrals/new/<bounty_id> — refer someone for a need."""
    bounty = get_or_404(Bounty, bounty_id)
    if bounty.status != 'open' or not bounty.is_publicly_visible:
        flash('This referral need is not open for referrals yet.', 'warning')
        return redirect(url_for('bounties.board'))
    if bounty.poster_id == current_user.id:
        flash('You cannot refer someone for your own need.', 'danger')
        return redirect(url_for('bounties.detail', id=bounty_id))

    form = ReferForm()
    form.connection_id.choices = _connection_choices(current_user.id)
    # M4: the share this referral came through (A shares a need post,
    # B refers from that share). Carried as a hidden form field; an
    # invalid share is ignored, never an error.
    share_id = (
        request.form.get('share_id') or request.args.get('share_id') or None
    )
    share = validate_share_for_referral(share_id, bounty, current_user.id)
    if request.method == 'POST' and form.validate_on_submit():
        if form.connection_id.data:
            referred_user = db.session.get(User, form.connection_id.data)
            if not referred_user:
                flash('That connection no longer exists.', 'danger')
                return redirect(url_for('referrals.new_referral', bounty_id=bounty_id))
            referred_name = referred_user.full_name
            referred_email = referred_user.email
            referred_user_id = referred_user.id
        else:
            referred_name = (form.referred_name.data or '').strip()
            referred_email = (form.referred_email.data or '').lower().strip()
            referred_user_id = None
            if not referred_name or not referred_email:
                flash('Pick a connection, or enter a name and an email.', 'warning')
                return render_template(
                    'referrals/refer_form.html', form=form, bounty=bounty
                )
            existing_user = User.query.filter_by(email=referred_email).first()
            if existing_user:
                referred_user_id = existing_user.id

        if referred_email == current_user.email:
            flash('You cannot refer yourself.', 'danger')
            return redirect(url_for('bounties.detail', id=bounty_id))

        # First timestamp wins (§4.2): one referral per person per need.
        existing = Referral.query.filter_by(
            bounty_id=bounty_id, referred_email=referred_email
        ).filter(Referral.status != 'cancelled').first()
        if existing:
            flash('Someone has already referred this person for this need.', 'info')
            return redirect(url_for('bounties.detail', id=bounty_id))

        referral = Referral(
            referrer_id=current_user.id,
            bounty_id=bounty_id,
            referred_name=referred_name,
            referred_email=referred_email,
            referred_user_id=referred_user_id,
            message=form.message.data.strip(),
            via_share_id=share.id if share else None,
        )
        db.session.add(referral)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Someone has already referred this person for this need.', 'info')
            return redirect(url_for('bounties.detail', id=bounty_id))

        if referred_user_id:
            notify(
                referred_user_id, 'referral_invite',
                f'{current_user.full_name} referred you for "{bounty.title}"',
                form.message.data.strip()[:200],
                actor_id=current_user.id,
                link_url=url_for('referrals.consent', token=referral.invite_token),
            )
            db.session.commit()
        if send_referral_invite_email(referral):
            flash(f'Invite sent to {referred_name}! They need to confirm before the poster sees it.', 'success')
        else:
            flash(
                'Referral created, but we could not send the invite email — '
                'share the invite link with them yourself (see your referrals).',
                'warning',
            )
        return redirect(url_for('referrals.mine'))

    return render_template(
        'referrals/refer_form.html', form=form, bounty=bounty,
        share_id=share.id if share else None,
    )


@referrals_bp.route('/invite/<token>')
def consent(token):
    """GET /referrals/invite/<token> — the referred person's consent page.
    Nobody is referred without knowing (§4.1): the referral only reaches
    the poster after the referred person confirms here."""
    referral = Referral.query.filter_by(invite_token=token).first_or_404()
    if not current_user.is_authenticated:
        session['pending_referral_token'] = token
    return render_template('referrals/consent.html', referral=referral)


@referrals_bp.route('/<referral_id>/confirm', methods=['POST'])
@login_required
def confirm_referral(referral_id):
    """POST /referrals/<id>/confirm — the referred person consents. A
    Pitch is created so the existing pitch/match machinery takes over."""
    referral = get_or_404(Referral, referral_id)
    if current_user.email.lower() != referral.referred_email.lower():
        flash('This invite is for someone else.', 'danger')
        return redirect(url_for('bounties.board'))
    if referral.status != 'invited':
        flash('This referral was already answered.', 'info')
        return redirect(url_for('referrals.mine'))

    pitch = Pitch(
        bounty_id=referral.bounty_id,
        hunter_id=referral.referrer_id,
        candidate_teaser=referral.message,
        status='pending',
        # M4: the chain attribution carries through to the pitch,
        # which is what the payout split reads.
        via_share_id=referral.via_share_id,
    )
    db.session.add(pitch)
    db.session.flush()
    referral.pitch_id = pitch.id
    referral.status = 'consented'
    referral.referred_user_id = current_user.id
    from models import utc_now
    referral.consented_at = utc_now()

    bounty = referral.bounty
    notify(
        bounty.poster_id, 'referral_new',
        f'New referral for "{bounty.title}"',
        f'{referral.referrer.full_name} referred {referral.referred_name}.',
        actor_id=referral.referrer_id,
        link_url=url_for('bounties.detail', id=bounty.id),
    )
    notify(
        referral.referrer_id, 'referral_consented',
        f'{referral.referred_name} confirmed your referral',
        f'The poster of "{bounty.title}" can now review it.',
        actor_id=current_user.id,
        link_url=url_for('bounties.detail', id=bounty.id),
    )
    db.session.commit()
    session.pop('pending_referral_token', None)
    flash('Thanks for confirming! The poster can now review the referral.', 'success')
    from models.profile import Profile
    if not Profile.query.filter_by(user_id=current_user.id).first():
        return redirect(url_for('profile.setup'))
    return redirect(url_for('bounties.detail', id=bounty.id))


@referrals_bp.route('/<referral_id>/decline', methods=['POST'])
@login_required
def decline_referral(referral_id):
    """POST /referrals/<id>/decline — the referred person declines."""
    referral = get_or_404(Referral, referral_id)
    if current_user.email.lower() != referral.referred_email.lower():
        flash('This invite is for someone else.', 'danger')
        return redirect(url_for('bounties.board'))
    if referral.status != 'invited':
        flash('This referral was already answered.', 'info')
        return redirect(url_for('referrals.mine'))
    referral.status = 'declined'
    notify(
        referral.referrer_id, 'referral_declined',
        f'{referral.referred_name} declined your referral',
        f'They chose not to be referred for "{referral.bounty.title}".',
        actor_id=current_user.id,
    )
    db.session.commit()
    session.pop('pending_referral_token', None)
    flash('No problem — the referral is declined.', 'info')
    return redirect(url_for('bounties.board'))


@referrals_bp.route('/mine')
@login_required
def mine():
    """GET /referrals/mine — referrals I made + referrals of me."""
    made = Referral.query.filter_by(referrer_id=current_user.id).order_by(
        desc(Referral.created_at)
    ).all()
    received = Referral.query.filter(
        (Referral.referred_user_id == current_user.id)
        | (Referral.referred_email == current_user.email)
    ).order_by(desc(Referral.created_at)).all()
    return render_template('referrals/mine.html', made=made, received=received)


@referrals_bp.route('/<referral_id>')
@login_required
def detail(referral_id):
    """GET /referrals/<id> — referral detail (referrer, poster, or the
    referred person). Shows the invite link while it is still pending,
    so the referrer can share it manually if email failed."""
    referral = get_or_404(Referral, referral_id)
    allowed = (
        current_user.id in (referral.referrer_id, referral.referred_user_id)
        or current_user.id == referral.bounty.poster_id
        or current_user.email.lower() == referral.referred_email.lower()
    )
    if not allowed:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.board'))
    invite_url = url_for('referrals.consent', token=referral.invite_token, _external=True)
    return render_template(
        'referrals/detail.html', referral=referral, invite_url=invite_url
    )
