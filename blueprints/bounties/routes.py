"""Bounties routes for Paid2Match."""
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from flask import Blueprint, abort, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy import desc, or_
from sqlalchemy.exc import IntegrityError
import stripe

from extensions import db, get_or_404
from blueprints.bounties.forms import BountyForm, PitchForm
from models.bounty import Bounty
from models.dispute import Dispute
from models.pitch import Pitch
from models.message import Message
from models.moderation import Block
from models.match import Match
from models.payout import Payout
from models.post import Post
from models.profile import Profile
from models.admin_settings import AdminSettings
from models.bounty_upgrade import BountyUpgrade
from models.review import Review, maybe_reveal_reviews
from models.referral import Referral
from models.staked_vouch import StakedVouch
from models.stripe_event import StripeEventLog
from models.user import User
from models.video_vouch import VideoVouch
from blueprints.staked_vouches.routes import can_resolve as _stake_can_resolve
from utils.bumps import (
    create_recurring_bump,
    fulfill_boost,
    fulfill_bump_pack,
    fulfill_single_bump,
)
from utils.chains import validate_share_for_referral
from utils.feed import create_need_post
from utils.money_trail import build_trail, completed_match_for
from utils.network_match import suggest_connections_for_need
from utils.notifications import notify
from utils.pro import fulfill_pro_checkout, handle_subscription_event
from utils.slugs import parse_slug_code
from utils.payments import (
    cancel_authorization,
    compute_payout_amounts,
    create_checkout_session,
    create_refund,
    fee_rate_for,
    handle_account_updated,
    handle_payout_event,
    handle_transfer_event,
    idempotency_key_for,
    payment_intent_status,
    release_escrow,
    stripe_enabled,
    to_cents,
)
from utils.referrals import complete_referral_for_match
from utils.staged_rewards import (
    record_stage_event, stage_events_for_bounty,
)


bounties_bp = Blueprint('bounties', __name__)


@bounties_bp.route('/')
def board():
    """GET /bounties - Bounty board page with filtering and pagination."""
    page = request.args.get('page', 1, type=int)
    # Audit 2026-11-27: a negative page turns the (page-1)*per_page
    # slice into a negative slice, serving a middle chunk of the
    # board labelled with a nonsense page number. Clamp to 1.
    page = max(1, page)
    per_page = 12
    show_urgent = request.args.get('urgent') == '1'
    
    now = datetime.now(timezone.utc)
    
    query = Bounty.query.filter(
        Bounty.status == 'open',
        db.or_(
            Bounty.deadline.is_(None),
            Bounty.deadline > now
        ),
        # Paid bounties stay hidden until payment is secured; free ones show.
        Bounty.publicly_visible_filter()
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

    bounty_type = request.args.get('bounty_type')
    if bounty_type:
        query = query.filter_by(bounty_type=bounty_type)

    # M4: keyword search over titles/descriptions, skills filter
    # (the skills field is saved now — it was discarded before M4),
    # and category filter.
    search_q = (request.args.get('q') or '').strip()
    if search_q:
        query = query.filter(or_(
            Bounty.title.ilike(f'%{search_q}%'),
            Bounty.description.ilike(f'%{search_q}%'),
        ))

    skill = (request.args.get('skill') or '').strip()
    if skill:
        query = query.filter(Bounty.skills.ilike(f'%{skill}%'))

    category = request.args.get('category')
    if category:
        query = query.filter_by(category=category)

    # Phase 3: poster filter — the "We're hiring" strip's "View all"
    # links here with ?poster=<user id>.
    poster_id = request.args.get('poster')
    if poster_id:
        query = query.filter_by(poster_id=poster_id)

    # Phase 3: free-needs chip — reward null/0 or the is_free flag.
    show_free = request.args.get('free') == '1'
    if show_free:
        query = query.filter(db.or_(
            Bounty.reward_amount.is_(None),
            Bounty.reward_amount == 0,
            Bounty.is_free == True,  # noqa: E712
        ))

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
        # Audit 2026-11-27: the template's Previous/Next links use
        # prev_num/next_num — the fake object lacked them, so the
        # links rendered href="?page=" and Next reloaded page 1.
        'prev_num': page - 1,
        'next_num': page + 1,
        'iter_pages': lambda: range(1, (len(sorted_bounties) + per_page - 1) // per_page + 1)
    })()
    
    return render_template('bounties/board.html',
                        bounties=paginated,
                        pagination=pagination,
                        bounty_type=bounty_type,
                        location=location,
                        min_reward=min_reward,
                        max_reward=max_reward,
                        search_q=search_q,
                        skill=skill,
                        category=category,
                        poster_id=poster_id,
                        show_free=show_free,
                        show_urgent=show_urgent,
                        categories=Bounty.CATEGORIES)


def _resolve_bounty(slug_code):
    """Look up a bounty by its URL segment — a '<slug>-<shortcode>'
    pair, or a bare UUID (an old-style link). Returns
    (bounty, stale): stale is True when the URL's slug part does
    not match the canonical one, so the caller can 301 to its own
    canonical URL. (None, False) when nothing matches."""
    bounty = None
    stale = False
    try:
        uuid.UUID(slug_code)
        is_uuid = True
    except ValueError:
        is_uuid = False
    if is_uuid:
        bounty = db.session.get(Bounty, slug_code)
        if bounty is not None:
            stale = True
    else:
        slug_part, shortcode = parse_slug_code(slug_code)
        if shortcode:
            bounty = Bounty.query.filter_by(shortcode=shortcode).first()
            if bounty is not None and slug_part != bounty.slug:
                stale = True
    return bounty, stale


@bounties_bp.route('/<slug_code>')
def detail(slug_code):
    """GET /needs/<slug>-<shortcode> - Bounty detail page.

    The trailing -XXXXXX shortcode identifies the need; a wrong
    slug part 301s to the canonical URL (the slug never changes
    after creation, so this mostly catches hand-typed URLs). A
    bare UUID is an old-style link and 301s to the canonical URL
    too. Anything else 404s.
    """
    bounty, stale = _resolve_bounty(slug_code)
    if bounty is None:
        abort(404)
    if stale:
        return redirect(
            url_for('bounties.detail', slug_code=bounty.url_slug),
            code=301,
        )

    # Unsecured paid bounties are not public yet — only the poster may view
    # them (they need the page to secure payment). Everyone else goes back
    # to the board.
    if not bounty.is_publicly_visible and (
        not current_user.is_authenticated or current_user.id != bounty.poster_id
    ):
        flash('This referral need is not public yet.', 'warning')
        return redirect(url_for('bounties.board'))

    if bounty.is_expired and bounty.status == 'open':
        return redirect(url_for('bounties.expired', id=bounty.id))

    pitch_form = PitchForm()

    pitches = []
    if current_user.is_authenticated and current_user.id == bounty.poster_id:
        pitches = Pitch.query.filter_by(bounty_id=bounty.id).order_by(desc(Pitch.created_at)).all()

    user_pitched = None
    if current_user.is_authenticated:
        user_pitched = Pitch.query.filter_by(
            bounty_id=bounty.id,
            hunter_id=current_user.id
        ).first()

    # "Your network can close this": the viewer's own connections
    # closest to the right person for this need. Anonymous visitors
    # have no network here, so they get no panel.
    network_suggestions = (
        suggest_connections_for_need(current_user, bounty)
        if current_user.is_authenticated
        else []
    )

    # Video pitches: the poster sees every video pitch for the need
    # (like text pitches); anyone else sees only their own.
    video_pitches = []
    my_video_pitch = None
    if current_user.is_authenticated and current_user.id == bounty.poster_id:
        video_pitches = VideoVouch.query.filter_by(
            bounty_id=bounty.id, kind='pitch', status='active'
        ).order_by(desc(VideoVouch.created_at)).all()
    elif current_user.is_authenticated:
        my_video_pitch = VideoVouch.query.filter_by(
            bounty_id=bounty.id, kind='pitch', status='active',
            user_id=current_user.id,
        ).order_by(desc(VideoVouch.created_at)).first()

    # Staked vouches for this need — poster and admins only. A
    # stake names its candidate, so showing stakes to other
    # visitors would de-anonymise referrers whose pitches are
    # still pending. The poster resolves them from here.
    need_stakes = []
    staked_resolvable_ids = set()
    if current_user.is_authenticated and (
        current_user.id == bounty.poster_id
        or current_user.role == 'admin'
    ):
        need_stakes = StakedVouch.query.filter_by(
            bounty_id=bounty.id
        ).order_by(desc(StakedVouch.created_at)).all()
        staked_resolvable_ids = {
            s.id for s in need_stakes
            if s.status == 'active'
            and _stake_can_resolve(current_user, s)
        }

    return render_template('bounties/detail.html',
                        bounty=bounty,
                        pitches=pitches,
                        pitch_form=pitch_form,
                        user_pitched=user_pitched,
                        network_suggestions=network_suggestions,
                        video_pitches=video_pitches,
                        my_video_pitch=my_video_pitch,
                        need_stakes=need_stakes,
                        staked_resolvable_ids=staked_resolvable_ids,
                        # Phase 3: the referral behind each pitch, so
                        # the Message button opens the referral thread
                        # instead of the generic inbox.
                        pitch_referrals={
                            r.pitch_id: r.id
                            for r in Referral.query.filter(
                                Referral.pitch_id.in_(
                                    [p.id for p in pitches]
                                )
                            ).all()
                        } if pitches else {},
                        # Phase 3: recorded stage events are the
                        # poster's view of what is owed-and-pending;
                        # other visitors see only the declared stages.
                        stage_events=(
                            stage_events_for_bounty(bounty.id)
                            if current_user.is_authenticated
                            and current_user.id == bounty.poster_id
                            else []
                        ),
                        # M4: the share this visit came through (from a
                        # share card) — the pitch form carries it on.
                        share_id=request.args.get('share_id') or None,
                        # Money Trail: the detail page links to the
                        # trail only once a hire has completed.
                        completed_match=completed_match_for(bounty))


@bounties_bp.route('/<slug_code>/trail')
def trail(slug_code):
    """GET /needs/<slug>-<shortcode>/trail - the Money Trail.

    Public page showing the completed hire's full chain — who
    posted, who shared, who referred, who got hired — and what
    each person earned. A need with no completed hire yet gets an
    honest empty state, not a 404. Same visibility rule as the
    detail page: an unsecured paid need is visible to its poster
    only.
    """
    bounty, stale = _resolve_bounty(slug_code)
    if bounty is None:
        abort(404)
    if stale:
        return redirect(
            url_for('bounties.trail', slug_code=bounty.url_slug),
            code=301,
        )

    if not bounty.is_publicly_visible and (
        not current_user.is_authenticated or current_user.id != bounty.poster_id
    ):
        flash('This referral need is not public yet.', 'warning')
        return redirect(url_for('bounties.board'))

    return render_template(
        'bounties/trail.html',
        bounty=bounty,
        trail=build_trail(bounty),
    )


@bounties_bp.route('/<id>/edit', methods=['GET', 'POST'])
@login_required
def edit(id):
    """GET /needs/<id>/edit - Edit existing bounty
    POST /needs/<id>/edit - Update bounty
    """
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
    form = BountyForm(obj=bounty)
    
    if request.method == 'POST' and form.validate_on_submit():
        # A secured need's escrow already holds reward + 3% in CAD:
        # changing the reward or the currency afterwards would leave
        # the capture, the refund and the locked match fee disagreeing
        # with the need (e.g. a $1,000 CAD escrow behind a need now
        # showing €50). The amount and currency are locked while the
        # payment is secured; everything else stays editable. To
        # change the amount, cancel and refund first.
        if bounty.payment_status == 'secured':
            new_reward = (
                Decimal(str(form.reward_amount.data))
                if form.reward_amount.data is not None else None
            )
            old_reward = (
                Decimal(str(bounty.reward_amount))
                if bounty.reward_amount is not None else None
            )
            new_currency = (form.currency.data or 'CAD').upper()
            old_currency = (bounty.currency or 'CAD').upper()
            if new_reward != old_reward or new_currency != old_currency:
                flash(
                    'This need has a secured payment — the reward amount '
                    'and currency cannot be changed while the payment is '
                    'secured. Cancel and refund it first to change the '
                    'amount.',
                    'danger',
                )
                return render_template(
                    'bounties/edit.html', form=form, bounty=bounty
                )
        bounty_type = form.bounty_type.data
        category = (form.category.data or '').strip() or None
        if category and category not in Bounty.CATEGORIES.get(bounty_type, {}):
            flash('That category does not belong to the chosen vertical.', 'danger')
            return render_template('bounties/edit.html', form=form, bounty=bounty)
        bounty.title = form.title.data
        bounty.description = form.description.data
        bounty.bounty_type = bounty_type
        bounty.category = category
        bounty.skills = (form.skills.data or '').strip() or None
        bounty.bounty_direction = form.bounty_direction.data
        bounty.reward_amount = form.reward_amount.data or None
        bounty.currency = form.currency.data or 'CAD'
        # Phase 3: staged rewards are display-only declarations
        # (recorded as pending when a stage completes; no payout).
        bounty.stage_submit_reward = form.stage_submit_reward.data or None
        bounty.stage_interview_reward = form.stage_interview_reward.data or None
        bounty.stage_hire_reward = form.stage_hire_reward.data or None
        bounty.location = form.location.data or None
        bounty.deadline = form.deadline.data
        bounty.success_criteria = form.success_criteria.data or None

        db.session.commit()
        # M1: keep the linked feed post in sync with the need.
        post = Post.query.filter_by(bounty_id=bounty.id).first()
        if post:
            post.body = bounty.description or ''
            if bounty.is_free_bounty:
                post.post_type = 'need_free'
            else:
                post.post_type = 'need_paid'
            db.session.commit()
        flash('Referral need updated successfully!', 'success')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
    return render_template('bounties/edit.html', form=form, bounty=bounty)


@bounties_bp.route('/post', methods=['GET', 'POST'])
@login_required
def post():
    """GET /needs/post - Show post form
    POST /needs/post - Create new bounty
    """
    from models.bounty_payment_agreement import BountyPaymentAgreement
    from models.user import User
    
    form = BountyForm()

    if request.method == 'POST' and form.validate_on_submit():
        bounty_type = form.bounty_type.data
        category = (form.category.data or '').strip() or None
        if category and category not in Bounty.CATEGORIES.get(bounty_type, {}):
            flash('That category does not belong to the chosen vertical.', 'danger')
            return render_template('bounties/post.html', form=form)
        reward = form.reward_amount.data or 0

        bounty = Bounty(
            poster_id=current_user.id,
            title=form.title.data,
            description=form.description.data,
            bounty_type=bounty_type,
            category=category,
            skills=(form.skills.data or '').strip() or None,
            bounty_direction=form.bounty_direction.data,
            reward_amount=reward or None,
            currency=form.currency.data or 'CAD',
            # Phase 3 staged rewards (display only — see
            # models/staged_reward.py).
            stage_submit_reward=form.stage_submit_reward.data or None,
            stage_interview_reward=form.stage_interview_reward.data or None,
            stage_hire_reward=form.stage_hire_reward.data or None,
            location=form.location.data or None,
            deadline=form.deadline.data or None,
            success_criteria=form.success_criteria.data or None,
            status='open',
            payment_status='unsecured',
            is_free=bool(request.form.get('is_free') or not reward),
            payer_type=request.form.get('payer_type', 'poster')
        )

        db.session.add(bounty)
        db.session.commit()

        # M1: every need also appears as a post in the feed.
        create_need_post(bounty)
        db.session.commit()

        if request.form.get('is_free') or not reward:
            bounty.is_free = True
            bounty.payment_status = 'unsecured'
            db.session.commit()
            flash('Referral need created!', 'success')
            return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

        if bounty.payer_type == 'poster':
            flash('Referral need created! Secure payment to publish it.', 'success')
            return redirect(url_for('bounties.secure_payment', id=bounty.id))
        else:
            payer_username = request.form.get('payer_username', '').strip()
            if payer_username:
                payer_user = User.query.filter_by(email=payer_username).first()
                if not payer_user:
                    flash(f'User "{payer_username}" not found. Referral need created without invitation.', 'warning')
                    return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
                
                if payer_user.id == current_user.id:
                    flash('You cannot invite yourself. Referral need created.', 'warning')
                    return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
                
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
                flash('Referral need created! Invite someone to pay to proceed.', 'success')
            
            return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    return render_template('bounties/post.html', form=form)


@bounties_bp.route('/<id>/payment-link')
@login_required
def payment_link(id):
    """GET /needs/<id>/payment-link - Get shareable payment link for third-party payment."""
    bounty = get_or_404(Bounty, id)

    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    if bounty.payer_type != 'third_party':
        flash('This need does not use third-party payment', 'warning')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    if bounty.payment_status == 'secured':
        flash('Payment already secured', 'info')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    payment_url = url_for('bounties.secure_third_party', id=bounty.id, _external=True)

    return render_template('bounties/payment_link.html', bounty=bounty, payment_url=payment_url)


@bounties_bp.route('/<id>/pitch', methods=['POST'])
@login_required
def pitch(id):
    """POST /needs/<id>/pitch - Submit a pitch."""
    bounty = get_or_404(Bounty, id)
    form = PitchForm()

    # No referring on a paid bounty whose payment is not secured yet.
    if not bounty.is_publicly_visible:
        flash('This referral need is not open for referrals yet — payment has not been secured.', 'warning')
        return redirect(url_for('bounties.board'))

    # Can't refer on your own bounty
    if bounty.poster_id == current_user.id:
        flash('You cannot refer someone for your own referral need', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
    # Already referred?
    existing = Pitch.query.filter_by(
        bounty_id=id,
        hunter_id=current_user.id
    ).first()
    if existing:
        flash('You have already referred someone for this need', 'warning')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
    if form.validate_on_submit():
        # M4: the share this referral came through (from the detail
        # page's hidden field, or the query string). An invalid share
        # is ignored, never an error.
        share_id = (
            request.form.get('share_id') or request.args.get('share_id') or None
        )
        share = validate_share_for_referral(share_id, bounty, current_user.id)
        pitch = Pitch(
            bounty_id=id,
            hunter_id=current_user.id,
            candidate_teaser=form.candidate_teaser.data,
            status='pending',
            via_share_id=share.id if share else None,
        )
        db.session.add(pitch)
        # Phase 3: a submitted referral completes the 'submit' stage
        # (recorded only — no payout; see utils/staged_rewards.py).
        db.session.flush()
        record_stage_event(pitch, 'submit')
        db.session.commit()

        # M1: notify the poster (in-app + email — a key event).
        notify(
            bounty.poster_id, 'referral_new',
            f'New referral for "{bounty.title}"',
            f'{current_user.full_name} referred someone.',
            actor_id=current_user.id,
            link_url=url_for('bounties.detail', slug_code=bounty.url_slug),
        )
        db.session.commit()
        flash('Referral submitted! The poster will be notified.', 'success')

    return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))


@bounties_bp.route('/pitches/<pitch_id>/respond', methods=['POST'])
@login_required
def respond_to_pitch(pitch_id):
    """POST /needs/pitches/<pitch_id>/respond - Accept or reject a pitch."""
    pitch = get_or_404(Pitch, pitch_id)
    bounty = get_or_404(Bounty, pitch.bounty_id)

    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    response = request.form.get('response')

    if response not in ('interested', 'pass'):
        flash('Invalid response', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    pitch.status = 'interested' if response == 'interested' else 'rejected'
    pitch.employer_response = request.form.get('employer_response') or None
    pitch.updated_at = datetime.now(timezone.utc)
    # M2: first-response time for the no-ghosting SLA (5 business days).
    pitch.responded_at = datetime.now(timezone.utc)
    # Phase 3: an accepted referral completes the 'interview' stage
    # (recorded only — no payout; see utils/staged_rewards.py).
    if response == 'interested':
        record_stage_event(pitch, 'interview')
    db.session.commit()

    if response == 'interested':
        employer = bounty.poster
        hunter = pitch.hunter

        # Audit 2026-11-27: a block means "they cannot follow or
        # message you" — every other send path enforces it, but the
        # pitch-accept auto-message had no check, so accepting a
        # pitch DM'd a hunter who had blocked the poster. A deleted
        # or deactivated hunter cannot receive mail either. The
        # pitch is still accepted and the referral_accepted
        # notification still fires (the hunter needs to know the
        # outcome of their own pitch); only the DM is skipped.
        hunter_blocked = Block.query.filter_by(
            blocker_id=hunter.id, blocked_id=employer.id,
            kind='block',
        ).first()
        if (not hunter_blocked and not hunter.is_deleted
                and hunter.is_active):
            auto_message = Message(
                sender_id=employer.id,
                recipient_id=hunter.id,
                bounty_id=bounty.id,
                pitch_id=pitch.id,
                content=f'Your referral for "{bounty.title}" has been accepted! I am interested in connecting. Let\'s discuss further.'
            )
            db.session.add(auto_message)
        # M1: notify the referrer (in-app + email). No separate message_new
        # for the auto-message — referral_accepted covers it.
        notify(
            hunter.id, 'referral_accepted',
            f'Your referral for "{bounty.title}" was accepted',
            'Details are now revealed — discuss next steps.',
            actor_id=employer.id,
            link_url=url_for('bounties.detail', slug_code=bounty.url_slug),
        )
        db.session.commit()

        flash('Referral accepted! A message has been sent to the referrer. Details are now revealed.', 'success')
        return redirect(url_for('messages.inbox'))

    # M1: notify the referrer the referral was passed on (in-app + email).
    notify(
        pitch.hunter_id, 'referral_rejected',
        f'Your referral for "{bounty.title}" was passed',
        'The poster chose another referral for this need.',
        actor_id=current_user.id,
        link_url=url_for('bounties.detail', slug_code=bounty.url_slug),
    )
    db.session.commit()
    flash('Referral passed.', 'info')
    return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))


@bounties_bp.route('/pitches/<pitch_id>/confirm-match', methods=['POST'])
@login_required
def confirm_match(pitch_id):
    """POST /needs/pitches/<pitch_id>/confirm-match - Confirm a match placement."""
    pitch = get_or_404(Pitch, pitch_id)
    bounty = get_or_404(Bounty, pitch.bounty_id)

    if current_user.id not in (pitch.hunter_id, bounty.poster_id):
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    if pitch.status != 'interested':
        flash('This referral has not been accepted yet.', 'warning')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    match = Match.query.filter_by(pitch_id=pitch_id).first()
    if not match:
        reward = float(bounty.reward_amount) if bounty.reward_amount else 0
        transaction_fee_collected = float(bounty.transaction_fee) if bounty.transaction_fee else 0
        # M2: the platform fee is admin-configurable (default 20%,
        # 15% for founding referrers) — the old hardcoded 12% is gone.
        # The rate is locked in at match creation.
        if reward > 0:
            _gross, platform_fee, _net = compute_payout_amounts(
                reward, fee_rate_for(pitch.hunter)
            )
        else:
            platform_fee = 0
        transaction_fee = reward * current_app.config.get(
            'TRANSACTION_FEE_PERCENT', 3.0) / 100
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
        try:
            db.session.commit()
        except IntegrityError:
            # Two simultaneous first-confirmations race here — the
            # loser falls back to the winner's match
            # (uq_matches_pitch_id) instead of 500ing. A second
            # match for one pitch would release the same escrow
            # twice, so the constraint is the real guard; this is
            # just the graceful path. Nothing else is pending in
            # the session yet, so the rollback is safe.
            db.session.rollback()
            match = Match.query.filter_by(pitch_id=pitch_id).first()
            if match is None:
                raise

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
        bounty.status = 'completed'
        pitch.status = 'accepted'

        for user_id, score_delta in [(match.hunter_id, 10), (match.employer_id, 5)]:
            profile = Profile.query.filter_by(user_id=user_id).first()
            if profile:
                profile.reputation_score = (profile.reputation_score or 0) + score_delta

        # M1: referral completion side effects (karma, helped, free bump,
        # win post, share-chain credit) + match-confirmed notifications.
        complete_referral_for_match(match)
        for user_id in (match.hunter_id, match.employer_id):
            notify(
                user_id, 'match_confirmed',
                f'Match confirmed for "{bounty.title}"',
                'Both parties confirmed the hire.',
                link_url=url_for('bounties.confirmed_match', match_id=match.id),
            )

        # M2: release the escrow (capture the authorized payment, then
        # transfer the referrer's share to their Connect account). The
        # old code set payout_status='pending_release' here — a value
        # that was never valid, so payouts dead-ended. Free needs
        # complete immediately.
        reward = float(bounty.reward_amount or 0)
        if reward > 0:
            escrow_ok, escrow_msg = release_escrow(match)
        else:
            match.set_payout_status('completed')
            escrow_ok, escrow_msg = True, 'Free need — nothing to pay out.'
        db.session.commit()

        if escrow_ok:
            flash('Match confirmed by both parties! Payout is being processed.', 'success')
        else:
            flash(f'Match confirmed by both parties! Payout: {escrow_msg}', 'success')
        return redirect(url_for('bounties.confirmed_match', match_id=match.id))

    flash('Confirmation received. Waiting for the other party to confirm.', 'info')
    return redirect(url_for('bounties.pending_match', match_id=match.id))


@bounties_bp.route('/matches/<match_id>/pending')
@login_required
def pending_match(match_id):
    """GET /needs/matches/<match_id>/pending - Show pending match confirmation."""
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
    """GET /needs/matches/<match_id>/confirmed - Show confirmed match."""
    match = get_or_404(Match, match_id)
    if current_user.id not in (match.hunter_id, match.employer_id):
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.board'))
    pitch = db.session.get(Pitch, match.pitch_id) if match.pitch_id else None
    bounty = db.session.get(Bounty, match.bounty_id)
    # M2: reviews for this match (only revealed ones are shown) and
    # whether the viewer can still rate or file a dispute.
    reviews = Review.query.filter_by(match_id=match.id).all()
    revealed_reviews = [r for r in reviews if r.is_revealed()]
    my_review = next(
        (r for r in reviews if r.reviewer_id == current_user.id), None
    )
    open_dispute = Dispute.query.filter_by(
        match_id=match.id, status='open'
    ).first()
    return render_template('bounties/confirmed_match.html',
                      match=match, pitch=pitch, bounty=bounty,
                      revealed_reviews=revealed_reviews,
                      my_review=my_review,
                      open_dispute=open_dispute)


@bounties_bp.route('/matches/<match_id>/rate', methods=['GET', 'POST'])
@login_required
def rate_match(match_id):
    """GET/POST /needs/matches/<match_id>/rate - Rate the other party
    after a confirmed match. Double-blind: reviews are revealed only
    once both sides have rated, or after 14 days."""
    match = get_or_404(Match, match_id)
    if current_user.id not in (match.hunter_id, match.employer_id):
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.board'))
    if not (match.hunter_confirmed and match.employer_confirmed):
        flash('You can rate once both sides have confirmed the match.', 'warning')
        return redirect(url_for('bounties.confirmed_match', match_id=match.id))
    existing = Review.query.filter_by(
        match_id=match.id, reviewer_id=current_user.id
    ).first()
    if existing:
        flash('You have already rated this match.', 'info')
        return redirect(url_for('bounties.confirmed_match', match_id=match.id))
    # The reviewee's role determines the rating dimensions.
    reviewee_id = match.employer_id if current_user.id == match.hunter_id else match.hunter_id
    reviewee_role = 'poster' if current_user.id == match.hunter_id else 'referrer'
    labels = Review.DIMENSION_LABELS[reviewee_role]
    if request.method == 'POST':
        try:
            primary = int(request.form.get('rating_primary', 0))
            secondary = int(request.form.get('rating_secondary', 0))
        except ValueError:
            primary = secondary = 0
        if primary not in range(1, 6) or secondary not in range(1, 6):
            flash('Please give both ratings (1–5 stars).', 'danger')
            return render_template('bounties/rate.html', match=match,
                                   reviewee_role=reviewee_role, labels=labels)
        review = Review(
            match_id=match.id,
            reviewer_id=current_user.id,
            reviewee_id=reviewee_id,
            reviewee_role=reviewee_role,
            rating_primary=primary,
            rating_secondary=secondary,
            comment=request.form.get('comment') or None,
        )
        db.session.add(review)
        db.session.commit()
        revealed = maybe_reveal_reviews(match.id)
        db.session.commit()
        if revealed:
            for user_id in (match.hunter_id, match.employer_id):
                notify(
                    user_id, 'review_visible',
                    'New review on your match',
                    'Both sides have rated — the reviews are now visible.',
                    link_url=url_for('bounties.confirmed_match', match_id=match.id),
                )
            db.session.commit()
            flash('Thanks for rating! Both reviews are now visible.', 'success')
        else:
            flash(
                'Thanks for rating! Your review will be revealed once the '
                'other side rates too (or after 14 days).',
                'success',
            )
        return redirect(url_for('bounties.confirmed_match', match_id=match.id))
    return render_template('bounties/rate.html', match=match,
                           reviewee_role=reviewee_role, labels=labels)


@bounties_bp.route('/matches/<match_id>/dispute', methods=['GET', 'POST'])
@login_required
def file_dispute(match_id):
    """GET/POST /needs/matches/<match_id>/dispute - File a dispute on
    a match. One open dispute per match; the admin resolve flow already
    exists (see admin routes)."""
    match = get_or_404(Match, match_id)
    if current_user.id not in (match.hunter_id, match.employer_id):
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.board'))
    open_dispute = Dispute.query.filter_by(
        match_id=match.id, status='open'
    ).first()
    if open_dispute:
        flash('There is already an open dispute on this match.', 'info')
        return redirect(url_for('bounties.confirmed_match', match_id=match.id))
    if request.method == 'POST':
        reason = (request.form.get('reason') or '').strip()
        if len(reason) < 10:
            flash('Please describe what happened (at least 10 characters).', 'danger')
            return render_template('bounties/dispute_form.html', match=match)
        dispute = Dispute(
            match_id=match.id,
            raised_by_id=current_user.id,
            reason=reason,
            status='open',
        )
        db.session.add(dispute)
        other_id = match.employer_id if current_user.id == match.hunter_id else match.hunter_id
        notify(
            other_id, 'dispute_opened',
            f'A dispute was opened on your match for "{match.bounty.title}"',
            'An admin will review it. You will be notified of the outcome.',
            actor_id=current_user.id,
            link_url=url_for('bounties.confirmed_match', match_id=match.id),
        )
        db.session.commit()
        flash('Dispute filed. An admin will review it.', 'success')
        return redirect(url_for('bounties.confirmed_match', match_id=match.id))
    return render_template('bounties/dispute_form.html', match=match)


@bounties_bp.route('/recruitment')
def recruitment_board():
    """GET /needs/recruitment - Recruitment bounties only."""
    page = request.args.get('page', 1, type=int)
    per_page = 12
    now = datetime.now(timezone.utc)
    query = Bounty.query.filter(
        Bounty.bounty_type == 'recruitment',
        Bounty.status == 'open',
        db.or_(Bounty.deadline.is_(None), Bounty.deadline > now),
        Bounty.publicly_visible_filter()
    )
    query = _apply_filters(query)
    query = query.order_by(desc(Bounty.created_at))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('bounties/recruitment_board.html',
                          bounties=pagination.items, pagination=pagination,
                          bounty_type='recruitment')


@bounties_bp.route('/services')
def services_board():
    """GET /needs/services - Services-referral needs only (M4
    vertical #2: refer a service provider — a plumber, a designer —
    to someone who needs them)."""
    page = request.args.get('page', 1, type=int)
    per_page = 12
    now = datetime.now(timezone.utc)
    query = Bounty.query.filter(
        Bounty.bounty_type == 'services',
        Bounty.status == 'open',
        db.or_(Bounty.deadline.is_(None), Bounty.deadline > now),
        Bounty.publicly_visible_filter()
    )
    query = _apply_filters(query)
    query = query.order_by(desc(Bounty.created_at))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('bounties/services_board.html',
                          bounties=pagination.items, pagination=pagination,
                          bounty_type='services')


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
    # M4: keyword search + skills + category (same as the main board).
    search_q = (request.args.get('q') or '').strip()
    if search_q:
        query = query.filter(or_(
            Bounty.title.ilike(f'%{search_q}%'),
            Bounty.description.ilike(f'%{search_q}%'),
        ))
    skill = (request.args.get('skill') or '').strip()
    if skill:
        query = query.filter(Bounty.skills.ilike(f'%{skill}%'))
    category = request.args.get('category')
    if category:
        query = query.filter_by(category=category)
    return query


@bounties_bp.route('/<id>/secure-payment')
@login_required
def secure_payment(id):
    """GET /needs/<id>/secure-payment - Create Stripe Checkout for paid bounty.
    
    NEW LOGIC: User posts for free, pays when ready to secure.
    Amount: bounty_amount + 3% transaction fee
    Example: $1,000 bounty = $1,030 total charge
    """
    bounty = get_or_404(Bounty, id)

    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    if not bounty.reward_amount or bounty.reward_amount == 0:
        flash('This need is free — no payment needed', 'info')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    if bounty.payment_status == 'secured':
        flash('Payment already secured', 'info')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    # M2: fail closed — when payments are disabled, a paid need cannot
    # be secured at all (the old free passthrough marked it secured
    # without charging). The kill-switch stays; it just blocks paid
    # features instead of giving them away.
    if not stripe_enabled():
        flash('Payments are currently unavailable — this need cannot be secured right now.', 'warning')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    base_url = request.host_url.rstrip('/')
    success_url = f'{base_url}{url_for("bounties.detail", slug_code=bounty.url_slug)}?paid=1'
    cancel_url = f'{base_url}{url_for("bounties.detail", slug_code=bounty.url_slug)}'

    bounty_amount = float(bounty.reward_amount)
    transaction_fee = bounty_amount * current_app.config.get(
        'TRANSACTION_FEE_PERCENT', 3.0) / 100
    total_charge = bounty_amount + transaction_fee

    # to_cents (Decimal, half-up) — int(total * 100) truncates float
    # artifacts and undercharges by a cent (e.g. $999.99 → 102998
    # instead of 102999).
    amount_cents = to_cents(total_charge)
    # The description is the product name on the Stripe Checkout
    # page — it must say CAD explicitly: the need may be
    # denominated in EUR/USD, but Stripe always charges the
    # numeric amount in Canadian dollars.
    description = f'Referral Need Reward (CAD): {bounty.title} (CA${bounty_amount:.2f} + 3% fee: CA${transaction_fee:.2f})'

    # M2: manual capture — the payment is authorized (escrow), not
    # taken. It is captured when the match is confirmed by both sides.
    # The idempotency key is per bounty + payment state + amount +
    # payer: a double-click returns the SAME session instead of
    # authorizing twice (the second authorization would be orphaned
    # — the webhook only records one payment intent). After a refund
    # the payment_status changes, so a re-secure gets a fresh key —
    # and so does a re-secure after the reward was edited, because
    # the amount is in the key: without it, editing an unsecured
    # need's reward and securing again reused the key with a
    # different amount, and Stripe rejected the checkout outright.
    checkout_url = create_checkout_session(
        success_url=success_url,
        cancel_url=cancel_url,
        amount_cents=amount_cents,
        description=description,
        metadata={
            'bounty_id': bounty.id,
            'bounty_amount': str(bounty_amount),
            'transaction_fee': str(transaction_fee)
        },
        capture_method='manual',
        idempotency_key=(
            f'escrow-{bounty.id}-{bounty.payment_status}-'
            f'{amount_cents}-{current_user.id}'
        ),
    )

    if not checkout_url:
        flash('Stripe is not configured. Please ask an admin to add Stripe API keys.', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    return redirect(checkout_url)


@bounties_bp.route('/<id>/secure-third-party', methods=['GET', 'POST'])
@login_required
def secure_third_party(id):
    """GET/POST /needs/<id>/secure-third-party - Third party secures payment for bounty."""
    from models.bounty_payment_agreement import BountyPaymentAgreement
    
    bounty = get_or_404(Bounty, id)

    if bounty.payer_type != 'third_party':
        flash('This need does not require third-party payment', 'warning')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    if bounty.payment_status == 'secured':
        flash('Payment already secured', 'info')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    # M2: fail closed — when payments are disabled, a paid need cannot
    # be secured at all (see secure_payment).
    if not stripe_enabled():
        flash('Payments are currently unavailable — this need cannot be secured right now.', 'warning')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

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
        
        flash('You have not been invited to pay for this need.', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    if request.method == 'POST':
        base_url = request.host_url.rstrip('/')
        success_url = f'{base_url}{url_for("bounties.detail", slug_code=bounty.url_slug)}?paid=1'
        cancel_url = f'{base_url}{url_for("bounties.detail", slug_code=bounty.url_slug)}'

        bounty_amount = float(bounty.reward_amount)
        transaction_fee = bounty_amount * current_app.config.get(
            'TRANSACTION_FEE_PERCENT', 3.0) / 100
        total_charge = bounty_amount + transaction_fee

        # to_cents (Decimal, half-up) — int(total * 100) truncates
        # float artifacts and undercharges by a cent.
        amount_cents = to_cents(total_charge)
        # The description is the product name on the Stripe
        # Checkout page — it must say CAD explicitly (see
        # secure_payment).
        description = f'Referral Need Reward (CAD): {bounty.title} (CA${bounty_amount:.2f} + 3% fee: CA${transaction_fee:.2f})'

        # M2: manual capture — the payment is authorized (escrow), not
        # taken. It is captured when the match is confirmed by both sides.
        # Idempotency key per bounty + payment state + amount + payer
        # (see secure_payment): a double-click returns the same
        # session instead of authorizing twice.
        checkout_url = create_checkout_session(
            success_url=success_url,
            cancel_url=cancel_url,
            amount_cents=amount_cents,
            description=description,
            metadata={
                'bounty_id': bounty.id,
                'bounty_amount': str(bounty_amount),
                'transaction_fee': str(transaction_fee),
                'third_party_payer': current_user.id
            },
            capture_method='manual',
            idempotency_key=(
                f'escrow-{bounty.id}-{bounty.payment_status}-'
                f'{amount_cents}-{current_user.id}'
            ),
        )

        if not checkout_url:
            flash('Stripe is not configured.', 'danger')
            return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

        return redirect(checkout_url)

    return render_template('bounties/secure_third_party.html', bounty=bounty)


@bounties_bp.route('/invitation/<agreement_id>', methods=['GET', 'POST'])
@login_required
def invitation(agreement_id):
    """GET/POST /needs/invitation/<agreement_id> - View and respond to payment invitation."""
    from models.bounty_payment_agreement import BountyPaymentAgreement
    
    agreement = get_or_404(BountyPaymentAgreement, agreement_id)
    bounty = get_or_404(Bounty, agreement.bounty_id)
    
    if agreement.payer_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
    if agreement.status != 'pending':
        if agreement.status == 'accepted':
            return redirect(url_for('bounties.secure_third_party', id=bounty.id))
        flash(f'Invitation already {agreement.status}', 'info')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
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
    """GET /needs/my-invitations - View all payment invitations."""
    from models.bounty_payment_agreement import BountyPaymentAgreement
    
    pending_invitations = BountyPaymentAgreement.query.filter_by(
        payer_id=current_user.id,
        status='pending'
    ).order_by(BountyPaymentAgreement.invited_at.desc()).all()
    
    return render_template('bounties/my_invitations.html', invitations=pending_invitations)


def _handle_m3_checkout(session, session_id, m3_kind, metadata, logger):
    """Fulfill an M3 (bump/boost/Pro/recurring) checkout.session.completed
    event. All fulfillment is idempotent on the session id, so a
    duplicate event is a no-op. Does not commit — the caller commits."""
    user_id = metadata.get('user_id')
    user = db.session.get(User, user_id) if user_id else None
    if user is None:
        logger.warning(f'M3 checkout {session_id}: unknown user {user_id}')
        return

    if m3_kind == 'bump_pack':
        try:
            pack_size = int(metadata.get('pack_size', 0))
            price_cents = int(metadata.get('price_cents', 0))
        except (TypeError, ValueError):
            logger.warning(f'M3 bump pack {session_id}: bad metadata {metadata}')
            return
        _purchase, created = fulfill_bump_pack(
            user.id, pack_size, price_cents, session_id
        )
        logger.info(f'Bump pack fulfilled for {user_id}: {pack_size} bumps (new={created})')
        return

    if m3_kind == 'pro':
        sub = fulfill_pro_checkout(session)
        logger.info(f'Pro checkout fulfilled for {user_id}: {sub}')
        return

    if m3_kind == 'paid_space':
        from utils.paid_spaces import fulfill_paid_space_checkout
        membership = fulfill_paid_space_checkout(session)
        logger.info(
            f'Paid-space checkout fulfilled for {user_id}: {membership}'
        )
        return

    post_id = metadata.get('post_id')
    post = db.session.get(Post, post_id) if post_id else None
    if post is None:
        logger.warning(f'M3 checkout {session_id}: unknown post {post_id}')
        return

    if m3_kind == 'bump_single':
        _bump, error = fulfill_single_bump(user, post, session_id)
        if error:
            logger.warning(f'Single bump fulfillment failed: {error}')
        else:
            logger.info(f'Single bump fulfilled for post {post_id}')
    elif m3_kind == 'boost':
        try:
            days = int(metadata.get('days', 1))
        except (TypeError, ValueError):
            days = 1
        _boost, error = fulfill_boost(user, post, days, session_id)
        if error:
            logger.warning(f'Boost fulfillment failed: {error}')
        else:
            logger.info(f'Boost fulfilled for post {post_id} ({days} days)')
    elif m3_kind == 'recurring_bump':
        subscription_id = session.get('subscription')
        if not subscription_id:
            logger.warning(f'Recurring bump {session_id}: no subscription id')
            return
        _recurring, error = create_recurring_bump(user, post, subscription_id)
        if error:
            logger.info(f'Recurring bump created; first bump deferred: {error}')
        else:
            logger.info(f'Recurring bump created for post {post_id}')
    else:
        logger.warning(f'Unknown m3_kind {m3_kind!r} in checkout {session_id}')


@bounties_bp.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    """
    POST /needs/stripe-webhook - Handle Stripe webhook events.
    
    Following Stripe best practices:
    - Always verify webhook signature when webhook secret is configured
    - Return 400 only for signature verification failures
    - Return 200 quickly to acknowledge receipt
    - Process events asynchronously where possible
    - Handle idempotency to avoid duplicate processing
    - Log structured information for debugging
    """
    import logging

    # M2: when payments are disabled the webhook is inert — fail closed
    # so it cannot be abused to flip payment state.
    if not stripe_enabled():
        return '', 400

    logger = logging.getLogger(__name__)
    logger.info(f'Webhook called from {request.remote_addr}')
    logger.info(f'Headers: {dict(request.headers)}')
    logger.info(f'Payload length: {len(request.data)} bytes')

    secret_key = AdminSettings.get('STRIPE_SECRET_KEY')
    if not secret_key:
        logger.error('Stripe secret key not configured')
        return '', 400

    stripe.api_key = secret_key
    webhook_secret = AdminSettings.get('STRIPE_WEBHOOK_SECRET', '')
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    # SECURITY: Always verify the Stripe signature. The previous dev-mode bypass
    # (STRIPE_WEBHOOK_DEV_MODE / DEBUG skipping verification) has been removed — a
    # webhook without a valid signature is rejected (fail closed), otherwise an
    # attacker could forge checkout.session.completed events and mark bounties paid.
    if not webhook_secret:
        logger.error('Stripe webhook secret not configured — refusing unverified webhook')
        return '', 400
    if not sig_header:
        logger.warning('Missing Stripe-Signature header')
        return '', 400
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        logger.info(f'Webhook signature verified: {event.get("id")}')
    except stripe.error.SignatureVerificationError as e:
        logger.error(f'Invalid webhook signature: {e}')
        return '', 400
    except ValueError as e:
        logger.error(f'Invalid webhook payload: {e}')
        return '', 400
    
    event_id = event.get('id')
    event_type = event.get('type')

    logger.info(f'Processing webhook event: {event_id}, type: {event_type}')

    # Dedup: Stripe redelivers events (retries, and our endpoint is
    # live at two URLs), and not every handler is idempotent on its
    # own — a redelivered upgrade-EXTENSION re-extends the upgrade
    # a second time for one payment, and a redelivered bounty
    # checkout re-notifies the poster. Record the event id before
    # dispatching; a duplicate delivery is acknowledged without
    # reprocessing. The row commits with the business changes
    # below, so a crash before the first commit leaves nothing
    # recorded and the redelivery processes normally.
    if event_id:
        already = StripeEventLog.query.filter_by(event_id=event_id).first()
        if already is not None:
            logger.info(f'Event {event_id} already processed, skipping')
            return '', 200
        db.session.add(StripeEventLog(
            event_id=event_id, event_type=event_type or '',
        ))
        try:
            db.session.flush()
        except IntegrityError:
            # Two concurrent deliveries of the same event raced
            # past the check — the loser skips. Nothing else is
            # pending in the session yet, so the rollback is safe.
            db.session.rollback()
            logger.info(
                f'Event {event_id} already processed (race), skipping'
            )
            return '', 200

    # Handle the event
    if event_type == 'checkout.session.completed':
        session = event['data']['object']
        session_id = session.get('id')

        # M3: bump/boost/Pro/recurring checkouts carry an m3_kind in
        # their metadata and are fulfilled here (idempotent on the
        # session id) — before the bounty flow, which needs a bounty_id.
        metadata = session.get('metadata') or {}
        m3_kind = metadata.get('m3_kind')
        if m3_kind == 'tip':
            # Tips: a tip Checkout session — fulfilled by utils.tips
            # (additive branch; the M3 dispatcher below never sees
            # it — it would look for a post_id that tips do not have).
            from utils.tips import fulfill_tip_checkout
            fulfill_tip_checkout(session)
            db.session.commit()
            return '', 200
        if m3_kind:
            _handle_m3_checkout(session, session_id, m3_kind, metadata, logger)
            db.session.commit()
            return '', 200

        # Check for duplicate processing using session ID
        from models.bounty_upgrade import BountyUpgrade as BU
        existing_processed = BU.query.filter_by(stripe_session_id=session_id).first()
        if existing_processed and existing_processed.is_active:
            logger.info(f'Session {session_id} already processed, skipping')
            return '', 200

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
                        # price_paid is Numeric (Decimal) and
                        # total_price is a float from the metadata —
                        # adding them directly raises TypeError and
                        # 500s the webhook, so the paid extension
                        # never lands (Stripe retries and crashes
                        # again every time).
                        existing.price_paid = (
                            (existing.price_paid or Decimal('0'))
                            + Decimal(str(total_price))
                        )
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
            # M1: notify the poster (in-app + email — a key event).
            notify(
                bounty.poster_id, 'payment_secured',
                f'Payment secured for "{bounty.title}"',
                'Your referral need is now public — people can refer.',
                link_url=url_for('bounties.detail', slug_code=bounty.url_slug),
            )
            db.session.commit()
            # The need just went public: its feed post's audience
            # hears about it now — space members + @mentions in the
            # description. They were deliberately NOT notified at
            # creation: an unsecured paid need is not public until
            # escrow, so notifying then would leak it to strangers
            # (see _post_visible_to_strangers in social/routes.py).
            need_post = Post.query.filter_by(bounty_id=bounty.id).first()
            if need_post is not None:
                from blueprints.social.routes import _notify_space_members
                from utils.mentions import notify_mentions
                _notify_space_members(need_post.space, need_post)
                poster = bounty.poster
                notify_mentions(
                    need_post.body or '', bounty.poster_id,
                    poster.full_name if poster else '',
                    url_for('social.post_detail', post_id=need_post.id),
                )
                db.session.commit()
    
    elif event_type == 'payment_intent.succeeded':
        # Optional: handle direct payment intent events
        logger.info(f'Payment intent succeeded: {event["data"]["object"].get("id")}')

    elif event_type == 'payment_intent.payment_failed':
        # Optional: handle failed payments
        logger.warning(f'Payment failed: {event["data"]["object"].get("id")}')

    # M2: Connect events — onboarding status, transfer completion and
    # bank payouts. Handlers live in utils/payments.py; they update
    # payout status and notify the referrer.
    elif event_type == 'account.updated':
        handle_account_updated(event['data']['object'])
        db.session.commit()

    elif event_type.startswith('transfer.'):
        handled = handle_transfer_event(event_type, event['data']['object'])
        if handled is None:
            # Paid spaces: the transfer may be a space-owner share
            # (SpacePayout), not a match payout.
            from utils.paid_spaces import handle_space_transfer_event
            handle_space_transfer_event(event_type, event['data']['object'])
        # Tips: a tip transfer is not a Payout, so the handler above
        # ignores it — the tip handler confirms it (or marks the tip
        # failed when Stripe reports the transfer failed).
        from utils.tips import handle_tip_transfer_event
        handle_tip_transfer_event(event_type, event['data']['object'])
        db.session.commit()

    # Paid spaces: a paid invoice splits the platform fee from the
    # space owner's share (utils/paid_spaces.py). Additive — the
    # checkout/subscription branches above are untouched.
    elif event_type == 'invoice.paid':
        from utils.paid_spaces import handle_space_invoice_paid
        handle_space_invoice_paid(event['data']['object'])
        db.session.commit()

    elif event_type.startswith('payout.'):
        handle_payout_event(
            event_type, event['data']['object'], event.get('account')
        )
        db.session.commit()

    # M3: Pro + recurring-bump subscription lifecycle.
    elif event_type.startswith('customer.subscription.'):
        handle_subscription_event(event['data']['object'], event_type)
        db.session.commit()

    else:
        logger.info(f'Unhandled event type: {event_type}')
    
    # Always return 200 quickly to acknowledge receipt
    return '', 200


@bounties_bp.route('/api')
def api_list():
    """GET /api/bounties - JSON API for featured bounties."""
    limit = request.args.get('limit', 3, type=int)
    # Clamp: an unbounded ?limit=100000 would dump the whole board
    # in one response (and a negative limit means "no limit" in
    # SQLAlchemy/SQLite).
    limit = max(1, min(limit, 100))
    
    bounties = Bounty.query.filter(
        Bounty.status == 'open',
        Bounty.publicly_visible_filter()
    ).order_by(
        desc(Bounty.created_at)
    ).limit(limit).all()
    
    return jsonify([{
        'id': b.id,
        'title': b.title,
        'bounty_type': b.bounty_type,
        'category': b.category,
        'skills': b.skills,
        'reward_amount': float(b.reward_amount) if b.reward_amount else None,
        'location': b.location,
        'status': b.status,
        'created_at': b.created_at.isoformat() if b.created_at else None
    } for b in bounties])


@bounties_bp.route('/<id>/expired')
def expired(id):
    """GET /needs/<id>/expired - Show expired bounty options."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
    from datetime import datetime, timedelta
    now_date = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
    
    return render_template('bounties/expired.html', bounty=bounty, now_date=now_date)


@bounties_bp.route('/<id>/repost', methods=['POST'])
@login_required
def repost(id):
    """POST /needs/<id>/repost - Repost an expired bounty with new deadline."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
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
    
    flash('Referral need reposted with new deadline!', 'success')
    return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))


@bounties_bp.route('/<id>/cancel-refund', methods=['POST'])
@login_required
def cancel_refund(id):
    """POST /needs/<id>/cancel-refund - Cancel bounty and refund payment minus fees."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    # A completed hire can never be cancelled for a refund: the
    # escrow was already captured and the referrer already paid (or
    # is being paid). Refunding the poster now would hand them their
    # money back while the referrer keeps the payout — the platform
    # eats the full reward. payment_status stays 'secured' after
    # capture, so it cannot be the guard; the match/payouts are.
    # This check runs before the payments-disabled branch so a
    # completed need is never closed either, whatever the
    # kill-switch state.
    completed_match = Match.query.filter_by(bounty_id=bounty.id).filter(
        Match.hunter_confirmed.is_(True),
        Match.employer_confirmed.is_(True),
    ).first()
    paid_out = Payout.query.filter_by(bounty_id=bounty.id).first()
    if bounty.status == 'completed' or completed_match is not None or paid_out is not None:
        flash('This need already has a completed hire — it cannot be cancelled or refunded. If something went wrong, please contact support.', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    # M2: fail closed — when payments are disabled and a payment was
    # secured, refuse: the Stripe authorization cannot be cancelled
    # while the kill-switch is on, so the bounty must stay secured
    # until payments come back. An unsecured need can still be closed
    # locally (no money involved).
    if not stripe_enabled():
        if bounty.payment_status == 'secured':
            flash('Payments are currently unavailable — the secured payment cannot be released right now. Please try again later.', 'warning')
            return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
        bounty.status = 'closed'
        db.session.commit()
        flash('Need cancelled.', 'info')
        return redirect(url_for('profile.view', user_id=current_user.id))

    if bounty.payment_status == 'secured' and bounty.stripe_payment_intent:
        reward = float(bounty.reward_amount or 0)
        # M2: cancel the authorization when the payment was never
        # captured (the escrow hold is released in full — the poster
        # pays nothing); refund a captured payment minus Stripe's
        # processing fees.
        if payment_intent_status(bounty.stripe_payment_intent) == 'succeeded':
            # The refund keeps Stripe's processing fee (Stripe does
            # not return it on refunds) — the rates live in config
            # (STRIPE_FEE_PERCENT / STRIPE_FEE_FIXED), not hardcoded.
            fee_pct = current_app.config.get('STRIPE_FEE_PERCENT', 2.9)
            fee_fixed = current_app.config.get('STRIPE_FEE_FIXED', 0.30)
            refund_cents = to_cents(
                max(0.0, reward - (reward * fee_pct / 100 + fee_fixed))
            )
            _refund_id, refund_error = create_refund(
                bounty.stripe_payment_intent, refund_cents
            )
            if not _refund_id:
                flash(f'Refund failed: {refund_error}', 'danger')
                return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
        else:
            ok, error = cancel_authorization(bounty.stripe_payment_intent)
            if not ok:
                flash(f'Could not release the payment: {error}', 'danger')
                return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
        bounty.payment_status = 'refunded'

    bounty.status = 'closed'
    db.session.commit()

    flash('Need cancelled. Refund initiated (minus fees).', 'success')
    return redirect(url_for('profile.view', user_id=current_user.id))


@bounties_bp.route('/check-expired')
def check_expired():
    """GET /needs/check-expired - Mark expired bounties and return count."""
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
    """GET /needs/<id>/upgrade - Show upgrade options
    POST /needs/<id>/upgrade - Process upgrade selection
    """
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
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

        # M2: fail closed — when payments are disabled, upgrades cannot
        # be bought at all (the old branch activated them for free).
        if not stripe_enabled():
            flash('Payments are currently unavailable — upgrades cannot be purchased right now.', 'warning')
            return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
        total_price = price_per_day * duration_days

        base_url = request.host_url.rstrip('/')
        success_url = f'{base_url}{url_for("bounties.upgrade_success", id=id, upgrade_type=upgrade_type)}?days={duration_days}'
        cancel_url = f'{base_url}{url_for("bounties.upgrade", id=id)}'

        # to_cents (Decimal, half-up) — int(total * 100) truncates
        # float artifacts and undercharges by a cent.
        amount_cents = to_cents(total_price)
        description = f'{info["name"]} Upgrade (CAD): {bounty.title} ({duration_days} days)'

        checkout_url = create_checkout_session(
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
            },
            # A double-click must return the same session, not a
            # second charge for a second upgrade row (the webhook
            # creates one upgrade per session id).
            idempotency_key=idempotency_key_for(
                'upgrade', bounty.id, upgrade_type, duration_days,
            ),
        )

        if not checkout_url:
            flash('Stripe is not configured', 'danger')
            return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

        return redirect(checkout_url)

    active_upgrades = [u for u in bounty.upgrades.all() if u.is_valid]
    return render_template('bounties/upgrade.html', bounty=bounty, active_upgrades=active_upgrades,
                           upgrade_types=BountyUpgrade.UPGRADE_TYPES)


@bounties_bp.route('/<id>/upgrade-success')
@login_required
def upgrade_success(id):
    """GET /needs/<id>/upgrade-success - Confirmation page after upgrade payment."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
    upgrade_type = request.args.get('upgrade_type')
    days = request.args.get('days', type=int)
    
    active = [u for u in bounty.upgrades.all() if u.is_valid and u.upgrade_type == upgrade_type]
    
    if active:
        flash(f'{BountyUpgrade.UPGRADE_TYPES[upgrade_type]["name"]} upgrade is active!', 'success')
    else:
        flash('Upgrade pending. Payment may still be processing.', 'info')
    
    return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))


@bounties_bp.route('/<id>/upgrade/extend', methods=['POST'])
@login_required
def extend_upgrade(id):
    """POST /needs/<id>/upgrade/extend - Extend an existing upgrade."""
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
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

    # M2: fail closed — when payments are disabled, upgrades cannot be
    # extended at all (the old branch extended them for free).
    if not stripe_enabled():
        flash('Payments are currently unavailable — upgrades cannot be extended right now.', 'warning')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))

    base_url = request.host_url.rstrip('/')
    success_url = f'{base_url}{url_for("bounties.upgrade_success", id=id, upgrade_type=upgrade_type)}?days={additional_days}&extend=true'
    cancel_url = f'{base_url}{url_for("bounties.upgrade", id=id)}'

    # to_cents (Decimal, half-up) — int(total * 100) truncates float
    # artifacts and undercharges by a cent.
    amount_cents = to_cents(total_price)
    description = f'{info["name"]} Extension (CAD): {bounty.title} ({additional_days} days)'

    checkout_url = create_checkout_session(
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
        },
        # See upgrade(): a double-click returns the same session.
        idempotency_key=idempotency_key_for(
            'upgrade-extend', existing.id, additional_days,
        ),
    )
    
    if not checkout_url:
        flash('Stripe is not configured', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
    return redirect(checkout_url)


@bounties_bp.route('/<id>/close', methods=['GET', 'POST'])
@login_required
def close(id):
    """GET /needs/<id>/close - Show close bounty confirmation
    POST /needs/<id>/close - Close bounty with option to refund or reuse payment
    """
    bounty = get_or_404(Bounty, id)
    
    if bounty.poster_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
    if bounty.status in ['completed', 'closed']:
        flash('This need is already closed', 'info')
        return redirect(url_for('bounties.detail', slug_code=bounty.url_slug))
    
    if request.method == 'POST':
        close_action = request.form.get('close_action', 'close')

        if bounty.payment_status == 'secured' and bounty.stripe_payment_intent:
            if close_action == 'refund':
                # cancel_refund is POST-only: redirecting to it
                # issues a GET and 405s, so the refund never ran
                # and the need stayed open. Call it directly — same
                # request, same poster, same guards.
                return cancel_refund(id)
            elif close_action == 'reuse':
                # "Reuse" is only honest when the payment was never
                # captured: the authorization simply expires. When
                # the PaymentIntent was already captured (a pre-M2
                # immediate-capture payment with no match, or a
                # manual-payout capture), the money is gone from the
                # poster's card — closing with "nothing was charged"
                # would keep it. Refuse and point at the refund.
                # (payment_intent_status returns None when Stripe
                # cannot be reached; then there is no evidence of a
                # capture, so the close proceeds as before.)
                if payment_intent_status(
                        bounty.stripe_payment_intent) == 'succeeded':
                    flash(
                        'This payment was already captured, so it cannot '
                        'just expire — choose Request Refund to get your '
                        'money back (minus fees).',
                        'warning',
                    )
                    return redirect(url_for('bounties.close', id=id))
                # There is no mechanism that moves a secured
                # authorization onto a new need, so "reuse" only
                # ever meant: close without a refund. Commit the
                # close and say plainly what happens to the money:
                # nothing was captured, so the authorization simply
                # expires.
                bounty.status = 'closed'
                db.session.commit()
                flash(
                    'Need closed. No refund was requested — the payment '
                    'was never captured, so nothing was charged and the '
                    'card authorization will expire on its own.',
                    'success',
                )
                return redirect(url_for('bounties.post'))

        bounty.status = 'closed'
        db.session.commit()

        flash('Need closed successfully.', 'success')
        return redirect(url_for('profile.view', user_id=current_user.id))

    return render_template(
        'bounties/close.html',
        bounty=bounty,
        # The refund breakdown must quote the same rates the refund
        # route charges from — config, not template literals.
        transaction_fee_percent=current_app.config.get(
            'TRANSACTION_FEE_PERCENT', 3.0),
        stripe_fee_percent=current_app.config.get('STRIPE_FEE_PERCENT', 2.9),
        stripe_fee_fixed=current_app.config.get('STRIPE_FEE_FIXED', 0.30),
    )