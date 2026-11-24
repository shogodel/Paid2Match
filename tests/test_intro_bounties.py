"""Warm-intro bounties: pay for an introduction, not just a hire."""
from decimal import Decimal

import pytest
from flask import g

from extensions import db
from models.intro_bounty import (
    IntroBounty, IntroClaim, IntroRewardEvent,
)


def _clear_babel_locale_cache():
    """Drop the per-request locale caches after switching language
    mid-test (same helper as tests/test_phase2.py)."""
    g.pop('_flask_babel', None)
    g.pop('_p2m_locale', None)


def _make_intro_bounty(app, poster_id, target='Hiring Manager at Acme',
                       amount=Decimal('50'), currency='CAD',
                       status='open', org='Acme Corp'):
    """Create an intro bounty directly in the DB."""
    with app.app_context():
        bounty = IntroBounty(
            poster_id=poster_id,
            target_name=target,
            target_org=org,
            context='I want to pitch our AI receptionist to them.',
            amount=amount,
            currency=currency,
            status=status,
        )
        db.session.add(bounty)
        db.session.commit()
        db.session.refresh(bounty)
        return bounty


def _make_claim(app, bounty_id, claimer_id, status='pending'):
    with app.app_context():
        claim = IntroClaim(
            intro_bounty_id=bounty_id,
            claimer_id=claimer_id,
            note='We worked together for years.',
            status=status,
        )
        db.session.add(claim)
        db.session.commit()
        db.session.refresh(claim)
        return claim


def _new_form_data(**overrides):
    data = {
        'target_name': 'Hiring Manager at Acme',
        'target_org': 'Acme Corp',
        'context': 'I want to pitch our AI receptionist to them.',
        'amount': '50',
        'currency': 'CAD',
        'submit': 'Post intro bounty',
    }
    data.update(overrides)
    return data


# ------------------------------------------------------- create + validate


class TestCreateIntroBounty:
    def test_create_happy_path(self, app, client, logged_in_user,
                               sample_user):
        r = client.post(
            '/intros/new', data=_new_form_data(), follow_redirects=True
        )
        assert r.status_code == 200
        assert b'Your intro bounty is live.' in r.data
        with app.app_context():
            bounty = IntroBounty.query.filter_by(
                poster_id=sample_user.id
            ).first()
            assert bounty is not None
            assert bounty.target_name == 'Hiring Manager at Acme'
            assert bounty.target_org == 'Acme Corp'
            assert bounty.amount == Decimal('50')
            assert bounty.currency == 'CAD'
            assert bounty.status == 'open'

    def test_new_requires_login(self, client):
        r = client.get('/intros/new')
        assert r.status_code == 302
        assert '/auth/login' in r.headers['Location']

    def test_amount_zero_rejected(self, app, client, logged_in_user,
                                  sample_user):
        r = client.post(
            '/intros/new', data=_new_form_data(amount='0'),
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b'The reward must be more than zero.' in r.data
        with app.app_context():
            assert IntroBounty.query.filter_by(
                poster_id=sample_user.id
            ).count() == 0

    def test_amount_negative_rejected(self, app, client, logged_in_user,
                                      sample_user):
        r = client.post(
            '/intros/new', data=_new_form_data(amount='-25'),
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b'The reward must be more than zero.' in r.data
        with app.app_context():
            assert IntroBounty.query.filter_by(
                poster_id=sample_user.id
            ).count() == 0

    def test_bad_currency_rejected(self, app, client, logged_in_user,
                                   sample_user):
        r = client.post(
            '/intros/new', data=_new_form_data(currency='GBP'),
            follow_redirects=True,
        )
        assert r.status_code == 200
        with app.app_context():
            assert IntroBounty.query.filter_by(
                poster_id=sample_user.id
            ).count() == 0

    def test_eur_currency_accepted(self, app, client, logged_in_user,
                                   sample_user):
        r = client.post(
            '/intros/new', data=_new_form_data(currency='EUR'),
            follow_redirects=True,
        )
        assert r.status_code == 200
        with app.app_context():
            bounty = IntroBounty.query.filter_by(
                poster_id=sample_user.id
            ).first()
            assert bounty is not None
            assert bounty.currency == 'EUR'

    def test_model_rejects_zero_amount(self, app, sample_user):
        with app.app_context():
            with pytest.raises(ValueError, match='greater than 0'):
                IntroBounty(
                    poster_id=sample_user.id,
                    target_name='Someone',
                    context='Why.',
                    amount=Decimal('0'),
                )

    def test_model_rejects_bad_currency(self, app, sample_user):
        with app.app_context():
            with pytest.raises(ValueError, match='currency must be'):
                IntroBounty(
                    poster_id=sample_user.id,
                    target_name='Someone',
                    context='Why.',
                    amount=Decimal('50'),
                    currency='GBP',
                )

    def test_model_defaults_currency_to_cad(self, app, sample_user):
        with app.app_context():
            bounty = IntroBounty(
                poster_id=sample_user.id,
                target_name='Someone',
                context='Why.',
                amount=Decimal('50'),
            )
            db.session.add(bounty)
            db.session.commit()
            assert bounty.currency == 'CAD'


# ------------------------------------------------------------------- list


class TestIntroBountyList:
    def test_list_shows_open_only(self, app, client, sample_user):
        _make_intro_bounty(app, sample_user.id, target='Open Target')
        _make_intro_bounty(
            app, sample_user.id, target='Claimed Target', status='claimed')
        _make_intro_bounty(
            app, sample_user.id, target='Completed Target',
            status='completed')
        _make_intro_bounty(
            app, sample_user.id, target='Cancelled Target',
            status='cancelled')
        html = client.get('/intros/').data.decode()
        assert 'Open Target' in html
        assert 'Claimed Target' not in html
        assert 'Completed Target' not in html
        assert 'Cancelled Target' not in html

    def test_list_newest_first(self, app, client, sample_user):
        from datetime import datetime, timedelta, timezone
        first = _make_intro_bounty(
            app, sample_user.id, target='First Target')
        _make_intro_bounty(app, sample_user.id, target='Second Target')
        with app.app_context():
            bounty = db.session.get(IntroBounty, first.id)
            bounty.created_at = (
                datetime.now(timezone.utc) - timedelta(days=1)
            )
            db.session.commit()
        html = client.get('/intros/').data.decode()
        assert html.index('Second Target') < html.index('First Target')

    def test_list_shows_money_with_currency(self, app, client, sample_user):
        _make_intro_bounty(
            app, sample_user.id, target='Euro Target',
            amount=Decimal('75'), currency='EUR')
        html = client.get('/intros/').data.decode()
        assert '€75.00' in html

    def test_list_translates_to_french(self, app, client, sample_user):
        _make_intro_bounty(app, sample_user.id, target='Open Target')
        client.get('/set-language/fr')
        _clear_babel_locale_cache()
        html = client.get('/intros/').data.decode()
        assert 'Primes d’introduction' in html
        assert 'Intro bounties</h2>' not in html


# ------------------------------------------------------------------ claim


class TestClaimFlow:
    def test_claim_happy_path(self, app, client, login_as, sample_user,
                              sample_hunter):
        bounty = _make_intro_bounty(app, sample_user.id)
        login_as(client, 'hunter@example.com')
        r = client.post(
            f'/intros/{bounty.id}/claim',
            data={'note': 'We worked together for years.'},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b'Your claim was sent to the poster.' in r.data
        with app.app_context():
            fresh = db.session.get(IntroBounty, bounty.id)
            assert fresh.status == 'claimed'
            claim = IntroClaim.query.filter_by(
                intro_bounty_id=bounty.id,
                claimer_id=sample_hunter.id,
            ).first()
            assert claim is not None
            assert claim.status == 'pending'
            assert claim.note == 'We worked together for years.'

    def test_claim_requires_login(self, app, client, sample_user):
        bounty = _make_intro_bounty(app, sample_user.id)
        r = client.post(f'/intros/{bounty.id}/claim', data={})
        assert r.status_code == 302
        assert '/auth/login' in r.headers['Location']

    def test_cannot_claim_own_intro(self, app, client, logged_in_user,
                                    sample_user):
        bounty = _make_intro_bounty(app, sample_user.id)
        r = client.post(
            f'/intros/{bounty.id}/claim', data={},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b'You cannot claim your own intro bounty.' in r.data
        with app.app_context():
            assert IntroClaim.query.filter_by(
                intro_bounty_id=bounty.id
            ).count() == 0
            assert db.session.get(IntroBounty, bounty.id).status == 'open'

    def test_duplicate_pending_claim_blocked(self, app, client, login_as,
                                             sample_user, sample_hunter):
        bounty = _make_intro_bounty(app, sample_user.id)
        login_as(client, 'hunter@example.com')
        client.post(f'/intros/{bounty.id}/claim', data={})
        r = client.post(
            f'/intros/{bounty.id}/claim', data={},
            follow_redirects=True,
        )
        assert b'You already have a pending claim' in r.data
        with app.app_context():
            assert IntroClaim.query.filter_by(
                intro_bounty_id=bounty.id,
                claimer_id=sample_hunter.id,
            ).count() == 1

    def test_rejected_claimer_may_claim_again(self, app, client, login_as,
                                              sample_user, sample_hunter):
        bounty = _make_intro_bounty(app, sample_user.id)
        _make_claim(app, bounty.id, sample_hunter.id, status='rejected')
        login_as(client, 'hunter@example.com')
        r = client.post(
            f'/intros/{bounty.id}/claim', data={},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b'Your claim was sent to the poster.' in r.data
        with app.app_context():
            assert IntroClaim.query.filter_by(
                intro_bounty_id=bounty.id,
                claimer_id=sample_hunter.id,
                status='pending',
            ).count() == 1

    def test_claim_on_completed_bounty_blocked(self, app, client, login_as,
                                               sample_user, sample_hunter):
        bounty = _make_intro_bounty(
            app, sample_user.id, status='completed')
        login_as(client, 'hunter@example.com')
        r = client.post(
            f'/intros/{bounty.id}/claim', data={},
            follow_redirects=True,
        )
        assert b'This intro bounty is no longer open.' in r.data
        with app.app_context():
            assert IntroClaim.query.filter_by(
                intro_bounty_id=bounty.id
            ).count() == 0


# ----------------------------------------------------------------- detail


class TestDetailVisibility:
    def test_poster_sees_all_claims(self, app, client, logged_in_user,
                                    sample_user, sample_hunter,
                                    second_user):
        bounty = _make_intro_bounty(app, sample_user.id, status='claimed')
        _make_claim(app, bounty.id, sample_hunter.id)
        _make_claim(app, bounty.id, second_user.id)
        html = client.get(f'/intros/{bounty.id}').data.decode()
        assert 'Hunter User' in html
        assert 'Second User' in html
        assert 'Confirm intro made' in html

    def test_other_user_sees_only_own_claim(self, app, client, login_as,
                                            sample_user, sample_hunter,
                                            second_user):
        bounty = _make_intro_bounty(app, sample_user.id, status='claimed')
        _make_claim(app, bounty.id, sample_hunter.id)
        _make_claim(app, bounty.id, second_user.id)
        login_as(client, 'hunter@example.com')
        html = client.get(f'/intros/{bounty.id}').data.decode()
        # The other claimer's name must not appear anywhere (the
        # logged-in hunter's own name shows in the navbar, so it
        # is not a useful assertion here).
        assert 'Second User' not in html
        assert 'Your claims' in html
        assert 'Confirm intro made' not in html

    def test_detail_404_for_unknown_id(self, client):
        r = client.get('/intros/does-not-exist')
        assert r.status_code == 404


# ---------------------------------------------------------------- confirm


class TestConfirmFlow:
    def test_confirm_happy_path_records_pending_stripe_reward(
            self, app, client, logged_in_user, sample_user, sample_hunter):
        bounty = _make_intro_bounty(
            app, sample_user.id, status='claimed',
            amount=Decimal('50'))
        claim = _make_claim(app, bounty.id, sample_hunter.id)
        r = client.post(
            f'/intros/{bounty.id}/claims/{claim.id}/confirm',
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b'Intro confirmed.' in r.data
        with app.app_context():
            fresh_claim = db.session.get(IntroClaim, claim.id)
            assert fresh_claim.status == 'confirmed'
            assert fresh_claim.decided_at is not None
            assert db.session.get(IntroBounty, bounty.id).status == \
                'completed'
            event = IntroRewardEvent.query.filter_by(
                claim_id=claim.id
            ).first()
            assert event is not None
            assert event.status == 'pending_stripe'
            assert event.amount == Decimal('50')
            assert event.intro_bounty_id == bounty.id

    def test_confirm_forbidden_for_non_poster(self, app, client, login_as,
                                              sample_user, sample_hunter,
                                              second_user):
        bounty = _make_intro_bounty(app, sample_user.id, status='claimed')
        claim = _make_claim(app, bounty.id, sample_hunter.id)
        login_as(client, 'second@example.com')
        r = client.post(
            f'/intros/{bounty.id}/claims/{claim.id}/confirm')
        assert r.status_code == 403
        with app.app_context():
            assert db.session.get(IntroClaim, claim.id).status == 'pending'
            assert IntroRewardEvent.query.count() == 0

    def test_confirm_forbidden_for_claimer(self, app, client, login_as,
                                           sample_user, sample_hunter):
        bounty = _make_intro_bounty(app, sample_user.id, status='claimed')
        claim = _make_claim(app, bounty.id, sample_hunter.id)
        login_as(client, 'hunter@example.com')
        r = client.post(
            f'/intros/{bounty.id}/claims/{claim.id}/confirm')
        assert r.status_code == 403

    def test_confirm_twice_does_not_double_record(
            self, app, client, logged_in_user, sample_user, sample_hunter):
        bounty = _make_intro_bounty(app, sample_user.id, status='claimed')
        claim = _make_claim(app, bounty.id, sample_hunter.id)
        client.post(f'/intros/{bounty.id}/claims/{claim.id}/confirm')
        r = client.post(
            f'/intros/{bounty.id}/claims/{claim.id}/confirm',
            follow_redirects=True,
        )
        assert b'This claim has already been decided.' in r.data
        with app.app_context():
            assert IntroRewardEvent.query.filter_by(
                claim_id=claim.id
            ).count() == 1

    def test_record_for_is_idempotent(self, app, sample_user,
                                      sample_hunter):
        bounty = _make_intro_bounty(app, sample_user.id, status='claimed')
        claim = _make_claim(app, bounty.id, sample_hunter.id)
        with app.app_context():
            first = IntroRewardEvent.record_for(
                db.session.get(IntroClaim, claim.id))
            db.session.commit()
            second = IntroRewardEvent.record_for(
                db.session.get(IntroClaim, claim.id))
            assert second.id == first.id
            assert IntroRewardEvent.query.filter_by(
                claim_id=claim.id
            ).count() == 1


# ----------------------------------------------------------------- reject


class TestRejectFlow:
    def test_reject_reopens_bounty(self, app, client, logged_in_user,
                                   sample_user, sample_hunter):
        bounty = _make_intro_bounty(app, sample_user.id, status='claimed')
        claim = _make_claim(app, bounty.id, sample_hunter.id)
        r = client.post(
            f'/intros/{bounty.id}/claims/{claim.id}/reject',
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b'Claim rejected.' in r.data
        with app.app_context():
            fresh_claim = db.session.get(IntroClaim, claim.id)
            assert fresh_claim.status == 'rejected'
            assert fresh_claim.decided_at is not None
            assert db.session.get(IntroBounty, bounty.id).status == 'open'
            assert IntroRewardEvent.query.count() == 0

    def test_reject_keeps_bounty_claimed_while_other_claim_pending(
            self, app, client, logged_in_user, sample_user, sample_hunter,
            second_user):
        bounty = _make_intro_bounty(app, sample_user.id, status='claimed')
        claim = _make_claim(app, bounty.id, sample_hunter.id)
        _make_claim(app, bounty.id, second_user.id)
        client.post(f'/intros/{bounty.id}/claims/{claim.id}/reject')
        with app.app_context():
            assert db.session.get(IntroBounty, bounty.id).status == \
                'claimed'

    def test_reject_forbidden_for_non_poster(self, app, client, login_as,
                                             sample_user, sample_hunter,
                                             second_user):
        bounty = _make_intro_bounty(app, sample_user.id, status='claimed')
        claim = _make_claim(app, bounty.id, sample_hunter.id)
        login_as(client, 'second@example.com')
        r = client.post(
            f'/intros/{bounty.id}/claims/{claim.id}/reject')
        assert r.status_code == 403
        with app.app_context():
            assert db.session.get(IntroClaim, claim.id).status == 'pending'

    def test_reject_does_not_reopen_completed_bounty(
            self, app, client, logged_in_user, sample_user, sample_hunter,
            second_user):
        bounty = _make_intro_bounty(app, sample_user.id, status='completed')
        _make_claim(app, bounty.id, sample_hunter.id, status='confirmed')
        leftover = _make_claim(app, bounty.id, second_user.id)
        client.post(f'/intros/{bounty.id}/claims/{leftover.id}/reject')
        with app.app_context():
            assert db.session.get(IntroBounty, bounty.id).status == \
                'completed'


# ----------------------------------------------------------------- cancel


class TestCancelFlow:
    def test_cancel_open_bounty(self, app, client, logged_in_user,
                                sample_user):
        bounty = _make_intro_bounty(app, sample_user.id)
        r = client.post(
            f'/intros/{bounty.id}/cancel', follow_redirects=True)
        assert r.status_code == 200
        assert b'Intro bounty cancelled.' in r.data
        with app.app_context():
            assert db.session.get(IntroBounty, bounty.id).status == \
                'cancelled'

    def test_cancel_claimed_bounty_rejects_pending_claims(
            self, app, client, logged_in_user, sample_user, sample_hunter):
        bounty = _make_intro_bounty(app, sample_user.id, status='claimed')
        claim = _make_claim(app, bounty.id, sample_hunter.id)
        client.post(f'/intros/{bounty.id}/cancel')
        with app.app_context():
            assert db.session.get(IntroBounty, bounty.id).status == \
                'cancelled'
            fresh_claim = db.session.get(IntroClaim, claim.id)
            assert fresh_claim.status == 'rejected'
            assert fresh_claim.decided_at is not None

    def test_cancel_forbidden_for_non_poster(self, app, client, login_as,
                                             sample_user, sample_hunter):
        bounty = _make_intro_bounty(app, sample_user.id)
        login_as(client, 'hunter@example.com')
        r = client.post(f'/intros/{bounty.id}/cancel')
        assert r.status_code == 403
        with app.app_context():
            assert db.session.get(IntroBounty, bounty.id).status == 'open'

    def test_cancel_completed_bounty_blocked(self, app, client,
                                             logged_in_user, sample_user,
                                             sample_hunter):
        bounty = _make_intro_bounty(app, sample_user.id, status='completed')
        _make_claim(app, bounty.id, sample_hunter.id, status='confirmed')
        r = client.post(
            f'/intros/{bounty.id}/cancel', follow_redirects=True)
        assert b'This intro bounty can no longer be cancelled.' in r.data
        with app.app_context():
            assert db.session.get(IntroBounty, bounty.id).status == \
                'completed'
