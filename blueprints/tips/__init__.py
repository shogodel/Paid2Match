"""Tips blueprint — send money to a user."""
from flask import Blueprint

tips_bp = Blueprint('tips', __name__, url_prefix='/tips')

from blueprints.tips import routes  # noqa: E402,F401
