"""Endorsements service (M3, §3.3).

Gates for giving an endorsement: the endorser needs 250+ karma
(KARMA_ENDORSE), at most 5 endorsements per calendar month, one
endorsement per (endorser, endorsee, skill), and no self-endorsement.

Trust weight: an endorsement from a Trusted+ user counts double in
the community component (max 5 points). The level check uses
``include_endorsements=False`` so the computation cannot recurse
(A endorses B, B endorses A).
"""
from datetime import datetime, timezone

from sqlalchemy import func

from extensions import db
from models import utc_now
from models.endorsement import (
    ENDORSEMENT_BASE_WEIGHT,
    ENDORSEMENT_MAX_POINTS,
    ENDORSEMENT_TRUSTED_WEIGHT,
    ENDORSEMENTS_PER_MONTH,
    Endorsement,
)
from models.profile import Profile
from models.user import User
from utils.karma import KARMA_ENDORSE
from utils.notifications import notify
from utils.trust import trust_level


def endorsements_this_month(endorser_id):
    """Endorsements the user has given this calendar month."""
    now = utc_now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return Endorsement.query.filter(
        Endorsement.endorser_id == endorser_id,
        Endorsement.created_at >= month_start,
    ).count()


def can_endorse(endorser, endorsee_id, skill):
    """(ok, reason) — can this user endorse this person for this skill?"""
    if endorser.id == endorsee_id:
        return False, 'You cannot endorse yourself.'
    profile = Profile.query.filter_by(user_id=endorser.id).first()
    if not profile or (profile.karma or 0) < KARMA_ENDORSE:
        return False, (
            f'You need {KARMA_ENDORSE} karma to endorse others — '
            'complete referrals and help people to earn it.'
        )
    if endorsements_this_month(endorser.id) >= ENDORSEMENTS_PER_MONTH:
        return False, (
            f'You have used your {ENDORSEMENTS_PER_MONTH} endorsements '
            'for this month.'
        )
    existing = Endorsement.query.filter(
        Endorsement.endorser_id == endorser.id,
        Endorsement.endorsee_id == endorsee_id,
        # Case-insensitive: 'Python' and 'python' are the same skill —
        # an exact-match check let one endorser vouch twice for it.
        func.lower(Endorsement.skill) == (skill or '').lower(),
    ).first()
    if existing is not None:
        return False, 'You already endorsed this person for that skill.'
    return True, None


def give_endorsement(endorser, endorsee, skill, comment=None):
    """Create the endorsement and notify the endorsee. Returns
    (endorsement, error). Does not commit — the caller commits."""
    ok, reason = can_endorse(endorser, endorsee.id, skill)
    if not ok:
        return None, reason
    endorsement = Endorsement(
        endorser_id=endorser.id,
        endorsee_id=endorsee.id,
        skill=skill,
        comment=(comment or '').strip() or None,
    )
    db.session.add(endorsement)
    db.session.flush()
    notify(
        endorsee.id, 'endorsement',
        f'{endorser.full_name} endorsed you for {skill}',
        comment or None,
        actor_id=endorser.id,
        # Built by hand, not url_for: this runs outside a request
        # context (tests and background jobs call it directly).
        link_url=f'/profile/{endorsee.handle or endorsee.id}',
    )
    return endorsement, None


def endorsement_points(user_id):
    """Community points from endorsements (max 5): each endorsement
    weighs 1, or 2 when the endorser is Trusted+."""
    endorsements = Endorsement.query.filter_by(endorsee_id=user_id).all()
    total = 0
    for endorsement in endorsements:
        endorser = db.session.get(User, endorsement.endorser_id)
        weight = ENDORSEMENT_BASE_WEIGHT
        if endorser is not None:
            level, _s, _b = trust_level(endorser, include_endorsements=False)
            if level in ('Trusted', 'Ambassador'):
                weight = ENDORSEMENT_TRUSTED_WEIGHT
        total += weight
        # Audit 2026-11-25: the result is capped at 5 — stop scoring
        # endorsers once the cap is reached (each endorser costs a
        # full trust computation).
        if total >= ENDORSEMENT_MAX_POINTS:
            break
    return min(ENDORSEMENT_MAX_POINTS, total)


def endorsements_for(user_id):
    """Endorsements received, newest first."""
    return Endorsement.query.filter_by(endorsee_id=user_id).order_by(
        Endorsement.created_at.desc()
    ).all()
