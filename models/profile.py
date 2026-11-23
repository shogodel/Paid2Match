"""Profile model with LinkedIn-style fields."""
import json

from extensions import db
from flask_babel import lazy_gettext as _l
from models import generate_uuid, utc_now


class Profile(db.Model):
    """User profile model - LinkedIn style."""
    __tablename__ = 'profiles'

    VALID_PROFILE_TYPES = ['business', 'hr_professional', 'worker', 'independent']
    
    # Basic Info
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    profile_type = db.Column(db.String(30), nullable=False)
    language = db.Column(db.String(10), default='en')
    display_name = db.Column(db.String(120), nullable=True)
    headline = db.Column(db.String(200), nullable=True)
    bio = db.Column(db.Text, nullable=True)
    location = db.Column(db.String(200), nullable=True)
    avatar_url = db.Column(db.String(500), nullable=True)
    reputation_score = db.Column(db.Integer, default=0)
    verified = db.Column(db.Boolean, default=False)
    company_name = db.Column(db.String(200), nullable=True)
    skills = db.Column(db.Text, nullable=True)
    website = db.Column(db.String(500), nullable=True)
    linkedin_url = db.Column(db.String(500), nullable=True)
    github_url = db.Column(db.String(500), nullable=True)
    twitter_handle = db.Column(db.String(50), nullable=True)
    # M1: full social-link field set (§2.1). Verification is M3 — links
    # added here show as "not verified" until then.
    instagram_url = db.Column(db.String(500), nullable=True)
    facebook_url = db.Column(db.String(500), nullable=True)
    tiktok_url = db.Column(db.String(500), nullable=True)
    youtube_url = db.Column(db.String(500), nullable=True)
    # M1: karma + helped counter (§4.2). karma is a cached sum of the last
    # 12 months of KarmaEvents (see utils/karma.py); helped_count is the
    # public "Helped N people" counter; free_bumps banks 1 free bump per
    # completed referral (§1.3) for M3 to spend.
    karma = db.Column(db.Integer, default=0)
    helped_count = db.Column(db.Integer, default=0)
    free_bumps = db.Column(db.Integer, default=0)
    remote_ok = db.Column(db.Boolean, default=False)
    relocation_assistance = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    # Privacy
    privacy_level = db.Column(db.Integer, default=0)  # 0=public, 1=limited, 2=private
    # M3: opt-in — may Pro users see your NAME in their 90-day profile
    # viewer list? Default off: names are never shown without consent
    # (§1.4 design rules). Counts are always anonymous-safe.
    profile_view_names = db.Column(db.Boolean, default=False)
    

    def __repr__(self):
        return f'<Profile {self.display_name or self.id}>'

    def to_dict(self):
        """Serialize the profile (mirrors other models' to_dict)."""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'profile_type': self.profile_type,
            'display_name': self.display_name,
            'headline': self.headline,
            'bio': self.bio,
            'location': self.location,
            'company_name': self.company_name,
            'skills': self.skills,
            'website': self.website,
            'linkedin_url': self.linkedin_url,
            'github_url': self.github_url,
            'twitter_handle': self.twitter_handle,
            'instagram_url': self.instagram_url,
            'facebook_url': self.facebook_url,
            'tiktok_url': self.tiktok_url,
            'youtube_url': self.youtube_url,
            'karma': self.karma,
            'helped_count': self.helped_count,
            'free_bumps': self.free_bumps,
            'remote_ok': self.remote_ok,
            'relocation_assistance': self.relocation_assistance,
            'avatar_url': self.avatar_url,
            'reputation_score': self.reputation_score,
            'verified': self.verified,
            'privacy_level': self.privacy_level,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    @property
    def completion_percentage(self):
        """Calculate profile completion percentage (weights sum to 100)."""
        score = 0
        fields = [
            ('display_name', 15),
            ('headline', 10),
            ('bio', 15),
            ('location', 10),
            ('avatar_url', 15),
            ('skills', 15),
            ('website', 5),
            ('linkedin_url', 5),
            ('github_url', 5),
            ('twitter_handle', 5),
        ]
        for field, weight in fields:
            if getattr(self, field, None):
                score += weight
        return min(score, 100)

    @property
    def completion_tips(self):
        """Return tips to complete profile."""
        tips = []
        if not self.display_name:
            tips.append({'field': 'display_name', 'label': _l('Add your name'), 'priority': 'high'})
        if not self.headline:
            tips.append({'field': 'headline', 'label': _l('Add a headline'), 'priority': 'medium'})
        if not self.bio:
            tips.append({'field': 'bio', 'label': _l('Write a bio'), 'priority': 'high'})
        if not self.avatar_url:
            tips.append({'field': 'avatar_url', 'label': _l('Upload a photo'), 'priority': 'high'})
        if not self.skills:
            tips.append({'field': 'skills', 'label': _l('Add skills'), 'priority': 'medium'})
        return tips

    @property
    def gamification_stats(self):
        """Return gamification stats."""
        points = self.reputation_score or 0
        return {
            'total_points': points,
            'level': min(points // 100 + 1, 10),
            'rank': 'Newcomer' if points < 50 else 'Explorer' if points < 150 else 'Contributor'
        }
