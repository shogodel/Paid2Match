"""AdminSettings model."""
from extensions import db
from models import utc_now


class AdminSettings(db.Model):
    """Key-value store for admin settings (API keys, etc.)."""
    __tablename__ = 'admin_settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f'<AdminSettings {self.key}>'

    @classmethod
    def get(cls, key, default=None):
        """Get a setting value."""
        setting = cls.query.filter_by(key=key).first()
        return setting.value if setting else default

    @classmethod
    def set(cls, key, value):
        """Set a setting value."""
        setting = cls.query.filter_by(key=key).first()
        if setting:
            setting.value = value
        else:
            setting = cls(key=key, value=value)
            db.session.add(setting)
        db.session.commit()