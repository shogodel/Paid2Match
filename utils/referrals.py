"""Referral completion (M1, §4): what happens when a match is confirmed
by both sides.

- The referrer earns +50 karma (capped +150/day), +1 "helped", +1 free
  bump banked for M3 (§4.2, §1.3).
- A win post is auto-generated for the referrer (§1.1) — they can delete
  it, which is how they decline the public story.
- Sharers of the need get attribution credit + karma (§4.4): +10 karma
  each and a notification. Two-level splits are M4, not here.
- Circular referrals (A referred B, B referred A) earn zero karma (§4.2).

Completion rewards apply to ALL completed matches — referral-linked and
direct pitches alike — because a completed match is a completed referral.
The consent flow is the preferred path (the Refer button), but direct
pitching remains for referrers representing someone.
"""
from extensions import db
from models import utc_now
from models.post import Post, Share
from models.referral import KarmaEvent, Referral
from utils.chains import compute_chain_split, resolve_chain_sharer
from utils.karma import (
    KARMA_REFERRAL_COMPLETED, KARMA_SHARE_LED_TO_HIRE, award_karma,
    record_helped,
)
from utils.notifications import notify


def _is_circular(referral, referrer_id):
    """True if the referred person previously completed a referral of the
    referrer (A refers B, B refers A)."""
    if not referral or not referral.referred_user_id:
        return False
    return Referral.query.filter_by(
        referrer_id=referral.referred_user_id,
        referred_user_id=referrer_id,
        status='completed',
    ).first() is not None


def complete_referral_for_match(match):
    """Run the completion side effects for a dual-confirmed match.
    Idempotent: safe to call twice (the second call is a no-op).
    Does not commit — the caller commits."""
    referral = None
    if match.pitch_id:
        referral = Referral.query.filter_by(pitch_id=match.pitch_id).first()
    if referral and referral.status == 'completed':
        return {'already_completed': True}
    if not referral and KarmaEvent.query.filter_by(
        reason='referral_completed', ref_type='match', ref_id=match.id
    ).first():
        return {'already_completed': True}

    referrer_id = match.hunter_id
    bounty = match.bounty
    circular = _is_circular(referral, referrer_id)

    karma_awarded = 0
    if not circular:
        karma_awarded = award_karma(
            referrer_id, KARMA_REFERRAL_COMPLETED, 'referral_completed',
            ref_type='match', ref_id=match.id,
        )
    record_helped(referrer_id)

    if referral:
        referral.status = 'completed'
        referral.completed_at = utc_now()
        notify(
            referrer_id, 'referral_completed',
            f"Your referral for \"{bounty.title}\" is complete!",
            'The match was confirmed by both sides.',
            link_url=f'/bounties/matches/{match.id}/confirmed',
        )

    # Win post, auto-generated for the referrer (§1.1). The amount is
    # what the referrer actually receives: with a chain (M4), the net
    # splits 70/30 with the sharer, so the referrer's share is 70%.
    referrer = match.hunter
    referred_name = referral.referred_name if referral else 'a great candidate'
    net = float(match.payout_amount or 0) - float(match.platform_fee or 0)
    if resolve_chain_sharer(match):
        net = float(compute_chain_split(net)[0])
    if bounty and bounty.reward_amount and float(bounty.reward_amount) > 0:
        win_body = (
            f"{referrer.full_name} referred {referred_name} for "
            f"\"{bounty.title}\" — and it worked! Earned ${net:,.0f}."
        )
    else:
        win_body = (
            f"{referrer.full_name} referred {referred_name} for "
            f"\"{bounty.title if bounty else 'a community need'}\" — and it worked! "
            "Another person helped, no money involved."
        )
    win_post = Post(
        author_id=referrer_id, post_type='win', body=win_body,
        bounty_id=bounty.id if bounty else None,
    )
    db.session.add(win_post)

    # Share-chain credit (§4.4): sharers of the need's post, other than
    # the referrer and the poster, get +10 karma + a notification.
    sharers_credited = []
    if bounty:
        shares = Share.query.join(
            Post, Share.post_id == Post.id
        ).filter(Post.bounty_id == bounty.id).all()
        seen = set()
        for share in shares:
            if share.user_id in (referrer_id, bounty.poster_id) or share.user_id in seen:
                continue
            seen.add(share.user_id)
            award_karma(
                share.user_id, KARMA_SHARE_LED_TO_HIRE, 'share_led_to_hire',
                ref_type='match', ref_id=match.id,
            )
            notify(
                share.user_id, 'share_led_to_hire',
                'Your share led to a hire!',
                f"You shared \"{bounty.title}\" and someone in your audience "
                "referred the winning person.",
                link_url=f'/bounties/{bounty.id}',
            )
            sharers_credited.append(share.user_id)

    return {
        'already_completed': False,
        'karma_awarded': karma_awarded,
        'circular': circular,
        'win_post': win_post,
        'sharers_credited': sharers_credited,
    }
