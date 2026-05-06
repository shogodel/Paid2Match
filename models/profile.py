"""Profile model with LinkedIn-style fields."""
import json

from extensions import db
from models import generate_uuid, utc_now


class Profile(db.Model):
    """User profile model - LinkedIn style."""
    __tablename__ = 'profiles'

    VALID_PROFILE_TYPES = ['business', 'hr_professional', 'worker', 'real_estate', 'healthcare', 'independent']
    
    # Basic Info
    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    profile_type = db.Column(db.String(30), nullable=False)
    language = db.Column(db.String(10), default='en')
    display_name = db.Column(db.String(120), nullable=True)
    bio = db.Column(db.Text, nullable=True)
    location = db.Column(db.String(200), nullable=True)
    avatar_url = db.Column(db.String(500), nullable=True)
    reputation_score = db.Column(db.Integer, default=0)
    verified = db.Column(db.Boolean, default=False)
    company_name = db.Column(db.String(200), nullable=True)
    skills = db.Column(db.Text, nullable=True)
    website = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    # Privacy
    privacy_level = db.Column(db.Integer, default=0)  # 0=public, 1=limited, 2=private
    

    def __repr__(self):
        return f'<Profile {self.display_name or self.id}>'

    @property
    def completion_percentage(self):
        """Calculate profile completion percentage."""
        score = 0
        fields = [
            ('display_name', 15),
            ('headline', 10),
            ('bio', 15),
            ('location', 5),
            ('avatar_url', 10),
            ('skills', 10),
            ('website', 5),
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
            tips.append({'field': 'display_name', 'label': 'Add your name', 'priority': 'high'})
        if not self.headline:
            tips.append({'field': 'headline', 'label': 'Add a headline', 'priority': 'medium'})
        if not self.bio:
            tips.append({'field': 'bio', 'label': 'Write a bio', 'priority': 'high'})
        if not self.avatar_url:
            tips.append({'field': 'avatar_url', 'label': 'Upload a photo', 'priority': 'high'})
        if not self.skills:
            tips.append({'field': 'skills', 'label': 'Add skills', 'priority': 'medium'})
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
