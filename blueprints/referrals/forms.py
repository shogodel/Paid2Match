"""Referral forms (M1)."""
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, SubmitField, TextAreaField
from flask_babel import lazy_gettext as _l
from wtforms.validators import DataRequired, Email, Length, Optional


class ReferForm(FlaskForm):
    """Refer someone for a need: pick a connection, or enter a name +
    contact, and say why they fit (§4.1)."""
    connection_id = SelectField(_l('Or pick someone you follow'), choices=[], validators=[Optional()],
    )
    referred_name = StringField(_l('Their name'), validators=[Optional(), Length(max=120)])
    referred_email = StringField(_l('Their email'), validators=[Optional(), Email(), Length(max=120)])
    message = TextAreaField(_l('Why are they a great fit?'), validators=[
        DataRequired(), Length(min=50, max=1000),
    ])
    submit = SubmitField(_l('Send referral'))
