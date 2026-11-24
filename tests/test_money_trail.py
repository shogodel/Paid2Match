"""Money Trail tests — the public chain of a completed hire.

Covers build_trail (utils/money_trail.py) and the public trail page
(GET /needs/<slug>-<shortcode>/trail). Payout rows are built
directly — no Stripe calls anywhere; the trail only reads them.
"""
from decimal import Decimal

from extensions import db
from models.bounty import Bounty
from models.match import Match
from models.payout import Payout
from models.pitch import Pitch
from models.post import Share
from models.referral import Referral
from utils.feed import create_need_post
from utils.money_trail import build_trail, completed_match_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _paid_bounty(app, poster, reward=1000, currency='CAD', title='Paid need'):
    bounty = Bounty(
        poster_id=poster.id,
        title=title,
        description='A detailed description of the need, long enough to pass validation checks.',
        bounty_type='recruitment',
        bounty_direction='seeking_opportunity',
        reward_amount=reward,
        currency=currency,
        status='open',
        payment_status='secured',
        stripe_payment_intent='pi_test_escrow',
        location='Remote',
    )
    db.session.add(bounty)
    db.session.commit()
    db.session.refresh(bounty)
    return bounty


def _free_bounty(app, poster, title='Free need'):
    bounty = Bounty(
        poster_id=poster.id,
        title=title,
        description='A detailed description of the need, long enough to pass validation checks.',
        bounty_type='recruitment',
        bounty_direction='seeking_opportunity',
        reward_amount=None,
        status='open',
        payment_status='released',
        location='Remote',
    )
    db.session.add(bounty)
    db.session.commit()
    db.session.refresh(bounty)
    return bounty


def _share(app, user, bounty):
    post = create_need_post(bounty)
    db.session.commit()
    share = Share(user_id=user.id, post_id=post.id,
                  comment='Sharing this — great opportunity!')
    db.session.add(share)
    db.session.commit()
    db.session.refresh(share)
    return share


def _confirmed_match(app, bounty, hunter, poster, via_share_id=None,
                     confirmed=True):
    pitch = Pitch(bounty_id=bounty.id, hunter_id=hunter.id,
                  candidate_teaser='x' * 60, status='accepted',
                  via_share_id=via_share_id)
    db.session.add(pitch)
    db.session.flush()
    match = Match(
        bounty_id=bounty.id, pitch_id=pitch.id,
        hunter_id=hunter.id, employer_id=poster.id,
        hunter_confirmed=confirmed, employer_confirmed=confirmed,
        payout_amount=float(bounty.reward_amount or 0) or None,
        platform_fee=float(bounty.reward_amount or 0) * 0.20 or None,
        payout_status='pending',
    )
    db.session.add(match)
    db.session.commit()
    db.session.refresh(match)
    return match


def _referral(app, bounty, referrer, pitch, hired_user=None,
              via_share_id=None, name='Hired Person'):
    referral = Referral(
        referrer_id=referrer.id,
        bounty_id=bounty.id,
        pitch_id=pitch.id,
        via_share_id=via_share_id,
        referred_name=hired_user.full_name if hired_user else name,
        referred_email=(
            hired_user.email if hired_user else 'hired@example.com'
        ),
        referred_user_id=hired_user.id if hired_user else None,
        message='They are a great fit for this need and I vouch for them.',
        status='completed',
    )
    db.session.add(referral)
    db.session.commit()
    db.session.refresh(referral)
    return referral


def _payouts(app, match, bounty, sharer=None, currency=None):
    """The Payout rows release_escrow would create for a $1,000
    reward at the 20% fee: $200 platform, $800 net, split 70/30
    ($560 referrer / $240 sharer) when there is a chain."""
    currency = currency or bounty.currency
    referrer_amount = Decimal('560') if sharer else Decimal('800')
    referrer_payout = Payout(
        match_id=match.id,
        referrer_id=match.hunter_id,
        bounty_id=bounty.id,
        kind='referral',
        gross_amount=Decimal('1000'),
        platform_fee=Decimal('200'),
        amount=referrer_amount,
        currency=currency,
        method='stripe',
        status='pending',
    )
    db.session.add(referrer_payout)
    chain_payout = None
    if sharer:
        chain_payout = Payout(
            match_id=match.id,
            referrer_id=sharer.id,
            bounty_id=bounty.id,
            kind='chain_share',
            gross_amount=Decimal('240'),
            platform_fee=Decimal('0'),
            amount=Decimal('240'),
            currency=currency,
            method='stripe',
            status='pending',
        )
        db.session.add(chain_payout)
    db.session.commit()
    return referrer_payout, chain_payout


def _roles(trail):
    return [hop['role'] for hop in trail]


def _amounts(trail):
    return {hop['role']: hop['amount'] for hop in trail}


# ---------------------------------------------------------------------------
# build_trail
# ---------------------------------------------------------------------------

class TestBuildTrail:
    def test_trail_with_chain(self, app, sample_user, sample_hunter,
                              second_user, third_user):
        bounty = _paid_bounty(app, sample_user)
        share = _share(app, second_user, bounty)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        _referral(app, bounty, sample_hunter, match.pitch, third_user,
                  via_share_id=share.id)
        _payouts(app, match, bounty, sharer=second_user)

        trail = build_trail(bounty)

        assert _roles(trail) == [
            'poster', 'platform', 'sharer', 'referrer', 'hired',
        ]
        assert _amounts(trail) == {
            'poster': Decimal('1000'),
            'platform': Decimal('200'),
            'sharer': Decimal('240'),
            'referrer': Decimal('560'),
            'hired': None,
        }
        assert all(hop['currency'] == 'CAD' for hop in trail)
        by_role = {hop['role']: hop for hop in trail}
        assert by_role['poster']['user'].id == sample_user.id
        assert by_role['platform']['user'] is None
        assert by_role['platform']['name'] == 'Paid2Match'
        assert by_role['sharer']['user'].id == second_user.id
        assert by_role['referrer']['user'].id == sample_hunter.id
        assert by_role['hired']['user'].id == third_user.id

    def test_trail_without_chain(self, app, sample_user, sample_hunter,
                                 third_user):
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _referral(app, bounty, sample_hunter, match.pitch, third_user)
        _payouts(app, match, bounty)

        trail = build_trail(bounty)

        assert _roles(trail) == ['poster', 'platform', 'referrer', 'hired']
        assert _amounts(trail) == {
            'poster': Decimal('1000'),
            'platform': Decimal('200'),
            'referrer': Decimal('800'),
            'hired': None,
        }

    def test_trail_free_need_has_no_money_figures(
            self, app, sample_user, sample_hunter, third_user):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _referral(app, bounty, sample_hunter, match.pitch, third_user)

        trail = build_trail(bounty)

        assert _roles(trail) == ['poster', 'platform', 'referrer', 'hired']
        assert all(hop['amount'] is None for hop in trail)

    def test_trail_empty_without_completed_match(
            self, app, sample_user, sample_hunter):
        bounty = _paid_bounty(app, sample_user)
        _confirmed_match(app, bounty, sample_hunter, sample_user,
                         confirmed=False)

        assert build_trail(bounty) == []
        assert completed_match_for(bounty) is None

    def test_multiple_matches_uses_the_completed_one(
            self, app, sample_user, sample_hunter, second_user):
        bounty = _paid_bounty(app, sample_user)
        # An unconfirmed match from another hunter exists first.
        _confirmed_match(app, bounty, second_user, sample_user,
                         confirmed=False)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _payouts(app, match, bounty)

        trail = build_trail(bounty)

        by_role = {hop['role']: hop for hop in trail}
        assert by_role['referrer']['user'].id == sample_hunter.id
        assert by_role['referrer']['amount'] == Decimal('800')

    def test_trail_without_payout_rows_derives_amounts(
            self, app, sample_user, sample_hunter, second_user):
        """A paid match whose Payout rows were never created still
        shows the right split, derived from the match's fee."""
        bounty = _paid_bounty(app, sample_user)
        share = _share(app, second_user, bounty)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        # No Payout rows at all.
        assert Payout.query.filter_by(match_id=match.id).count() == 0

        trail = build_trail(bounty)

        assert _amounts(trail) == {
            'poster': Decimal('1000'),
            'platform': Decimal('200'),
            'sharer': Decimal('240'),
            'referrer': Decimal('560'),
            'hired': None,
        }

    def test_hired_person_without_account_shows_name(
            self, app, sample_user, sample_hunter):
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _referral(app, bounty, sample_hunter, match.pitch,
                  hired_user=None, name='No Account Yet')
        _payouts(app, match, bounty)

        trail = build_trail(bounty)

        hired = [hop for hop in trail if hop['role'] == 'hired'][0]
        assert hired['user'] is None
        assert hired['name'] == 'No Account Yet'

    def test_direct_pitch_has_anonymous_hired_hop(
            self, app, sample_user, sample_hunter):
        """A direct pitch has no Referral — the hire is still shown,
        with no name attached."""
        bounty = _paid_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _payouts(app, match, bounty)

        trail = build_trail(bounty)

        hired = [hop for hop in trail if hop['role'] == 'hired'][0]
        assert hired['user'] is None
        assert hired['name'] is None


# ---------------------------------------------------------------------------
# Trail page
# ---------------------------------------------------------------------------

class TestTrailPage:
    def _setup_chain(self, app, sample_user, sample_hunter, second_user,
                     third_user, currency='CAD'):
        bounty = _paid_bounty(app, sample_user, currency=currency)
        share = _share(app, second_user, bounty)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user,
                                 via_share_id=share.id)
        _referral(app, bounty, sample_hunter, match.pitch, third_user,
                  via_share_id=share.id)
        _payouts(app, match, bounty, sharer=second_user,
                 currency=currency)
        return bounty

    def test_trail_page_shows_chain_and_amounts(
            self, client, app, sample_user, sample_hunter, second_user,
            third_user):
        bounty = self._setup_chain(app, sample_user, sample_hunter,
                                   second_user, third_user)

        r = client.get(f'/needs/{bounty.url_slug}/trail')

        assert r.status_code == 200
        html = r.data.decode()
        assert 'CA$1,000.00' in html
        assert 'CA$200.00' in html
        assert 'CA$240.00' in html
        assert 'CA$560.00' in html
        assert 'Test User' in html
        assert 'Second User' in html
        assert 'Third User' in html
        assert 'Money Trail' in html

    def test_trail_page_uncompleted_need_shows_empty_state(
            self, client, app, sample_user):
        bounty = _paid_bounty(app, sample_user)

        r = client.get(f'/needs/{bounty.url_slug}/trail')

        assert r.status_code == 200
        assert 'No completed hire yet' in r.data.decode()

    def test_trail_page_eur_amounts(
            self, client, app, sample_user, sample_hunter, second_user,
            third_user):
        bounty = self._setup_chain(app, sample_user, sample_hunter,
                                   second_user, third_user, currency='EUR')

        r = client.get(f'/needs/{bounty.url_slug}/trail')

        assert r.status_code == 200
        html = r.data.decode()
        assert '€1,000.00' in html
        assert '€560.00' in html
        assert '€240.00' in html
        assert 'CA$' not in html

    def test_trail_page_usd_amounts(
            self, client, app, sample_user, sample_hunter, second_user,
            third_user):
        bounty = self._setup_chain(app, sample_user, sample_hunter,
                                   second_user, third_user, currency='USD')

        r = client.get(f'/needs/{bounty.url_slug}/trail')

        assert r.status_code == 200
        html = r.data.decode()
        assert 'US$1,000.00' in html
        assert 'US$560.00' in html
        assert 'CA$' not in html

    def test_trail_page_free_need_shows_hops_without_money(
            self, client, app, sample_user, sample_hunter, third_user):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        _referral(app, bounty, sample_hunter, match.pitch, third_user)

        r = client.get(f'/needs/{bounty.url_slug}/trail')

        assert r.status_code == 200
        html = r.data.decode()
        assert 'Test User' in html
        assert 'Third User' in html
        assert 'CA$' not in html
        assert 'No completed hire yet' not in html

    def test_trail_page_404_for_unknown_need(self, client, app):
        r = client.get('/needs/no-such-need-zzzzzz/trail')
        assert r.status_code == 404

    def test_trail_page_stale_slug_redirects(
            self, client, app, sample_user):
        bounty = _paid_bounty(app, sample_user)
        r = client.get(f'/needs/wrong-slug-{bounty.shortcode}/trail')
        assert r.status_code == 301
        assert r.headers['Location'].endswith(
            f'/needs/{bounty.url_slug}/trail')

    def test_detail_links_to_trail_when_completed(
            self, client, app, sample_user, sample_hunter, second_user,
            third_user):
        bounty = self._setup_chain(app, sample_user, sample_hunter,
                                   second_user, third_user)

        html = client.get(f'/needs/{bounty.url_slug}').data.decode()

        assert f'/needs/{bounty.url_slug}/trail' in html
        assert 'See the Money Trail' in html

    def test_detail_has_no_trail_link_without_completed_match(
            self, client, app, sample_user):
        bounty = _paid_bounty(app, sample_user)

        html = client.get(f'/needs/{bounty.url_slug}').data.decode()

        assert 'See the Money Trail' not in html
        assert f'/needs/{bounty.url_slug}/trail' not in html

    def test_trail_page_in_french(
            self, client, app, sample_user, sample_hunter, second_user,
            third_user):
        """The trail page renders translated in French — the hop
        labels and empty state come from the compiled catalog."""
        from flask import g
        bounty = self._setup_chain(app, sample_user, sample_hunter,
                                   second_user, third_user)
        client.get('/set-language/fr')
        for attr in ('_p2m_locale', 'babel_locale'):
            if hasattr(g, attr):
                delattr(g, attr)

        html = client.get(f'/needs/{bounty.url_slug}/trail').data.decode()

        assert 'Parcours de l’argent' in html
        assert 'A publié le besoin' in html
        assert 'Frais de plateforme' in html
        assert 'A partagé le besoin' in html
        assert 'A recommandé la personne embauchée' in html
        assert 'A décroché le poste' in html
        assert 'Money Trail' not in html
