"""Human-reviewed pledge register (M3, §3.4).

An employer pledges: no AI screening, and a human response to every
referral within 5 business days. The register is public; each pledger
shows their no-ghosting compliance, and the platform publishes its
platform-wide compliance rate — the one trust promise users can
verify themselves.

Compliance is computed from the SLA data (utils/sla.py): pitches whose
response deadline has passed, and whether they were answered on time.
A pledger with an open breach shows as suspended on the register
(computed from the breach, not stored — the breach is the fact).
Revocation is an admin action.
"""
from datetime import timedelta

from extensions import db
from models import utc_now
from models.bounty import Bounty
from models.endorsement import Pledge
from models.pitch import Pitch
from utils.sla import pitch_breached, pitch_due_at, has_open_breach

# The compliance window the register publishes (§3.4: "this month").
COMPLIANCE_WINDOW_DAYS = 30


def _as_aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=utc_now().tzinfo)


def ghosting_compliance(user_id=None, days=COMPLIANCE_WINDOW_DAYS):
    """(rate, due, on_time) — share of pitches answered within the
    5-business-day SLA, over pitches created in the last ``days`` whose
    deadline has passed. ``user_id`` restricts to one poster's needs;
    None means platform-wide. rate is None when nothing was due."""
    now = utc_now()
    window_start = now - timedelta(days=days)
    query = Pitch.query.filter(Pitch.created_at >= window_start)
    if user_id is not None:
        query = query.join(Bounty, Pitch.bounty_id == Bounty.id).filter(
            Bounty.poster_id == user_id
        )
    pitches = query.all()
    due = 0
    on_time = 0
    for pitch in pitches:
        if pitch_due_at(pitch) > now:
            continue  # deadline not reached yet — not due
        due += 1
        if not pitch_breached(pitch, now=now):
            on_time += 1
    rate = round(on_time / due, 2) if due else None
    return rate, due, on_time


def platform_ghosting_compliance(days=COMPLIANCE_WINDOW_DAYS):
    """Platform-wide no-ghosting compliance for the register."""
    return ghosting_compliance(user_id=None, days=days)


def effective_pledge_status(pledge):
    """'revoked' (admin action), 'suspended' (an open SLA breach — the
    pledge promise is currently broken), or 'active'."""
    if pledge.status == 'revoked':
        return 'revoked'
    if has_open_breach(pledge.user_id):
        return 'suspended'
    return 'active'


def create_pledge(user, company_name, website=None):
    """Create a pledge for a user. One pledge per user — a revoked
    pledge can be re-made (a fresh pledge row replaces it). Returns
    (pledge, error). Does not commit — the caller commits."""
    existing = Pledge.query.filter_by(user_id=user.id).first()
    if existing is not None and existing.status == 'active':
        return None, 'You already have an active pledge.'
    if existing is not None:
        db.session.delete(existing)
        db.session.flush()
    pledge = Pledge(
        user_id=user.id,
        company_name=(company_name or '').strip(),
        website=(website or '').strip() or None,
    )
    db.session.add(pledge)
    db.session.flush()
    return pledge, None


def register_entries():
    """Public register rows: every non-revoked pledge with its
    effective status and the pledger's 30-day compliance."""
    pledges = Pledge.query.filter(Pledge.status != 'revoked').order_by(
        Pledge.created_at
    ).all()
    entries = []
    for pledge in pledges:
        rate, due, _on_time = ghosting_compliance(pledge.user_id)
        entries.append({
            'pledge': pledge,
            'status': effective_pledge_status(pledge),
            'compliance_rate': rate,
            'compliance_due': due,
        })
    return entries
