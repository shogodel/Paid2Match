"""Models package for Paid2Match."""
import uuid
from datetime import datetime, timezone


def generate_uuid():
    """Generate a UUID string."""
    return str(uuid.uuid4())


def utc_now():
    """Return current UTC time - SQLAlchemy 2.0 compatible."""
    return datetime.now(timezone.utc)


from .user import User
from .profile import Profile
from .bounty import Bounty
from .pitch import Pitch
from .match import Match
from .message import Message
from .admin_settings import AdminSettings
from .dispute import Dispute
from .bounty_payment_agreement import BountyPaymentAgreement
from .audit_log import AuditLog

__all__ = [
    'User',
    'Profile',
    'Bounty',
    'Pitch',
    'Match',
    'Message',
    'AdminSettings',
    'Dispute',
    'BountyPaymentAgreement',
    'AuditLog',
    'generate_uuid',
    'utc_now',
]