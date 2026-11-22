"""Referral chains (M4, §4.4): two-level payout splits.

When A shares a need post and B refers the winning person from that
share, the payout splits: the platform keeps its fee first (20% by
default), then the referrer's 80% share splits 70/30 between B (the
referrer, who found the person) and A (the sharer, whose audience
made the connection). On a $1,000 reward: $200 platform, $560
referrer, $240 sharer.

The split ratio is a documented default, not a spec value: report-v2
§4.4 says two-level splits come at M4 but gives no ratio. 70/30
rewards the referrer (who did the finding) while making the share
worth real money — the point of the chain is that sharing a need is
a meaningful, paid act, not a favour.

Attribution is explicit, not inferred: the referral carries the
share it came through (Pitch/Referral.via_share_id), set by the
refer/pitch routes when the referrer acts from a share card. The M1
karma credit still goes to ALL sharers of the need (§4.4 attribution
credit); the money split goes to exactly one sharer — the one whose
share the winning referral came through. A share earns a split only
when the sharer is neither the referrer nor the poster.

These functions do NOT commit — the caller commits.
"""
from decimal import Decimal, ROUND_HALF_UP

from extensions import db
from models.post import Post, Share
from models.referral import Referral

# Share of the referrer's post-fee net that goes to the referrer
# (70%) and to the sharer (30%).
CHAIN_REFERRER_SHARE = Decimal('0.70')
CHAIN_SHARER_SHARE = Decimal('0.30')


def validate_share_for_referral(share_id, bounty, referrer_id):
    """The Share a referral may be attributed through, or None.

    Valid only when: the share exists, its post is linked to this
    bounty, the sharer is not the referrer (referring from your own
    share earns no split), and the sharer is not the poster (sharing
    your own need earns no split).
    """
    if not share_id:
        return None
    share = db.session.get(Share, share_id)
    if share is None:
        return None
    if share.user_id == referrer_id:
        return None
    if share.user_id == bounty.poster_id:
        return None
    post = db.session.get(Post, share.post_id)
    if post is None or post.bounty_id != bounty.id:
        return None
    return share


def resolve_chain_sharer(match):
    """The user id of the sharer in a match's chain, or None.

    Read from the pitch's via_share_id (set by the refer/pitch
    routes, copied through consent); falls back to the Referral's
    via_share_id when the pitch has none. Re-validated here — a share
    recorded before M4, or one whose post was deleted, earns nothing.
    """
    share_id = None
    pitch = match.pitch
    if pitch is not None:
        share_id = pitch.via_share_id
    if not share_id and match.pitch_id:
        referral = Referral.query.filter_by(pitch_id=match.pitch_id).first()
        if referral is not None:
            share_id = referral.via_share_id
    if not share_id:
        return None
    share = validate_share_for_referral(
        share_id, match.bounty, match.hunter_id
    )
    return share.user_id if share else None


def compute_chain_split(net):
    """Split the referrer's net (post-fee) amount 70/30 between the
    referrer and the sharer. The sharer gets the remainder so the two
    parts sum exactly to the net (no cent is created or lost)."""
    net = Decimal(str(net))
    referrer = (net * CHAIN_REFERRER_SHARE).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP
    )
    return referrer, net - referrer
