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
                                ("under an hour", "~X hours", "~X days").
                                Thin wrapper over utils.trust (the
                                single source of truth). None when no
                                pitch was answered.
  completed_referrals_count   — re-exported from utils/trust.py
                                (dual-confirmed matches as hunter).
  response_rate               — re-exported from utils/trust.py
                                (share of received pitches answered).
"""
from extensions import db
from models.bounty import Bounty
from models.match import Match
from utils.trust import (
    average_response_time,
    completed_referrals_count,
    format_response_time,
    response_rate,
)

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
    # Audit 2026-11-25: the old code issued one Match query per
    # terminal bounty. One IN query covers them all.
    terminal_ids = [b.id for b in terminal]
    filled_ids = {
        bounty_id for (bounty_id,) in db.session.query(
            Match.bounty_id
        ).filter(
            Match.bounty_id.in_(terminal_ids),
            Match.hunter_confirmed.is_(True),
            Match.employer_confirmed.is_(True),
        ).distinct().all()
    } if terminal_ids else set()
    filled = sum(1 for b in terminal if b.id in filled_ids)
    return filled / len(terminal)


def median_response_time(user_id):
    """Median first-response time on the user's needs, formatted.
    Thin wrapper over utils.trust.average_response_time /
    format_response_time — trust.py is the single source of truth
    for the computation and the display string, so the
    reputation-metrics strip shows exactly the string the Trust
    card shows. None when no pitch on the user's needs was ever
    answered."""
    avg = average_response_time(user_id)
    if avg is None:
        return None
    return format_response_time(avg[0])


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
