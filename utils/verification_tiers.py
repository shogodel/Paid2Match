"""Free tiered verification (Phase 3).

One clickable badge, three tiers, built ONLY from signals that already
exist — there is no phone-verification backend and none is invented:

  Tier 0  Not verified     — email not verified. No badge is shown.
  Tier 1  Email verified   — the signup email was confirmed.
  Tier 2  Verified         — Tier 1 + at least one verified linked
                             social account OR an admin-verified
                             profile (or Stripe KYC on its own).
  Tier 3  Fully verified   — Tier 2 + Stripe KYC (identity check).

The tier is additive: each verified signal raises it by one (email,
then linked/admin, then KYC), capped at 3. A signal is never hidden
because another one is missing. The badge links to
/social/verification, which explains the tiers and how to reach each.
"""
from flask_babel import lazy_gettext as _l

from extensions import db
from models.connect_account import ConnectAccount
from models.linked_account import LinkedAccount
from models.profile import Profile
from models.user import User

TIER_LABELS = {
    0: _l('Not verified'),
    1: _l('Email verified'),
    2: _l('Verified'),
    3: _l('Fully verified'),
}

# What the next tier needs, for the explainer page's "your next step".
NEXT_STEP = {
    0: _l('Verify your email address — check your inbox for the confirmation link.'),
    1: _l('Link a social account (LinkedIn or GitHub) or ask an admin to verify your profile.'),
    2: _l('Complete the Stripe identity check (KYC) from your payout settings.'),
    3: None,  # Top tier — nothing left to do.
}


def _tier_from_signals(signals):
    """Additive tier from a signals dict (see verification_tier)."""
    if not signals['email_verified']:
        return 0
    tier = 1
    if signals['linked_verified'] or signals['admin_verified']:
        tier += 1
    if signals['kyc']:
        tier += 1
    return min(tier, 3)


def verification_tiers_for(user_ids):
    """{user_id: (tier, label, signals)} for many users — one query
    per signal, so the people directory (50 users) stays cheap.
    signals: email_verified, linked_verified, admin_verified, kyc."""
    ids = list(user_ids)
    if not ids:
        return {}
    users = {
        u.id: u
        for u in User.query.filter(User.id.in_(ids)).all()
    }
    admin_verified = {
        p.user_id
        for p in Profile.query.filter(
            Profile.user_id.in_(ids),
            Profile.verified == True,  # noqa: E712
        ).all()
    }
    linked_verified = {
        a.user_id
        for a in LinkedAccount.query.filter(
            LinkedAccount.user_id.in_(ids),
            LinkedAccount.status == 'verified',
        ).all()
        if a.is_currently_verified
    }
    kyc = {
        c.user_id
        for c in ConnectAccount.query.filter(
            ConnectAccount.user_id.in_(ids),
            # kyc_passed is a Python property, not a column — filter
            # on the columns it is built from.
            ConnectAccount.details_submitted == True,  # noqa: E712
            ConnectAccount.payouts_enabled == True,  # noqa: E712
        ).all()
    }
    out = {}
    for user_id in ids:
        user = users.get(user_id)
        signals = {
            'email_verified': bool(user and user.email_verified),
            'linked_verified': user_id in linked_verified,
            'admin_verified': user_id in admin_verified,
            'kyc': user_id in kyc,
        }
        tier = _tier_from_signals(signals)
        out[user_id] = (tier, TIER_LABELS[tier], signals)
    return out


def verification_tier(user):
    """(tier, label, signals) for one user. Accepts a User or a user id."""
    user_id = user.id if isinstance(user, User) else user
    return verification_tiers_for([user_id]).get(
        user_id, (0, TIER_LABELS[0], {
            'email_verified': False,
            'linked_verified': False,
            'admin_verified': False,
            'kyc': False,
        })
    )
