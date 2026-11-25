"""Profile forms."""
from flask_wtf import FlaskForm
from flask_babel import lazy_gettext as _l
from flask_wtf.file import FileField
from wtforms import StringField, TextAreaField, SelectField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Length, Optional, Regexp, URL, ValidationError
from wtforms.widgets import TextArea


#: Shared by the handle field's Regexp message and the setup
#: template's hint — one msgid for both.
HANDLE_RULES = _l(
    'Handles use 3–30 lowercase letters, numbers, dots, dashes '
    'or underscores.'
)


class ProfileForm(FlaskForm):
    """Form for editing profile."""
    profile_type = SelectField(_l('Profile Type'),
        choices=[
            ('business', _l('Business')),
            ('hr_professional', _l('HR Professional')),
            ('worker', _l('Worker')),
            ('independent', _l('Independent'))
        ],
        validators=[DataRequired()]
    )
    display_name = StringField(_l('Display Name'), validators=[DataRequired(), Length(min=2, max=120)])
    headline = StringField(_l('Headline'), validators=[Optional(), Length(max=200)])
    bio = TextAreaField(_l('Bio'), validators=[Optional(), Length(max=2000)])
    location = StringField(_l('Location'), validators=[Optional(), Length(max=200)])
    company_name = StringField(_l('Company Name'), validators=[Optional(), Length(max=200)])
    skills = TextAreaField(_l('Skills (comma-separated)'), validators=[Optional(), Length(max=1000)])
    website = StringField(_l('Website'), validators=[Optional(), URL(), Length(max=500)])
    linkedin_url = StringField(_l('LinkedIn URL'), validators=[Optional(), URL(), Length(max=500)])
    github_url = StringField(_l('GitHub URL'), validators=[Optional(), URL(), Length(max=500)])
    instagram_url = StringField(_l('Instagram URL'), validators=[Optional(), URL(), Length(max=500)])
    facebook_url = StringField(_l('Facebook URL'), validators=[Optional(), URL(), Length(max=500)])
    tiktok_url = StringField(_l('TikTok URL'), validators=[Optional(), URL(), Length(max=500)])
    youtube_url = StringField(_l('YouTube URL'), validators=[Optional(), URL(), Length(max=500)])
    twitter_handle = StringField(_l('Twitter Handle'), validators=[Optional(), Length(max=50)])
    avatar = FileField(_l('Profile Picture'))
    remote_ok = BooleanField(_l('Open to Remote Work'))
    relocation_assistance = BooleanField(_l('Needs Relocation Assistance'))
    # M3: opt-in — Pro users may see your name in their 90-day profile
    # viewer list. Off by default (§1.4: never identify viewers
    # without their consent).
    profile_view_names = BooleanField(_l('Let Pro users see my name when I view their profile')
    )
    privacy_level = SelectField(_l('Privacy Level'),
        choices=[(0, 'Public'), (1, 'Limited'), (2, 'Private')],
        coerce=int
    )
    submit = SubmitField(_l('Save Profile'))


class SetupForm(FlaskForm):
    """Form for initial profile setup."""
    profile_type = SelectField(_l('I am a...'),
        choices=[
            ('business', _l('Business')),
            ('hr_professional', _l('HR Professional')),
            ('worker', _l('Worker')),
            ('independent', _l('Independent'))
        ],
        validators=[DataRequired()]
    )
    display_name = StringField(_l('Display Name'), validators=[DataRequired(), Length(min=2, max=120)])
    # Nice URLs: the handle is the /profile/<handle> address. The
    # route prefills the generated suggestion; blank means "keep my
    # current handle" (a handle was already generated at sign-up),
    # so the field is Optional — format and uniqueness are only
    # checked when the user types something.
    handle = StringField(_l('Profile handle'), validators=[
        Optional(),
        Regexp(r'^[a-z0-9._-]{3,30}$', message=HANDLE_RULES),
    ])
    headline = StringField(_l('Headline'), validators=[Optional(), Length(max=200)])
    bio = TextAreaField(_l('Bio'), validators=[Optional(), Length(max=2000)])
    location = StringField(_l('Location'), validators=[Optional(), Length(max=200)])
    company_name = StringField(_l('Company Name'), validators=[Optional(), Length(max=200)])
    skills = TextAreaField(_l('Skills (comma-separated)'), validators=[Optional(), Length(max=1000)])
    website = StringField(_l('Website'), validators=[Optional(), URL(), Length(max=500)])
    linkedin_url = StringField(_l('LinkedIn URL'), validators=[Optional(), URL(), Length(max=500)])
    github_url = StringField(_l('GitHub URL'), validators=[Optional(), URL(), Length(max=500)])
    instagram_url = StringField(_l('Instagram URL'), validators=[Optional(), URL(), Length(max=500)])
    facebook_url = StringField(_l('Facebook URL'), validators=[Optional(), URL(), Length(max=500)])
    tiktok_url = StringField(_l('TikTok URL'), validators=[Optional(), URL(), Length(max=500)])
    youtube_url = StringField(_l('YouTube URL'), validators=[Optional(), URL(), Length(max=500)])
    twitter_handle = StringField(_l('Twitter Handle'), validators=[Optional(), Length(max=50)])
    avatar = FileField(_l('Profile Picture'))
    remote_ok = BooleanField(_l('Open to Remote Work'))
    relocation_assistance = BooleanField(_l('Needs Relocation Assistance'))
    submit = SubmitField(_l('Create Profile'))

    def validate_handle(self, field):
        """Uniqueness + reserved check (format is the Regexp
        above). Optional() skips this validator when the field is
        blank."""
        from flask_login import current_user
        from models.user import User
        from utils.slugs import is_reserved_handle
        if not field.data:
            return
        handle = field.data.strip()
        if is_reserved_handle(handle):
            raise ValidationError(_l('That handle is reserved.'))
        existing = User.query.filter_by(handle=handle).first()
        if existing and existing.id != current_user.id:
            raise ValidationError(_l('That handle is already taken.'))


class LinkedAccountForm(FlaskForm):
    """Link a social account for bio-link or code verification (M3, §2)."""
    url = StringField(_l('Your profile URL on that platform'), validators=[
        DataRequired(), URL(), Length(max=500),
    ])
    handle = StringField(_l('Handle (optional)'), validators=[Optional(), Length(max=200)])
    method = SelectField(_l('How to verify'), choices=[
        ('bio_link', _l('Bio link — I put my Paid2Match link in my bio')),
        ('code', _l('Unique code — I post a code on that platform')),
    ], validators=[DataRequired()])
    visibility = SelectField(_l('Who can see this link'), choices=[
        ('public', _l('Everyone')),
        ('followers', _l('My followers only')),
        ('hidden', _l('Hidden (counts for trust, not displayed)')),
    ], validators=[DataRequired()])
    submit = SubmitField(_l('Add link'))


class EndorseForm(FlaskForm):
    """Endorse someone for a skill (M3, §3.3)."""
    skill = StringField(_l('Skill'), validators=[DataRequired(), Length(min=2, max=100)])
    comment = TextAreaField(_l('Why? (optional)'), validators=[Optional(), Length(max=500)])
    submit = SubmitField(_l('Endorse'))
