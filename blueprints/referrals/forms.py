"""Referral forms (M1)."""
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional


class ReferForm(FlaskForm):
    """Refer someone for a need: pick a connection, or enter a name +
    contact, and say why they fit (§4.1)."""
    connection_id = SelectField(
        'Or pick someone you follow', choices=[], validators=[Optional()],
    )
    referred_name = StringField('Their name', validators=[Optional(), Length(max=120)])
    referred_email = StringField('Their email', validators=[Optional(), Email(), Length(max=120)])
    message = TextAreaField('Why are they a great fit?', validators=[
        DataRequired(), Length(min=50, max=1000),
    ])
    submit = SubmitField('Send referral')
