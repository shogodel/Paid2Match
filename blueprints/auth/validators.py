"""Auth validators for Paid2Match."""
from wtforms.validators import Email, ValidationError


def validate_unique_email(form, field):
    """Validator to check unique email."""
    from models.user import User
    user = User.query.filter_by(email=field.data.lower()).first()
    if user:
        raise ValidationError('An account with this email already exists')