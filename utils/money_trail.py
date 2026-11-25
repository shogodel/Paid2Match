"""The Money Trail: the public chain of a completed hire.

Every completed hire shows the full chain — who posted the need,
who shared it, who referred the winning person, who got hired —
and what each person earned. The trail is READ-ONLY: it reads the
bounty, its completed Match, its Referral and its Payout records
and never moves money or writes anything.

Hop order follows the money: the poster puts up the reward, the
platform keeps its fee, the sharer (when the referral came through
a share) takes 30% of the post-fee net, the referrer takes the
rest, and the hired person gets the job (no money figure — the
job is their end of the chain).

Amounts come from the Payout records when they exist (the
'referral' payout's net for the referrer, the 'chain_share'
payout for the sharer, the referrer payout's platform_fee for
the platform). When a paid match has no Payout rows yet — e.g.
a match confirmed before payouts existed — the amounts are
derived from the match's recorded fee, or recomputed from the
reward and the referrer's fee rate with the 70/30 chain split
(utils/chains.compute_chain_split). Free needs carry no money
figures at all: every hop's amount is None.

A hop is a dict:
    {'role': 'poster'|'platform'|'sharer'|'referrer'|'hired',
     'user': User or None,
     'name': display name when there is no User (the platform's
             brand name, or a referred person's name when they
             have no account),
     'amount': Decimal or None (None = no money figure),
     'currency': the need's currency code,
     'label': translated role label}
"""
from decimal import Decimal

from flask_babel import lazy_gettext as _l

from extensions import db
from models.match import Match
from models.payout import Payout
from models.referral import Referral
from models.user import User
from utils.chains import compute_chain_split, resolve_chain_sharer

ROLE_LABELS = {
    'poster': _l('Posted the need'),
    'platform': _l('Platform fee'),
    'sharer': _l('Shared the need'),
    'referrer': _l('Referred the hire'),
    'hired': _l('Got hired'),
}

PLATFORM_NAME = 'Paid2Match'


def completed_match_for(bounty):
    """The bounty's completed match, or None.

    A match is completed when both sides confirmed it (the
    confirm-match route sets bounty.status to 'completed' at the
    same moment, but the flags are the source of truth — a match
    can be dual-confirmed without the status having caught up).
    When several matches completed (a re-hire), the most recent
    one is the trail's match.
    """
    return (
        Match.query.filter_by(bounty_id=bounty.id)
        .filter(
            Match.hunter_confirmed.is_(True),
            Match.employer_confirmed.is_(True),
        )
        .order_by(Match.created_at.desc())
        .first()
    )


def _derived_amounts(match, reward, sharer_id):
    """(gross, fee, referrer, sharer) for a paid match with no
    Payout rows. The match's own recorded fee wins when present
    (locked in at match creation); otherwise the fee is recomputed
    from the reward and the referrer's fee rate. The post-fee net
    splits 70/30 when there is a sharer, else it all goes to the
    referrer."""
    gross = (
        Decimal(str(match.payout_amount))
        if match.payout_amount is not None
        else reward
    )
    if match.platform_fee is not None:
        fee = Decimal(str(match.platform_fee))
    else:
        from utils.payments import compute_payout_amounts, fee_rate_for

        _gross, fee, _net = compute_payout_amounts(
            float(gross), fee_rate_for(match.hunter)
        )
        fee = Decimal(str(fee))
    net = gross - fee
    if sharer_id:
        referrer_amount, sharer_amount = compute_chain_split(net)
    else:
        referrer_amount, sharer_amount = net, None
    return gross, fee, referrer_amount, sharer_amount


def build_trail(bounty, match=None):
    """The ordered money-trail hops for a bounty, or [] when no
    hire has completed yet. Read-only — no rows are written. When
    `match` is given, the trail is built for that match — it must
    be a completed match for this bounty; anything else falls back
    to the latest completed match, so a trail is never built for
    an uncompleted hire (the wins wall passes a story's match)."""
    if match is not None and (
        match.bounty_id != bounty.id
        or not match.hunter_confirmed
        or not match.employer_confirmed
    ):
        match = None
    if match is None:
        match = completed_match_for(bounty)
    if match is None:
        return []

    currency = bounty.currency
    is_free = bounty.is_free_bounty
    reward = Decimal(str(bounty.reward_amount or 0))

    referrer_payout = Payout.query.filter_by(
        match_id=match.id, kind='referral'
    ).first()
    chain_payout = Payout.query.filter_by(
        match_id=match.id, kind='chain_share'
    ).first()

    # The sharer is the chain payout's recipient when a chain payout
    # exists; otherwise the chain is re-resolved from the pitch /
    # referral's via_share_id (a share recorded before the payout
    # was created, or a match whose payouts were never created).
    sharer_id = None
    if chain_payout is not None:
        sharer_id = chain_payout.referrer_id
    else:
        sharer_id = resolve_chain_sharer(match)

    if is_free:
        gross = fee = referrer_amount = sharer_amount = None
    elif referrer_payout is not None:
        gross = Decimal(str(referrer_payout.gross_amount))
        fee = Decimal(str(referrer_payout.platform_fee))
        referrer_amount = Decimal(str(referrer_payout.amount))
        sharer_amount = (
            Decimal(str(chain_payout.amount)) if chain_payout else None
        )
    else:
        gross, fee, referrer_amount, sharer_amount = _derived_amounts(
            match, reward, sharer_id
        )

    # The hired person: the referral's referred user when they have
    # an account, else their name as given by the referrer. A direct
    # pitch has no referral at all — the hire is still shown, with
    # no name attached.
    referral = None
    if match.pitch_id:
        referral = Referral.query.filter_by(pitch_id=match.pitch_id).first()
    hired_user = referral.referred_user if referral else None
    hired_name = (
        referral.referred_name
        if referral and hired_user is None
        else None
    )

    def hop(role, user, amount, name=None):
        return {
            'role': role,
            'user': user,
            'name': name,
            'amount': amount,
            'currency': currency,
            'label': ROLE_LABELS[role],
        }

    trail = [
        hop('poster', match.employer, gross),
        hop('platform', None, fee, name=PLATFORM_NAME),
    ]
    if sharer_id:
        trail.append(
            hop('sharer', db.session.get(User, sharer_id), sharer_amount)
        )
    trail.append(hop('referrer', match.hunter, referrer_amount))
    trail.append(hop('hired', hired_user, None, name=hired_name))
    return trail
