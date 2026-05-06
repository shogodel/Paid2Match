"""Authentication routes for Paid2Match."""
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from blueprints.auth.forms import LoginForm, RegistrationForm
from models.user import User

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
        
        remember = form.remember.data == True
        login_user(user, remember=remember)
        
        user.last_login = datetime.now(timezone.utc)
        db.session.commit()
        
        flash(f'Welcome back, {user.full_name}!', 'success')
        
        next_page = session.pop('next', None)
        if next_page and _is_safe_url(next_page):
            return redirect(next_page)
        return redirect(url_for('bounties.board'))
    
    session['next'] = request.args.get('next')
    
    return render_template('auth/login.html', form=form)


def _is_safe_url(target: str) -> bool:
    """Validate that the redirect URL is internal to prevent open redirect attacks."""
    from urllib.parse import urlparse
    ref_url = urlparse(request.host_url)
    target_url = urlparse(target)
    return target_url.scheme in ('', 'http', 'https') and (
        target_url.netloc in ('', ref_url.netloc) or target_url.netloc.startswith('localhost')
    )


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """GET /auth/register - Show registration form
    POST /auth/register - Process registration
    """
    if current_user.is_authenticated:
        return redirect(url_for('bounties.board'))
    
    form = RegistrationForm()
    
    if request.method == 'POST' and form.validate_on_submit():
        full_name = form.full_name.data.strip()
        email = form.email.data.lower().strip()
        password = form.password.data
        
        # Hash password with werkzeug
        password_hash = generate_password_hash(password)
        
        # Create new user
        user = User(
            email=email,
            full_name=full_name,
            password_hash=password_hash,
            role='user',
            is_active=True
        )
        db.session.add(user)
        db.session.commit()

        # Auto-login after registration — profile creation deferred to /profile/setup
        login_user(user, remember=True)

        flash('Account created! Please complete your profile.', 'success')
        return redirect(url_for('profile.setup'))
    
    return render_template('auth/register.html', form=form)


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