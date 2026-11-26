"""Social forms (M1)."""
from flask_wtf import FlaskForm
from flask_babel import lazy_gettext as _l
from flask_wtf.file import FileField
from wtforms import (
    DecimalField, HiddenField, SelectField, StringField, SubmitField, TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional, URL

from models.bounty import MIN_PAID_BOUNTY_CAD


class PostForm(FlaskForm):
    """Create/edit a feed post. Win posts are auto-generated from completed
    referrals, so they are not a choice here."""
    post_type = SelectField(_l('Type'), choices=[
        ('update', _l('Discussion')),
        ('ask', _l('Ask')),
        ('intro', _l('Intro')),
        ('available', _l('Available')),
        ('need_free', _l('Community referral (free need)')),
        ('need_paid', _l('Referral need (paid)')),
    ], validators=[DataRequired()])
    title = StringField(_l('Title (for referral needs)'), validators=[Optional(), Length(max=200)])
    body = TextAreaField(_l("What's on your mind?"), validators=[
        DataRequired(), Length(min=1, max=2000),
    ])
    photo = FileField(_l('Photo (optional)'))
    reward_amount = DecimalField(_l('Bounty ($)'), validators=[
        Optional(), NumberRange(min=0, max=100000),
    ])
    location = StringField(_l('Location'), validators=[Optional(), Length(max=200)])
    # Phase 1 composer: attach one of your open opportunities to a
    # plain post (the bounty card renders nested inside the post).
    # Validated in the route (must be yours and open).
    bounty_id = HiddenField(validators=[Optional()])
    # Phase 2: the topic space the post belongs to (blank = general
    # feed). Validated in the route (must exist).
    space_id = HiddenField(validators=[Optional()])
    # Phase 2: poll options, one per line (blank = no poll).
    poll_options = TextAreaField(_l('Poll options (one per line)'), validators=[
        Optional(), Length(max=700),
    ])
    submit = SubmitField(_l('Post'))

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
            # Phase 3 bounty floor: a paid need states at least $50
            # (MIN_PAID_BOUNTY_CAD); a free need is need_free instead.
            if not self.reward_amount.data or self.reward_amount.data < MIN_PAID_BOUNTY_CAD:
                self.reward_amount.errors.append(
                    str(_l('A paid need needs a bounty of at least $%(floor)s.',
                           floor=MIN_PAID_BOUNTY_CAD))
                )
                ok = False
        return ok


class CommentForm(FlaskForm):
    """Comment on a post (or reply to a top-level comment)."""
    body = TextAreaField(_l('Comment'), validators=[
        DataRequired(), Length(min=1, max=1000),
    ])
    parent_id = HiddenField()
    submit = SubmitField(_l('Comment'))


class ShareForm(FlaskForm):
    """Reshare a post with an optional comment."""
    comment = TextAreaField(_l('Add a comment (optional)'), validators=[Optional(), Length(max=500)])
    submit = SubmitField(_l('Share'))


class ConvertForm(FlaskForm):
    """Turn a post into a referral need (§1.1, §4.1)."""
    need_kind = SelectField(_l('Kind'), choices=[
        ('free', _l('Free — community referral')),
        ('paid', _l('Paid — escrow a bounty')),
    ], validators=[DataRequired()])
    title = StringField(_l('Title'), validators=[DataRequired(), Length(min=10, max=200)])
    reward_amount = DecimalField(_l('Bounty ($)'), validators=[
        Optional(), NumberRange(min=0, max=100000),
    ])
    location = StringField(_l('Location'), validators=[Optional(), Length(max=200)])
    submit = SubmitField(_l('Turn into a referral need'))

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators)
        if self.need_kind.data == 'paid':
            # Phase 3 bounty floor (see PostForm above).
            if not self.reward_amount.data or self.reward_amount.data < MIN_PAID_BOUNTY_CAD:
                self.reward_amount.errors.append(
                    str(_l('A paid need needs a bounty of at least $%(floor)s.',
                           floor=MIN_PAID_BOUNTY_CAD))
                )
                ok = False
        return ok


class ReportForm(FlaskForm):
    """Report a post, comment, or profile (moderation v0)."""
    reason = SelectField(_l('Reason'), choices=[
        ('spam', _l('Spam')),
        ('harassment', _l('Harassment')),
        ('fake', _l('Fake content')),
        ('scam', _l('Scam')),
        ('other', _l('Other')),
    ], validators=[DataRequired()])
    details = TextAreaField(_l('Details (optional)'), validators=[Optional(), Length(max=1000)])
    submit = SubmitField(_l('Submit report'))


class PledgeForm(FlaskForm):
    """Pledge human-reviewed hiring (M3, §3.4)."""
    company_name = StringField(_l('Company name'), validators=[
        DataRequired(), Length(min=2, max=200),
    ])
    website = StringField(_l('Company website (optional)'), validators=[
        # URL() requires an http(s) scheme: without it a
        # 'javascript:...' website would render as a clickable link
        # on the public pledge register (stored XSS on click).
        Optional(), URL(), Length(max=500),
    ])
    submit = SubmitField(_l('Publish my pledge'))
