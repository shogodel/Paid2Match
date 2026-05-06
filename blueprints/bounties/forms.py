"""Bounties forms for Paid2Match."""
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, DecimalField, DateField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange


class BountyForm(FlaskForm):
    """Form for creating a new bounty."""
    title = StringField('Title', validators=[
        DataRequired(message='Title is required'),
        Length(min=10, max=200, message='Title must be between 10 and 200 characters')
    ])
    bounty_type = SelectField('Bounty Type', choices=[
        ('', 'Select bounty type...'),
        ('recruitment', 'Recruitment'),
        ('real_estate', 'Real Estate'),
        ('healthcare', 'Healthcare'),
        ('legal', 'Legal')
    ], validators=[DataRequired(message='Please select a bounty type')])
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
    is_free = SubmitField('Free Bounty (No payment required)')
    submit = SubmitField('Post Bounty')


class PitchForm(FlaskForm):
    """Form for submitting a pitch."""
    candidate_teaser = TextAreaField('Your Pitch', validators=[
        DataRequired(message='Please write a pitch'),
        Length(min=50, max=1000, message='Pitch must be between 50 and 1000 characters')
    ])
    submit = SubmitField('Submit Pitch')