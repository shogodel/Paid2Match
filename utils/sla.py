"""No-ghosting SLA (M2).

Rule (kept simple and visible): a poster has 5 business days to
respond to a pitch (interested or rejected). A pitch still pending
past its due date is an *open* breach; a pitch answered after its due
date is a *late* breach. Breaches feed the trust score (penalties)
and suspend the trust badge while any breach is open.

Referrer-side SLA is deliberately NOT measured here: the only
measurable referrer "response" is match confirmation, which
legitimately takes longer than 5 days (interviews happen first) —
applying the same SLA would punish normal behaviour.
"""

from datetime import timedelta, timezone

from models import Pitch, utc_now

# Business days a poster has to answer a pitch.
SLA_BUSINESS_DAYS = 5


def _as_aware(dt):
    """SQLite returns datetimes naive even though utc_now() is aware —
    normalize before comparing (same helper as utils/trust.py)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def add_business_days(start, days):
    """Add ``days`` business days (weekdays only) to a datetime."""
    result = start
    added = 0
    while added < days:
        result += timedelta(days=1)
        if result.weekday() < 5:  # Monday=0 .. Friday=4
            added += 1
    return result


def pitch_due_at(pitch):
    """The deadline for a poster to respond to this pitch."""
    return add_business_days(_as_aware(pitch.created_at), SLA_BUSINESS_DAYS)


def pitch_responded_at(pitch):
    """When the poster first responded — the M2 ``responded_at``
    column, falling back to ``updated_at`` for pre-M2 pitches."""
    return _as_aware(pitch.responded_at or pitch.updated_at)


def pitch_breached(pitch, now=None):
    """Whether a pitch breached the SLA. A still-pending pitch past
    its due date is an open breach; an answered pitch that was
    answered after its due date is a late breach."""
    now = _as_aware(now or utc_now())
    due = pitch_due_at(pitch)
    if pitch.status == 'pending':
        return now > due
    return pitch_responded_at(pitch) > due


def sla_breaches(user_id, now=None):
    """All SLA breaches for a poster, newest first. Each breach is a
    dict with the pitch, the bounty, whether it is still open, and
    ``breached_at`` (the due date for open breaches, the response
    time for late ones)."""
    now = _as_aware(now or utc_now())
    pitches = (
        Pitch.query
        .filter(Pitch.bounty.has(poster_id=user_id))
        .order_by(Pitch.created_at.desc())
        .all()
    )
    breaches = []
    for pitch in pitches:
        if not pitch_breached(pitch, now=now):
            continue
        open_breach = pitch.status == 'pending'
        breaches.append({
            'pitch': pitch,
            'bounty': pitch.bounty,
            'open': open_breach,
            'due_at': pitch_due_at(pitch),
            'breached_at': pitch_due_at(pitch) if open_breach else pitch_responded_at(pitch),
        })
    return breaches


def has_open_breach(user_id, now=None):
    """Whether the poster has any pitch still unanswered past its due
    date — while true, the trust badge is suspended."""
    return any(b['open'] for b in sla_breaches(user_id, now=now))


def sla_breach_count_90d(user_id, now=None):
    """Breaches (open or late) from the last 90 days — the window the
    trust score penalizes."""
    now = _as_aware(now or utc_now())
    cutoff = now - timedelta(days=90)
    return sum(1 for b in sla_breaches(user_id, now=now) if b['breached_at'] >= cutoff)


def sla_breach_count(user_id, now=None):
    """All-time breach count (for the public profile / admin)."""
    return len(sla_breaches(user_id, now=now))
