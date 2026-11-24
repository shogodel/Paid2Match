"""Authentication routes for Paid2Match.

OAuth-only: sign-in is Google, GitHub, or Facebook ("Continue with
Google" / "Continue with GitHub" / "Continue with Facebook"). There is
no email/password registration or login — the provider verifies the
email, so accounts created or matched through OAuth are marked
email_verified. Client id/secret for each provider come from the
environment only (config.py).
"""
import secrets
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash

from extensions import db
from models.user import User
from utils.facebook_oauth import (
    exchange_facebook_code,
    facebook_authorize_url,
    facebook_available,
    fetch_facebook_userinfo,
)
from utils.google_oauth import (
    google_available,
    google_authorize_url,
    exchange_google_code,
    fetch_google_userinfo,
)
from utils.linked_accounts import (
    GITHUB_SIGNIN_SCOPE,
    exchange_oauth_code,
    fetch_github_primary_email,
    fetch_github_user,
    oauth_authorize_url,
    oauth_available,
    oauth_state_token,
)

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login')
def login():
    """GET /auth/login - OAuth-only sign-in page (Google / GitHub /
    Facebook). Signing in with a provider also creates the account on
    first use.
    """
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))

    session['next'] = request.args.get('next')

    return render_template('auth/login.html')


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


def _complete_oauth_login(email, full_name):
    """Find-or-create the user for a provider-verified email, log them
    in, and return the post-login redirect response. Shared by the
    Google, GitHub, and Facebook callbacks. Returns (response, error)
    — error is a flash message when the account cannot be used
    (deactivated)."""
    now = datetime.now(timezone.utc)
    user = User.query.filter_by(email=email).first()
    if user:
        if not user.is_active or user.is_deleted:
            return None, 'This account has been deactivated.'
        created = False
        # The provider verified this email, so the account is verified now.
        user.email_verified = True
        user.email_verified_at = now
    else:
        created = True
        user = User(
            email=email,
            full_name=full_name[:120],
            # No usable password: this account signs in with a provider.
            # A random value, hashed with werkzeug, so password login
            # can never succeed for it (there is no password login).
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

    # Where to land: the stored ?next= URL first (e.g. a deep link or a
    # referral consent page), then a referral invite waiting for this
    # login, otherwise the board.
    next_page = session.pop('next', None)
    if next_page and _is_safe_url(next_page):
        return redirect(next_page), None
    pending_token = session.pop('pending_referral_token', None)
    if pending_token:
        return redirect(url_for('referrals.consent', token=pending_token)), None
    return redirect(url_for('bounties.board')), None


@auth_bp.route('/google/login')
def google_login():
    """GET /auth/google/login - Start Google sign-in ("Continue with
    Google"). Redirects to Google's authorize endpoint with a random
    state token stored in the session."""
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))

    if not google_available():
        flash('Google sign-in is not available right now — please try GitHub instead.', 'warning')
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
        flash('Google sign-in was cancelled or failed — please try again.', 'danger')
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
        flash('Google did not share an email address for this account.', 'danger')
        return redirect(url_for('auth.login'))

    # Google tells us whether it verified the email; only a verified
    # email skips our own confirmation step.
    if userinfo.get('email_verified') is False:
        flash('Google has not verified that email address.', 'danger')
        return redirect(url_for('auth.login'))

    full_name = (userinfo.get('name') or '').strip() or email.split('@')[0]
    response, error = _complete_oauth_login(email, full_name)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.login'))
    return response


@auth_bp.route('/github/login')
def github_login():
    """GET /auth/github/login - Start GitHub sign-in ("Continue with
    GitHub"). Redirects to GitHub's authorize endpoint with a random
    state token stored in the session. Reuses the GitHub OAuth client
    in utils/linked_accounts.py (the same one the account-linking flow
    uses), with the user:email scope added so a hidden email can still
    be read for login."""
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))

    if not oauth_available('github'):
        flash('GitHub sign-in is not available right now — please try Google instead.', 'warning')
        return redirect(url_for('auth.login'))

    state = oauth_state_token()
    session['github_oauth_state'] = state
    authorize_url = oauth_authorize_url(
        'github',
        url_for('auth.github_callback', _external=True),
        state,
        scope=GITHUB_SIGNIN_SCOPE,
    )
    return redirect(authorize_url)


@auth_bp.route('/github/callback')
def github_callback():
    """GET /auth/github/callback - Handle GitHub's redirect after
    sign-in. Verifies the state token, exchanges the code for an access
    token, reads the user's GitHub profile, then logs them into the
    matching account (creating it, already verified, when there is none
    — GitHub has already verified the email).

    Many GitHub users hide their email, so the profile's email is often
    null: the callback then falls back to the /user/emails endpoint and
    takes the primary verified address. With no verified email
    obtainable, the login fails — accounts are never created without
    an email."""
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))

    if request.args.get('error'):
        flash('GitHub sign-in was cancelled or failed — please try again.', 'danger')
        return redirect(url_for('auth.login'))

    # One-time state check (CSRF): the token must match the one stored
    # when the sign-in started.
    expected_state = session.pop('github_oauth_state', None)
    if not expected_state or request.args.get('state') != expected_state:
        flash('That GitHub sign-in link is invalid or has expired — please try again.', 'danger')
        return redirect(url_for('auth.login'))

    code = request.args.get('code')
    if not code:
        flash('GitHub did not return a sign-in code — please try again.', 'danger')
        return redirect(url_for('auth.login'))

    redirect_uri = url_for('auth.github_callback', _external=True)
    token, error = exchange_oauth_code('github', code, redirect_uri)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.login'))

    profile, error = fetch_github_user(token)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.login'))

    email = (profile.get('email') or '').lower().strip()
    if not email:
        # Hidden email: ask GitHub for the private addresses and take
        # the primary verified one.
        email, error = fetch_github_primary_email(token)
        if error:
            flash(error, 'danger')
            return redirect(url_for('auth.login'))
        email = (email or '').lower().strip()
    if not email:
        flash('GitHub did not share a verified email address for this account — make an email visible on GitHub, or sign in with Google instead.', 'danger')
        return redirect(url_for('auth.login'))

    full_name = (
        (profile.get('name') or '').strip()
        or (profile.get('login') or '').strip()
        or email.split('@')[0]
    )
    response, error = _complete_oauth_login(email, full_name)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.login'))
    return response


@auth_bp.route('/facebook/login')
def facebook_login():
    """GET /auth/facebook/login - Start Facebook sign-in ("Continue
    with Facebook"). Redirects to Facebook's authorize endpoint with a
    random state token stored in the session."""
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))

    if not facebook_available():
        flash('Facebook sign-in is not available right now — please try Google instead.', 'warning')
        return redirect(url_for('auth.login'))

    state = oauth_state_token()
    session['facebook_oauth_state'] = state
    authorize_url = facebook_authorize_url(
        url_for('auth.facebook_callback', _external=True), state
    )
    return redirect(authorize_url)


@auth_bp.route('/facebook/callback')
def facebook_callback():
    """GET /auth/facebook/callback - Handle Facebook's redirect after
    sign-in. Verifies the state token, exchanges the code for an access
    token, reads the user's Facebook profile, then logs them into the
    matching account (creating it, already verified, when there is none
    — Facebook has already verified the email).

    Facebook only shares an email when the user granted the email
    permission and the account has a confirmed email address — people
    who signed up with a phone number may have none. With no email the
    login fails cleanly with a message pointing at Google or GitHub;
    accounts are never created without an email."""
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))

    if request.args.get('error'):
        flash('Facebook sign-in was cancelled or failed — please try again.', 'danger')
        return redirect(url_for('auth.login'))

    # One-time state check (CSRF): the token must match the one stored
    # when the sign-in started.
    expected_state = session.pop('facebook_oauth_state', None)
    if not expected_state or request.args.get('state') != expected_state:
        flash('That Facebook sign-in link is invalid or has expired — please try again.', 'danger')
        return redirect(url_for('auth.login'))

    code = request.args.get('code')
    if not code:
        flash('Facebook did not return a sign-in code — please try again.', 'danger')
        return redirect(url_for('auth.login'))

    redirect_uri = url_for('auth.facebook_callback', _external=True)
    token, error = exchange_facebook_code(code, redirect_uri)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.login'))

    userinfo, error = fetch_facebook_userinfo(token)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.login'))

    email = (userinfo.get('email') or '').lower().strip()
    if not email:
        flash("Facebook did not share an email address for this account — please sign in with Google or GitHub instead.", 'danger')
        return redirect(url_for('auth.login'))

    full_name = (userinfo.get('name') or '').strip() or email.split('@')[0]
    response, error = _complete_oauth_login(email, full_name)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth.login'))
    return response


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
    POST /auth/delete-account - Soft delete user account.
    Auth is OAuth-only, so there is no password to check — deletion is
    confirmed by typing DELETE.
    """
    if request.method == 'POST':
        if (request.form.get('confirmation') or '').strip() != 'DELETE':
            flash('Type DELETE to confirm account deletion.', 'danger')
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
