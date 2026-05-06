"""Auth forms for Paid2Match."""
import re
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email, Length, EqualTo, ValidationError

from models.user import User


class LoginForm(FlaskForm):
    """Login form with CSRF protection."""
    email = StringField('Email', validators=[
        DataRequired(message='Email is required'),
        Email(message='Please enter a valid email address')
    ])
    password = PasswordField('Password', validators=[
        DataRequired(message='Password is required')
    ])
    remember = SubmitField('Remember Me')
    submit = SubmitField('Login')


class RegistrationForm(FlaskForm):
    """Registration form with CSRF protection."""
    full_name = StringField('Full Name', validators=[
        DataRequired(message='Full name is required'),
        Length(min=2, max=120, message='Name must be between 2 and 120 characters')
    ])
    email = StringField('Email', validators=[
        DataRequired(message='Email is required'),
        Email(message='Please enter a valid email address')
    ])
    password = PasswordField('Password', validators=[
        DataRequired(message='Password is required'),
        Length(min=8, max=128, message='Password must be at least 8 characters'),
    ])
    confirm_password = PasswordField('Confirm Password', validators=[
        DataRequired(message='Please confirm your password'),
        EqualTo('password', message='Passwords must match')
    ])
    submit = SubmitField('Create Account')

    def validate_password(self, field):
        """Validate password strength."""
        password = field.data
        errors = []
        if len(password) < 8:
            errors.append('at least 8 characters')
        if not re.search(r'[A-Z]', password):
            errors.append('an uppercase letter')
        if not re.search(r'[a-z]', password):
            errors.append('a lowercase letter')
        if not re.search(r'\d', password):
            errors.append('a digit')
        if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            errors.append('a special character')
        if errors:
            raise ValidationError(f'Password must contain: {", ".join(errors)}')

    def validate_email(self, email):
        """Check if email already exists in database."""
        if email.data:
            user = User.query.filter_by(email=email.data.lower()).first()
            if user:
                raise ValidationError('An account with this email already exists')