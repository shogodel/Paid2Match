"""Profile forms."""
from flask_wtf import FlaskForm
from flask_wtf.file import FileField
from wtforms import StringField, TextAreaField, SelectField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Length, Optional, URL
from wtforms.widgets import TextArea


class ProfileForm(FlaskForm):
    """Form for editing profile."""
    profile_type = SelectField(
        'Profile Type',
        choices=[
            ('business', 'Business'),
            ('hr_professional', 'HR Professional'),
            ('worker', 'Worker'),
            ('independent', 'Independent')
        ],
        validators=[DataRequired()]
    )
    display_name = StringField('Display Name', validators=[DataRequired(), Length(min=2, max=120)])
    headline = StringField('Headline', validators=[Optional(), Length(max=200)])
    bio = TextAreaField('Bio', validators=[Optional(), Length(max=2000)])
    location = StringField('Location', validators=[Optional(), Length(max=200)])
    company_name = StringField('Company Name', validators=[Optional(), Length(max=200)])
    skills = TextAreaField('Skills (comma-separated)', validators=[Optional(), Length(max=1000)])
    website = StringField('Website', validators=[Optional(), URL(), Length(max=500)])
    linkedin_url = StringField('LinkedIn URL', validators=[Optional(), URL(), Length(max=500)])
    github_url = StringField('GitHub URL', validators=[Optional(), URL(), Length(max=500)])
    twitter_handle = StringField('Twitter Handle', validators=[Optional(), Length(max=50)])
    avatar = FileField('Profile Picture')
    remote_ok = BooleanField('Open to Remote Work')
    relocation_assistance = BooleanField('Needs Relocation Assistance')
    privacy_level = SelectField(
        'Privacy Level',
        choices=[(0, 'Public'), (1, 'Limited'), (2, 'Private')],
        coerce=int
    )
    submit = SubmitField('Save Profile')


class SetupForm(FlaskForm):
    """Form for initial profile setup."""
    profile_type = SelectField(
        'I am a...',
        choices=[
            ('business', 'Business'),
            ('hr_professional', 'HR Professional'),
            ('worker', 'Worker'),
            ('independent', 'Independent')
        ],
        validators=[DataRequired()]
    )
    display_name = StringField('Display Name', validators=[DataRequired(), Length(min=2, max=120)])
    headline = StringField('Headline', validators=[Optional(), Length(max=200)])
    bio = TextAreaField('Bio', validators=[Optional(), Length(max=2000)])
    location = StringField('Location', validators=[Optional(), Length(max=200)])
    company_name = StringField('Company Name', validators=[Optional(), Length(max=200)])
    skills = TextAreaField('Skills (comma-separated)', validators=[Optional(), Length(max=1000)])
    website = StringField('Website', validators=[Optional(), URL(), Length(max=500)])
    linkedin_url = StringField('LinkedIn URL', validators=[Optional(), URL(), Length(max=500)])
    github_url = StringField('GitHub URL', validators=[Optional(), URL(), Length(max=500)])
    twitter_handle = StringField('Twitter Handle', validators=[Optional(), Length(max=50)])
    avatar = FileField('Profile Picture')
    remote_ok = BooleanField('Open to Remote Work')
    relocation_assistance = BooleanField('Needs Relocation Assistance')
    submit = SubmitField('Create Profile')
