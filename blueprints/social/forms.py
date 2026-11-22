"""Social forms (M1)."""
from flask_wtf import FlaskForm
from flask_wtf.file import FileField
from wtforms import (
    DecimalField, HiddenField, SelectField, StringField, SubmitField, TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional


class PostForm(FlaskForm):
    """Create/edit a feed post. Win posts are auto-generated from completed
    referrals, so they are not a choice here."""
    post_type = SelectField('Type', choices=[
        ('update', 'Update'),
        ('ask', 'Ask'),
        ('intro', 'Intro'),
        ('need_free', 'Community referral (free need)'),
        ('need_paid', 'Referral need (paid)'),
    ], validators=[DataRequired()])
    title = StringField('Title (for referral needs)', validators=[Optional(), Length(max=200)])
    body = TextAreaField("What's on your mind?", validators=[
        DataRequired(), Length(min=1, max=2000),
    ])
    photo = FileField('Photo (optional)')
    reward_amount = DecimalField('Bounty ($)', validators=[
        Optional(), NumberRange(min=0, max=100000),
    ])
    location = StringField('Location', validators=[Optional(), Length(max=200)])
    submit = SubmitField('Post')

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators)
        if self.post_type.data in ('need_free', 'need_paid'):
            if not (self.title.data or '').strip():
                self.title.errors.append('A title is required for a referral need.')
                ok = False
            elif len(self.title.data.strip()) < 10:
                self.title.errors.append('Title must be at least 10 characters.')
                ok = False
        if self.post_type.data == 'need_paid':
            if not self.reward_amount.data or self.reward_amount.data <= 0:
                self.reward_amount.errors.append('A paid need needs a bounty above $0.')
                ok = False
        return ok


class CommentForm(FlaskForm):
    """Comment on a post (or reply to a top-level comment)."""
    body = TextAreaField('Comment', validators=[
        DataRequired(), Length(min=1, max=1000),
    ])
    parent_id = HiddenField()
    submit = SubmitField('Comment')


class ShareForm(FlaskForm):
    """Reshare a post with an optional comment."""
    comment = TextAreaField('Add a comment (optional)', validators=[Optional(), Length(max=500)])
    submit = SubmitField('Share')


class ConvertForm(FlaskForm):
    """Turn a post into a referral need (§1.1, §4.1)."""
    need_kind = SelectField('Kind', choices=[
        ('free', 'Free — community referral'),
        ('paid', 'Paid — escrow a bounty'),
    ], validators=[DataRequired()])
    title = StringField('Title', validators=[DataRequired(), Length(min=10, max=200)])
    reward_amount = DecimalField('Bounty ($)', validators=[
        Optional(), NumberRange(min=0, max=100000),
    ])
    location = StringField('Location', validators=[Optional(), Length(max=200)])
    submit = SubmitField('Turn into a referral need')

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators)
        if self.need_kind.data == 'paid':
            if not self.reward_amount.data or self.reward_amount.data <= 0:
                self.reward_amount.errors.append('A paid need needs a bounty above $0.')
                ok = False
        return ok


class ReportForm(FlaskForm):
    """Report a post, comment, or profile (moderation v0)."""
    reason = SelectField('Reason', choices=[
        ('spam', 'Spam'),
        ('harassment', 'Harassment'),
        ('fake', 'Fake content'),
        ('scam', 'Scam'),
        ('other', 'Other'),
    ], validators=[DataRequired()])
    details = TextAreaField('Details (optional)', validators=[Optional(), Length(max=1000)])
    submit = SubmitField('Submit report')


class PledgeForm(FlaskForm):
    """Pledge human-reviewed hiring (M3, §3.4)."""
    company_name = StringField('Company name', validators=[
        DataRequired(), Length(min=2, max=200),
    ])
    website = StringField('Company website (optional)', validators=[
        Optional(), Length(max=500),
    ])
    submit = SubmitField('Publish my pledge')
