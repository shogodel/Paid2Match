"""Flask application factory."""
import os
from flask import Flask, flash, g, jsonify, render_template, redirect, url_for, session, request
from flask_login import current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from extensions import db, login_manager, migrate, bcrypt, babel, mail, csrf, get_or_404
from models import Profile


def _is_safe_redirect(url):
    """True when url is a same-origin redirect target: a relative path
    (not protocol-relative), or an absolute URL on this host. Used by
    the language switcher so it can return the user to their page
    without becoming an open redirect."""
    if not url:
        return False
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.netloc:
        return url.startswith('/') and not url.startswith('//')
    return parsed.netloc == request.host


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
        """Get locale for translations - priority: session > user
        profile > browser > default English.

        Phase 2 cleanup: one session key only ('language'). The old
        code cached the profile language under a second key
        ('user_language') that fought the switcher; the profile
        language is now read live per request (cached on flask.g for
        the request, so it costs one query at most). A stale
        'user_language' from an older session is migrated once.

        Lazy strings (lazy_gettext) are evaluated wherever a label
        is used — including app-context-only code with no request
        (CLI commands, unit tests, background jobs). The session /
        request / current_user lookups below all need a request
        context, so without one this returns the cached value or
        English instead of raising.
        """
        from flask import has_request_context
        if not has_request_context():
            return getattr(g, '_p2m_locale', None) or 'en'
        if getattr(g, '_p2m_locale', None):
            return g._p2m_locale
        # One-time migration of the legacy session key.
        if 'language' not in session and 'user_language' in session:
            session['language'] = session.pop('user_language')
        # 1. Explicit session preference (set by the switcher).
        lang = session.get('language')
        if lang in ['en', 'fr', 'es']:
            g._p2m_locale = lang
            return lang
        # 2. User profile preference (if logged in).
        if current_user.is_authenticated:
            profile = db.session.query(Profile).filter_by(
                user_id=current_user.id
            ).first()
            if profile and profile.language in ['en', 'fr', 'es']:
                g._p2m_locale = profile.language
                return profile.language
        # 3. Browser preference.
        browser_lang = request.accept_languages.best_match(['en', 'fr', 'es'])
        if browser_lang:
            g._p2m_locale = browser_lang
            return browser_lang
        # 4. Default to English.
        g._p2m_locale = 'en'
        return 'en'
    
    babel.init_app(app, locale_selector=get_locale)

    # Make gettext available in all Jinja2 templates
    from flask_babel import gettext, ngettext
    app.jinja_env.globals.update(
        gettext=gettext,
        ngettext=ngettext,
        _=gettext
    )

    # Relative timestamps for feed cards: "now", "5m", "2h",
    # "Yesterday", weekday, or "Mar 4" — never the verbose absolute
    # date the old cards used.
    from datetime import datetime, timezone

    def _relative_time(value):
        if not value:
            return ''
        now = datetime.now(timezone.utc)
        dt = value if value.tzinfo is not None else value.replace(
            tzinfo=timezone.utc)
        seconds = (now - dt).total_seconds()
        if seconds < 60:
            return 'now'
        if seconds < 3600:
            return f"{int(seconds // 60)}m"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h"
        if seconds < 172800:
            return 'Yesterday'
        if seconds < 604800:
            return dt.strftime('%a')
        if dt.year == now.year:
            return dt.strftime('%b %-d')
        return dt.strftime('%b %-d, %Y')

    app.jinja_env.filters['relativetime'] = _relative_time

    # Warm Rainbow (2026-11-23): avatar placeholders rotate through 8
    # warm tones so the feed feels full of people. The tone is a stable
    # hash of the user id (md5, not Python's salted hash) so a user
    # keeps their colour across restarts and servers. Returns 0-7.
    import hashlib

    def _avatar_tone(user_id):
        digest = hashlib.md5(str(user_id or '').encode()).hexdigest()
        return int(digest, 16) % 8

    app.jinja_env.filters['avatar_tone'] = _avatar_tone

    # Phase 2: @mentions in post/comment text link to the profile.
    from utils.mentions import link_mentions
    app.jinja_env.filters['mention_links'] = link_mentions

    # Phase 2: trust-level / rank display labels. The level strings
    # are logic keys in Python (compared, not displayed); templates
    # translate them at render time through this filter.
    from utils.trust import level_label
    app.jinja_env.filters['level_label'] = level_label

    # Multi-currency: {{ amount|money(currency) }} renders an amount
    # as entered, e.g. CA$1,000.00 — no FX conversion anywhere.
    from utils.currency import format_money
    app.jinja_env.filters['money'] = format_money

    # Phase 3: the Lucide icon for a notification type, used by the
    # shared notification-item partial (bell panel + full page).
    from utils.notifications import notification_icon
    app.jinja_env.globals['notification_icon'] = notification_icon

    # Phase 3: trust visibility on cards. has_active_pledge drives
    # the pledge badge on board cards, bounty detail, and nested need
    # cards in the feed; poster_reputation drives the poster line on
    # board cards and the detail "Posted by" card. Both are cached
    # per request (pledges on flask.g; metrics memoize per id here).
    from utils.pledges import has_active_pledge
    from utils.reputation import reputation_metrics as _reputation_metrics
    app.jinja_env.globals['has_active_pledge'] = has_active_pledge

    def _cached_reputation_metrics(user_id):
        cache = g.get('_reputation_cache')
        if cache is None:
            cache = g._reputation_cache = {}
        if user_id not in cache:
            cache[user_id] = _reputation_metrics(user_id)
        return cache[user_id]

    app.jinja_env.globals['poster_reputation'] = _cached_reputation_metrics

    # Flask-Login configured in extensions.py

    @login_manager.user_loader
    def load_user(user_id):
        from models.user import User
        return db.session.get(User, user_id)

    @login_manager.unauthorized_handler
    def unauthorized():
        """Phase 1: feed interactions are AJAX. An AJAX request to a
        login-walled route gets a 401 JSON (the client redirects to
        login); plain browser navigation keeps the classic flash +
        redirect to the login page."""
        wants_json = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.accept_mimetypes.best == 'application/json'
        )
        if wants_json:
            return jsonify({'error': 'authentication required'}), 401
        flash(login_manager.login_message, login_manager.login_message_category)
        return redirect(url_for('auth.login', next=request.url))

    # Register blueprints
    from blueprints.auth import auth_bp
    from blueprints.profile import profile_bp
    from blueprints.bounties import bounties_bp
    from blueprints.bounties.legacy import legacy_bounties_bp
    from blueprints.messages import messages_bp
    from blueprints.admin import admin_bp
    from blueprints.social import social_bp
    from blueprints.referrals import referrals_bp
    from blueprints.payments import payments_bp
    from blueprints.pledge import pledge_bp
    from blueprints.video_vouches import video_vouches_bp
    from blueprints.intro_bounties import intro_bounties_bp
    from blueprints.staked_vouches import staked_vouches_bp
    from blueprints.wins import wins_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(bounties_bp, url_prefix='/needs')
    # Old /bounties/* URLs 301 to their /needs/* equivalents.
    app.register_blueprint(legacy_bounties_bp, url_prefix='/bounties')
    app.register_blueprint(messages_bp, url_prefix='/messages')
    app.register_blueprint(profile_bp, url_prefix='/profile')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(social_bp, url_prefix='/social')
    app.register_blueprint(referrals_bp, url_prefix='/referrals')
    app.register_blueprint(payments_bp)
    app.register_blueprint(pledge_bp, url_prefix='/pledge')
    app.register_blueprint(video_vouches_bp, url_prefix='/video-vouches')
    app.register_blueprint(intro_bounties_bp, url_prefix='/intros')
    app.register_blueprint(staked_vouches_bp, url_prefix='/stakes')
    app.register_blueprint(wins_bp, url_prefix='/wins')

    # M1: unread notification count for the navbar badge. Phase 3:
    # the bell panel also needs the latest items (with actor avatars),
    # server-rendered so the panel works before any JS runs.
    @app.context_processor
    def inject_notifications():
        from utils.notifications import (
            recent_notifications, unread_count,
        )
        if not current_user.is_authenticated:
            return {
                'unread_notifications': 0,
                'recent_notifications': [],
            }
        return {
            'unread_notifications': unread_count(current_user.id),
            'recent_notifications': recent_notifications(
                current_user.id, limit=10,
            ),
        }

    # Phase 2: the active locale for <html lang> and the switcher's
    # active-language indicator.
    @app.context_processor
    def inject_locale():
        return {'current_locale': get_locale()}

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

    # Phase 2: seed the topic spaces (models/space.py SEEDED_SPACES).
    # Idempotent — safe to re-run; only missing slugs are created.
    # Run once after `flask db upgrade` on a fresh deploy.
    @app.cli.command('seed-spaces')
    def seed_spaces_command():
        from models.space import seed_spaces
        created = seed_spaces()
        db.session.commit()
        if created:
            print(f"Seeded {len(created)} space(s): "
                  + ', '.join(s.slug for s in created))
        else:
            print('Spaces already seeded — nothing to do.')

    # Phase 3: weekly digest email (utils/digest.py). Recipients are
    # active users with a verified email. Mail is NOT configured on
    # the live container (no MAIL_* env vars), so the command runs
    # and reports 0 sent there until SMTP is fixed — expected, not a
    # bug; see DEPLOYMENT.md for the cron line.
    @app.cli.command('send-weekly-digest')
    def send_weekly_digest_command():
        from utils.digest import send_weekly_digest
        sent, skipped = send_weekly_digest()
        print(f'Weekly digest: {sent} sent, {skipped} skipped.')

    # Language switcher route
    @app.route('/set-language/<lang>')
    def set_language(lang):
        """Change language preference. The preference is stored in the
        session and, when logged in, on the profile (so it survives
        across devices). Returns the user to the page they were on:
        an explicit ?next= wins, then the referrer (same host only),
        else the homepage."""
        if lang in ['en', 'fr', 'es']:
            session['language'] = lang
            # Drop the legacy key (Phase 2 locale cleanup).
            session.pop('user_language', None)
            g._p2m_locale = lang
            if current_user.is_authenticated:
                profile = db.session.query(Profile).filter_by(
                    user_id=current_user.id
                ).first()
                if profile and profile.language != lang:
                    profile.language = lang
                    db.session.commit()
        target = request.args.get('next') or ''
        if not _is_safe_redirect(target):
            target = request.referrer or ''
            if not _is_safe_redirect(target):
                target = ''
        if target:
            return redirect(target)
        return redirect(url_for('index'))

    # PWA (Phase 2): the manifest and the service worker must be
    # served from the site root. A service worker under /static/
    # could only control /static/* — /sw.js controls the whole
    # origin. Both are network-only by design (the SW must never be
    # served stale, so it gets Cache-Control: no-cache).
    @app.route('/manifest.json')
    def manifest():
        return app.send_static_file('manifest.json'), 200, {
            'Content-Type': 'application/manifest+json',
        }

    @app.route('/sw.js')
    def service_worker():
        return app.send_static_file('sw.js'), 200, {
            'Content-Type': 'application/javascript',
            'Cache-Control': 'no-cache',
        }

    @app.route('/offline')
    def offline():
        """Offline fallback page — precached by the service worker and
        served from cache when a navigation fails."""
        return render_template('offline.html')

    @app.route('/help')
    def help_page():
        """Public help page — sign-in recovery and support contact."""
        return render_template('help.html')

    @app.route('/privacy')
    def privacy():
        """Public privacy policy page."""
        return render_template('legal/privacy.html')

    @app.route('/terms')
    def terms():
        """Public terms of service page."""
        return render_template('legal/terms.html')

    @app.route('/cookie-policy')
    def cookie_policy():
        """Public cookie policy page."""
        return render_template('legal/cookies.html')

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

        # Live paid-out counter: per-currency totals of completed
        # payouts, computed fresh on every request (not cached with
        # the stats above — the counter must move the moment a
        # payout completes). Three separate figures, never a blend.
        from utils.paid_out import paid_out_totals
        paid_out = paid_out_totals()

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
        # a signed-in viewer's blocked authors are excluded). Rendered
        # with the same post-card component as the feed, so the card
        # needs the same context (like/share state, trust levels).
        from models.post import Post
        from utils.feed import _visible_posts_query
        from blueprints.social.forms import ShareForm
        from blueprints.social.routes import (
            _author_levels, _author_profiles, _liked_and_shared_ids,
            _poll_votes, _saved_ids,
        )
        viewer_id = current_user.id if current_user.is_authenticated else None
        recent_posts = _visible_posts_query(viewer_id).order_by(
            Post.created_at.desc()
        ).limit(4).all()
        post_ids = [p.id for p in recent_posts]
        liked_ids, shared_ids = _liked_and_shared_ids(post_ids)

        return render_template('index.html',
                          paid_out=paid_out,
                          total_users=stats['total_users'],
                          total_bounties=stats['total_bounties'],
                          total_rewards=stats['total_rewards'],
                          successful_matches=stats['successful_matches'],
                          featured=featured,
                          top_helpers=top_helpers,
                          recent_posts=recent_posts,
                          liked_ids=liked_ids,
                          shared_ids=shared_ids,
                          saved_ids=_saved_ids(post_ids),
                          poll_votes=_poll_votes(post_ids),
                          author_levels=_author_levels(recent_posts),
                          author_profiles=_author_profiles(recent_posts),
                          share_form=ShareForm())

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
