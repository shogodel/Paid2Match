"""Linked social accounts (M3, §2): verify you control an account
elsewhere, so the profile is a reputation page, not a claim.

Three verification methods, strongest first:
- OAuth (LinkedIn, GitHub): sign in with the other platform.
- Bio-link check (rel=me model): the user puts their Paid2Match
  profile URL in the other account's bio; we fetch that page and
  check for the link back.
- Unique code: the user posts a short code (P2M-XXXXX) in a bio or
  post; we check it by fetching the page — or, when the fetch is
  blocked (Instagram/TikTok often block server fetches), the founder
  checks it manually from the review queue.

Verification expires after 90 days (re-verification is a current
fact, not a lifetime award). A profile with 2+ verified links gets a
"Verified elsewhere" marker and +5 trust-score identity points (§3.1).

Law 25: linking is opt-in per link, revocable (unlinking deletes the
record), and the platform stores only the handle, platform,
verification state and dates — no tokens, no scraped data. Follower
counts are never stored and never feed the trust score.
"""
import secrets
import string

from extensions import db
from models import generate_uuid, utc_now

# How long a verification lasts before re-verification is due.
REVERIFY_DAYS = 90

# Platforms a profile can link (§2.1). X is stored as 'x' (the profile
# form collects a twitter_handle; the linked-account URL is the X URL).
PLATFORMS = [
    'linkedin', 'github', 'instagram', 'facebook',
    'tiktok', 'x', 'youtube', 'website',
]
PLATFORM_LABELS = {
    'linkedin': 'LinkedIn',
    'github': 'GitHub',
    'instagram': 'Instagram',
    'facebook': 'Facebook',
    'tiktok': 'TikTok',
    'x': 'X',
    'youtube': 'YouTube',
    'website': 'Website',
}
# OAuth is only realistic for LinkedIn + GitHub at this scale (§2.2) —
# Meta/TikTok/X OAuth needs app review; those go through bio-link or code.
OAUTH_PLATFORMS = ['linkedin', 'github']

# Unambiguous alphabet for verification codes (no 0/O, 1/I/L).
_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def generate_verification_code():
    """A short code the user posts on the other platform, e.g. P2M-7K2Q9."""
    return 'P2M-' + ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(5))


class LinkedAccount(db.Model):
    """One linked external account for one user."""
    __tablename__ = 'linked_accounts'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'platform', name='uq_linked_user_platform'),
    )

    STATUSES = ['pending', 'verified', 'expired', 'rejected']
    METHODS = ['oauth', 'bio_link', 'code', 'manual']
    VISIBILITIES = ['public', 'followers', 'hidden']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    platform = db.Column(db.String(20), nullable=False)
    handle = db.Column(db.String(200), nullable=True)
    url = db.Column(db.String(500), nullable=True)
    verification_method = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')
    verification_code = db.Column(db.String(20), nullable=True)
    # True when the user asked the founder to check manually (the fetch
    # failed or the platform blocks it). The admin queue lists these.
    manual_review = db.Column(db.Boolean, default=False)
    admin_note = db.Column(db.Text, nullable=True)
    # Per-link visibility (§2.4): hidden links still count for trust.
    visibility = db.Column(db.String(20), nullable=False, default='public')
    verified_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    user = db.relationship('User', backref='linked_accounts')

    def __repr__(self):
        return f'<LinkedAccount {self.user_id} {self.platform} {self.status}>'

    @property
    def platform_label(self):
        return PLATFORM_LABELS.get(self.platform, self.platform)

    @property
    def is_currently_verified(self):
        """Verified and not expired. Expiry is a current fact — a
        verified link whose 90 days are up no longer counts."""
        if self.status != 'verified':
            return False
        if self.expires_at is None:
            return True
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=utc_now().tzinfo)
        return utc_now() < expires

    def visible_to(self, viewer_id, viewer_follows=False):
        """Whether this link is shown to a viewer (§2.4)."""
        if viewer_id == self.user_id:
            return True
        if self.visibility == 'public':
            return True
        if self.visibility == 'followers':
            return viewer_follows
        return False  # hidden: trust only, never displayed
