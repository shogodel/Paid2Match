"""Flask extensions - shared instance to avoid circular imports."""
from flask import abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_bcrypt import Bcrypt
from flask_babel import Babel
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
bcrypt = Bcrypt()
babel = Babel()
csrf = CSRFProtect()

# Configure LoginManager defaults
login_manager.login_view = 'auth.login'
login_manager.login_message_category = 'info'
login_manager.login_message = 'Please log in to access this page.'


def get_or_404(model, id):
    """Get an object by ID or raise 404 - SQLAlchemy 2.0 compatible."""
    obj = db.session.get(model, id)
    if obj is None:
        abort(404)
    return obj