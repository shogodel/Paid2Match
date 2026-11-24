"""Legacy /bounties/* redirects (nice URLs).

The bounties blueprint moved to /needs. Every old URL 301s to its
new equivalent; anything unmapped lands on the board. The Stripe
webhook is the exception: Stripe POSTs must not be redirected (a
301 turns the POST into a GET and the signature check would fail),
so the webhook stays live at the old URL as well as the new one —
the Stripe dashboard URL needs no change.
"""
import uuid

from flask import Blueprint, redirect, request, url_for

from extensions import db
from models.bounty import Bounty

legacy_bounties_bp = Blueprint('legacy_bounties', __name__)

#: Old single-segment routes with no id in them.
_SIMPLE_ROUTES = {
    'post': 'bounties.post',
    'recruitment': 'bounties.recruitment_board',
    'services': 'bounties.services_board',
    'api': 'bounties.api_list',
    'check-expired': 'bounties.check_expired',
    'my-invitations': 'bounties.my_invitations',
}

#: /<bounty_id>/<action> sub-routes (the id is still a UUID there).
_BOUNTY_ACTIONS = {
    'edit': 'bounties.edit',
    'payment-link': 'bounties.payment_link',
    'pitch': 'bounties.pitch',
    'secure-payment': 'bounties.secure_payment',
    'secure-third-party': 'bounties.secure_third_party',
    'expired': 'bounties.expired',
    'repost': 'bounties.repost',
    'cancel-refund': 'bounties.cancel_refund',
    'upgrade': 'bounties.upgrade',
    'upgrade-success': 'bounties.upgrade_success',
    'upgrade/extend': 'bounties.extend_upgrade',
    'close': 'bounties.close',
}

#: /pitches/<pitch_id>/<action>.
_PITCH_ACTIONS = {
    'respond': 'bounties.respond_to_pitch',
    'confirm-match': 'bounties.confirm_match',
}

#: /matches/<match_id>/<action>.
_MATCH_ACTIONS = {
    'pending': 'bounties.pending_match',
    'confirmed': 'bounties.confirmed_match',
    'rate': 'bounties.rate_match',
    'dispute': 'bounties.file_dispute',
}


def _with_query(url):
    """Carry the query string across the redirect (?paid=1 and
    board filters survive the move)."""
    qs = request.query_string.decode('utf-8', 'replace')
    return f'{url}?{qs}' if qs else url


def _board():
    return redirect(_with_query(url_for('bounties.board')), code=301)


@legacy_bounties_bp.route('/')
def board():
    return _board()


@legacy_bounties_bp.route('/<first>', methods=['GET', 'POST'])
def _single(first):
    endpoint = _SIMPLE_ROUTES.get(first)
    if endpoint:
        return redirect(_with_query(url_for(endpoint)), code=301)
    # Otherwise it is a bounty id — resolve it to build the slug URL.
    bounty = None
    try:
        uuid.UUID(first)
        bounty = db.session.get(Bounty, first)
    except ValueError:
        pass
    if bounty is not None:
        return redirect(
            _with_query(url_for(
                'bounties.detail', slug_code=bounty.url_slug
            )),
            code=301,
        )
    return _board()


@legacy_bounties_bp.route('/<first>/<path:rest>', methods=['GET', 'POST'])
def _nested(first, rest):
    if first == 'pitches':
        parts = rest.split('/')
        if len(parts) == 2 and parts[1] in _PITCH_ACTIONS:
            return redirect(_with_query(url_for(
                _PITCH_ACTIONS[parts[1]], pitch_id=parts[0]
            )), code=301)
    elif first == 'matches':
        parts = rest.split('/')
        if len(parts) == 2 and parts[1] in _MATCH_ACTIONS:
            return redirect(_with_query(url_for(
                _MATCH_ACTIONS[parts[1]], match_id=parts[0]
            )), code=301)
    elif first == 'invitation':
        if '/' not in rest:
            return redirect(_with_query(url_for(
                'bounties.invitation', agreement_id=rest
            )), code=301)
    elif rest in _BOUNTY_ACTIONS:
        # <bounty_id>/<action> — the sub-routes still take the UUID,
        # so the id carries straight over (unknown ids land on the
        # board instead of a 404 on the new side).
        bounty = None
        try:
            uuid.UUID(first)
            bounty = db.session.get(Bounty, first)
        except ValueError:
            pass
        if bounty is not None:
            return redirect(_with_query(url_for(
                _BOUNTY_ACTIONS[rest], id=first
            )), code=301)
    # Catch-all: anything else under /bounties/* goes to the board.
    return _board()


# The Stripe webhook stays live at the old URL (see the module
# docstring). Same view function as the /needs one, so the CSRF
# exemption in app.py (matched by function location) covers it.
from blueprints.bounties.routes import stripe_webhook  # noqa: E402

legacy_bounties_bp.add_url_rule(
    '/stripe-webhook', 'stripe_webhook', stripe_webhook, methods=['POST']
)
