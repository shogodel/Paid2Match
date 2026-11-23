"""Staged rewards (Phase 3, Paraform model — DISPLAY ONLY).

A need declares stage rewards (Bounty.stage_*_reward); when a stage
completes, record_stage_event() records the event. Recording is all
this module does: it never calls Stripe, never creates a Payout, and
never moves money. Every event is status 'pending_stripe' — the UI
shows that plainly. The payout wiring slice is DEFERRED until live
Stripe keys exist (none are in the container env).

Stage completion points (wired into the existing flows):
- submit: a pitch is created — direct pitch (bounties.pitch) or the
  referred person consenting (referrals.confirm_referral).
- interview: the poster marks a pitch 'interested'
  (bounties.respond_to_pitch).
- hire: both sides confirm the match
  (utils.referrals.complete_referral_for_match).
"""
from flask_babel import lazy_gettext as _l

from extensions import db
from models.staged_reward import StageRewardEvent

STAGE_LABELS = {
    'submit': _l('Referral submitted'),
    'interview': _l('Interview'),
    'hire': _l('Hire'),
}

# Bounty column per stage.
_STAGE_COLUMNS = {
    'submit': 'stage_submit_reward',
    'interview': 'stage_interview_reward',
    'hire': 'stage_hire_reward',
}


def declared_stage_amount(bounty, stage):
    """The amount the poster declared for this stage, or None when
    the need declares no reward for it (or no staged rewards at all)."""
    if bounty is None or stage not in _STAGE_COLUMNS:
        return None
    amount = getattr(bounty, _STAGE_COLUMNS[stage])
    return amount if amount and amount > 0 else None


def record_stage_event(pitch, stage):
    """Record that a stage completed for this pitch — if (and only
    if) the need declares a reward for that stage. Idempotent: a
    second call for the same pitch + stage returns the existing
    event. Returns the event, or None when the need declares nothing
    for this stage. Records the event ONLY — no payout is attempted
    (there is no payout path until Stripe is wired). Does not commit
    — the caller commits."""
    if pitch is None or stage not in _STAGE_COLUMNS:
        return None
    amount = declared_stage_amount(pitch.bounty, stage)
    if amount is None:
        return None
    existing = StageRewardEvent.query.filter_by(
        pitch_id=pitch.id, stage=stage,
    ).first()
    if existing is not None:
        return existing
    event = StageRewardEvent(
        bounty_id=pitch.bounty_id,
        pitch_id=pitch.id,
        stage=stage,
        amount=amount,
        status='pending_stripe',
    )
    db.session.add(event)
    return event


def stage_events_for_bounty(bounty_id):
    """All recorded stage events for a need, oldest first."""
    return StageRewardEvent.query.filter_by(
        bounty_id=bounty_id,
    ).order_by(StageRewardEvent.created_at).all()


def stage_events_for_pitch(pitch_id):
    """All recorded stage events for a pitch, oldest first."""
    return StageRewardEvent.query.filter_by(
        pitch_id=pitch_id,
    ).order_by(StageRewardEvent.created_at).all()
