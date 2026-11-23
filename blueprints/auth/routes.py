"""Authentication routes for Paid2Match."""
import secrets
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from blueprints.auth.forms import (
    LoginForm, RegistrationForm, ForgotPasswordForm,
    ResetPasswordForm, ResendVerificationForm,
)
from models.user import User
from utils.email import (
    send_verification_email, send_password_reset_email,
    confirm_confirmation_token, confirm_reset_token,
)
from utils.google_oauth import (
    google_available,
    google_authorize_url,
    exchange_google_code,
    fetch_google_userinfo,
)
from utils.linked_accounts import oauth_state_token

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """GET /auth/login - Show login form
    POST /auth/login - Process login
    """
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))
    
    form = LoginForm()
    
    if request.method == 'POST' and form.validate_on_submit():
        email = form.email.data.lower().strip()
        password = form.password.data
        
        user = User.query.filter_by(email=email).first()
        
        if not user or not check_password_hash(user.password_hash or '', password):
            flash('Invalid email or password', 'danger')
            return render_template('auth/login.html', form=form)
        
        if not user.is_active:
            flash('This account has been deactivated', 'danger')
            return render_template('auth/login.html', form=form)

        if user.is_deleted:
            flash('This account has been deleted', 'danger')
            return render_template('auth/login.html', form=form)

        if not user.email_verified:
            flash('Please verify your email first — check your inbox for the confirmation link, or resend it below.', 'warning')
            return render_template('auth/login.html', form=form)
        
        remember = form.remember.data == True
        login_user(user, remember=remember)
        
        user.last_login = datetime.now(timezone.utc)
        db.session.commit()
        
        flash(f'Welcome back, {user.full_name}!', 'success')
        
        next_page = session.pop('next', None)
        if next_page and _is_safe_url(next_page):
            return redirect(next_page)
        # M1: a referral invite waiting for this login goes to the consent page.
        pending_token = session.pop('pending_referral_token', None)
        if pending_token:
            return redirect(url_for('referrals.consent', token=pending_token))
        return redirect(url_for('bounties.board'))
    
    session['next'] = request.args.get('next')
    
    return render_template('auth/login.html', form=form)


def _is_safe_url(target: str) -> bool:
    """Validate that the redirect URL is internal to prevent open redirect attacks."""
    from urllib.parse import urlparse
    ref_url = urlparse(request.host_url)
    target_url = urlparse(target)
    # Reject any URL with a disallowed scheme, or a host that is not this app's host
    # (this also blocks protocol-relative URLs like //evil.com and the previous
    # 'localhost'.evil.com substring bypass). Only relative paths or same-origin
    # absolute URLs are allowed.
    if target_url.scheme not in ('', 'http', 'https'):
        return False
    if target_url.netloc and target_url.netloc != ref_url.netloc:
        return False
    return True


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """GET /auth/register - Show registration form
    POST /auth/register - Process registration
    """
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))
    
    form = RegistrationForm()

    # M1: prefill from a referral invite (?ref=<token>) so the invited
    # person doesn't have to retype their name and email.
    if request.method == 'GET':
        ref_token = request.args.get('ref')
        if ref_token:
            from models.referral import Referral
            referral = Referral.query.filter_by(invite_token=ref_token).first()
            if referral:
                form.full_name.data = referral.referred_name
                form.email.data = referral.referred_email
                session['pending_referral_token'] = ref_token

    if request.method == 'POST' and form.validate_on_submit():
        full_name = form.full_name.data.strip()
        email = form.email.data.lower().strip()
        password = form.password.data
        
        # Hash password with werkzeug
        password_hash = generate_password_hash(password)

        # Create new user — unverified until they confirm via email.
        user = User(
            email=email,
            full_name=full_name,
            password_hash=password_hash,
            role='user',
            is_active=True,
            email_verified=False
        )
        db.session.add(user)
        db.session.commit()

        if send_verification_email(user):
            flash('Account created! Check your email to confirm your account, then log in.', 'success')
        else:
            # Mail is not configured (or failed) — the account exists but the
            # user cannot confirm yet; an admin can verify it manually.
            flash('Account created, but we could not send the confirmation email. Please contact support to verify your account.', 'warning')
        return redirect(url_for('auth.login'))

    return render_template('auth/register.html', form=form)


@auth_bp.route('/google/login')
def google_login():
    """GET /auth/google/login - Start Google sign-in ("Continue with
    Google"). Redirects to Google's authorize endpoint with a random
    state token stored in the session."""
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))

    if not google_available():
        flash('Google sign-in is not available right now — please log in with your email and password.', 'warning')
        return redirect(url_for('auth.login'))

    state = oauth_state_token()
    session['google_oauth_state'] = state
    authorize_url = google_authorize_url(
        url_for('auth.google_callback', _external=True), state
    )
    return redirect(authorize_url)


@auth_bp.route('/google/callback')
def google_callback():
    """GET /auth/google/callback - Handle Google's redirect after
    sign-in. Verifies the state token, exchanges the code for an access
    token, reads the user's Google profile, then logs them into the
    matching account (creating it, already verified, when there is none
    — Google has already verified the email)."""
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))

    if request.args.get('error'):
        flash('Google sign-in was cancelled or failed — please try again, or log in with your email.', 'danger')
        return redirect(url_for('auth.login'))

    # One-time state check (CSRF): the token must match the one stored
    # when the sign-in started.
    expected_state = session.pop('google_oauth_state', None)
    if not expected_state or request.args.get('state') != expected_state:
        flash('That Google sign-in link is invalid or has expired — please try again.', 'danger')
        return redirect(url_for('auth.login'))

    code = request.args.get('code')
    if not code:
        flash('Google did not return a sign-in code — please try again.', 'danger')
        return redirect(url_for('auth.login'))

    redirect_uri = url_for('auth.google_callback', _external=True)
    token, error = exchange_google_code(code, redirect_uri)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.login'))

    userinfo, error = fetch_google_userinfo(token)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.login'))

    email = (userinfo.get('email') or '').lower().strip()
    if not email:
        flash('Google did not share an email address for this account — please log in with your email instead.', 'danger')
        return redirect(url_for('auth.login'))

    # Google tells us whether it verified the email; only a verified
    # email skips our own confirmation step.
    if userinfo.get('email_verified') is False:
        flash('Google has not verified that email address — please log in with your email instead.', 'danger')
        return redirect(url_for('auth.login'))

    now = datetime.now(timezone.utc)
    user = User.query.filter_by(email=email).first()
    if user:
        if not user.is_active or user.is_deleted:
            flash('This account has been deactivated.', 'danger')
            return redirect(url_for('auth.login'))
        created = False
        # Google verified this email, so the account is verified now.
        user.email_verified = True
        user.email_verified_at = now
    else:
        created = True
        full_name = (userinfo.get('name') or '').strip() or email.split('@')[0]
        user = User(
            email=email,
            full_name=full_name[:120],
            # No usable password: this account signs in with Google.
            # A random value, hashed with werkzeug, so password login
            # can never succeed for it (the reset flow still works if
            # the user ever wants a password).
            password_hash=generate_password_hash(secrets.token_urlsafe(48)),
            role='user',
            is_active=True,
            email_verified=True,
            email_verified_at=now,
        )
        db.session.add(user)

    user.last_login = now
    db.session.commit()

    login_user(user, remember=True)
    if created:
        flash(f'Welcome to Paid2Match, {user.full_name}! Please complete your profile.', 'success')
    else:
        flash(f'Welcome back, {user.full_name}!', 'success')
    return redirect(url_for('bounties.board'))


@auth_bp.route('/confirm/<token>')
def confirm_email(token):
    """GET /auth/confirm/<token> - Verify email from the confirmation link."""
    email = confirm_confirmation_token(token)
    if not email:
        flash('That confirmation link is invalid or has expired. Request a new one below.', 'danger')
        return redirect(url_for('auth.resend_verification'))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash('No account matches that confirmation link.', 'danger')
        return redirect(url_for('auth.register'))

    if user.email_verified:
        flash('Your email is already verified — you can log in.', 'info')
        return redirect(url_for('auth.login'))

    user.email_verified = True
    user.email_verified_at = datetime.now(timezone.utc)
    db.session.commit()

    login_user(user, remember=True)
    flash('Email confirmed — welcome to Paid2Match! Please complete your profile.', 'success')
    # M1: if they came from a referral invite, take them to the consent page.
    pending_token = session.pop('pending_referral_token', None)
    if pending_token:
        return redirect(url_for('referrals.consent', token=pending_token))
    return redirect(url_for('profile.setup'))


@auth_bp.route('/resend-verification', methods=['GET', 'POST'])
def resend_verification():
    """GET/POST /auth/resend-verification - Send a new confirmation email."""
    form = ResendVerificationForm()
    if request.method == 'POST' and form.validate_on_submit():
        email = form.email.data.lower().strip()
        user = User.query.filter_by(email=email).first()
        # Same message whether or not the account exists / is verified, so
        # the form cannot be used to probe for accounts.
        if user and not user.email_verified and user.is_active and not user.is_deleted:
            send_verification_email(user)
        flash('If that email belongs to an unverified account, a new confirmation link is on its way.', 'info')
        return redirect(url_for('auth.login'))
    return render_template('auth/resend_verification.html', form=form)


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """GET/POST /auth/forgot-password - Request a password reset link."""
    form = ForgotPasswordForm()
    if request.method == 'POST' and form.validate_on_submit():
        email = form.email.data.lower().strip()
        user = User.query.filter_by(email=email).first()
        # Same message whether or not the account exists, so the form cannot
        # be used to probe for accounts.
        if user and user.is_active and not user.is_deleted:
            send_password_reset_email(user)
        flash('If an account exists for that email, a password reset link is on its way.', 'info')
        return redirect(url_for('auth.login'))
    return render_template('auth/forgot_password.html', form=form)


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """GET/POST /auth/reset-password/<token> - Choose a new password."""
    email = confirm_reset_token(token)
    if not email:
        flash('That reset link is invalid or has expired. Request a new one.', 'danger')
        return redirect(url_for('auth.forgot_password'))

    user = User.query.filter_by(email=email).first()
    if not user or not user.is_active or user.is_deleted:
        flash('That reset link is no longer valid for this account.', 'danger')
        return redirect(url_for('auth.forgot_password'))

    form = ResetPasswordForm()
    if request.method == 'POST' and form.validate_on_submit():
        user.password_hash = generate_password_hash(form.password.data)
        db.session.commit()
        flash('Your password has been reset. You can log in now.', 'success')
        return redirect(url_for('auth.login'))
    return render_template('auth/reset_password.html', form=form, token=token)


@auth_bp.route('/logout')
@login_required
def logout():
    """GET /auth/logout - Logout user and clear session."""
    logout_user()
    flash('You have been logged out', 'info')
    return redirect(url_for('bounties.board'))


@auth_bp.route('/')
def index():
    """Redirect /auth to login."""
    return redirect(url_for('auth.login'))


@auth_bp.route('/delete-account', methods=['GET', 'POST'])
@login_required
def delete_account():
    """GET /auth/delete-account - Show confirmation form
    POST /auth/delete-account - Soft delete user account
    """
    if request.method == 'POST':
        password = request.form.get('password')
        
        if not check_password_hash(current_user.password_hash or '', password):
            flash('Incorrect password. Account deletion cancelled.', 'danger')
            return redirect(url_for('auth.delete_account'))
        
        from models.profile import Profile
        
        current_user.soft_delete()
        
        profile = Profile.query.filter_by(user_id=current_user.id).first()
        if profile:
            profile.bio = ''
            profile.location = ''
            profile.headline = ''
        
        db.session.commit()
        logout_user()
        
        flash('Your account has been deleted. Thank you for using Paid2Match.', 'info')
        return redirect(url_for('bounties.board'))
    
    return render_template('auth/delete_account.html')


@auth_bp.route('/settings', methods=['GET'])
@login_required
def settings():
    """GET /auth/settings - Show account settings page."""
    return render_template('auth/settings.html')