"""Earnings dashboard (M4): the referrer-facing rollup of everything
they earned — direct referrals, chain shares, and per-need stats.
Pure reads; no commits, no Stripe calls."""
from collections import Counter, defaultdict
from decimal import Decimal

from extensions import db
from models.bounty import Bounty
from models.match import Match
from models.payout import Payout
from models.pitch import Pitch
from models.referral import Referral

PAID_STATUSES = ('completed',)
PENDING_STATUSES = ('pending', 'processing')


def _sum_amounts(payouts, statuses):
    return sum(
        (Decimal(str(p.amount)) for p in payouts if p.status in statuses),
        Decimal('0'),
    )


def get_earnings_summary(user_id):
    """Everything the earnings dashboard shows for one user:
    totals (all / referral / chain), referrals by status, counts,
    and per-need stats (referrals, matches, earned) for every need
    the user referred on."""
    payouts = Payout.query.filter_by(referrer_id=user_id).all()
    referral_payouts = [p for p in payouts if p.kind == 'referral']
    chain_payouts = [p for p in payouts if p.kind == 'chain_share']

    referrals = Referral.query.filter_by(referrer_id=user_id).all()
    referrals_by_status = dict(Counter(r.status for r in referrals))
    pitches = Pitch.query.filter_by(hunter_id=user_id).all()
    # A pitch linked to a Referral is the same referral counted
    # twice — direct pitches are the ones with no Referral row.
    # (Pitch.referral is a backref collection: [] when unlinked.)
    direct_pitches = [p for p in pitches if not p.referral]
    matches = Match.query.filter_by(hunter_id=user_id).all()

    # Per-need stats, grouped by bounty.
    referral_counts = defaultdict(int)
    for referral in referrals:
        referral_counts[referral.bounty_id] += 1
    for pitch in direct_pitches:
        referral_counts[pitch.bounty_id] += 1
    match_counts = defaultdict(int)
    for match in matches:
        match_counts[match.bounty_id] += 1
    earned_by_bounty = defaultdict(lambda: Decimal('0'))
    for payout in payouts:
        if payout.status in PAID_STATUSES and payout.bounty_id:
            earned_by_bounty[payout.bounty_id] += Decimal(str(payout.amount))

    bounty_ids = set(referral_counts) | set(match_counts) | set(earned_by_bounty)
    bounties = {
        b.id: b
        for b in Bounty.query.filter(Bounty.id.in_(bounty_ids)).all()
    } if bounty_ids else {}
    per_need = [
        {
            'bounty': bounties[bounty_id],
            'referrals': referral_counts.get(bounty_id, 0),
            'matches': match_counts.get(bounty_id, 0),
            'earned': float(earned_by_bounty.get(bounty_id, Decimal('0'))),
        }
        for bounty_id in bounty_ids
        if bounty_id in bounties
    ]
    per_need.sort(key=lambda row: (-row['earned'], -row['referrals']))

    return {
        'total_paid': float(_sum_amounts(payouts, PAID_STATUSES)),
        'total_pending': float(_sum_amounts(payouts, PENDING_STATUSES)),
        'referral_paid': float(_sum_amounts(referral_payouts, PAID_STATUSES)),
        'referral_pending': float(
            _sum_amounts(referral_payouts, PENDING_STATUSES)
        ),
        'chain_paid': float(_sum_amounts(chain_payouts, PAID_STATUSES)),
        'chain_pending': float(_sum_amounts(chain_payouts, PENDING_STATUSES)),
        'referrals_by_status': referrals_by_status,
        'referrals_total': len(referrals),
        'direct_pitches_total': len(direct_pitches),
        'matches_total': len(matches),
        'completed_matches': sum(
            1 for m in matches
            if m.hunter_confirmed and m.employer_confirmed
        ),
        'per_need': per_need,
    }
