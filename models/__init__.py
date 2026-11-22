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
from .post import Post, Like, Comment, Share
from .follow import Follow
from .notification import Notification
from .referral import Referral, KarmaEvent
from .moderation import Report, Block
from .profile_view import ProfileView
from .connect_account import ConnectAccount
from .payout import Payout
from .review import Review, maybe_reveal_reviews, revealed_reviews_for_user
from .verification import VerificationRequest
from .bump import Bump, BumpPackPurchase, Boost, RecurringBump
from .subscription import ProSubscription
from .linked_account import LinkedAccount
from .endorsement import Endorsement, Pledge
from .post_view import PostView, SearchAppearance

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
    'Post',
    'Like',
    'Comment',
    'Share',
    'Follow',
    'Notification',
    'Referral',
    'KarmaEvent',
    'Report',
    'Block',
    'ProfileView',
    'ConnectAccount',
    'Payout',
    'Review',
    'maybe_reveal_reviews',
    'revealed_reviews_for_user',
    'VerificationRequest',
    'Bump',
    'BumpPackPurchase',
    'Boost',
    'RecurringBump',
    'ProSubscription',
    'LinkedAccount',
    'Endorsement',
    'Pledge',
    'PostView',
    'SearchAppearance',
    'generate_uuid',
    'utc_now',
]