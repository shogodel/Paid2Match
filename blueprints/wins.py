"""Wins wall — the public wall of completed hires (/wins).

A completed hire gets up to three short stories, one per side:
the poster, the referrer, and the hired candidate. Only stories
for COMPLETED matches (both sides confirmed) ever appear on the
wall, and only the people involved in the hire may write one —
everyone else gets a 403.

The hired candidate is usually not a Paid2Match user: candidates
exist only as teasers on pitches, and Match has no candidate user
link. So the 'hired' story is written by one of the two
participants who know the hire — the poster or the referrer —
sharing the hired person's side. If a candidate link is ever added
to Match, eligible_roles() is the one place to tighten this.
"""

from flask import (
    Blueprint, abort, flash, redirect, render_template, url_for,
)
from flask_babel import gettext as _
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from wtforms import SelectField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length

from extensions import db, get_or_404
from models.bounty import Bounty
from models.match import Match
from models.win_story import WinStory
from utils.money_trail import build_trail

wins_bp = Blueprint('wins', __name__)


class WinStoryForm(FlaskForm):
    """One short story about a completed hire, from one side."""
    role = SelectField(_('Your role'), validators=[DataRequired()])
    body = TextAreaField(_('Your story'), validators=[
        DataRequired(),
        Length(max=WinStory.BODY_MAX_LENGTH),
    ])
    submit = SubmitField(_('Share your story'))


def completed_matches_for(bounty_id):
    """The completed matches on a bounty — dual-confirmed, which is
    the codebase's definition of a successful match (see the
    homepage stats and utils/payments.release_escrow)."""
    return Match.query.filter_by(
        bounty_id=bounty_id,
        hunter_confirmed=True,
        employer_confirmed=True,
    )


def eligible_roles(bounty, user_id):
    """The story roles one user may claim on one bounty.

    'poster' — the user posted the need. 'referrer' — the user is
    the referrer (hunter) on a completed match for the need.
    'hired' — the user is the poster or the referrer on a completed
    match: the hired candidate themself is not a platform user (see
    the module docstring), so a participant shares their side.
    An empty list means the user may not write any story here.
    """
    roles = []
    completed = completed_matches_for(bounty.id).all()
    if not completed:
        return roles
    if bounty.poster_id == user_id:
        roles.append('poster')
    is_referrer = any(m.hunter_id == user_id for m in completed)
    if is_referrer:
        roles.append('referrer')
    if bounty.poster_id == user_id or is_referrer:
        roles.append('hired')
    return roles


def _available_roles(bounty, user_id):
    """Eligible roles that do not have a story yet — one story per
    role per bounty."""
    taken = {
        s.role
        for s in WinStory.query.filter_by(bounty_id=bounty.id).all()
    }
    return [r for r in eligible_roles(bounty, user_id) if r not in taken]


def _earned_for(story, payouts=None, referral_by_pitch=None):
    """The amount the referrer earned on the hire behind a story,
    as ``(amount, currency)`` — or ``(None, None)`` when it is not
    known. Read from the money trail, the single source of truth
    for who earned what: the referrer hop's amount is the net the
    referrer actually earned — the payout amount when Payout rows
    exist, else the derived net (gross minus platform fee, 70/30
    when a sharer is in the chain). Never the gross reward: the
    referrer earns the net, not what the poster put up. A free
    need has no amount. The wall shows this badge on referrer
    stories only — the poster paid the reward (they earned
    nothing) and the hired candidate got a job, not money.
    ``payouts`` / ``referral_by_pitch`` are the wall's bulk preloads
    (see wall()); without them build_trail queries per story."""
    bounty = story.bounty
    if bounty is None:
        return None, None
    trail = build_trail(
        bounty, match=story.match, payouts=payouts,
        referral_by_pitch=referral_by_pitch,
    )
    referrer_hop = next(
        (hop for hop in trail if hop['role'] == 'referrer'), None
    )
    if referrer_hop is None:
        return None, None
    return referrer_hop['amount'], referrer_hop['currency']


@wins_bp.route('/')
def wall():
    """GET /wins — the public wall: stories for completed matches
    only, newest first, with the need title and the amount earned
    where known. A story whose match is not completed never
    appears, even if it exists in the database."""
    # The needs with at least one completed match (a plain IN
    # subquery — an EXISTS correlated on WinStory would collapse
    # against the Match join below, SQLAlchemy auto-correlation).
    completed_bounty_ids = db.session.query(Match.bounty_id).filter(
        Match.hunter_confirmed.is_(True),
        Match.employer_confirmed.is_(True),
    ).distinct()
    stories = (
        WinStory.query
        .options(
            # Audit 2026-11-25: _earned_for read story.bounty,
            # story.match, match.pitch, match.employer and
            # match.hunter lazily — 5 queries per story. Eager-load
            # them all (4 queries total, however many stories).
            selectinload(WinStory.bounty),
            selectinload(WinStory.match).selectinload(Match.pitch),
            selectinload(WinStory.match).selectinload(Match.employer),
            selectinload(WinStory.match).selectinload(Match.hunter),
        )
        .outerjoin(Match, Match.id == WinStory.match_id)
        .filter(WinStory.bounty_id.in_(completed_bounty_ids))
        .filter(or_(
            WinStory.match_id.is_(None),
            (Match.hunter_confirmed.is_(True))
            & (Match.employer_confirmed.is_(True)),
        ))
        .order_by(WinStory.created_at.desc())
        .all()
    )
    # One bulk load of the Payout rows and Referrals every trail
    # below needs (was 2 Payout queries + up to 2 Referral queries
    # per story, inside build_trail).
    from models.payout import Payout
    from models.referral import Referral
    match_ids = [s.match_id for s in stories if s.match_id]
    payouts = {
        (p.match_id, p.kind): p
        for p in Payout.query.filter(
            Payout.match_id.in_(match_ids)
        ).all()
    } if match_ids else {}
    pitch_ids = [
        s.match.pitch_id for s in stories
        if s.match is not None and s.match.pitch_id
    ]
    referral_by_pitch = {
        r.pitch_id: r
        for r in Referral.query.filter(
            Referral.pitch_id.in_(pitch_ids)
        ).all()
    } if pitch_ids else {}
    for story in stories:
        amount, currency = _earned_for(
            story, payouts=payouts,
            referral_by_pitch=referral_by_pitch,
        )
        story.earned_amount = amount
        story.earned_currency = currency
    return render_template('wins/wall.html', stories=stories)


@wins_bp.route('/new/<bounty_id>', methods=['GET', 'POST'])
@login_required
def new_story(bounty_id):
    """GET/POST /wins/new/<bounty_id> — add one short story about a
    completed hire. The poster, the referrer and the hired
    candidate may each add one story per completed bounty; anyone
    else gets a 403, and a second story in the same role is
    refused (the form only offers roles still open, and the
    database unique constraint is the backstop)."""
    bounty = get_or_404(Bounty, bounty_id)
    available = _available_roles(bounty, current_user.id)
    if not eligible_roles(bounty, current_user.id):
        # Not a participant in a completed hire on this need.
        abort(403)
    if not available:
        flash(
            _('You have already shared every story you can for '
              'this need.'),
            'info',
        )
        return redirect(url_for('wins.wall'))
    form = WinStoryForm()
    form.role.choices = [
        (role, str(WinStory.ROLE_LABELS[role])) for role in available
    ]
    if form.validate_on_submit():
        role = form.role.data
        if role not in _available_roles(bounty, current_user.id):
            # Taken between the form render and the submit (or a
            # crafted POST) — refuse, do not crash on the constraint.
            flash(
                _('A story for that role already exists for this '
                  'need.'),
                'warning',
            )
            return redirect(url_for('wins.wall'))
        completed = completed_matches_for(bounty.id).order_by(
            Match.created_at.desc()
        ).all()
        # Link the story to the match it is about: the author's own
        # match when they are the referrer, else the latest
        # completed match on the need.
        match = next(
            (m for m in completed if m.hunter_id == current_user.id),
            completed[0],
        )
        story = WinStory(
            bounty_id=bounty.id,
            match_id=match.id,
            author_id=current_user.id,
            role=role,
            body=form.body.data.strip(),
        )
        db.session.add(story)
        try:
            db.session.commit()
        except IntegrityError:
            # The unique constraint caught a race — same refusal.
            db.session.rollback()
            flash(
                _('A story for that role already exists for this '
                  'need.'),
                'warning',
            )
            return redirect(url_for('wins.wall'))
        flash(
            _('Your story is now on the wins wall. Thank you for '
              'sharing it!'),
            'success',
        )
        return redirect(url_for('wins.wall'))
    return render_template('wins/new.html', form=form, bounty=bounty)
