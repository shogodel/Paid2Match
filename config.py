"""Application configuration."""
import os
from datetime import timedelta
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()


class Config:
    """Base configuration."""
    
    # Security
    SECRET_KEY = os.getenv('SECRET_KEY')
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600  # 1 hour (instead of None/infinite)
    WTF_CSRF_SSL_STRICT = True  # Only send cookie over HTTPS
    WTF_CSRF_HEADER_NAME = 'X-CSRFToken'  # For AJAX requests
    
    # Database
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 3600,
    }
    
    # Session — Flask's default signed-cookie sessions (Flask-Session
    # is not installed; there is no server-side session store, so no
    # SESSION_TYPE). Only the Flask-Login user id lives in the cookie.
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    # Flask-Login's remember cookie defaults to 365 days — cap it at
    # the same 7 days as the session, or "7-day sessions" would be a
    # fiction (OAuth login always sets remember=True).
    REMEMBER_COOKIE_DURATION = timedelta(days=7)
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # Upload
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')

    # Video vouches (30-second pitches/vouches). The upload route
    # raises the per-request content limit to VIDEO_VOUCH_MAX_BYTES
    # (the global 16MB cap stays for everything else). Default folder
    # is <instance>/uploads/video-vouches — on the live container
    # this MUST be a mounted volume or uploads are wiped on every
    # rebuild; see docs/VIDEO_VOUCH_UPLOADS.md.
    VIDEO_VOUCH_UPLOAD_FOLDER = os.getenv('VIDEO_VOUCH_UPLOAD_FOLDER') or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'instance', 'uploads', 'video-vouches',
    )
    VIDEO_VOUCH_MAX_SECONDS = 30
    VIDEO_VOUCH_MAX_BYTES = 50 * 1024 * 1024  # 50MB
    VIDEO_VOUCH_ALLOWED_EXTENSIONS = {'mp4', 'webm', 'mov'}
    
    # Redis (for caching/sessions)
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    
    # External APIs - loaded from .env
    # NOTE: there are deliberately NO STRIPE_* key settings here.
    # Stripe keys live in AdminSettings in the database (Admin >
    # Settings) and utils/payments.py reads them from there — env
    # vars named STRIPE_API_KEY / STRIPE_SECRET_KEY /
    # STRIPE_PUBLISHABLE_KEY / STRIPE_WEBHOOK_SECRET are dead and
    # setting them configures nothing. (They used to be read here
    # and misled operators into thinking payments were configured.)

    # Master switch for all Stripe/payment functionality (the kill-switch).
    # Set STRIPE_ENABLED=false in the environment to completely disable payments
    # (checkout, upgrades, refunds, Connect onboarding, and the webhook).
    # M2: when False, paid features FAIL CLOSED — they are unavailable with a
    # clear message, never free (the old free-passthrough branches are gone).
    # Manual Interac payout recording still works while disabled — that is the
    # interim path for paying referrers before Connect is approved. Default
    # True = payments active. Keys live in AdminSettings (STRIPE_PUBLISHABLE_KEY
    # / STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET).
    STRIPE_ENABLED = os.getenv('STRIPE_ENABLED', 'true').lower() == 'true'
    
    AUTH0_DOMAIN = os.getenv('AUTH0_DOMAIN')
    AUTH0_CLIENT_ID = os.getenv('AUTH0_CLIENT_ID')
    AUTH0_CLIENT_SECRET = os.getenv('AUTH0_CLIENT_SECRET')
    AUTH0_CALLBACK_URL = os.getenv('AUTH0_CALLBACK_URL')

    # Linked-account verification OAuth (M3, §2.2): LinkedIn + GitHub.
    # From the environment only — never hardcoded, never in the DB.
    # When unset, OAuth is unavailable and bio-link/code verification
    # is the route (the UI hides the OAuth buttons).
    LINKEDIN_CLIENT_ID = os.getenv('LINKEDIN_CLIENT_ID')
    LINKEDIN_CLIENT_SECRET = os.getenv('LINKEDIN_CLIENT_SECRET')

    # Kill-switch for the LinkedIn button, mirroring
    # FACEBOOK_LOGIN_ENABLED: the LinkedIn app needs a Company Page
    # (which needs an account a day old with 2 connections), so the
    # button only renders when this is true. Set
    # LINKEDIN_LOGIN_ENABLED=true in the environment (and recreate
    # the container) once the app exists and sign-in actually works.
    # The /auth/linkedin/* routes keep working regardless — this flag
    # only controls the button's visibility.
    LINKEDIN_LOGIN_ENABLED = os.getenv(
        'LINKEDIN_LOGIN_ENABLED', 'false').lower() in ('1', 'true', 'yes')
    GITHUB_CLIENT_ID = os.getenv('GITHUB_CLIENT_ID')
    GITHUB_CLIENT_SECRET = os.getenv('GITHUB_CLIENT_SECRET')

    # Google sign-in ("Continue with Google") — OAuth 2.0 client from
    # Google Cloud Console (APIs & Services > Credentials). From the
    # environment only — never hardcoded, never in the DB. When unset,
    # the Google button is hidden and /auth/google/* redirects to login.
    GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
    GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')

    # Facebook sign-in ("Continue with Facebook") — OAuth 2.0 client
    # from the Meta app (developers.facebook.com, Facebook Login
    # product, scopes public_profile + email). From the environment
    # only — never hardcoded, never in the DB. When unset, the Facebook
    # button is hidden and /auth/facebook/* redirects to login.
    FACEBOOK_CLIENT_ID = os.getenv('FACEBOOK_CLIENT_ID')
    FACEBOOK_CLIENT_SECRET = os.getenv('FACEBOOK_CLIENT_SECRET')

    # Kill-switch for the Facebook button: the Meta app stays in
    # Development mode until business verification passes, and while
    # it does, strangers hitting "Continue with Facebook" get a
    # dev-mode error. The button only renders when this is true.
    # Set FACEBOOK_LOGIN_ENABLED=true in the environment (and
    # recreate the container) once the app is Live. The
    # /auth/facebook/* routes keep working regardless — this flag
    # only controls the button's visibility.
    FACEBOOK_LOGIN_ENABLED = os.getenv(
        'FACEBOOK_LOGIN_ENABLED', 'false').lower() in ('1', 'true', 'yes')
    
    MAPBOX_ACCESS_TOKEN = os.getenv('MAPBOX_ACCESS_TOKEN')
    
    # Admin settings
    ADMIN_API_KEY = os.getenv('ADMIN_API_KEY')
    ADMIN_TOKEN_EXPIRY = 3600  # 1 hour

    # The transaction fee added to every paid need at secure time
    # (reward + 3% is what Stripe actually charges). One constant —
    # the old code hardcoded 0.03 in three places and the close page
    # hardcoded 3.0 in the template, so a rate change silently
    # diverged the charge, the match record and the refund quote.
    # (A second constant, REFUND_TRANSACTION_FEE_PERCENT, used to
    # live here too — it was never read anywhere, so it was removed
    # rather than risk a future edit changing the wrong one.)
    TRANSACTION_FEE_PERCENT = 3.0
    STRIPE_FEE_PERCENT = 2.9
    STRIPE_FEE_FIXED = 0.30

    # Email — Resend HTTPS API first (RESEND_API_KEY), SMTP
    # (Flask-Mail) as fallback; see EMAIL_SETUP.md. If neither is
    # set, the app runs without sending mail: signup still works, but
    # notification/digest emails are skipped (and logged), and an
    # admin must verify accounts manually until mail is configured.
    # RESEND_API_KEY switches sending to the Resend HTTPS API instead of
    # SMTP — required on hosts where outbound SMTP ports are blocked
    # (DigitalOcean blocks 25/465/587/2525 on this droplet).
    RESEND_API_KEY = os.getenv('RESEND_API_KEY')
    MAIL_SERVER = os.getenv('MAIL_SERVER')
    MAIL_PORT = int(os.getenv('MAIL_PORT', '587'))
    MAIL_USE_TLS = os.getenv('MAIL_USE_TLS', 'true').lower() == 'true'
    MAIL_USE_SSL = os.getenv('MAIL_USE_SSL', 'false').lower() == 'true'
    MAIL_USERNAME = os.getenv('MAIL_USERNAME')
    MAIL_PASSWORD = os.getenv('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.getenv('MAIL_DEFAULT_SENDER')
    MAIL_SUPPRESS_SEND = False


class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG = True
    TESTING = False
    SQLALCHEMY_ECHO = False
    SESSION_COOKIE_SECURE = False
    # Detailed error messages for debugging
    PREFERRED_URL_SCHEME = 'http'
    

class StagingConfig(Config):
    """Staging configuration."""
    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = True
    # Logging enabled, errors shown but not detailed internals
    PREFERRED_URL_SCHEME = 'https'


class ProductionConfig(Config):
    """Production configuration."""
    DEBUG = False
    TESTING = False
    SESSION_COOKIE_SECURE = True
    # No detailed error messages
    PREFERRED_URL_SCHEME = 'https'


class TestingConfig(Config):
    """Testing configuration."""
    DEBUG = True
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv('TEST_DATABASE_URL', 'sqlite:///:memory:')
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    # Never send real mail in tests; Flask-Mail's record_messages() captures it.
    # MAIL_SERVER must be set (any value) so the send path runs at all.
    MAIL_SERVER = 'localhost'
    MAIL_SUPPRESS_SEND = True
    MAIL_DEFAULT_SENDER = 'test@paid2match.local'


config_by_name = {
    'development': DevelopmentConfig,
    'staging': StagingConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
