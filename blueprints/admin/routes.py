"""Admin routes for Paid2Match."""
import os
import hmac
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, abort
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SelectField, TextAreaField, BooleanField, SubmitField, IntegerField
from wtforms.validators import DataRequired, Length, ValidationError
from sqlalchemy import desc, func, or_

from extensions import db, get_or_404
from models.user import User
from models.bounty import Bounty
from models.match import Match
from models.dispute import Dispute
from models.admin_settings import AdminSettings
from models.endorsement import Pledge
from models.employer_pledge import EmployerPledge
from models.linked_account import LinkedAccount
from models.payout import Payout
from models.profile import Profile
from models.audit_log import AuditLog
from models.verification import VerificationRequest
from models import utc_now
from utils.bumps import run_recurring_bumps
from utils.linked_accounts import mark_verified
from utils.moderation import (
    DEFAULT_POST_LIMIT_MEMBER,
    DEFAULT_POST_LIMIT_NEWCOMER,
    POST_LIMIT_MEMBER_KEY,
    POST_LIMIT_NEWCOMER_KEY,
    get_auto_hide_threshold,
    maybe_restore_after_dismiss,
)
from utils.notifications import notify
from email_validator import validate_email, EmailNotValidError
from utils.payments import get_platform_fee_percent, record_manual_payout
from werkzeug.security import generate_password_hash

admin_bp = Blueprint('admin', __name__)


def admin_required(f):
    """Decorator to require admin role. Redirects non-admin to homepage."""
    @login_required
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.role != 'admin':
            flash('Admin access required', 'danger')
            return redirect(url_for('bounties.board'))
        return f(*args, **kwargs)
    return decorated_function


def moderator_required(f):
    """M1: decorator for the moderation review queue.

    View access: admin, moderator, or any user with 5000+ karma (§3.4).
    Acting on reports (dismiss/hide) is restricted to admin + moderator
    inside the route itself — 5000-karma users can view but not act.
    """
    @login_required
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from utils.trust import can_view_moderation_queue
        if not can_view_moderation_queue(current_user):
            flash('Moderation access required', 'danger')
            return redirect(url_for('bounties.board'))
        return f(*args, **kwargs)
    return decorated_function


class SettingsForm(FlaskForm):
    """Form for admin settings with masked API keys."""
    auth0_domain = StringField('Auth0 Domain')
    auth0_client_id = StringField('Auth0 Client ID')
    auth0_client_secret = PasswordField('Auth0 Client Secret')
    stripe_publishable_key = StringField('Stripe Publishable Key')
    stripe_secret_key = PasswordField('Stripe Secret Key')
    stripe_webhook_secret = PasswordField('Stripe Webhook Secret')
    mapbox_token = StringField('Mapbox Token')
    claude_api_key = PasswordField('Claude API Key')
    facebook_page_token = PasswordField('Facebook Page Token')
    sendgrid_api_key = PasswordField('SendGrid API Key')
    linkedin_api_token = PasswordField('LinkedIn API Token')
    # M2: platform fee percentage (0–50, default 20). Validated in the
    # settings route so a bad value flashes a clear error.
    platform_fee_percent = IntegerField('Platform Fee (%)')
    # M3 moderation v1: auto-hide threshold (default 3 reports) and
    # daily post limits by trust level (Newcomer 3, Member+ 10).
    auto_hide_threshold = IntegerField('Auto-hide report threshold')
    post_limit_newcomer = IntegerField('Daily post limit — Newcomer')
    post_limit_member = IntegerField('Daily post limit — Member+')
    submit = SubmitField('Save Settings')


# Settings key mapping (form field -> database key)
SETTINGS_MAP = {
    'auth0_domain': 'AUTH0_DOMAIN',
    'auth0_client_id': 'AUTH0_CLIENT_ID',
    'auth0_client_secret': 'AUTH0_CLIENT_SECRET',
    'stripe_publishable_key': 'STRIPE_PUBLISHABLE_KEY',
    'stripe_secret_key': 'STRIPE_SECRET_KEY',
    'stripe_webhook_secret': 'STRIPE_WEBHOOK_SECRET',
    'mapbox_token': 'MAPBOX_ACCESS_TOKEN',
    'claude_api_key': 'CLAUDE_API_KEY',
    'facebook_page_token': 'FACEBOOK_PAGE_TOKEN',
    'sendgrid_api_key': 'SENDGRID_API_KEY',
    'linkedin_api_token': 'LINKEDIN_API_TOKEN',
    'platform_fee_percent': 'PLATFORM_FEE_PERCENT',
    'auto_hide_threshold': 'AUTO_HIDE_REPORT_THRESHOLD',
    'post_limit_newcomer': 'POST_LIMIT_NEWCOMER',
    'post_limit_member': 'POST_LIMIT_MEMBER',
}

# M2: the old STRIPE_SETTING_KEYS skipping logic is gone — M2 IS the
# Stripe reconfiguration it referred to. Admins must be able to set
# Stripe keys while payments are disabled (that is how payments get
# re-enabled), so the settings routes no longer hide or skip them.


@admin_bp.route('/')
@admin_required
def dashboard():
    """GET /admin - Dashboard with stats."""
    from datetime import datetime, timedelta, timezone
    
    now = datetime.now(timezone.utc)
    last_7_days = now - timedelta(days=7)
    
    # Calculate trends (last 7 days vs previous 7 days)
    prev_7_days = last_7_days - timedelta(days=7)
    
    stats = {
        'total_users': User.query.count(),
        'total_bounties': Bounty.query.count(),
        'open_bounties': Bounty.query.filter_by(status='open').count(),
        'secured_bounties': Bounty.query.filter_by(payment_status='secured').count(),
        'total_matches': Match.query.count(),
        'open_disputes': Dispute.query.filter_by(status='open').count(),
        'total_profiles': Profile.query.count(),
        'new_users_7d': User.query.filter(User.created_at >= last_7_days).count(),
        'new_bounties_7d': Bounty.query.filter(Bounty.created_at >= last_7_days).count(),
    }
    
    recent_users = User.query.order_by(desc(User.created_at)).limit(5).all()
    recent_bounties = Bounty.query.order_by(desc(Bounty.created_at)).limit(5).all()
    
    # Activity for chart (last 7 days)
    daily_stats = []
    for i in range(6, -1, -1):
        day = (now - timedelta(days=i)).date()
        day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)
        day_end = datetime.combine(day, datetime.max.time()).replace(tzinfo=timezone.utc)
        
        users_created = User.query.filter(
            User.created_at >= day_start,
            User.created_at <= day_end
        ).count()
        
        bounties_created = Bounty.query.filter(
            Bounty.created_at >= day_start,
            Bounty.created_at <= day_end
        ).count()
        
        daily_stats.append({
            'date': day.strftime('%b %d'),
            'users': users_created,
            'bounties': bounties_created
        })
    
    return render_template('admin/dashboard.html',
                        stats=stats,
                        recent_users=recent_users,
                        recent_bounties=recent_bounties,
                        daily_stats=daily_stats)


@admin_bp.route('/users')
@admin_required
def users():
    """GET /admin/users - List all users with search and filters."""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    query = User.query
    
    search = request.args.get('search')
    if search:
        query = query.filter(
            or_(User.email.ilike(f'%{search}%'), 
                User.full_name.ilike(f'%{search}%'))
        )
    
    role_filter = request.args.get('role')
    if role_filter:
        query = query.filter_by(role=role_filter)
    
    pagination = query.order_by(desc(User.created_at)).paginate(
        page=page, per_page=per_page, error_out=False)
    users = pagination.items
    
    return render_template('admin/users.html',
                        users=users,
                        pagination=pagination,
                        search=search,
                        role_filter=role_filter)


@admin_bp.route('/users/<id>/activate', methods=['POST'])
@admin_required
def activate_user(id):
    """POST /admin/users/<id>/activate - Activate user."""
    user = get_or_404(User, id)
    user.is_active = True
    db.session.commit()
    
    AuditLog.log(
        admin_id=current_user.id,
        action='activate_user',
        target_type='user',
        target_id=id,
        details=f'Activated user {user.email}'
    )
    
    flash(f'User {user.email} activated', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<id>/deactivate', methods=['POST'])
@admin_required
def deactivate_user(id):
    """POST /admin/users/<id>/deactivate - Deactivate user."""
    user = get_or_404(User, id)
    user.is_active = False
    db.session.commit()
    flash(f'User {user.email} deactivated', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<id>/verify-email', methods=['POST'])
@admin_required
def verify_user_email(id):
    """POST /admin/users/<id>/verify-email - Manually mark a user's email verified.

    Escape hatch for when the confirmation email cannot reach the user
    (mail misconfigured, typo'd address the user cannot fix, etc.).
    """
    from datetime import datetime, timezone
    user = get_or_404(User, id)
    user.email_verified = True
    user.email_verified_at = datetime.now(timezone.utc)
    db.session.commit()
    flash(f'Email verified for {user.email}', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<id>/change-email', methods=['POST'])
@admin_required
def change_user_email(id):
    """POST /admin/users/<id>/change-email - Change a user's email.

    Account-recovery tool: support uses this after verifying the user
    by email (the user writes in from the address on their account).
    The new address is marked unverified — the next OAuth sign-in
    re-verifies it against the provider. The change is audit-logged.
    """
    user = get_or_404(User, id)
    new_email = request.form.get('new_email', '').strip().lower()
    try:
        validate_email(new_email, check_deliverability=False)
    except EmailNotValidError:
        flash('Please enter a valid email address.', 'danger')
        return redirect(url_for('admin.users'))
    # Case-insensitive comparisons: legacy rows may hold a mixed-case
    # email, and '=' is case-sensitive — filter_by would miss the
    # clash and create two accounts for the same address.
    if new_email == (user.email or '').lower():
        flash('That is already the user\'s email address.', 'info')
        return redirect(url_for('admin.users'))
    if User.query.filter(func.lower(User.email) == new_email).first():
        flash('That email address is already in use.', 'danger')
        return redirect(url_for('admin.users'))
    old_email = user.email
    user.email = new_email
    user.email_verified = False
    user.email_verified_at = None
    db.session.commit()

    AuditLog.log(
        admin_id=current_user.id,
        action='change_user_email',
        target_type='user',
        target_id=id,
        details=f'Changed email from {old_email} to {new_email} (account recovery)'
    )

    flash(f'Email changed from {old_email} to {new_email}.', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<id>/role', methods=['POST'])
@admin_required
def change_role(id):
    """POST /admin/users/<id>/role - Change user role."""
    user = get_or_404(User, id)
    new_role = request.form.get('role')
    if new_role in ['user', 'moderator', 'admin']:
        user.role = new_role
        db.session.commit()
        flash(f'Role changed to {new_role}', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<id>/founding-referrer', methods=['POST'])
@admin_required
def toggle_founding_referrer(id):
    """POST /admin/users/<id>/founding-referrer - Toggle the founding
    referrer flag (15% platform fee instead of the configured rate,
    plus free Pro). Granting the flag starts the 12-month window
    (User.founding_referrer_since); removing it clears the window,
    so a later re-grant starts a fresh 12 months."""
    from datetime import timedelta

    from utils.payments import FOUNDING_WINDOW_DAYS

    user = get_or_404(User, id)
    user.founding_referrer = not user.founding_referrer
    if user.founding_referrer:
        user.founding_referrer_since = utc_now()
        expires = user.founding_referrer_since + timedelta(
            days=FOUNDING_WINDOW_DAYS
        )
        state = (
            'a founding referrer (15% fee until '
            f'{expires.strftime("%b %d, %Y")})'
        )
    else:
        user.founding_referrer_since = None
        state = 'a standard referrer'
    db.session.commit()
    flash(f'{user.email} is now {state}.', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/bounties')
@admin_required
def bounties():
    """GET /admin/bounties - List all bounties."""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    query = Bounty.query
    
    search = request.args.get('search')
    if search:
        query = query.filter(
            or_(Bounty.title.ilike(f'%{search}%'), 
                Bounty.description.ilike(f'%{search}%'))
        )
    
    status = request.args.get('status')
    if status:
        query = query.filter_by(status=status)
    
    
    payment_filter = request.args.get('payment_status')
    if payment_filter:
        query = query.filter_by(payment_status=payment_filter)
    
    pagination = query.order_by(desc(Bounty.created_at)).paginate(
        page=page, per_page=per_page, error_out=False)
    bounties_list = pagination.items
    
    return render_template('admin/bounties.html',
                        bounties=bounties_list,
                        pagination=pagination,
                        search=search,
                        status=status,
                        payment_filter=payment_filter)


@admin_bp.route('/bounties/<id>/approve', methods=['POST'])
@admin_required
def approve_bounty(id):
    """POST /admin/bounties/<id>/approve - Approve bounty."""
    bounty = get_or_404(Bounty, id)
    bounty.status = 'open'
    db.session.commit()
    flash('Referral need approved and published', 'success')
    return redirect(url_for('admin.bounties'))


@admin_bp.route('/bounties/<id>/reject', methods=['POST'])
@admin_required
def reject_bounty(id):
    """POST /admin/bounties/<id>/reject - Reject bounty."""
    bounty = get_or_404(Bounty, id)
    bounty.status = 'closed'
    db.session.commit()
    flash('Referral need rejected', 'success')
    return redirect(url_for('admin.bounties'))


@admin_bp.route('/bounties/<id>/close', methods=['POST'])
@admin_required
def close_bounty(id):
    """POST /admin/bounties/<id>/close - Close bounty."""
    bounty = get_or_404(Bounty, id)
    bounty.status = 'closed'
    db.session.commit()
    flash('Referral need closed', 'success')
    return redirect(url_for('admin.bounties'))


@admin_bp.route('/disputes')
@admin_required
def disputes():
    """GET /admin/disputes - List disputes."""
    status = request.args.get('status', 'open')
    
    query = Dispute.query
    if status:
        query = query.filter_by(status=status)
    
    disputes_list = query.order_by(desc(Dispute.created_at)).all()
    
    return render_template('admin/disputes.html',
                        disputes=disputes_list,
                        status=status)


@admin_bp.route('/disputes/<id>/resolve', methods=['POST'])
@admin_required
def resolve_dispute(id):
    """POST /admin/disputes/<id>/resolve - Resolve dispute.

    M2: the admin records an outcome (upheld = the respondent lost,
    dismissed = the raiser lost). The outcome drives the trust-score
    dispute penalty (-15 to the loser).
    M3: both parties are notified of the resolution."""
    dispute = get_or_404(Dispute, id)
    resolution = request.form.get('resolution')
    outcome = request.form.get('outcome')
    if outcome not in Dispute.VALID_OUTCOMES:
        flash('Choose an outcome: upheld or dismissed.', 'danger')
        return redirect(url_for('admin.disputes'))

    dispute.status = 'resolved'
    dispute.resolution = resolution
    dispute.outcome = outcome
    dispute.resolved_by_id = current_user.id
    dispute.resolved_at = utc_now()

    match = db.session.get(Match, dispute.match_id)
    if match is not None:
        bounty_title = match.bounty.title if match.bounty else 'your referral'
        link = url_for('bounties.detail', slug_code=match.bounty.url_slug) if match.bounty else None
        # The outcome reads differently per side: 'upheld' means the
        # raiser was right (the other party lost).
        raiser_text = (
            'upheld in your favour' if outcome == 'upheld'
            else 'dismissed'
        )
        other_text = (
            'upheld against you' if outcome == 'upheld'
            else 'dismissed in your favour'
        )
        resolution_suffix = f' Resolution: {resolution}' if resolution else ''
        notify(
            dispute.raised_by_id, 'dispute_resolved',
            'Your dispute was resolved',
            f'Your dispute about "{bounty_title}" was {raiser_text}.'
            + resolution_suffix,
            link_url=link,
        )
        other_id = (
            match.employer_id
            if dispute.raised_by_id == match.hunter_id
            else match.hunter_id
        )
        notify(
            other_id, 'dispute_resolved',
            'A dispute was resolved',
            f'The dispute about "{bounty_title}" was {other_text}.'
            + resolution_suffix,
            link_url=link,
        )

    db.session.commit()
    flash('Dispute resolved', 'success')
    return redirect(url_for('admin.disputes'))


@admin_bp.route('/verifications')
@admin_required
def verifications():
    """GET /admin/verifications - Verification request queue (M2)."""
    status = request.args.get('status', 'pending')

    query = VerificationRequest.query
    if status:
        query = query.filter_by(status=status)

    requests = query.order_by(desc(VerificationRequest.created_at)).all()

    return render_template('admin/verifications.html',
                        requests=requests,
                        status=status)


@admin_bp.route('/verifications/<id>/approve', methods=['POST'])
@admin_required
def approve_verification(id):
    """POST /admin/verifications/<id>/approve - Approve a verification
    request: marks all the user's profiles verified."""
    req = get_or_404(VerificationRequest, id)
    if req.status != 'pending':
        flash('This request was already reviewed.', 'info')
        return redirect(url_for('admin.verifications'))
    req.status = 'approved'
    req.reviewed_by_id = current_user.id
    req.reviewed_at = utc_now()
    for profile in Profile.query.filter_by(user_id=req.user_id).all():
        profile.verified = True
    notify(
        req.user_id, 'verification_approved',
        'Your profile is verified!',
        'An admin approved your verification request. Your profile now '
        'shows the verified badge.',
        link_url=url_for('profile.view', user_id=req.user_id),
    )
    db.session.commit()
    flash('Verification approved.', 'success')
    return redirect(url_for('admin.verifications'))


@admin_bp.route('/verifications/<id>/reject', methods=['POST'])
@admin_required
def reject_verification(id):
    """POST /admin/verifications/<id>/reject - Reject a verification
    request with an admin note."""
    req = get_or_404(VerificationRequest, id)
    if req.status != 'pending':
        flash('This request was already reviewed.', 'info')
        return redirect(url_for('admin.verifications'))
    req.status = 'rejected'
    req.admin_note = request.form.get('admin_note') or None
    req.reviewed_by_id = current_user.id
    req.reviewed_at = utc_now()
    notify(
        req.user_id, 'verification_rejected',
        'Your verification request was not approved',
        req.admin_note or 'An admin reviewed your request and did not '
        'approve it. You can request verification again.',
        link_url=url_for('profile.view', user_id=req.user_id),
    )
    db.session.commit()
    flash('Verification rejected.', 'success')
    return redirect(url_for('admin.verifications'))


@admin_bp.route('/payouts')
@admin_required
def payouts():
    """GET /admin/payouts - All payouts (M2), with a manual-payout form
    for pending/failed rows and for dual-confirmed matches that have
    no payout row yet (e.g. confirmed before M2)."""
    status = request.args.get('status')

    query = Payout.query
    if status:
        query = query.filter_by(status=status)

    payouts_list = query.order_by(desc(Payout.created_at)).all()

    completed_matches = (
        Match.query
        .filter(Match.hunter_confirmed.is_(True),
                Match.employer_confirmed.is_(True))
        .outerjoin(Payout, Payout.match_id == Match.id)
        .filter(Payout.id.is_(None))
        .order_by(desc(Match.confirmed_at))
        .all()
    )

    return render_template('admin/payouts.html',
                        payouts=payouts_list,
                        status=status,
                        completed_matches=completed_matches)


@admin_bp.route('/payouts/<match_id>/record-manual', methods=['POST'])
@admin_required
def record_manual_payout_route(match_id):
    """POST /admin/payouts/<match_id>/record-manual - Record an Interac
    payout the admin already made to the referrer (M2 interim path
    before Connect is approved)."""
    match = get_or_404(Match, match_id)
    if not (match.hunter_confirmed and match.employer_confirmed):
        flash('Only a dual-confirmed match can be paid out.', 'danger')
        return redirect(url_for('admin.payouts'))
    # M4: a chain match has two payouts (referrer + sharer); the form
    # says which one this record is for.
    payout_kind = request.form.get('payout_kind') or 'referral'
    if payout_kind not in ('referral', 'chain_share'):
        flash('Unknown payout kind.', 'danger')
        return redirect(url_for('admin.payouts'))
    existing = Payout.query.filter_by(
        match_id=match.id, kind=payout_kind
    ).first()
    if existing and existing.status in ('completed', 'processing'):
        flash('This payout is already completed or in progress.', 'info')
        return redirect(url_for('admin.payouts'))
    amount_raw = (request.form.get('amount') or '').strip()
    try:
        amount = float(amount_raw)
    except ValueError:
        amount = None
    if amount is None or amount <= 0:
        flash('Enter a valid payout amount greater than 0.', 'danger')
        return redirect(url_for('admin.payouts'))
    reference = (request.form.get('reference') or '').strip()
    try:
        _payout, capture_note = record_manual_payout(
            match, amount, reference, current_user, payout_kind=payout_kind
        )
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.payouts'))
    db.session.commit()
    msg = f'Manual payout of ${amount:,.2f} CAD recorded.'
    if capture_note:
        msg += ' ' + capture_note
    flash(msg, 'success')
    return redirect(url_for('admin.payouts'))


@admin_bp.route('/reports')
@moderator_required
def reports():
    """GET /admin/reports — M1 moderation review queue.

    View: admin, moderator, or 5000+ karma. Actions (below): admin/moderator
    only. Acting on a report hides the content; the trust penalty (−10 per
    upheld report) applies automatically through compute_trust_score.
    """
    from models.moderation import Report
    status = request.args.get('status', 'open')
    query = Report.query
    if status:
        query = query.filter_by(status=status)
    reports_list = query.order_by(desc(Report.created_at)).all()
    # M3: overdue reports (open > 24h, the review SLA) sort first
    # (stable sort keeps newest-first within each group).
    if status == 'open':
        reports_list.sort(key=lambda r: r.is_overdue, reverse=True)
    open_count = Report.query.filter_by(status='open').count()
    overdue_count = sum(
        1 for r in Report.query.filter_by(status='open').all() if r.is_overdue
    )
    can_act = current_user.role in ('admin', 'moderator')
    return render_template('admin/reports.html',
                           reports=reports_list,
                           status=status,
                           open_count=open_count,
                           overdue_count=overdue_count,
                           can_act=can_act)


@admin_bp.route('/reports/<id>/resolve', methods=['POST'])
@moderator_required
def resolve_report(id):
    """POST /admin/reports/<id>/resolve — dismiss or uphold a report."""
    from datetime import datetime, timezone
    from models.moderation import Report
    from models.post import Comment, Post
    if current_user.role not in ('admin', 'moderator'):
        flash('Only moderators can act on reports', 'danger')
        return redirect(url_for('admin.reports'))
    report = get_or_404(Report, id)
    if report.status != 'open':
        flash('This report was already resolved', 'info')
        return redirect(url_for('admin.reports'))
    action = request.form.get('action')
    if action == 'dismiss':
        report.status = 'dismissed'
        # M3: if this was the last open report on auto-hidden content,
        # the content comes back.
        restored = maybe_restore_after_dismiss(report.target_type, report.target_id)
        flash(
            'Report dismissed' + (' — content restored.' if restored else ''),
            'success',
        )
    elif action == 'hide':
        if report.target_type == 'post':
            target = db.session.get(Post, report.target_id)
            if target:
                target.is_hidden = True
                # An admin hide is manual, not auto — clear the
                # auto-hide marker so a later dismiss can't restore it.
                target.auto_hidden_at = None
        elif report.target_type == 'comment':
            target = db.session.get(Comment, report.target_id)
            if target:
                target.is_hidden = True
                target.auto_hidden_at = None
        elif report.target_type == 'profile':
            # Hiding a profile hides its posts from feeds (moderator judgment;
            # user keeps access — a ban is a separate admin action).
            for post in Post.query.filter_by(author_id=report.target_id, is_hidden=False).all():
                post.is_hidden = True
                post.auto_hidden_at = None
        report.status = 'actioned'
        flash('Content hidden, report upheld', 'success')
    else:
        flash('Unknown action', 'danger')
        return redirect(url_for('admin.reports'))
    report.reviewed_by_id = current_user.id
    report.reviewed_at = datetime.now(timezone.utc)
    db.session.commit()
    return redirect(url_for('admin.reports'))


@admin_bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    """GET /admin/settings - API key management."""
    form = SettingsForm()

    if request.method == 'POST' and form.validate_on_submit():
        # M2: the platform fee is a plain integer setting — validate it
        # before saving anything.
        fee_raw = (request.form.get('platform_fee_percent') or '').strip()
        if fee_raw:
            try:
                fee_value = int(fee_raw)
            except ValueError:
                fee_value = None
            if fee_value is None or not 0 <= fee_value <= 50:
                flash('Platform fee must be a whole number between 0 and 50.', 'danger')
                return redirect(url_for('admin.settings'))
        # M3: moderation settings — whole numbers, at least 1.
        for field_name, label in [
            ('auto_hide_threshold', 'Auto-hide threshold'),
            ('post_limit_newcomer', 'Newcomer post limit'),
            ('post_limit_member', 'Member post limit'),
        ]:
            raw = (request.form.get(field_name) or '').strip()
            if raw:
                try:
                    value = int(raw)
                except ValueError:
                    value = None
                if value is None or value < 1:
                    flash(f'{label} must be a whole number of at least 1.', 'danger')
                    return redirect(url_for('admin.settings'))
        for field_name, key in SETTINGS_MAP.items():
            raw = request.form.get(field_name)
            if raw and not raw.startswith('***') and raw.strip():
                AdminSettings.set(key, raw.strip())
        flash('Settings saved successfully', 'success')
        return redirect(url_for('admin.settings'))

    # M2: prefill the platform fee with its current value (it is not a
    # secret, so it shows directly — unlike the API keys, which load
    # via AJAX reveal).
    form.platform_fee_percent.data = get_platform_fee_percent()
    # M3: prefill the moderation settings the same way.
    form.auto_hide_threshold.data = get_auto_hide_threshold()
    form.post_limit_newcomer.data = AdminSettings.get(
        POST_LIMIT_NEWCOMER_KEY, str(DEFAULT_POST_LIMIT_NEWCOMER)
    )
    form.post_limit_member.data = AdminSettings.get(
        POST_LIMIT_MEMBER_KEY, str(DEFAULT_POST_LIMIT_MEMBER)
    )
    return render_template('admin/settings.html', form=form)


@admin_bp.route('/settings/reveal', methods=['POST'])
@admin_required
def reveal_setting():
    """POST /admin/settings/reveal - AJAX: return real value for a key."""
    key = request.form.get('key')
    if key not in SETTINGS_MAP.values():
        return jsonify({'error': 'Invalid key'}), 403
    if current_user.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    value = AdminSettings.get(key)
    return jsonify({'key': key, 'value': value or ''})


@admin_bp.route('/settings/all', methods=['GET'])
@admin_required
def all_settings():
    """GET /admin/settings/all - List all keys with masked values."""
    keys_and_values = {}
    for key in SETTINGS_MAP.values():
        val = AdminSettings.get(key)
        if val and len(val) > 4:
            keys_and_values[key] = val[:2] + '***' + val[-2:]
        else:
            keys_and_values[key] = '***' if val else ''
    return jsonify(keys_and_values)


@admin_bp.route('/create-admin', methods=['GET', 'POST'])
def create_admin():
    """Bootstrap the first admin user.

    SECURITY: This must never be anonymously reachable. It only works when an
    ADMIN_SETUP_TOKEN env var is configured AND the caller supplies it (form/query
    param `token`). If the token is unset, self-service creation is disabled entirely
    (create the first admin via a trusted method such as a shell/seed script instead).
    There is no default password.
    """
    # Already bootstrapped?
    if User.query.filter_by(role='admin').first():
        flash('Admin already exists', 'warning')
        return redirect(url_for('bounties.board'))

    setup_token = os.getenv('ADMIN_SETUP_TOKEN')
    if not setup_token:
        # Self-service admin creation is disabled. Bootstrap via a trusted method.
        abort(403)

    supplied = request.form.get('token') or request.args.get('token')
    if not hmac.compare_digest(supplied or '', setup_token):
        abort(403)

    email = request.form.get('email')
    password = request.form.get('password')

    if not email or not password:
        # CSRFProtect guards every POST: the bootstrap form must
        # carry a token like every other form, or the submit 400s.
        from flask_wtf.csrf import generate_csrf
        token = generate_csrf()
        return f'''
        <form method="POST">
            <input type="hidden" name="csrf_token" value="{token}">
            <p>Token: <input type="text" name="token" required></p>
            <p>Email: <input type="email" name="email" required></p>
            <p>Password: <input type="password" name="password" required></p>
            <button type="submit">Create Admin</button>
        </form>
        '''

    user = User(
        email=email,
        full_name='Admin',
        password_hash=generate_password_hash(password),
        role='admin',
        is_active=True
    )
    db.session.add(user)
    db.session.commit()

    flash(f'Admin created: {email}', 'success')
    return redirect(url_for('bounties.board'))

# ---------------------------------------------------------------------------
# Linked accounts review queue + pledges + recurring bumps (M3)
# ---------------------------------------------------------------------------

@admin_bp.route('/linked-accounts')
@admin_required
def linked_accounts():
    """GET /admin/linked-accounts — manual review queue for linked
    accounts (M3, §2.2): pending links where the user asked for a
    manual check, plus all pending links. Manual review for the first
    100 users is a feature — the founder personally knows them."""
    manual_only = request.args.get('manual', '1') == '1'
    query = LinkedAccount.query.filter_by(status='pending')
    if manual_only:
        query = query.filter_by(manual_review=True)
    accounts = query.order_by(desc(LinkedAccount.created_at)).all()
    pending_count = LinkedAccount.query.filter_by(status='pending').count()
    manual_count = LinkedAccount.query.filter_by(
        status='pending', manual_review=True
    ).count()
    return render_template(
        'admin/linked_accounts.html',
        accounts=accounts,
        manual_only=manual_only,
        pending_count=pending_count,
        manual_count=manual_count,
    )


@admin_bp.route('/linked-accounts/<id>/approve', methods=['POST'])
@admin_required
def approve_linked_account(id):
    """POST /admin/linked-accounts/<id>/approve — the founder checked
    the link by hand; mark it verified (90-day expiry)."""
    account = get_or_404(LinkedAccount, id)
    if account.status == 'verified' and account.is_currently_verified:
        flash('This link is already verified.', 'info')
        return redirect(url_for('admin.linked_accounts'))
    mark_verified(account)
    notify(
        account.user_id, 'linked_verified',
        f'Your {account.platform} account is verified!',
        'An admin checked your link manually. It counts toward your '
        'trust score for 90 days.',
        link_url=url_for('profile.linked_accounts'),
    )
    db.session.commit()
    flash('Link verified.', 'success')
    return redirect(url_for('admin.linked_accounts'))


@admin_bp.route('/linked-accounts/<id>/reject', methods=['POST'])
@admin_required
def reject_linked_account(id):
    """POST /admin/linked-accounts/<id>/reject — reject a link with an
    admin note."""
    account = get_or_404(LinkedAccount, id)
    if account.status != 'pending':
        flash('This link was already reviewed.', 'info')
        return redirect(url_for('admin.linked_accounts'))
    account.status = 'rejected'
    account.admin_note = request.form.get('admin_note') or None
    account.manual_review = False
    notify(
        account.user_id, 'linked_rejected',
        f'Your {account.platform} link was not verified',
        account.admin_note or (
            'An admin reviewed your link and could not verify it. '
            'Check the link and try again.'
        ),
        link_url=url_for('profile.linked_accounts'),
    )
    db.session.commit()
    flash('Link rejected.', 'success')
    return redirect(url_for('admin.linked_accounts'))


@admin_bp.route('/pledges')
@admin_required
def pledges():
    """GET /admin/pledges — all pledges, with their effective status
    (a pledger with an open SLA breach shows suspended)."""
    from utils.pledges import effective_pledge_status, ghosting_compliance
    pledges_list = Pledge.query.order_by(desc(Pledge.created_at)).all()
    entries = []
    for pledge in pledges_list:
        rate, due, _on_time = ghosting_compliance(pledge.user_id)
        entries.append({
            'pledge': pledge,
            'status': effective_pledge_status(pledge),
            'compliance_rate': rate,
            'compliance_due': due,
        })
    return render_template('admin/pledges.html', entries=entries)


@admin_bp.route('/pledges/<id>/revoke', methods=['POST'])
@admin_required
def revoke_pledge(id):
    """POST /admin/pledges/<id>/revoke — revoke a pledge (the employer
    broke the promise — e.g. AI screening, repeated ghosting)."""
    pledge = get_or_404(Pledge, id)
    if pledge.status == 'revoked':
        flash('This pledge is already revoked.', 'info')
        return redirect(url_for('admin.pledges'))
    pledge.status = 'revoked'
    pledge.revoked_at = utc_now()
    pledge.revoke_reason = (request.form.get('reason') or '').strip() or None
    notify(
        pledge.user_id, 'pledge_revoked',
        'Your human-reviewed hiring pledge was revoked',
        pledge.revoke_reason or (
            'An admin revoked your pledge. Your company is no longer '
            'on the register.'
        ),
        link_url=url_for('social.pledges'),
    )
    db.session.commit()
    flash('Pledge revoked.', 'success')
    return redirect(url_for('admin.pledges'))


# ---------------------------------------------------------------------------
# Human-reviewed employer pledges (models/employer_pledge.py)
#
# NOTE: /admin/pledges above reviews the M3 self-published Pledge
# (models/endorsement.py). These routes review the separate
# human-reviewed EmployerPledge, so they live under
# /admin/employer-pledges to avoid colliding with it.
# ---------------------------------------------------------------------------

@admin_bp.route('/employer-pledges')
@admin_required
def employer_pledges():
    """GET /admin/employer-pledges — review queue for human-reviewed
    employer pledges (pending first, then newest)."""
    status = request.args.get('status', 'pending')
    query = EmployerPledge.query
    if status:
        query = query.filter_by(status=status)
    pledges_list = query.order_by(desc(EmployerPledge.created_at)).all()
    pending_count = EmployerPledge.query.filter_by(status='pending').count()
    return render_template(
        'admin/employer_pledges.html',
        pledges=pledges_list,
        status=status,
        pending_count=pending_count,
    )


@admin_bp.route('/employer-pledges/<id>/approve', methods=['POST'])
@admin_required
def approve_employer_pledge(id):
    """POST /admin/employer-pledges/<id>/approve — approve a pending
    pledge: the organization appears on the public register."""
    pledge = get_or_404(EmployerPledge, id)
    if pledge.status != 'pending':
        flash('This pledge was already reviewed.', 'info')
        return redirect(url_for('admin.employer_pledges'))
    pledge.status = 'approved'
    pledge.decided_at = utc_now()
    pledge.decided_by_id = current_user.id
    notify(
        pledge.user_id, 'employer_pledge_approved',
        'Your employer pledge was approved!',
        f'"{pledge.org_name}" is now on the human-reviewed employer '
        'pledge register.',
        link_url=url_for('pledge.register'),
    )
    db.session.commit()
    AuditLog.log(
        admin_id=current_user.id,
        action='approve_employer_pledge',
        target_type='employer_pledge',
        target_id=id,
        details=f'Approved employer pledge for {pledge.org_name}',
    )
    flash('Employer pledge approved.', 'success')
    return redirect(url_for('admin.employer_pledges'))


@admin_bp.route('/employer-pledges/<id>/revoke', methods=['POST'])
@admin_required
def revoke_employer_pledge(id):
    """POST /admin/employer-pledges/<id>/revoke — revoke an approved
    (or pending) pledge: the organization leaves the register. The
    employer may re-pledge afterwards."""
    pledge = get_or_404(EmployerPledge, id)
    if pledge.status == 'revoked':
        flash('This pledge is already revoked.', 'info')
        return redirect(url_for('admin.employer_pledges'))
    pledge.status = 'revoked'
    pledge.decided_at = utc_now()
    pledge.decided_by_id = current_user.id
    notify(
        pledge.user_id, 'employer_pledge_revoked',
        'Your employer pledge was revoked',
        f'"{pledge.org_name}" is no longer on the human-reviewed '
        'employer pledge register. You may submit a new pledge.',
        link_url=url_for('pledge.register'),
    )
    db.session.commit()
    AuditLog.log(
        admin_id=current_user.id,
        action='revoke_employer_pledge',
        target_type='employer_pledge',
        target_id=id,
        details=f'Revoked employer pledge for {pledge.org_name}',
    )
    flash('Employer pledge revoked.', 'success')
    return redirect(url_for('admin.employer_pledges'))


@admin_bp.route('/recurring-bumps/run', methods=['POST'])
@admin_required
def run_recurring_bumps_route():
    """POST /admin/recurring-bumps/run — apply due recurring bumps
    now (the same runner the `flask run-recurring-bumps` CLI command
    runs on a schedule)."""
    results = run_recurring_bumps()
    db.session.commit()
    applied = sum(1 for _id, ok, _msg in results if ok)
    flash(
        f'Recurring bumps run: {applied} applied, '
        f'{len(results) - applied} deferred.',
        'success',
    )
    return redirect(url_for('admin.dashboard'))
