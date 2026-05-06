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
    
    # Session
    SESSION_TYPE = 'filesystem'
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # Upload
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
    
    # Redis (for caching/sessions)
    REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
    
    # External APIs - loaded from .env
    STRIPE_API_KEY = os.getenv('STRIPE_API_KEY')
    STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY')
    STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
    
    AUTH0_DOMAIN = os.getenv('AUTH0_DOMAIN')
    AUTH0_CLIENT_ID = os.getenv('AUTH0_CLIENT_ID')
    AUTH0_CLIENT_SECRET = os.getenv('AUTH0_CLIENT_SECRET')
    AUTH0_CALLBACK_URL = os.getenv('AUTH0_CALLBACK_URL')
    
    MAPBOX_ACCESS_TOKEN = os.getenv('MAPBOX_ACCESS_TOKEN')
    
    # Admin settings
    ADMIN_API_KEY = os.getenv('ADMIN_API_KEY')
    ADMIN_TOKEN_EXPIRY = 3600  # 1 hour

    # Refund fees
    REFUND_TRANSACTION_FEE_PERCENT = 3.0
    STRIPE_FEE_PERCENT = 2.9
    STRIPE_FEE_FIXED = 0.30


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


config_by_name = {
    'development': DevelopmentConfig,
    'staging': StagingConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
