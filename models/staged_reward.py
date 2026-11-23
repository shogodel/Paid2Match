"""Staged-reward events (Phase 3, Paraform model — DISPLAY ONLY).

A need can declare stage rewards (Bounty.stage_*_reward): a small
amount for a referral submitted, a larger one for an interview, the
main amount for a hire. When a stage completes, the event is RECORDED
here — and that is all that happens.

There is deliberately NO payout path in this module: no live Stripe
keys exist in the container (no STRIPE_* env vars, keys' location
unresolved), so nothing here can move money, and nothing here tries.
Every event carries status 'pending_stripe' — the only status that
exists until the payout wiring slice lands. When it does, a 'paid'
status and a Stripe transfer call join this module; until then, the
UI shows these events as pending and says so plainly, so no user is
ever led to believe money moved.
"""
from extensions import db
from models import generate_uuid, utc_now


class StageRewardEvent(db.Model):
    """One completed stage of one referral, recorded for a future
    payout. One event per (pitch, stage) — recording is idempotent,
    so a stage can never be recorded (or paid) twice."""
    __tablename__ = 'stage_reward_events'
    __table_args__ = (
        db.UniqueConstraint(
            'pitch_id', 'stage', name='uq_stage_event_pitch_stage'
        ),
    )

    STAGES = ['submit', 'interview', 'hire']
    # The only status until Stripe payout wiring exists.
    STATUSES = ['pending_stripe']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    bounty_id = db.Column(db.String(36), db.ForeignKey('bounties.id'), nullable=False, index=True)
    pitch_id = db.Column(db.String(36), db.ForeignKey('pitches.id'), nullable=False, index=True)
    stage = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending_stripe')
    created_at = db.Column(db.DateTime, default=utc_now)

    bounty = db.relationship('Bounty', backref='stage_reward_events')
    pitch = db.relationship('Pitch', backref='stage_reward_events')

    @property
    def label(self):
        """The translated display label for this stage."""
        from utils.staged_rewards import STAGE_LABELS
        return STAGE_LABELS.get(self.stage, self.stage)

    def __repr__(self):
        return f'<StageRewardEvent {self.stage} ${self.amount} ({self.status})>'
