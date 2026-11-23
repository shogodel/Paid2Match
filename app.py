"""Flask application factory."""
import os
from flask import Flask, render_template, redirect, url_for, session, request
from flask_login import current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from extensions import db, login_manager, migrate, bcrypt, babel, mail, csrf, get_or_404
from models import Profile


def create_app(config_name: str = None) -> Flask:
    """Create and configure the Flask application."""

    # Load config
    if config_name is None:
        config_name = os.getenv('FLASK_ENV', 'development')

    # Create Flask app
    app = Flask(
        __name__,
        template_folder='templates',
        static_folder='static'
    )

    # Load configuration
    from config import config_by_name
    app.config.from_object(config_by_name.get(config_name, config_by_name['development']))

    # The app runs behind the SWAG reverse proxy, which terminates HTTPS
    # and forwards X-Forwarded-Proto/Host. Trust one proxy hop so
    # url_for(..., _external=True) builds https://paid2match.work/...
    # URLs (Google rejects an http:// redirect_uri). Gunicorn is only
    # reachable through the proxy, so these headers cannot be spoofed
    # by outside clients.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    bcrypt.init_app(app)
    mail.init_app(app)
    csrf.init_app(app)
    
    # Exempt Stripe webhook from CSRF protection (uses signature verification)
    csrf.exempt('blueprints.bounties.routes.stripe_webhook')
    
    # Setup Babel locale selector
    def get_locale():
        """Get locale for translations - priority: session > user profile > browser > default English."""
        # 1. Check session preference (cached from profile or manual set)
        lang = session.get('language')
        if lang in ['en', 'fr', 'es']:
            return lang
        
        # 2. Check user profile preference (if logged in) - cache in session
        if current_user.is_authenticated and 'user_language' not in session:
            profile = db.session.query(Profile).filter_by(user_id=current_user.id).first()
            if profile and profile.language in ['en', 'fr', 'es']:
                session['user_language'] = profile.language
                return profile.language
        
        # Return cached profile language if available
        if 'user_language' in session:
            return session['user_language']
        
        # 3. Check browser preference
        browser_lang = request.accept_languages.best_match(['en', 'fr', 'es'])
        if browser_lang:
            return browser_lang
        
        # 4. Default to English
        return 'en'
    
    babel.init_app(app, locale_selector=get_locale)

    # Make gettext available in all Jinja2 templates
    from flask_babel import gettext, ngettext
    app.jinja_env.globals.update(
        gettext=gettext,
        ngettext=ngettext,
        _=gettext
    )

    # Flask-Login configured in extensions.py

    @login_manager.user_loader
    def load_user(user_id):
        from models.user import User
        return db.session.get(User, user_id)

    # Register blueprints
    from blueprints.auth import auth_bp
    from blueprints.profile import profile_bp
    from blueprints.bounties import bounties_bp
    from blueprints.messages import messages_bp
    from blueprints.admin import admin_bp
    from blueprints.social import social_bp
    from blueprints.referrals import referrals_bp
    from blueprints.payments import payments_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(bounties_bp, url_prefix='/bounties')
    app.register_blueprint(messages_bp, url_prefix='/messages')
    app.register_blueprint(profile_bp, url_prefix='/profile')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(social_bp, url_prefix='/social')
    app.register_blueprint(referrals_bp, url_prefix='/referrals')
    app.register_blueprint(payments_bp)

    # M1: unread notification count for the navbar badge.
    @app.context_processor
    def inject_notifications():
        from utils.notifications import unread_count
        count = unread_count(current_user.id) if current_user.is_authenticated else 0
        return {'unread_notifications': count}

    # M3: recurring-bump runner — apply due $5/month auto-bumps. Run
    # on a schedule on the server (e.g. daily cron: flask
    # run-recurring-bumps). The admin panel has a manual button too.
    @app.cli.command('run-recurring-bumps')
    def run_recurring_bumps_command():
        from utils.bumps import run_recurring_bumps
        results = run_recurring_bumps()
        db.session.commit()
        applied = sum(1 for _id, ok, _msg in results if ok)
        print(f'{applied} recurring bumps applied, {len(results) - applied} deferred.')

    # Language switcher route
    @app.route('/set-language/<lang>')
    def set_language(lang):
        """Change language preference."""
        if lang in ['en', 'fr', 'es']:
            session['language'] = lang
            # Clear cached profile language so new preference takes effect
            session.pop('user_language', None)
        return redirect(url_for('index'))

    # Index route - landing page with cached stats
    @app.route('/')
    def index():
        from sqlalchemy import func
        from models.user import User
        from models.bounty import Bounty
        from models.match import Match

        # Use cached stats if available (5 min cache via simple dict)
        cache_key = 'homepage_stats'
        if cache_key not in app.config:
            total_users = db.session.query(User).filter_by(is_active=True).count()
            total_bounties = db.session.query(Bounty).filter(
                Bounty.status.in_(['open', 'completed'])
            ).count()
            total_rewards = db.session.query(
                func.sum(Bounty.reward_amount)
            ).filter(
                Bounty.status == 'open',
                Bounty.reward_amount.isnot(None)
            ).scalar() or 0
            successful_matches = db.session.query(Match).filter_by(
                hunter_confirmed=True,
                employer_confirmed=True
            ).count()
            app.config[cache_key] = {
                'total_users': total_users,
                'total_bounties': total_bounties,
                'total_rewards': total_rewards,
                'successful_matches': successful_matches
            }
        
        stats = app.config[cache_key]
        featured = db.session.query(Bounty).filter(
            Bounty.status == 'open',
            Bounty.publicly_visible_filter()
        ).order_by(
            Bounty.created_at.desc()
        ).limit(3).all()

        # M1: Top Helpers — the 3 highest-karma profiles with at least one
        # completed referral (helped_count > 0).
        top_helpers = db.session.query(Profile).filter(
            Profile.helped_count > 0
        ).order_by(
            Profile.karma.desc()
        ).limit(3).all()

        # Homepage social preview: the 4 most recent public posts, using
        # the same visibility rule as Discover (hidden posts never show;
        # a signed-in viewer's blocked authors are excluded).
        from models.post import Post
        from utils.feed import _visible_posts_query
        viewer_id = current_user.id if current_user.is_authenticated else None
        recent_posts = _visible_posts_query(viewer_id).order_by(
            Post.created_at.desc()
        ).limit(4).all()

        return render_template('index.html',
                          total_users=stats['total_users'],
                          total_bounties=stats['total_bounties'],
                          total_rewards=stats['total_rewards'],
                          successful_matches=stats['successful_matches'],
                          featured=featured,
                          top_helpers=top_helpers,
                          recent_posts=recent_posts)

    # Error handlers
    @app.errorhandler(404)
    def not_found_error(error):
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return render_template('errors/500.html'), 500

    # Request teardown - cleanup session
    @app.teardown_appcontext
    def shutdown_session(exception=None):
        db.session.remove()

    return app


# Development runner only
if __name__ == '__main__':
    app = create_app()
    debug = app.config.get('DEBUG', False)
    app.run(host='0.0.0.0', port=5000, debug=debug)
