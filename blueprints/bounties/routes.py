"""Bounties routes for Paid2Match."""
import os
from datetime import datetime, timedelta, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy import desc, or_
import stripe

from extensions import db, get_or_404
from blueprints.bounties.forms import BountyForm, PitchForm
from models.bounty import Bounty
from models.pitch import Pitch
from models.message import Message
from models.match import Match
from models.profile import Profile
from models.admin_settings import AdminSettings
from models.bounty_upgrade import BountyUpgrade


def _get_stripe_keys():
    """Get Stripe keys from AdminSettings."""
    return {
        'publishable': AdminSettings.get('STRIPE_PUBLISHABLE_KEY'),
        'secret': AdminSettings.get('STRIPE_SECRET_KEY'),
    }


def _stripe_checkout_url(success_url, cancel_url, amount_cents, description, metadata=None):
    """Create a Stripe Checkout session and return the URL."""
    keys = _get_stripe_keys()
    if not keys['secret']:
        return None
    stripe.api_key = keys['secret']
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{
            'price_data': {
                'currency': 'usd',
                'product_data': {'name': description},
                'unit_amount': amount_cents,
            },
            'quantity': 1,
        }],
        mode='payment',
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata or {},
    )
    return session.url


bounties_bp = Blueprint('bounties', __name__)


@bounties_bp.route('/')
def board():
    """GET /bounties - Bounty board page with filtering and pagination."""
    page = request.args.get('page', 1, type=int)
    per_page = 12
    show_urgent = request.args.get('urgent') == '1'
    
    now = datetime.now(timezone.utc)
    
    query = Bounty.query.filter(
        Bounty.status == 'open',
        db.or_(
            Bounty.deadline.is_(None),
            Bounty.deadline > now
        )
    )
    
    if show_urgent:
        from models.bounty_upgrade import BountyUpgrade
        urgent_upgrades = db.session.query(BountyUpgrade.bounty_id).filter(
            BountyUpgrade.upgrade_type == 'urgent',
            BountyUpgrade.is_active == True,
            db.or_(
                BountyUpgrade.expires_at.is_(None),
                BountyUpgrade.expires_at > now
            )
        ).subquery()
        query = query.filter(Bounty.id.in_(db.select(urgent_upgrades)))
    
    location = request.args.get('location')
    if location:
        query = query.filter(Bounty.location.ilike(f'%{location}%'))
    
    min_reward = request.args.get('min_reward', type=float)
    if min_reward:
        query = query.filter(Bounty.reward_amount >= min_reward)
    
    max_reward = request.args.get('max_reward', type=float)
    if max_reward:
        query = query.filter(Bounty.reward_amount <= max_reward)
    
    max_reward = request.args.get('max_reward', type=float)
    if max_reward:
        query = query.filter(Bounty.reward_amount <= max_reward)
    
    show_urgent = request.args.get('urgent') == '1'
    
    bounty_type = request.args.get('bounty_type')
    if bounty_type:
        query = query.filter_by(bounty_type=bounty_type)
    
    bounties = query.all()
    
    def get_sort_key(b):
        featured = 3 if b.is_featured else 0
        highlighted = 2 if b.is_highlighted else 0
        urgent = 1 if b.is_urgent else 0
        return (featured + highlighted + urgent, b.created_at)
    
    sorted_bounties = sorted(bounties, key=get_sort_key, reverse=True)
    
    start = (page - 1) * per_page
    end = start + per_page
    paginated = sorted_bounties[start:end]
    
    pagination = type('Pagination', (), {
        'items': paginated,
        'page': page,
        'per_page': per_page,
        'total': len(sorted_bounties),
        'pages': (len(sorted_bounties) + per_page - 1) // per_page,
        'has_prev': page > 1,
        'has_next': page < (len(sorted_bounties) + per_page - 1) // per_page,
        'iter_pages': lambda: range(1, (len(sorted_bounties) + per_page - 1) // per_page + 1)
    })()
    
    return render_template('bounties/board.html',
                        bounties=paginated,
                        pagination=pagination,
                        bounty_type=bounty_type,
                        location=location,
                        min_reward=min_reward,
                        max_reward=max_reward)


@bounties_bp.route('/<id>')
def detail(id):
    """GET /bounties/<id> - Bounty detail page."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.is_expired and bounty.status == 'open':
        return redirect(url_for('bounties.expired', id=id))
    
    pitch_form = PitchForm()
    
    pitches = []
    if current_user.is_authenticated and current_user.id == bounty.poster_id:
        pitches = Pitch.query.filter_by(bounty_id=id).order_by(desc(Pitch.created_at)).all()
    
    user_pitched = None
    if current_user.is_authenticated:
        user_pitched = Pitch.query.filter_by(
            bounty_id=id,
            hunter_id=current_user.id
        ).first()
    
    return render_template('bounties/detail.html',
                        bounty=bounty,
                        pitches=pitches,
                        pitch_form=pitch_form,
                        user_pitched=user_pitched)


@bounties_bp.route('/<id>/edit', methods=['GET', 'POST'])
@login_required
def edit(id):
    """GET /bounties/<id>/edit - Edit existing bounty
    POST /bounties/<id>/edit - Update bounty
    """
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=id))
    
    form = BountyForm(obj=bounty)
    
    if request.method == 'POST' and form.validate_on_submit():
        bounty.title = form.title.data
        bounty.description = form.description.data
        bounty.bounty_type = form.bounty_type.data
        bounty.bounty_direction = form.bounty_direction.data
        bounty.reward_amount = form.reward_amount.data or None
        bounty.location = form.location.data or None
        bounty.deadline = form.deadline.data
        bounty.success_criteria = form.success_criteria.data or None
        
        db.session.commit()
        flash('Bounty updated successfully!', 'success')
        return redirect(url_for('bounties.detail', id=id))
    
    return render_template('bounties/edit.html', form=form, bounty=bounty)


@bounties_bp.route('/post', methods=['GET', 'POST'])
@login_required
def post():
    """GET /bounties/post - Show post form
    POST /bounties/post - Create new bounty
    """
    from models.bounty_payment_agreement import BountyPaymentAgreement
    from models.user import User
    
    form = BountyForm()

    if request.method == 'POST' and form.validate_on_submit():
        reward = form.reward_amount.data or 0

        bounty = Bounty(
            poster_id=current_user.id,
            title=form.title.data,
            description=form.description.data,
            bounty_type=form.bounty_type.data,
            bounty_direction=form.bounty_direction.data,
            reward_amount=reward or None,
            location=form.location.data or None,
            deadline=form.deadline.data or None,
            success_criteria=form.success_criteria.data or None,
            status='open',
            payment_status='unsecured',
            payer_type=request.form.get('payer_type', 'poster')
        )

        db.session.add(bounty)
        db.session.commit()

        if request.form.get('is_free') or not reward:
            bounty.is_free = True
            bounty.payment_status = 'unsecured'
            db.session.commit()
            flash('Bounty created!', 'success')
            return redirect(url_for('bounties.detail', id=bounty.id))

        if bounty.payer_type == 'poster':
            flash('Bounty created! Secure payment to publish it.', 'success')
            return redirect(url_for('bounties.upgrade', id=bounty.id))
        else:
            payer_username = request.form.get('payer_username', '').strip()
            if payer_username:
                payer_user = User.query.filter_by(username=payer_username).first()
                if not payer_user:
                    flash(f'User "{payer_username}" not found. Bounty created without invitation.', 'warning')
                    return redirect(url_for('bounties.detail', id=bounty.id))
                
                if payer_user.id == current_user.id:
                    flash('You cannot invite yourself. Bounty created.', 'warning')
                    return redirect(url_for('bounties.detail', id=bounty.id))
                
                agreement = BountyPaymentAgreement(
                    bounty_id=bounty.id,
                    inviter_id=current_user.id,
                    payer_id=payer_user.id,
                    message=request.form.get('payer_message', '')
                )
                db.session.add(agreement)
                db.session.commit()
                
                flash(f'Invitation sent to {payer_username}! They must accept before paying.', 'success')
            else:
                flash('Bounty created! Invite someone to pay to proceed.', 'success')
            
            return redirect(url_for('bounties.detail', id=bounty.id))

    return render_template('bounties/post.html', form=form)


@bounties_bp.route('/<id>/payment-link')
@login_required
def payment_link(id):
    """GET /bounties/<id>/payment-link - Get shareable payment link for third-party payment."""
    bounty = get_or_404(Bounty, id)

    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=id))

    if bounty.payer_type != 'third_party':
        flash('This bounty does not use third-party payment', 'warning')
        return redirect(url_for('bounties.detail', id=id))

    if bounty.payment_status == 'secured':
        flash('Payment already secured', 'info')
        return redirect(url_for('bounties.detail', id=id))

    payment_url = url_for('bounties.secure_third_party', id=bounty.id, _external=True)

    return render_template('bounties/payment_link.html', bounty=bounty, payment_url=payment_url)


@bounties_bp.route('/<id>/pitch', methods=['POST'])
@login_required
def pitch(id):
    """POST /bounties/<id>/pitch - Submit a pitch."""
    bounty = get_or_404(Bounty, id)
    form = PitchForm()
    
    # Can't pitch your own bounty
    if bounty.poster_id == current_user.id:
        flash('You cannot pitch on your own bounty', 'danger')
        return redirect(url_for('bounties.detail', id=id))
    
    # Already pitched?
    existing = Pitch.query.filter_by(
        bounty_id=id,
        hunter_id=current_user.id
    ).first()
    if existing:
        flash('You have already pitched on this bounty', 'warning')
        return redirect(url_for('bounties.detail', id=id))
    
    if form.validate_on_submit():
        pitch = Pitch(
            bounty_id=id,
            hunter_id=current_user.id,
            candidate_teaser=form.candidate_teaser.data,
            status='pending'
        )
        db.session.add(pitch)
        db.session.commit()
        
        flash('Pitch submitted! The bounty poster will be notified.', 'success')
        
        # TODO: Send notification to bounty poster
    
    return redirect(url_for('bounties.detail', id=id))


@bounties_bp.route('/pitches/<pitch_id>/respond', methods=['POST'])
@login_required
def respond_to_pitch(pitch_id):
    """POST /bounties/pitches/<pitch_id>/respond - Accept or reject a pitch."""
    pitch = get_or_404(Pitch, pitch_id)
    bounty = get_or_404(Bounty, pitch.bounty_id)

    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=bounty.id))

    response = request.form.get('response')

    if response not in ('interested', 'pass'):
        flash('Invalid response', 'danger')
        return redirect(url_for('bounties.detail', id=bounty.id))

    pitch.status = 'interested' if response == 'interested' else 'rejected'
    pitch.employer_response = request.form.get('employer_response') or None
    pitch.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    if response == 'interested':
        employer = bounty.poster
        hunter = pitch.hunter

        auto_message = Message(
            sender_id=employer.id,
            recipient_id=hunter.id,
            bounty_id=bounty.id,
            pitch_id=pitch.id,
            content=f'Your pitch for "{bounty.title}" has been accepted! I am interested in connecting. Let\'s discuss further.'
        )
        db.session.add(auto_message)
        db.session.commit()

        flash('Pitch accepted! A message has been sent to the hunter. Details are now revealed.', 'success')
        return redirect(url_for('messages.inbox'))

    flash('Pitch passed.', 'info')
    return redirect(url_for('bounties.detail', id=bounty.id))


@bounties_bp.route('/pitches/<pitch_id>/confirm-match', methods=['POST'])
@login_required
def confirm_match(pitch_id):
    """POST /bounties/pitches/<pitch_id>/confirm-match - Confirm a match placement."""
    pitch = get_or_404(Pitch, pitch_id)
    bounty = get_or_404(Bounty, pitch.bounty_id)

    if current_user.id not in (pitch.hunter_id, bounty.poster_id):
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=bounty.id))

    if pitch.status != 'interested':
        flash('This pitch has not been accepted yet.', 'warning')
        return redirect(url_for('bounties.detail', id=bounty.id))

    match = Match.query.filter_by(pitch_id=pitch_id).first()
    if not match:
        reward = float(bounty.reward_amount) if bounty.reward_amount else 0
        transaction_fee_collected = float(bounty.transaction_fee) if bounty.transaction_fee else 0
        platform_fee = reward * 0.12
        transaction_fee = reward * 0.03
        match = Match(
            bounty_id=bounty.id,
            pitch_id=pitch.id,
            hunter_id=pitch.hunter_id,
            employer_id=bounty.poster_id,
            payout_amount=reward if reward > 0 else None,
            platform_fee=platform_fee if reward > 0 else None,
            transaction_fee=transaction_fee_collected if reward > 0 else None,
            payout_status='pending'
        )
        db.session.add(match)
        db.session.commit()

    already_confirmed = False
    if current_user.id == match.hunter_id:
        if match.hunter_confirmed:
            already_confirmed = True
        else:
            match.hunter_confirmed = True
    elif current_user.id == match.employer_id:
        if match.employer_confirmed:
            already_confirmed = True
        else:
            match.employer_confirmed = True

    if already_confirmed:
        flash('You have already confirmed this match.', 'info')
        return redirect(url_for('bounties.confirmed_match', match_id=match.id))

    db.session.commit()

    if match.hunter_confirmed and match.employer_confirmed:
        match.confirmed_at = datetime.now(timezone.utc)
        match.payout_status = 'pending_release'
        bounty.status = 'completed'
        pitch.status = 'accepted'
        db.session.commit()

        for user_id, score_delta in [(match.hunter_id, 10), (match.employer_id, 5)]:
            profile = Profile.query.filter_by(user_id=user_id).first()
            if profile:
                profile.reputation_score = (profile.reputation_score or 0) + score_delta
        db.session.commit()

        flash('Match confirmed by both parties! Payment will be processed.', 'success')
        return redirect(url_for('bounties.confirmed_match', match_id=match.id))

    flash('Confirmation received. Waiting for the other party to confirm.', 'info')
    return redirect(url_for('bounties.pending_match', match_id=match.id))


@bounties_bp.route('/matches/<match_id>/pending')
@login_required
def pending_match(match_id):
    """GET /bounties/matches/<match_id>/pending - Show pending match confirmation."""
    match = get_or_404(Match, match_id)
    if current_user.id not in (match.hunter_id, match.employer_id):
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.board'))
    pitch = db.session.get(Pitch, match.pitch_id) if match.pitch_id else None
    bounty = db.session.get(Bounty, match.bounty_id)
    return render_template('bounties/pending_match.html',
                      match=match, pitch=pitch, bounty=bounty)


@bounties_bp.route('/matches/<match_id>/confirmed')
@login_required
def confirmed_match(match_id):
    """GET /bounties/matches/<match_id>/confirmed - Show confirmed match."""
    match = get_or_404(Match, match_id)
    if current_user.id not in (match.hunter_id, match.employer_id):
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.board'))
    pitch = db.session.get(Pitch, match.pitch_id) if match.pitch_id else None
    bounty = db.session.get(Bounty, match.bounty_id)
    return render_template('bounties/confirmed_match.html',
                      match=match, pitch=pitch, bounty=bounty)


@bounties_bp.route('/recruitment')
def recruitment_board():
    """GET /bounties/recruitment - Recruitment bounties only."""
    page = request.args.get('page', 1, type=int)
    per_page = 12
    now = datetime.now(timezone.utc)
    query = Bounty.query.filter(
        Bounty.bounty_type == 'recruitment',
        Bounty.status == 'open',
        db.or_(Bounty.deadline.is_(None), Bounty.deadline > now)
    )
    _apply_filters(query)
    query = query.order_by(desc(Bounty.created_at))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('bounties/recruitment_board.html',
                          bounties=pagination.items, pagination=pagination,
                          bounty_type='recruitment')


@bounties_bp.route('/real-estate')
def real_estate_board():
    """GET /bounties/real-estate - Real estate bounties only."""
    page = request.args.get('page', 1, type=int)
    per_page = 12
    now = datetime.now(timezone.utc)
    query = Bounty.query.filter(
        Bounty.bounty_type == 'real_estate',
        Bounty.status == 'open',
        db.or_(Bounty.deadline.is_(None), Bounty.deadline > now)
    )
    _apply_filters(query)
    query = query.order_by(desc(Bounty.created_at))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('bounties/real_estate_board.html',
                          bounties=pagination.items, pagination=pagination,
                          bounty_type='real_estate')


@bounties_bp.route('/healthcare')
def healthcare_board():
    """GET /bounties/healthcare - Healthcare bounties only."""
    page = request.args.get('page', 1, type=int)
    per_page = 12
    now = datetime.now(timezone.utc)
    query = Bounty.query.filter(
        Bounty.bounty_type == 'healthcare',
        Bounty.status == 'open',
        db.or_(Bounty.deadline.is_(None), Bounty.deadline > now)
    )
    _apply_filters(query)
    query = query.order_by(desc(Bounty.created_at))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('bounties/healthcare_board.html',
                            bounties=pagination.items, pagination=pagination,
                            bounty_type='healthcare')


@bounties_bp.route('/legal')
def legal_board():
    """GET /bounties/legal - Legal bounties only."""
    page = request.args.get('page', 1, type=int)
    per_page = 12
    now = datetime.now(timezone.utc)
    query = Bounty.query.filter(
        Bounty.bounty_type == 'legal',
        Bounty.status == 'open',
        db.or_(Bounty.deadline.is_(None), Bounty.deadline > now)
    )
    _apply_filters(query)
    query = query.order_by(desc(Bounty.created_at))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('bounties/legal_board.html',
                            bounties=pagination.items, pagination=pagination,
                            bounty_type='legal')


def _apply_filters(query):
    location = request.args.get('location')
    if location:
        query = query.filter(Bounty.location.ilike(f'%{location}%'))
    min_reward = request.args.get('min_reward', type=float)
    if min_reward:
        query = query.filter(Bounty.reward_amount >= min_reward)
    max_reward = request.args.get('max_reward', type=float)
    if max_reward:
        query = query.filter(Bounty.reward_amount <= max_reward)
    direction = request.args.get('direction')
    if direction:
        query = query.filter(Bounty.bounty_direction == direction)
    return query


@bounties_bp.route('/<id>/secure-payment')
@login_required
def secure_payment(id):
    """GET /bounties/<id>/secure-payment - Create Stripe Checkout for paid bounty.
    
    NEW LOGIC: User posts for free, pays when ready to secure.
    Amount: bounty_amount + 3% transaction fee
    Example: $1,000 bounty = $1,030 total charge
    """
    bounty = get_or_404(Bounty, id)

    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=id))

    if not bounty.reward_amount or bounty.reward_amount == 0:
        flash('This bounty is free — no payment needed', 'info')
        return redirect(url_for('bounties.detail', id=id))

    if bounty.payment_status == 'secured':
        flash('Payment already secured', 'info')
        return redirect(url_for('bounties.detail', id=id))

    # TODO(STRIPE): Free passthrough while payments are disabled (STRIPE_ENABLED=false).
    # Marks the bounty secured without charging, so posting stays functional. Remove this
    # branch when Stripe is reconfigured.
    if not current_app.config.get('STRIPE_ENABLED', True):
        if bounty.payment_status != 'secured':
            bounty.payment_status = 'secured'
            db.session.commit()
        flash('Payments are currently disabled — bounty marked secured (no charge).', 'info')
        return redirect(url_for('bounties.detail', id=id))

    base_url = request.host_url.rstrip('/')
    success_url = f'{base_url}{url_for("bounties.detail", id=id)}?paid=1'
    cancel_url = f'{base_url}{url_for("bounties.detail", id=id)}'

    bounty_amount = float(bounty.reward_amount)
    transaction_fee = bounty_amount * 0.03
    total_charge = bounty_amount + transaction_fee
    
    amount_cents = int(total_charge * 100)
    description = f'Bounty Reward: {bounty.title} (${bounty_amount:.2f} + 3% fee: ${transaction_fee:.2f})'

    checkout_url = _stripe_checkout_url(
        success_url=success_url,
        cancel_url=cancel_url,
        amount_cents=amount_cents,
        description=description,
        metadata={
            'bounty_id': bounty.id,
            'bounty_amount': str(bounty_amount),
            'transaction_fee': str(transaction_fee)
        }
    )

    if not checkout_url:
        flash('Stripe is not configured. Please ask an admin to add Stripe API keys.', 'danger')
        return redirect(url_for('bounties.detail', id=id))

    return redirect(checkout_url)


@bounties_bp.route('/<id>/secure-third-party', methods=['GET', 'POST'])
@login_required
def secure_third_party(id):
    """GET/POST /bounties/<id>/secure-third-party - Third party secures payment for bounty."""
    from models.bounty_payment_agreement import BountyPaymentAgreement
    
    bounty = get_or_404(Bounty, id)

    if bounty.payer_type != 'third_party':
        flash('This bounty does not require third-party payment', 'warning')
        return redirect(url_for('bounties.detail', id=id))

    if bounty.payment_status == 'secured':
        flash('Payment already secured', 'info')
        return redirect(url_for('bounties.detail', id=id))

    # TODO(STRIPE): Free passthrough while payments are disabled (STRIPE_ENABLED=false).
    # Third-party payer "pays" by simply securing the bounty for free. Remove when Stripe
    # is reconfigured.
    if not current_app.config.get('STRIPE_ENABLED', True):
        if bounty.payment_status != 'secured':
            bounty.payment_status = 'secured'
            db.session.commit()
        flash('Payments are currently disabled — bounty marked secured (no charge).', 'info')
        return redirect(url_for('bounties.detail', id=id))

    agreement = BountyPaymentAgreement.query.filter_by(
        bounty_id=id,
        payer_id=current_user.id,
        status='accepted'
    ).first()
    
    if not agreement:
        pending = BountyPaymentAgreement.query.filter_by(
            bounty_id=id,
            payer_id=current_user.id,
            status='pending'
        ).first()
        
        if pending:
            flash('You must accept the payment invitation first.', 'warning')
            return redirect(url_for('bounties.invitation', agreement_id=pending.id))
        
        flash('You have not been invited to pay for this bounty.', 'danger')
        return redirect(url_for('bounties.detail', id=id))

    if request.method == 'POST':
        base_url = request.host_url.rstrip('/')
        success_url = f'{base_url}{url_for("bounties.detail", id=id)}?paid=1'
        cancel_url = f'{base_url}{url_for("bounties.detail", id=id)}'

        bounty_amount = float(bounty.reward_amount)
        transaction_fee = bounty_amount * 0.03
        total_charge = bounty_amount + transaction_fee
        
        amount_cents = int(total_charge * 100)
        description = f'Bounty Reward: {bounty.title} (${bounty_amount:.2f} + 3% fee: ${transaction_fee:.2f})'

        checkout_url = _stripe_checkout_url(
            success_url=success_url,
            cancel_url=cancel_url,
            amount_cents=amount_cents,
            description=description,
            metadata={
                'bounty_id': bounty.id,
                'bounty_amount': str(bounty_amount),
                'transaction_fee': str(transaction_fee),
                'third_party_payer': current_user.id
            }
        )

        if not checkout_url:
            flash('Stripe is not configured.', 'danger')
            return redirect(url_for('bounties.detail', id=id))

        return redirect(checkout_url)

    return render_template('bounties/secure_third_party.html', bounty=bounty)


@bounties_bp.route('/invitation/<agreement_id>', methods=['GET', 'POST'])
@login_required
def invitation(agreement_id):
    """GET/POST /bounties/invitation/<agreement_id> - View and respond to payment invitation."""
    from models.bounty_payment_agreement import BountyPaymentAgreement
    
    agreement = get_or_404(BountyPaymentAgreement, agreement_id)
    bounty = get_or_404(Bounty, agreement.bounty_id)
    
    if agreement.payer_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=bounty.id))
    
    if agreement.status != 'pending':
        if agreement.status == 'accepted':
            return redirect(url_for('bounties.secure_third_party', id=bounty.id))
        flash(f'Invitation already {agreement.status}', 'info')
        return redirect(url_for('bounties.detail', id=bounty.id))
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'accept':
            agreement.accept()
            flash('Invitation accepted! Now you can secure the payment.', 'success')
            return redirect(url_for('bounties.secure_third_party', id=bounty.id))
        elif action == 'decline':
            agreement.decline()
            flash('Invitation declined.', 'info')
            return redirect(url_for('profile.view', user_id=current_user.id))
    
    return render_template('bounties/invitation.html', agreement=agreement, bounty=bounty)


@bounties_bp.route('/my-invitations')
@login_required
def my_invitations():
    """GET /bounties/my-invitations - View all payment invitations."""
    from models.bounty_payment_agreement import BountyPaymentAgreement
    
    pending_invitations = BountyPaymentAgreement.query.filter_by(
        payer_id=current_user.id,
        status='pending'
    ).order_by(BountyPaymentAgreement.invited_at.desc()).all()
    
    return render_template('bounties/my_invitations.html', invitations=pending_invitations)


@bounties_bp.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    """
    POST /bounties/stripe-webhook - Handle Stripe webhook events.
    
    Following Stripe best practices:
    - Always verify webhook signature when webhook secret is configured
    - Return 400 only for signature verification failures
    - Return 200 quickly to acknowledge receipt
    - Process events asynchronously where possible
    - Handle idempotency to avoid duplicate processing
    - Log structured information for debugging
    """
    import logging

    # TODO(STRIPE): When payments are disabled (STRIPE_ENABLED=false), the webhook is
    # inert — fail closed so it cannot be abused to flip payment state. Remove this guard
    # (or just ensure STRIPE_ENABLED=true) when Stripe is reconfigured.
    if not current_app.config.get('STRIPE_ENABLED', True):
        return '', 400

    logger = logging.getLogger(__name__)
    logger.info(f'Webhook called from {request.remote_addr}')
    logger.info(f'Headers: {dict(request.headers)}')
    logger.info(f'Payload length: {len(request.data)} bytes')
    
    keys = _get_stripe_keys()
    if not keys['secret']:
        logger.error('Stripe secret key not configured')
        return '', 400

    stripe.api_key = keys['secret']
    webhook_secret = AdminSettings.get('STRIPE_WEBHOOK_SECRET', '')
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    is_dev = current_app.config.get('DEBUG', False) or os.getenv('STRIPE_WEBHOOK_DEV_MODE', '').lower() == 'true'
    
    # Verify webhook signature (critical for security)
    if webhook_secret:
        # Dev mode: accept requests without signature for testing
        if is_dev and not sig_header:
            logger.warning('Dev mode: accepting webhook without signature verification')
            try:
                event = request.get_json()
            except Exception as e:
                logger.error(f'Failed to parse webhook JSON: {e}')
                return '', 400
        elif not sig_header:
            logger.warning('Missing Stripe-Signature header')
            return '', 400
        else:
            try:
                event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
                logger.info(f'Webhook signature verified: {event.get("id")}')
            except stripe.error.SignatureVerificationError as e:
                logger.error(f'Invalid webhook signature: {e}')
                return '', 400
            except ValueError as e:
                logger.error(f'Invalid webhook payload: {e}')
                return '', 400
    else:
        # Only fall back to unverified in development (not recommended for production)
        logger.warning('No webhook secret configured - skipping signature verification')
        try:
            event = request.get_json()
        except Exception as e:
            logger.error(f'Failed to parse webhook JSON: {e}')
            return '', 400
    
    event_id = event.get('id')
    event_type = event.get('type')
    
    logger.info(f'Processing webhook event: {event_id}, type: {event_type}')
    
    # Handle the event
    if event_type == 'checkout.session.completed':
        session = event['data']['object']
        session_id = session.get('id')
        
        # Check for duplicate processing using session ID
        from models.bounty_upgrade import BountyUpgrade as BU
        existing_processed = BU.query.filter_by(stripe_session_id=session_id).first()
        if existing_processed and existing_processed.is_active:
            logger.info(f'Session {session_id} already processed, skipping')
            return '', 200
        
        metadata = session.get('metadata') or {}
        bounty_id = metadata.get('bounty_id')
        is_upgrade = metadata.get('upgrade_session') == 'true'
        
        logger.info(f'Checkout completed: bounty_id={bounty_id}, is_upgrade={is_upgrade}')
        
        if not bounty_id:
            logger.warning(f'No bounty_id in metadata for session {session_id}')
            return '', 200
        
        if is_upgrade:
            bounty = db.session.get(Bounty, bounty_id)
            if not bounty:
                logger.error(f'Bounty not found: {bounty_id}')
                return '', 200
            
            upgrade_type = metadata.get('upgrade_type')
            duration_days = int(metadata.get('duration_days', 0))
            total_price = float(metadata.get('total_price', 0))
            extend_upgrade_id = metadata.get('extend_upgrade_id')
            
            if upgrade_type and duration_days > 0:
                now = datetime.now(timezone.utc)
                expires_at = now + timedelta(days=duration_days)
                
                if extend_upgrade_id:
                    existing = db.session.get(BU, extend_upgrade_id)
                    if existing:
                        existing.expires_at = existing.expires_at + timedelta(days=duration_days)
                        existing.price_paid = (existing.price_paid or 0) + total_price
                        db.session.commit()
                        logger.info(f'Extended upgrade {extend_upgrade_id} for bounty {bounty_id}')
                else:
                    upgrade = BU(
                        bounty_id=bounty_id,
                        upgrade_type=upgrade_type,
                        duration_days=duration_days,
                        price_paid=total_price,
                        stripe_session_id=session_id,
                        stripe_payment_intent=session.get('payment_intent'),
                        is_active=True,
                        starts_at=now,
                        expires_at=expires_at
                    )
                    db.session.add(upgrade)
                    db.session.commit()
                    logger.info(f'Created upgrade {upgrade.id} for bounty {bounty_id}')
                
                info = BU.UPGRADE_TYPES.get(upgrade_type, {})
                logger.info(f'Upgrade {upgrade_type} activated for bounty {bounty_id}')
        
        else:
            # Regular bounty payment - debug what's received
            logger.info('Regular payment - session: ' + str(session))
            logger.info('session metadata type: ' + str(type(session.get('metadata'))))
            logger.info('session metadata: ' + str(session.get('metadata')))
            
            bounty = db.session.get(Bounty, bounty_id)
            if not bounty:
                logger.error('Bounty not found: ' + bounty_id)
                return '', 200
            
            # Handle Stripe metadata - can be object or dict
            meta = session.get('metadata')
            if meta is None:
                meta_dict = {}
            elif hasattr(meta, 'toDict'):
                meta_dict = meta.toDict()
            elif hasattr(meta, '__dict__'):
                meta_dict = meta.__dict__
            else:
                meta_dict = dict(meta)
            
            logger.info('Parsed metadata: ' + str(meta_dict))
            logger.info('bounty_id from metadata: ' + str(meta_dict.get('bounty_id', 'NOT FOUND')))
            
            bounty.payment_status = 'secured'
            bounty.stripe_payment_intent = session.get('payment_intent') or session_id
            bounty.transaction_fee = float(meta_dict.get('transaction_fee', 0) or 0)
            db.session.commit()
            logger.info('Bounty ' + bounty_id + ' payment secured. Fee: ' + str(bounty.transaction_fee))
    
    elif event_type == 'payment_intent.succeeded':
        # Optional: handle direct payment intent events
        logger.info(f'Payment intent succeeded: {event["data"]["object"].get("id")}')
    
    elif event_type == 'payment_intent.payment_failed':
        # Optional: handle failed payments
        logger.warning(f'Payment failed: {event["data"]["object"].get("id")}')
    
    else:
        logger.info(f'Unhandled event type: {event_type}')
    
    # Always return 200 quickly to acknowledge receipt
    return '', 200


@bounties_bp.route('/api')
def api_list():
    """GET /api/bounties - JSON API for featured bounties."""
    limit = request.args.get('limit', 3, type=int)
    
    bounties = Bounty.query.filter_by(status='open').order_by(
        desc(Bounty.created_at)
    ).limit(limit).all()
    
    return jsonify([{
        'id': b.id,
        'title': b.title,
        'bounty_type': b.bounty_type,
        'reward_amount': float(b.reward_amount) if b.reward_amount else None,
        'location': b.location,
        'status': b.status,
        'created_at': b.created_at.isoformat() if b.created_at else None
    } for b in bounties])


@bounties_bp.route('/<id>/expired')
def expired(id):
    """GET /bounties/<id>/expired - Show expired bounty options."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=id))
    
    from datetime import datetime, timedelta
    now_date = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
    
    return render_template('bounties/expired.html', bounty=bounty, now_date=now_date)


@bounties_bp.route('/<id>/repost', methods=['POST'])
@login_required
def repost(id):
    """POST /bounties/<id>/repost - Repost an expired bounty with new deadline."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=id))
    
    new_deadline = request.form.get('deadline')
    if not new_deadline:
        flash('New deadline is required', 'danger')
        return redirect(url_for('bounties.expired', id=id))
    
    try:
        from datetime import datetime
        deadline_dt = datetime.fromisoformat(new_deadline)
    except ValueError:
        flash('Invalid deadline format', 'danger')
        return redirect(url_for('bounties.expired', id=id))
    
    bounty.deadline = deadline_dt
    bounty.status = 'open'
    
    db.session.commit()
    
    flash('Bounty reposted with new deadline!', 'success')
    return redirect(url_for('bounties.detail', id=id))


@bounties_bp.route('/<id>/cancel-refund', methods=['POST'])
@login_required
def cancel_refund(id):
    """POST /bounties/<id>/cancel-refund - Cancel bounty and refund payment minus fees."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=id))

    # TODO(STRIPE): When payments are disabled (STRIPE_ENABLED=false) there is nothing to
    # refund via Stripe, so just close/refund the record locally. Remove this branch when
    # Stripe is reconfigured (note: the live refund path also references bounty.amount which
    # does not exist — see audit; should be bounty.reward_amount).
    if not current_app.config.get('STRIPE_ENABLED', True):
        if bounty.payment_status == 'secured':
            bounty.payment_status = 'refunded'
        bounty.status = 'closed'
        db.session.commit()
        flash('Bounty cancelled. Payments are disabled — no Stripe refund issued.', 'info')
        return redirect(url_for('profile.view', user_id=current_user.id))

    refund_amount = bounty.amount

    if bounty.payment_status == 'secured' and bounty.stripe_payment_intent:
        keys = _get_stripe_keys()
        if keys['secret']:
            stripe.api_key = keys['secret']
            try:
                refund = stripe.Refund.create(
                    payment_intent=bounty.stripe_payment_intent,
                    amount=int((refund_amount - (refund_amount * 0.029 + 0.30)) * 100)
                )
                bounty.payment_status = 'refunded'
                print(f'Refunded {bounty.id}: {refund.id}')
            except Exception as e:
                print(f'Refund failed: {e}')
                flash('Refund failed. Please contact support.', 'danger')
                return redirect(url_for('bounties.detail', id=id))
    
    bounty.status = 'closed'
    db.session.commit()
    
    flash('Bounty cancelled. Refund initiated (minus fees).', 'success')
    return redirect(url_for('profile.view', user_id=current_user.id))


@bounties_bp.route('/check-expired')
def check_expired():
    """GET /bounties/check-expired - Mark expired bounties and return count."""
    if not current_user.is_authenticated:
        return jsonify({'error': 'Unauthorized'}), 401
    
    expired_count = Bounty.query.filter(
        Bounty.poster_id == current_user.id,
        Bounty.status == 'open',
        Bounty.deadline < datetime.now(timezone.utc)
    ).update({'status': 'expired'})
    db.session.commit()
    
    return jsonify({'expired': expired_count})


@bounties_bp.route('/<id>/upgrade', methods=['GET', 'POST'])
@login_required
def upgrade(id):
    """GET /bounties/<id>/upgrade - Show upgrade options
    POST /bounties/<id>/upgrade - Process upgrade selection
    """
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=id))
    
    if request.method == 'POST':
        upgrade_type = request.form.get('upgrade_type')
        duration_days = request.form.get('duration_days', type=int)
        
        if upgrade_type not in BountyUpgrade.UPGRADE_TYPES:
            flash('Invalid upgrade type', 'danger')
            return redirect(url_for('bounties.upgrade', id=id))
        
        if not duration_days or duration_days < 1:
            flash('Duration must be at least 1 day', 'danger')
            return redirect(url_for('bounties.upgrade', id=id))
        
        info = BountyUpgrade.UPGRADE_TYPES[upgrade_type]
        price_per_day = info['price_per_day']

        # TODO(STRIPE): Free passthrough while payments are disabled (STRIPE_ENABLED=false).
        # Activates the upgrade at no charge instead of creating a Stripe Checkout session.
        # Remove this branch (and the ones in extend_upgrade) when Stripe is reconfigured.
        if not current_app.config.get('STRIPE_ENABLED', True):
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(days=duration_days)
            upgrade = BountyUpgrade(
                bounty_id=bounty.id,
                upgrade_type=upgrade_type,
                duration_days=duration_days,
                price_paid=0,
                is_active=True,
                starts_at=now,
                expires_at=expires_at
            )
            db.session.add(upgrade)
            db.session.commit()
            flash(f"{info['name']} upgrade activated (free — Stripe disabled).", 'info')
            return redirect(url_for('bounties.detail', id=id))
        total_price = price_per_day * duration_days
        
        base_url = request.host_url.rstrip('/')
        success_url = f'{base_url}{url_for("bounties.upgrade_success", id=id, upgrade_type=upgrade_type)}?days={duration_days}'
        cancel_url = f'{base_url}{url_for("bounties.upgrade", id=id)}'
        
        amount_cents = int(total_price * 100)
        description = f'{info["name"]} Upgrade: {bounty.title} ({duration_days} days)'
        
        checkout_url = _stripe_checkout_url(
            success_url=success_url,
            cancel_url=cancel_url,
            amount_cents=amount_cents,
            description=description,
            metadata={
                'bounty_id': bounty.id,
                'upgrade_type': upgrade_type,
                'duration_days': str(duration_days),
                'total_price': str(total_price),
                'upgrade_session': 'true'
            }
        )
        
        if not checkout_url:
            flash('Stripe is not configured', 'danger')
            return redirect(url_for('bounties.detail', id=id))
        
        return redirect(checkout_url)
    
    active_upgrades = [u for u in bounty.upgrades.all() if u.is_valid]
    return render_template('bounties/upgrade.html', bounty=bounty, active_upgrades=active_upgrades,
                           upgrade_types=BountyUpgrade.UPGRADE_TYPES)


@bounties_bp.route('/<id>/upgrade-success')
@login_required
def upgrade_success(id):
    """GET /bounties/<id>/upgrade-success - Confirmation page after upgrade payment."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=id))
    
    upgrade_type = request.args.get('upgrade_type')
    days = request.args.get('days', type=int)
    
    active = [u for u in bounty.upgrades.all() if u.is_valid and u.upgrade_type == upgrade_type]
    
    if active:
        flash(f'{BountyUpgrade.UPGRADE_TYPES[upgrade_type]["name"]} upgrade is active!', 'success')
    else:
        flash('Upgrade pending. Payment may still be processing.', 'info')
    
    return redirect(url_for('bounties.detail', id=id))


@bounties_bp.route('/<id>/upgrade/extend', methods=['POST'])
@login_required
def extend_upgrade(id):
    """POST /bounties/<id>/upgrade/extend - Extend an existing upgrade."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=id))
    
    upgrade_type = request.form.get('upgrade_type')
    additional_days = request.form.get('duration_days', type=int)
    
    existing = BountyUpgrade.query.filter_by(
        bounty_id=id,
        upgrade_type=upgrade_type,
        is_active=True
    ).first()
    
    if not existing:
        flash('No active upgrade found', 'danger')
        return redirect(url_for('bounties.upgrade', id=id))
    
    info = BountyUpgrade.UPGRADE_TYPES[upgrade_type]
    total_price = info['price_per_day'] * additional_days

    # TODO(STRIPE): Free passthrough while payments are disabled (STRIPE_ENABLED=false).
    # Extends the upgrade at no charge. Remove when Stripe is reconfigured.
    if not current_app.config.get('STRIPE_ENABLED', True):
        existing.expires_at = existing.expires_at + timedelta(days=additional_days)
        db.session.commit()
        flash(f"{info['name']} upgrade extended (free — Stripe disabled).", 'info')
        return redirect(url_for('bounties.detail', id=id))

    base_url = request.host_url.rstrip('/')
    success_url = f'{base_url}{url_for("bounties.upgrade_success", id=id, upgrade_type=upgrade_type)}?days={additional_days}&extend=true'
    cancel_url = f'{base_url}{url_for("bounties.upgrade", id=id)}'
    
    amount_cents = int(total_price * 100)
    description = f'{info["name"]} Extension: {bounty.title} ({additional_days} days)'
    
    checkout_url = _stripe_checkout_url(
        success_url=success_url,
        cancel_url=cancel_url,
        amount_cents=amount_cents,
        description=description,
        metadata={
            'bounty_id': bounty.id,
            'upgrade_type': upgrade_type,
            'duration_days': str(additional_days),
            'total_price': str(total_price),
            'extend_upgrade_id': existing.id,
            'upgrade_session': 'true'
        }
    )
    
    if not checkout_url:
        flash('Stripe is not configured', 'danger')
        return redirect(url_for('bounties.detail', id=id))
    
    return redirect(checkout_url)


@bounties_bp.route('/<id>/close', methods=['GET', 'POST'])
@login_required
def close(id):
    """GET /bounties/<id>/close - Show close bounty confirmation
    POST /bounties/<id>/close - Close bounty with option to refund or reuse payment
    """
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', id=id))
    
    if bounty.status in ['completed', 'closed']:
        flash('This bounty is already closed', 'info')
        return redirect(url_for('bounties.detail', id=id))
    
    if request.method == 'POST':
        close_action = request.form.get('close_action', 'close')
        
        bounty.status = 'closed'
        
        if bounty.payment_status == 'secured' and bounty.stripe_payment_intent:
            if close_action == 'refund':
                return redirect(url_for('bounties.cancel_refund', id=id))
            elif close_action == 'reuse':
                flash('Bounty closed. Your payment is kept secure for your next bounty.', 'success')
                return redirect(url_for('bounties.post'))
        
        db.session.commit()
        
        flash('Bounty closed successfully.', 'success')
        return redirect(url_for('profile.view', user_id=current_user.id))
    
    return render_template('bounties/close.html', bounty=bounty)