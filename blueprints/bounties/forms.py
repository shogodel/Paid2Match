"""Bounties forms for Paid2Match."""
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, DecimalField, DateField, SubmitField
from flask_babel import lazy_gettext as _l
from wtforms.validators import DataRequired, Length, NumberRange, Optional, ValidationError

from models.bounty import MIN_PAID_BOUNTY_CAD, Bounty


def _category_choices():
    """Union of every vertical's categories (keys are unique across
    verticals except 'other', which shares a label). The route
    validates that the chosen category belongs to the chosen
    vertical."""
    choices = [('', 'No category')]
    seen = set()
    for vertical in Bounty.VALID_BOUNTY_TYPES:
        for key, label in Bounty.CATEGORIES[vertical].items():
            if key in seen:
                continue
            seen.add(key)
            choices.append((key, label))
    return choices


class BountyForm(FlaskForm):
    """Form for creating a new bounty."""
    title = StringField(_l('Title'), validators=[
        DataRequired(message='Title is required'),
        Length(min=10, max=200, message='Title must be between 10 and 200 characters')
    ])
    # M4: two verticals are live — recruitment and services referrals.
    # The validator rejects anything else (Bounty.VALID_BOUNTY_TYPES).
    bounty_type = SelectField(_l('Vertical'), choices=[
        (t, Bounty.BOUNTY_TYPE_LABELS[t]) for t in Bounty.VALID_BOUNTY_TYPES
    ], validators=[DataRequired(message='Please select a vertical')])
    # validate_choice=False: the route validates the category against
    # the chosen vertical (a category valid for one vertical is not
    # valid for the other); the form only carries the value.
    category = SelectField(_l('Category'), choices=_category_choices(),
                           validate_choice=False)
    bounty_direction = SelectField(_l('Direction'), choices=[
        ('', _l('What are you doing?')),
        ('seeking_opportunity', _l('Seeking Opportunity')),
        ('offering_opportunity', _l('Offering Opportunity'))
    ], validators=[DataRequired(message='Please select a direction')])
    description = TextAreaField(_l('Description'), validators=[
        DataRequired(message='Description is required'),
        Length(min=50, message='Please provide at least 50 characters')
    ])
    reward_amount = DecimalField(_l('Reward Amount ($)'), validators=[
        NumberRange(min=0, max=100000, message='Reward must be between $0 and $100,000')
    ])
    # Phase 3 staged rewards (Paraform model — display only): optional
    # amounts per referral stage. No floor here — micro-rewards are
    # the point ($5 for a submitted referral). Payouts are NOT wired
    # (no live Stripe keys); completed stages are recorded as pending.
    stage_submit_reward = DecimalField(_l('Reward when a referral is submitted ($)'), validators=[
        Optional(), NumberRange(min=0, max=100000, message='Stage reward must be between $0 and $100,000')
    ])
    stage_interview_reward = DecimalField(_l('Reward when a candidate is interviewed ($)'), validators=[
        Optional(), NumberRange(min=0, max=100000, message='Stage reward must be between $0 and $100,000')
    ])
    stage_hire_reward = DecimalField(_l('Reward when a hire is made ($)'), validators=[
        Optional(), NumberRange(min=0, max=100000, message='Stage reward must be between $0 and $100,000')
    ])

    def validate_reward_amount(self, field):
        """Phase 3 bounty floor: a paid need states at least $50. A
        free need carries no reward at all (0/blank) — the floor never
        touches it. The rule is amount-based, so it holds regardless
        of the free-need checkbox (a checked box zeroes the reward
        before the model ever sees it)."""
        if field.data and 0 < field.data < MIN_PAID_BOUNTY_CAD:
            raise ValidationError(
                _l('Paid needs need a bounty of at least $%(floor)s — '
                   'or post it as a free community need ($0).',
                   floor=MIN_PAID_BOUNTY_CAD)
            )
    location = StringField(_l('Location'), validators=[
        Length(max=200, message='Location must be under 200 characters')
    ])
    deadline = DateField(_l('Deadline'))
    success_criteria = TextAreaField(_l('Success Criteria'))
    skills = StringField(_l('Skills (comma-separated)'))
    is_free = SubmitField(_l('Free Need (No payment required)'))
    submit = SubmitField(_l('Post Referral Need'))


class PitchForm(FlaskForm):
    """Form for submitting a pitch."""
    candidate_teaser = TextAreaField(_l('Why they fit'), validators=[
        DataRequired(message='Please write why they fit'),
        Length(min=50, max=1000, message='Referral must be between 50 and 1000 characters')
    ])
    submit = SubmitField(_l('Submit Referral'))
