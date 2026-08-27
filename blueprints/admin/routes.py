"""Admin routes for Paid2Match."""
import os
import hmac
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, abort
from flask_login import login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SelectField, TextAreaField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Length, ValidationError
from sqlalchemy import desc, func, or_

from extensions import db, get_or_404
from models.user import User
from models.bounty import Bounty
from models.match import Match
from models.dispute import Dispute
from models.admin_settings import AdminSettings
from models.profile import Profile
from models.audit_log import AuditLog
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
}

# Stripe keys are skipped entirely when payments are disabled (STRIPE_ENABLED=false).
# TODO(STRIPE): Remove this set / the skipping logic once Stripe is reconfigured.
STRIPE_SETTING_KEYS = {
    'STRIPE_PUBLISHABLE_KEY',
    'STRIPE_SECRET_KEY',
    'STRIPE_WEBHOOK_SECRET',
}


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
    
    bounty_type = request.args.get('bounty_type')
    if bounty_type:
        query = query.filter_by(bounty_type=bounty_type)
    
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
                        bounty_type=bounty_type,
                        payment_filter=payment_filter)


@admin_bp.route('/bounties/<id>/approve', methods=['POST'])
@admin_required
def approve_bounty(id):
    """POST /admin/bounties/<id>/approve - Approve bounty."""
    bounty = get_or_404(Bounty, id)
    bounty.status = 'open'
    db.session.commit()
    flash('Bounty approved and published', 'success')
    return redirect(url_for('admin.bounties'))


@admin_bp.route('/bounties/<id>/reject', methods=['POST'])
@admin_required
def reject_bounty(id):
    """POST /admin/bounties/<id>/reject - Reject bounty."""
    bounty = get_or_404(Bounty, id)
    bounty.status = 'closed'
    db.session.commit()
    flash('Bounty rejected', 'success')
    return redirect(url_for('admin.bounties'))


@admin_bp.route('/bounties/<id>/close', methods=['POST'])
@admin_required
def close_bounty(id):
    """POST /admin/bounties/<id>/close - Close bounty."""
    bounty = get_or_404(Bounty, id)
    bounty.status = 'closed'
    db.session.commit()
    flash('Bounty closed', 'success')
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
    """POST /admin/disputes/<id>/resolve - Resolve dispute."""
    dispute = get_or_404(Dispute, id)
    resolution = request.form.get('resolution')
    
    dispute.status = 'resolved'
    dispute.resolution = resolution
    dispute.resolved_by_id = current_user.id
    from datetime import datetime, timezone
    dispute.resolved_at = datetime.now(timezone.utc)
    
    db.session.commit()
    flash('Dispute resolved', 'success')
    return redirect(url_for('admin.disputes'))


@admin_bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    """GET /admin/settings - API key management."""
    form = SettingsForm()

    if request.method == 'POST' and form.validate_on_submit():
        # TODO(STRIPE): Skip saving Stripe keys while payments are disabled.
        stripe_enabled = current_app.config.get('STRIPE_ENABLED', True)
        for field_name, key in SETTINGS_MAP.items():
            if not stripe_enabled and key in STRIPE_SETTING_KEYS:
                continue
            raw = request.form.get(field_name)
            if raw and not raw.startswith('***') and raw.strip():
                AdminSettings.set(key, raw.strip())
        flash('Settings saved successfully', 'success')
        return redirect(url_for('admin.settings'))

    # TODO(STRIPE): Tell the template to hide the Stripe card when payments are disabled.
    return render_template('admin/settings.html', form=form,
                          stripe_enabled=current_app.config.get('STRIPE_ENABLED', True))


@admin_bp.route('/settings/reveal', methods=['POST'])
@admin_required
def reveal_setting():
    """POST /admin/settings/reveal - AJAX: return real value for a key."""
    key = request.form.get('key')
    if key not in SETTINGS_MAP.values():
        return jsonify({'error': 'Invalid key'}), 403
    # TODO(STRIPE): Don't reveal Stripe keys while payments are disabled.
    if key in STRIPE_SETTING_KEYS and not current_app.config.get('STRIPE_ENABLED', True):
        return jsonify({'error': 'Stripe disabled'}), 403
    if current_user.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403
    value = AdminSettings.get(key)
    return jsonify({'key': key, 'value': value or ''})


@admin_bp.route('/settings/all', methods=['GET'])
@admin_required
def all_settings():
    """GET /admin/settings/all - List all keys with masked values."""
    keys_and_values = {}
    # TODO(STRIPE): Exclude Stripe keys from the listing while payments are disabled.
    stripe_enabled = current_app.config.get('STRIPE_ENABLED', True)
    for key in SETTINGS_MAP.values():
        if not stripe_enabled and key in STRIPE_SETTING_KEYS:
            continue
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
        return '''
        <form method="POST">
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