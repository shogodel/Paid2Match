"""AuditLog model for tracking admin actions."""
from datetime import datetime, timezone
from extensions import db


class AuditLog(db.Model):
    """Track admin actions for security and compliance."""
    __tablename__ = 'audit_logs'

    id = db.Column(db.String(36), primary_key=True)
    admin_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    target_type = db.Column(db.String(50), nullable=False)
    target_id = db.Column(db.String(36), nullable=False)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    admin = db.relationship('User', foreign_keys=[admin_id])

    def __repr__(self):
        return f'<AuditLog {self.action} on {self.target_type}:{self.target_id}>'

    @staticmethod
    def log(admin_id, action, target_type, target_id, details=None, ip_address=None, user_agent=None):
        """Create an audit log entry."""
        import uuid
        log_entry = AuditLog(
            id=str(uuid.uuid4()),
            admin_id=admin_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
            ip_address=ip_address,
            user_agent=user_agent
        )
        db.session.add(log_entry)
        db.session.commit()
        return log_entry