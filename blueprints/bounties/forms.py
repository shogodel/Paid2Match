"""Bounties forms for Paid2Match."""
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, DecimalField, DateField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange

from models.bounty import Bounty


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
    title = StringField('Title', validators=[
        DataRequired(message='Title is required'),
        Length(min=10, max=200, message='Title must be between 10 and 200 characters')
    ])
    # M4: two verticals are live — recruitment and services referrals.
    # The validator rejects anything else (Bounty.VALID_BOUNTY_TYPES).
    bounty_type = SelectField('Vertical', choices=[
        (t, Bounty.BOUNTY_TYPE_LABELS[t]) for t in Bounty.VALID_BOUNTY_TYPES
    ], validators=[DataRequired(message='Please select a vertical')])
    # validate_choice=False: the route validates the category against
    # the chosen vertical (a category valid for one vertical is not
    # valid for the other); the form only carries the value.
    category = SelectField('Category', choices=_category_choices(),
                           validate_choice=False)
    bounty_direction = SelectField('Direction', choices=[
        ('', 'What are you doing?'),
        ('seeking_opportunity', 'Seeking Opportunity'),
        ('offering_opportunity', 'Offering Opportunity')
    ], validators=[DataRequired(message='Please select a direction')])
    description = TextAreaField('Description', validators=[
        DataRequired(message='Description is required'),
        Length(min=50, message='Please provide at least 50 characters')
    ])
    reward_amount = DecimalField('Reward Amount ($)', validators=[
        NumberRange(min=0, max=100000, message='Reward must be between $0 and $100,000')
    ])
    location = StringField('Location', validators=[
        Length(max=200, message='Location must be under 200 characters')
    ])
    deadline = DateField('Deadline')
    success_criteria = TextAreaField('Success Criteria')
    skills = StringField('Skills (comma-separated)')
    is_free = SubmitField('Free Need (No payment required)')
    submit = SubmitField('Post Referral Need')


class PitchForm(FlaskForm):
    """Form for submitting a pitch."""
    candidate_teaser = TextAreaField('Why they fit', validators=[
        DataRequired(message='Please write why they fit'),
        Length(min=50, max=1000, message='Referral must be between 50 and 1000 characters')
    ])
    submit = SubmitField('Submit Referral')
