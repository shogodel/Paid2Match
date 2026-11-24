"""Human-reviewed employer pledge: public register + pledge form.

Distinct from the M3 self-published pledge register at
/social/pledges (models/endorsement.py Pledge): this pledge is
human-reviewed — an employer submits it, an admin approves or
revokes it, and only approved pledges appear on the public register
at /pledge.
"""
from flask import Blueprint, flash, redirect, render_template, url_for
from flask_babel import gettext as _, lazy_gettext as _l
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import BooleanField, StringField, SubmitField
from wtforms.validators import DataRequired, Length

from extensions import db
from models.employer_pledge import EmployerPledge

pledge_bp = Blueprint('pledge', __name__)


class EmployerPledgeForm(FlaskForm):
    """Pledge that a human reads every application."""
    org_name = StringField(_l('Organization name'), validators=[
        DataRequired(), Length(min=2, max=200),
    ])
    confirm = BooleanField(
        _l('We pledge that no AI screens our applicants: '
           'a human reads every application.'),
        validators=[DataRequired()],
    )
    submit = SubmitField(_l('Submit my pledge'))


@pledge_bp.route('/')
def register():
    """GET /pledge — the public register: approved pledges only,
    ordered by organization name. Pending and revoked pledges are
    never shown here."""
    pledges = EmployerPledge.query.filter_by(
        status='approved'
    ).order_by(EmployerPledge.org_name).all()
    my_pledge = None
    if current_user.is_authenticated:
        my_pledge = EmployerPledge.query.filter_by(
            user_id=current_user.id
        ).order_by(EmployerPledge.created_at.desc()).first()
    return render_template(
        'pledge/register.html',
        pledges=pledges,
        my_pledge=my_pledge,
    )


@pledge_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new_pledge():
    """GET/POST /pledge/new — submit an employer pledge for review.

    One active pledge per user: a new pledge is blocked while
    another is pending or approved. A revoked pledge may be
    re-pledged."""
    if EmployerPledge.active_for_user(current_user.id):
        flash(
            _('You already have an active pledge.'),
            'info',
        )
        return redirect(url_for('pledge.register'))
    form = EmployerPledgeForm()
    if form.validate_on_submit():
        pledge = EmployerPledge(
            user_id=current_user.id,
            org_name=form.org_name.data.strip(),
            status='pending',
        )
        db.session.add(pledge)
        db.session.commit()
        flash(
            _('Your pledge was submitted for review. An admin will '
              'approve it once it is checked.'),
            'success',
        )
        return redirect(url_for('pledge.register'))
    return render_template('pledge/new.html', form=form)
