"""Payments blueprint — Connect onboarding + payout dashboard (M2)."""
from flask import Blueprint

payments_bp = Blueprint('payments', __name__, url_prefix='/payments')

from blueprints.payments import routes  # noqa: E402,F401
