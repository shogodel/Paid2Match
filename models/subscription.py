"""Pro analytics subscription (M3, §1.4): $9/month via Stripe.

Pro adds: members reached (unique viewers per post), engagement rate
with the 1–3–5% benchmarks, 90-day profile-view history with viewer
names (opt-in on the viewer's side), search appearances, audience
demographics, referral funnel, bump performance, best time to post,
and CSV export. The free tier keeps the M1 basics.

Founding referrers get Pro free for 12 months (§1.4) — the founding
flag is admin-managed (same as the 15% fee in M2): the admin removes
it when the window ends.

Canceling keeps access until the end of the current period
(cancel_at_period_end): the subscription stays 'active' in Stripe
until then, and the webhook flips it to 'canceled' at period end.
"""
from extensions import db
from models import generate_uuid, utc_now

PRO_PRICE_CENTS = 900  # $9/month, CAD
PRO_PRICE_LABEL = '$9/month'


class ProSubscription(db.Model):
    """One user's Pro subscription, mirrored from Stripe."""
    __tablename__ = 'pro_subscriptions'

    STATUSES = ['active', 'canceled', 'past_due', 'incomplete']

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, unique=True, index=True)
    stripe_subscription_id = db.Column(db.String(200), nullable=False, unique=True)
    stripe_customer_id = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='active')
    cancel_at_period_end = db.Column(db.Boolean, default=False)
    current_period_start = db.Column(db.DateTime, nullable=True)
    current_period_end = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)

    user = db.relationship('User', backref='pro_subscription')

    def __repr__(self):
        return f'<ProSubscription {self.user_id} {self.status}>'

    @property
    def is_current(self):
        """Whether Pro access is live right now: an active subscription
        whose period has not ended (a cancel-at-period-end subscription
        stays active until then)."""
        if self.status != 'active':
            return False
        if self.current_period_end is None:
            return True
        end = self.current_period_end
        if end.tzinfo is None:
            end = end.replace(tzinfo=utc_now().tzinfo)
        return utc_now() < end
