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
from .post import Post, Like, Comment, Share, Bookmark
from .space import Space, SpaceMembership, SEEDED_SPACES, seed_spaces
from .poll import (
    Poll, PollOption, PollVote, POLL_MAX_OPTIONS, POLL_MIN_OPTIONS,
    POLL_OPTION_MAX_LENGTH, create_poll, parse_poll_options,
)
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
from .employer_pledge import EmployerPledge
from .post_view import PostView, SearchAppearance
from .staged_reward import StageRewardEvent
from .stripe_event import StripeEventLog
from .video_vouch import VideoVouch
from .intro_bounty import IntroBounty, IntroClaim, IntroRewardEvent
from .staked_vouch import StakedVouch, RESOLUTIONS
from .win_story import WinStory

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
    'Bookmark',
    'Space',
    'SpaceMembership',
    'SEEDED_SPACES',
    'seed_spaces',
    'Poll',
    'PollOption',
    'PollVote',
    'POLL_MAX_OPTIONS',
    'POLL_MIN_OPTIONS',
    'POLL_OPTION_MAX_LENGTH',
    'create_poll',
    'parse_poll_options',
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
    'EmployerPledge',
    'PostView',
    'SearchAppearance',
    'StageRewardEvent',
    'StripeEventLog',
    'VideoVouch',
    'IntroBounty',
    'IntroClaim',
    'IntroRewardEvent',
    'StakedVouch',
    'RESOLUTIONS',
    'WinStory',
    'generate_uuid',
    'utc_now',
]