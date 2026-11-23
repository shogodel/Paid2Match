"""Portable reputation metrics (Phase 3, BountyJobs model).

Fill rate / response time / completed referrals / response rate,
shown where the data exists — and nothing at all where it doesn't
(a zero with no data behind it would be a lie). All four metrics are
computed from existing data (bounties, pitches, matches, reviews);
no new math, no new storage.

  fill_rate(user_id)          — among the poster's terminal needs
                                (completed/closed/expired), the share
                                with >= 1 dual-confirmed Match.
                                None when there are no terminal needs.
  median_response_time(id)    — median pitch-created -> first-response
                                time for answered pitches on the user's
                                needs, formatted for display
                                ("under an hour", "≈ Xh", "≈ Xd").
                                None when no pitch was answered.
  completed_referrals_count   — re-exported from utils/trust.py
                                (dual-confirmed matches as hunter).
  response_rate               — re-exported from utils/trust.py
                                (share of received pitches answered).
"""
from datetime import timedelta
from statistics import median

from flask_babel import gettext as _

from models.bounty import Bounty
from models.match import Match
from models.pitch import Pitch
from utils.trust import completed_referrals_count, response_rate

__all__ = [
    'completed_referrals_count',
    'fill_rate',
    'median_response_time',
    'reputation_metrics',
    'response_rate',
]

TERMINAL_BOUNTY_STATUSES = ['completed', 'closed', 'expired']


def fill_rate(user_id):
    """Share of the poster's terminal needs that ended in a hire
    (a dual-confirmed Match). None when the poster has no terminal
    needs — a new poster shows nothing, not 0%."""
    terminal = Bounty.query.filter(
        Bounty.poster_id == user_id,
        Bounty.status.in_(TERMINAL_BOUNTY_STATUSES),
    ).all()
    if not terminal:
        return None
    filled = 0
    for bounty in terminal:
        if Match.query.filter_by(
            bounty_id=bounty.id,
            hunter_confirmed=True,
            employer_confirmed=True,
        ).first():
            filled += 1
    return filled / len(terminal)


def format_response_time(delta):
    """A timedelta as a human display string. Never shows seconds —
    the SLA is measured in business days, so hours are precise
    enough."""
    if delta is None:
        return None
    hours = delta.total_seconds() / 3600.0
    if hours < 1:
        return _('under an hour')
    if hours < 24:
        return _('≈ %(hours)dh', hours=round(hours))
    days = hours / 24.0
    return _('≈ %(days)dd', days=round(days))


def median_response_time(user_id):
    """Median first-response time on the user's needs, formatted.
    Only answered pitches count (responded_at set, falling back to
    updated_at for pitches answered before M2, as the SLA does).
    None when no pitch on the user's needs was ever answered."""
    pitches = Pitch.query.join(
        Bounty, Pitch.bounty_id == Bounty.id
    ).filter(
        Bounty.poster_id == user_id,
        Pitch.status.in_(['interested', 'rejected', 'accepted']),
    ).all()
    deltas = []
    for pitch in pitches:
        answered_at = pitch.responded_at or pitch.updated_at
        if answered_at and pitch.created_at:
            deltas.append(answered_at - pitch.created_at)
    if not deltas:
        return None
    return format_response_time(median(deltas))


def reputation_metrics(user_id):
    """All four metrics for one user, as a dict. Values are None
    where there is no data — the template shows nothing for those."""
    completed = completed_referrals_count(user_id)
    return {
        'fill_rate': fill_rate(user_id),
        'response_time': median_response_time(user_id),
        'completed_referrals': completed if completed else None,
        'response_rate': response_rate(user_id),
    }
