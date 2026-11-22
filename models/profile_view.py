"""Profile views (M1 analytics, §1.4): who viewed a profile, when.

The free tier shows the COUNT of profile views in the last 7 days — never
viewer names (names are Pro, M3, and opt-in on the viewer's side). Views
by the profile owner themself are not recorded.
"""
from extensions import db
from models import generate_uuid, utc_now


class ProfileView(db.Model):
    """One profile view event."""
    __tablename__ = 'profile_views'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    profile_user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    viewer_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utc_now, index=True)

    def __repr__(self):
        return f'<ProfileView {self.viewer_id} -> {self.profile_user_id}>'
