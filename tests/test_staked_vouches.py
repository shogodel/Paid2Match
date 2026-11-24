"""Tests for staked vouches — trust backed by money, not clicks.

A stake is a RECORDED commitment only: no money moves, no Stripe
calls. The tests assert the record behaviour — validation, the
one-active-stake rule, resolution statuses, permissions, and the
profile / need-page display. Auth is OAuth-only, so tests log in
via the session (see conftest login_as).
"""
from decimal import Decimal

import pytest

from extensions import db
from models.staked_vouch import StakedVouch


def _make_stake(voucher, candidate, amount=100, currency='CAD',
                bounty=None):
    """Create + commit a stake directly through the model."""
    stake = StakedVouch.create(
        voucher_id=voucher.id,
        candidate_id=candidate.id,
        amount=Decimal(str(amount)),
        currency=currency,
        bounty_id=bounty.id if bounty else None,
    )
    db.session.commit()
    return stake


# ---------------------------------------------------------------- model


class TestStakedVouchModel:
    def test_amount_zero_rejected(self, app, sample_user, second_user):
        with app.app_context():
            with pytest.raises(ValueError, match='greater than zero'):
                StakedVouch(
                    voucher_id=sample_user.id,
                    candidate_id=second_user.id,
                    amount=Decimal('0'),
                )

    def test_amount_negative_rejected(self, app, sample_user, second_user):
        with app.app_context():
            with pytest.raises(ValueError, match='greater than zero'):
                StakedVouch(
                    voucher_id=sample_user.id,
                    candidate_id=second_user.id,
                    amount=Decimal('-50'),
                )

    def test_amount_none_rejected(self, app, sample_user, second_user):
        with app.app_context():
            with pytest.raises(ValueError, match='greater than zero'):
                StakedVouch(
                    voucher_id=sample_user.id,
                    candidate_id=second_user.id,
                    amount=None,
                )

    def test_bad_currency_rejected(self, app, sample_user, second_user):
        with app.app_context():
            with pytest.raises(ValueError, match='CAD'):
                StakedVouch(
                    voucher_id=sample_user.id,
                    candidate_id=second_user.id,
                    amount=Decimal('100'),
                    currency='GBP',
                )

    def test_currency_normalised_uppercase(
            self, app, sample_user, second_user):
        with app.app_context():
            stake = StakedVouch(
                voucher_id=sample_user.id,
                candidate_id=second_user.id,
                amount=Decimal('100'),
                currency='eur',
            )
            assert stake.currency == 'EUR'

    def test_self_stake_rejected(self, app, sample_user):
        with app.app_context():
            with pytest.raises(ValueError, match='yourself'):
                StakedVouch(
                    voucher_id=sample_user.id,
                    candidate_id=sample_user.id,
                    amount=Decimal('100'),
                )

    def test_bad_status_rejected(self, app, sample_user, second_user):
        with app.app_context():
            with pytest.raises(ValueError, match='Invalid staked vouch'):
                StakedVouch(
                    voucher_id=sample_user.id,
                    candidate_id=second_user.id,
                    amount=Decimal('100'),
                    status='bogus',
                )

    def test_valid_stake_saves_with_defaults(
            self, app, sample_user, second_user, sample_bounty):
        with app.app_context():
            stake = _make_stake(
                sample_user, second_user, amount=250, currency='EUR',
                bounty=sample_bounty,
            )
            assert stake.status == 'active'
            assert stake.currency == 'EUR'
            assert float(stake.amount) == 250.0
            assert stake.resolved_at is None
            assert stake.resolved_by_id is None
            assert stake.bounty_id == sample_bounty.id
            assert stake.to_dict()['status'] == 'active'

    def test_duplicate_active_stake_blocked(
            self, app, sample_user, second_user, sample_bounty):
        with app.app_context():
            _make_stake(sample_user, second_user, bounty=sample_bounty)
            with pytest.raises(ValueError, match='already have an active'):
                StakedVouch.create(
                    voucher_id=sample_user.id,
                    candidate_id=second_user.id,
                    amount=Decimal('50'),
                    bounty_id=sample_bounty.id,
                )

    def test_duplicate_general_stake_blocked(
            self, app, sample_user, second_user):
        with app.app_context():
            _make_stake(sample_user, second_user)
            with pytest.raises(ValueError, match='already have an active'):
                StakedVouch.create(
                    voucher_id=sample_user.id,
                    candidate_id=second_user.id,
                    amount=Decimal('50'),
                )

    def test_same_pair_different_bounty_allowed(
            self, app, sample_user, second_user, sample_bounty,
            sample_paid_bounty):
        with app.app_context():
            _make_stake(sample_user, second_user, bounty=sample_bounty)
            stake = StakedVouch.create(
                voucher_id=sample_user.id,
                candidate_id=second_user.id,
                amount=Decimal('50'),
                bounty_id=sample_paid_bounty.id,
            )
            db.session.commit()
            assert stake.status == 'active'

    def test_general_and_need_stakes_are_different_triples(
            self, app, sample_user, second_user, sample_bounty):
        with app.app_context():
            _make_stake(sample_user, second_user)
            stake = StakedVouch.create(
                voucher_id=sample_user.id,
                candidate_id=second_user.id,
                amount=Decimal('50'),
                bounty_id=sample_bounty.id,
            )
            db.session.commit()
            assert stake.status == 'active'

    def test_resolved_stake_frees_the_triple(
            self, app, sample_user, second_user, sample_bounty):
        with app.app_context():
            stake = _make_stake(
                sample_user, second_user, bounty=sample_bounty)
            stake.resolve('completed', sample_user.id)
            db.session.commit()
            again = StakedVouch.create(
                voucher_id=sample_user.id,
                candidate_id=second_user.id,
                amount=Decimal('75'),
                bounty_id=sample_bounty.id,
            )
            db.session.commit()
            assert again.status == 'active'


# ---------------------------------------------------------------- create


class TestCreate:
    def test_new_page_renders_form(
            self, app, client, login_as, sample_user, second_user):
        login_as(client, 'testuser@example.com')
        r = client.get(f'/stakes/new?candidate_id={second_user.id}')
        assert r.status_code == 200
        assert b'Stake on Second User' in r.data
        assert b'name="amount"' in r.data
        assert b'name="currency"' in r.data
        # Record-only copy: no money moves.
        assert b'no money moves' in r.data

    def test_new_page_needs_candidate(
            self, app, client, login_as, sample_user):
        login_as(client, 'testuser@example.com')
        r = client.get('/stakes/new')
        assert r.status_code == 302

    def test_new_page_self_stake_blocked(
            self, app, client, login_as, sample_user):
        login_as(client, 'testuser@example.com')
        r = client.get(f'/stakes/new?candidate_id={sample_user.id}')
        assert r.status_code == 302

    def test_create_happy_path_from_profile(
            self, app, client, login_as, sample_user, second_user):
        login_as(client, 'testuser@example.com')
        r = client.post('/stakes/create', data={
            'candidate_id': second_user.id,
            'amount': '100',
            'currency': 'CAD',
            'next': '/stakes/',
        })
        assert r.status_code == 302
        with app.app_context():
            stake = StakedVouch.query.first()
            assert stake is not None
            assert stake.voucher_id == sample_user.id
            assert stake.candidate_id == second_user.id
            assert stake.bounty_id is None
            assert float(stake.amount) == 100.0
            assert stake.currency == 'CAD'
            assert stake.status == 'active'

    def test_create_from_pitch_stores_bounty_and_currency(
            self, app, client, login_as, sample_user, sample_hunter,
            sample_bounty):
        # The poster stakes on the pitch hunter for this need.
        login_as(client, 'testuser@example.com')
        r = client.post('/stakes/create', data={
            'candidate_id': sample_hunter.id,
            'bounty_id': sample_bounty.id,
            'amount': '250.50',
            'currency': 'EUR',
            'next': f'/needs/{sample_bounty.url_slug}',
        })
        assert r.status_code == 302
        with app.app_context():
            stake = StakedVouch.query.first()
            assert stake.bounty_id == sample_bounty.id
            assert float(stake.amount) == 250.50
            assert stake.currency == 'EUR'

    def test_create_bad_amount_rejected(
            self, app, client, login_as, sample_user, second_user):
        login_as(client, 'testuser@example.com')
        for bad in ('0', '-10', 'abc', ''):
            r = client.post('/stakes/create', data={
                'candidate_id': second_user.id,
                'amount': bad,
                'currency': 'CAD',
            }, follow_redirects=True)
            assert r.status_code == 200
        with app.app_context():
            assert StakedVouch.query.count() == 0

    def test_create_bad_currency_rejected(
            self, app, client, login_as, sample_user, second_user):
        login_as(client, 'testuser@example.com')
        r = client.post('/stakes/create', data={
            'candidate_id': second_user.id,
            'amount': '100',
            'currency': 'GBP',
        })
        assert r.status_code == 302
        with app.app_context():
            assert StakedVouch.query.count() == 0

    def test_create_self_stake_rejected(
            self, app, client, login_as, sample_user):
        login_as(client, 'testuser@example.com')
        r = client.post('/stakes/create', data={
            'candidate_id': sample_user.id,
            'amount': '100',
            'currency': 'CAD',
        })
        assert r.status_code == 302
        with app.app_context():
            assert StakedVouch.query.count() == 0

    def test_create_duplicate_rejected(
            self, app, client, login_as, sample_user, second_user,
            sample_bounty):
        with app.app_context():
            _make_stake(sample_user, second_user, bounty=sample_bounty)
        login_as(client, 'testuser@example.com')
        r = client.post('/stakes/create', data={
            'candidate_id': second_user.id,
            'bounty_id': sample_bounty.id,
            'amount': '50',
            'currency': 'CAD',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'already have an active stake' in r.data
        with app.app_context():
            assert StakedVouch.query.count() == 1

    def test_create_requires_login(self, client, second_user):
        r = client.post('/stakes/create', data={
            'candidate_id': second_user.id,
            'amount': '100',
            'currency': 'CAD',
        })
        assert r.status_code == 302
        assert '/auth/login' in r.headers['Location']


# ---------------------------------------------------------------- resolve


class TestResolve:
    def test_poster_resolves_completed(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter, sample_bounty):
        with app.app_context():
            stake = _make_stake(
                second_user, sample_hunter, bounty=sample_bounty)
            stake_id = stake.id
        login_as(client, 'testuser@example.com')  # the poster
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'completed',
        })
        assert r.status_code == 302
        with app.app_context():
            stake = db.session.get(StakedVouch, stake_id)
            assert stake.status == 'released'
            assert stake.resolved_at is not None
            assert stake.resolved_by_id == sample_user.id

    def test_poster_resolves_flaked(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter, sample_bounty):
        with app.app_context():
            stake = _make_stake(
                second_user, sample_hunter, bounty=sample_bounty)
            stake_id = stake.id
        login_as(client, 'testuser@example.com')
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'flaked',
        })
        assert r.status_code == 302
        with app.app_context():
            stake = db.session.get(StakedVouch, stake_id)
            assert stake.status == 'forfeited'
            assert stake.resolved_at is not None
            assert stake.resolved_by_id == sample_user.id

    def test_poster_resolves_cancelled(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter, sample_bounty):
        with app.app_context():
            stake = _make_stake(
                second_user, sample_hunter, bounty=sample_bounty)
            stake_id = stake.id
        login_as(client, 'testuser@example.com')
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'cancelled',
        })
        assert r.status_code == 302
        with app.app_context():
            stake = db.session.get(StakedVouch, stake_id)
            assert stake.status == 'returned'
            assert stake.resolved_at is not None
            assert stake.resolved_by_id == sample_user.id

    def test_resolving_non_active_stake_rejected(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter, sample_bounty):
        with app.app_context():
            stake = _make_stake(
                second_user, sample_hunter, bounty=sample_bounty)
            stake.resolve('completed', sample_user.id)
            db.session.commit()
            stake_id = stake.id
        login_as(client, 'testuser@example.com')
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'flaked',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'Only an active stake can be resolved' in r.data
        with app.app_context():
            stake = db.session.get(StakedVouch, stake_id)
            assert stake.status == 'released'

    def test_bad_outcome_rejected(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter, sample_bounty):
        with app.app_context():
            stake = _make_stake(
                second_user, sample_hunter, bounty=sample_bounty)
            stake_id = stake.id
        login_as(client, 'testuser@example.com')
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'maybe',
        })
        assert r.status_code == 302
        with app.app_context():
            stake = db.session.get(StakedVouch, stake_id)
            assert stake.status == 'active'

    def test_model_resolve_bad_outcome(
            self, app, sample_user, second_user):
        with app.app_context():
            stake = _make_stake(sample_user, second_user)
            with pytest.raises(ValueError, match='Unknown stake outcome'):
                stake.resolve('maybe', sample_user.id)

    def test_model_resolve_non_active(
            self, app, sample_user, second_user):
        with app.app_context():
            stake = _make_stake(sample_user, second_user)
            stake.resolve('completed', sample_user.id)
            with pytest.raises(ValueError, match='Only an active stake'):
                stake.resolve('flaked', sample_user.id)


# ---------------------------------------------------------------- permissions


class TestResolvePermissions:
    def test_voucher_cannot_resolve(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter, sample_bounty):
        # The voucher is not the poster — resolving is not theirs.
        with app.app_context():
            stake = _make_stake(
                second_user, sample_hunter, bounty=sample_bounty)
            stake_id = stake.id
        login_as(client, 'second@example.com')
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'completed',
        })
        assert r.status_code == 403

    def test_unrelated_user_cannot_resolve(
            self, app, client, login_as, sample_user, second_user,
            third_user, sample_hunter, sample_bounty):
        with app.app_context():
            stake = _make_stake(
                second_user, sample_hunter, bounty=sample_bounty)
            stake_id = stake.id
        login_as(client, 'third@example.com')
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'completed',
        })
        assert r.status_code == 403

    def test_candidate_cannot_resolve(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter, sample_bounty):
        with app.app_context():
            stake = _make_stake(
                second_user, sample_hunter, bounty=sample_bounty)
            stake_id = stake.id
        login_as(client, 'hunter@example.com')
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'completed',
        })
        assert r.status_code == 403

    def test_admin_can_resolve(
            self, app, client, login_as, sample_admin, second_user,
            sample_hunter, sample_bounty):
        with app.app_context():
            stake = _make_stake(
                second_user, sample_hunter, bounty=sample_bounty)
            stake_id = stake.id
        login_as(client, 'admin@example.com')
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'completed',
        })
        assert r.status_code == 302
        with app.app_context():
            stake = db.session.get(StakedVouch, stake_id)
            assert stake.status == 'released'
            assert stake.resolved_by_id == sample_admin.id

    def test_general_stake_is_admin_only(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter, sample_admin):
        # No need attached — there is no poster, so only an admin
        # may resolve. A regular user (even the need poster of an
        # unrelated need) is forbidden.
        with app.app_context():
            stake = _make_stake(second_user, sample_hunter)
            stake_id = stake.id
        login_as(client, 'testuser@example.com')
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'completed',
        })
        assert r.status_code == 403
        login_as(client, 'admin@example.com')
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'completed',
        })
        assert r.status_code == 302
        with app.app_context():
            stake = db.session.get(StakedVouch, stake_id)
            assert stake.status == 'released'

    def test_resolve_requires_login(
            self, app, client, sample_user, second_user):
        with app.app_context():
            stake = _make_stake(sample_user, second_user)
            stake_id = stake.id
        r = client.post(f'/stakes/{stake_id}/resolve', data={
            'outcome': 'completed',
        })
        assert r.status_code == 302
        assert '/auth/login' in r.headers['Location']


# ---------------------------------------------------------------- display


class TestDisplay:
    def test_stake_shows_on_candidate_profile(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter):
        with app.app_context():
            _make_stake(second_user, sample_hunter, amount=150)
            handle = sample_hunter.handle
        login_as(client, 'testuser@example.com')
        r = client.get(f'/profile/{handle}')
        assert r.status_code == 200
        assert b'Staked vouches' in r.data
        assert b'CA$150.00' in r.data
        assert b'Second User' in r.data
        assert b'Active' in r.data

    def test_stake_form_on_other_profile(
            self, app, client, login_as, sample_user, second_user):
        login_as(client, 'testuser@example.com')
        r = client.get(f'/profile/{second_user.handle}')
        assert r.status_code == 200
        assert b'Stake on this person' in r.data
        assert b'name="amount"' in r.data

    def test_no_stake_form_on_own_public_profile(
            self, app, client, login_as, sample_user):
        login_as(client, 'testuser@example.com')
        r = client.get(f'/profile/{sample_user.handle}')
        assert r.status_code == 200
        assert b'Stake on this person' not in r.data

    def test_own_profile_shows_active_stakes_placed(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter):
        with app.app_context():
            _make_stake(sample_user, second_user, amount=200)
            _make_stake(sample_user, sample_hunter, amount=75,
                        currency='USD')
        login_as(client, 'testuser@example.com')
        r = client.get('/profile/')
        assert r.status_code == 200
        assert b'My staked vouches' in r.data
        assert b'CA$200.00' in r.data
        assert b'US$75.00' in r.data
        assert b'Second User' in r.data
        assert b'Hunter User' in r.data

    def test_my_stakes_lists_placed_and_received(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter):
        with app.app_context():
            _make_stake(sample_user, second_user, amount=200)
            _make_stake(second_user, sample_user, amount=60)
        login_as(client, 'testuser@example.com')
        r = client.get('/stakes/')
        assert r.status_code == 200
        assert b'Stakes I placed' in r.data
        assert b'Stakes on me' in r.data
        assert b'CA$200.00' in r.data
        assert b'CA$60.00' in r.data

    def test_stake_form_on_pitch_row_for_poster(
            self, app, client, login_as, sample_user, sample_pitch,
            sample_bounty):
        # The pitch must be 'interested' (candidate revealed) for
        # the stake form to show on the row.
        with app.app_context():
            from models.pitch import Pitch
            pitch = db.session.get(Pitch, sample_pitch.id)
            pitch.status = 'interested'
            db.session.commit()
        login_as(client, 'testuser@example.com')  # the poster
        r = client.get(f'/needs/{sample_bounty.url_slug}')
        assert r.status_code == 200
        assert b'Stake on this person' in r.data
        assert sample_pitch.hunter_id.encode() in r.data

    def test_need_stakes_visible_to_poster_only(
            self, app, client, login_as, sample_user, second_user,
            sample_hunter, sample_bounty):
        with app.app_context():
            _make_stake(second_user, sample_hunter, amount=300,
                        bounty=sample_bounty)
        # Poster sees the stakes card + resolve buttons.
        login_as(client, 'testuser@example.com')
        r = client.get(f'/needs/{sample_bounty.url_slug}')
        assert r.status_code == 200
        assert b'CA$300.00' in r.data
        assert b'Completed / hired' in r.data
        # Another visitor does not (stakes name their candidate,
        # which would de-anonymise pending pitches).
        login_as(client, 'hunter@example.com')
        r = client.get(f'/needs/{sample_bounty.url_slug}')
        assert r.status_code == 200
        assert b'CA$300.00' not in r.data
        assert b'Completed / hired' not in r.data
