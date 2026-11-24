"""Live paid-out counter + wins wall (utils/paid_out.py,
models/win_story.py, blueprints/wins.py).

Covers: per-currency counter totals (never a blended sum), the
counter at zero, the homepage counter, the wall listing stories
for completed matches only (newest first, with need title and
amount earned), story submission (poster / referrer / hired each
once per bounty; unrelated users forbidden), and the model guards
(role, body length, one-per-role-per-bounty).
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from blueprints.wins import eligible_roles
from extensions import db
from models.match import Match
from models.payout import Payout
from models.win_story import WinStory
from utils.paid_out import paid_out_totals


# ---------------------------------------------------------------- helpers


def _make_match(bounty_id, hunter_id, employer_id, confirmed=True):
    """Create a Match, dual-confirmed (completed) by default."""
    match = Match(
        bounty_id=bounty_id,
        hunter_id=hunter_id,
        employer_id=employer_id,
        hunter_confirmed=confirmed,
        employer_confirmed=confirmed,
    )
    db.session.add(match)
    db.session.commit()
    return match


def _make_payout(match_id, referrer_id, bounty_id, amount, currency,
                 status='completed', kind='referral'):
    """Create a Payout row directly."""
    payout = Payout(
        match_id=match_id,
        referrer_id=referrer_id,
        bounty_id=bounty_id,
        kind=kind,
        gross_amount=Decimal(str(amount)),
        platform_fee=Decimal('0'),
        amount=Decimal(str(amount)),
        currency=currency,
        method='manual',
        status=status,
    )
    db.session.add(payout)
    db.session.commit()
    return payout


def _make_story(bounty_id, author_id, role, body, match_id=None,
                created_at=None):
    """Create a WinStory row directly (bypasses the route, so tests
    can seed stories the route would refuse — e.g. on uncompleted
    matches — and assert the wall hides them)."""
    story = WinStory(
        bounty_id=bounty_id,
        match_id=match_id,
        author_id=author_id,
        role=role,
        body=body,
    )
    if created_at is not None:
        story.created_at = created_at
    db.session.add(story)
    db.session.commit()
    return story


def _completed_setup(app, bounty, hunter, poster):
    """A completed match on the bounty; returns the match."""
    with app.app_context():
        return _make_match(bounty.id, hunter.id, poster.id)


# ---------------------------------------------------------------- counter


def test_paid_out_totals_per_currency(
    app, sample_bounty, sample_user, sample_hunter
):
    """Completed payouts sum per currency — three separate figures.
    Lowercase stored codes ('cad') group with their currency, and
    pending/failed payouts never count."""
    with app.app_context():
        match = _make_match(
            sample_bounty.id, sample_hunter.id, sample_user.id
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 100, 'cad'
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 50, 'CAD'
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 200, 'eur'
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 300, 'USD'
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 999, 'cad',
            status='pending',
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 888, 'eur',
            status='failed',
        )
        totals = paid_out_totals()
    assert totals == {
        'CAD': Decimal('150.00'),
        'EUR': Decimal('200.00'),
        'USD': Decimal('300.00'),
    }


def test_paid_out_totals_zero(app):
    """No payouts at all — all three figures are zero, never missing
    (the homepage counter renders CA$0.00 / US$0.00 / €0.00)."""
    with app.app_context():
        totals = paid_out_totals()
    assert totals == {
        'CAD': Decimal('0'),
        'EUR': Decimal('0'),
        'USD': Decimal('0'),
    }


def test_paid_out_totals_ignores_non_completed(
    app, sample_bounty, sample_user, sample_hunter
):
    """Pending and processing payouts are not paid out yet — the
    counter stays at zero until a payout completes."""
    with app.app_context():
        match = _make_match(
            sample_bounty.id, sample_hunter.id, sample_user.id
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 500, 'cad',
            status='pending',
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 700, 'usd',
            status='processing',
        )
        totals = paid_out_totals()
    assert totals['CAD'] == Decimal('0')
    assert totals['USD'] == Decimal('0')


def test_homepage_shows_counter_at_zero(client):
    """The homepage counter renders at $0: all three figures plus
    the link to the wins wall."""
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'Paid out so far' in resp.data
    assert b'CA$0.00' in resp.data
    assert b'US$0.00' in resp.data
    assert '€0.00'.encode() in resp.data
    assert b'href="/wins/"' in resp.data


def test_homepage_counter_separate_figures(
    client, app, sample_bounty, sample_user, sample_hunter
):
    """The homepage shows the per-currency figures side by side —
    and never a blended total across currencies."""
    with app.app_context():
        match = _make_match(
            sample_bounty.id, sample_hunter.id, sample_user.id
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 100, 'cad'
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 200, 'eur'
        )
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'CA$100.00' in resp.data
    assert '€200.00'.encode() in resp.data
    assert b'US$0.00' in resp.data
    # No blended sum anywhere: 100 + 200 must not appear as $300 in
    # any currency.
    assert b'CA$300.00' not in resp.data
    assert b'US$300.00' not in resp.data
    assert '€300.00'.encode() not in resp.data


def test_homepage_counter_is_live(
    client, app, sample_bounty, sample_user, sample_hunter
):
    """The counter is computed fresh per request, not cached with
    the homepage stats — a payout that completes after a first
    visit shows on the next one."""
    resp = client.get('/')
    assert b'CA$0.00' in resp.data
    with app.app_context():
        match = _make_match(
            sample_bounty.id, sample_hunter.id, sample_user.id
        )
        _make_payout(
            match.id, sample_hunter.id, sample_bounty.id, 250, 'cad'
        )
    resp = client.get('/')
    assert b'CA$250.00' in resp.data


# ---------------------------------------------------------------- wall


def test_wall_is_public(client):
    """The wins wall needs no login."""
    resp = client.get('/wins/')
    assert resp.status_code == 200
    assert b'Wins wall' in resp.data


def test_wall_lists_completed_stories_only(
    client, app, sample_bounty, sample_paid_bounty,
    sample_user, sample_hunter
):
    """Stories for completed matches show; a story on an
    uncompleted match never appears — neither on a bounty with no
    completed match at all, nor on one whose completed match is a
    different match. A story with no match link shows when its
    bounty has a completed match."""
    with app.app_context():
        completed = _make_match(
            sample_bounty.id, sample_hunter.id, sample_user.id
        )
        uncompleted_same_bounty = _make_match(
            sample_bounty.id, sample_hunter.id, sample_user.id,
            confirmed=False,
        )
        uncompleted_other = _make_match(
            sample_paid_bounty.id, sample_hunter.id, sample_user.id,
            confirmed=False,
        )
        _make_story(
            sample_bounty.id, sample_user.id, 'poster',
            'The poster win story.', match_id=completed.id,
        )
        _make_story(
            sample_bounty.id, sample_hunter.id, 'referrer',
            'The referrer win story.', match_id=None,
        )
        _make_story(
            sample_bounty.id, sample_hunter.id, 'hired',
            'Hidden: uncompleted match on a completed bounty.',
            match_id=uncompleted_same_bounty.id,
        )
        _make_story(
            sample_paid_bounty.id, sample_user.id, 'poster',
            'Hidden: bounty with no completed match.',
            match_id=uncompleted_other.id,
        )
    resp = client.get('/wins/')
    assert resp.status_code == 200
    assert b'The poster win story.' in resp.data
    assert b'The referrer win story.' in resp.data
    assert b'Hidden: uncompleted match' not in resp.data
    assert b'Hidden: bounty with no completed match.' not in resp.data


def test_wall_newest_first(
    client, app, sample_bounty, sample_paid_bounty,
    sample_user, sample_hunter
):
    """The wall lists stories newest first."""
    with app.app_context():
        match_old = _make_match(
            sample_bounty.id, sample_hunter.id, sample_user.id
        )
        match_new = _make_match(
            sample_paid_bounty.id, sample_hunter.id, sample_user.id
        )
        _make_story(
            sample_bounty.id, sample_user.id, 'poster',
            'The older win story.', match_id=match_old.id,
            created_at=datetime(2026, 1, 1),
        )
        _make_story(
            sample_paid_bounty.id, sample_user.id, 'poster',
            'The newer win story.', match_id=match_new.id,
            created_at=datetime(2026, 1, 2),
        )
    resp = client.get('/wins/')
    assert resp.status_code == 200
    assert resp.data.index(b'The newer win story.') < resp.data.index(
        b'The older win story.'
    )


def test_wall_shows_need_title_and_earned_amount(
    client, app, sample_paid_bounty, sample_user, sample_hunter
):
    """A story shows its need title and the amount earned — the
    referrer payout when one exists."""
    with app.app_context():
        match = _make_match(
            sample_paid_bounty.id, sample_hunter.id, sample_user.id
        )
        _make_payout(
            match.id, sample_hunter.id, sample_paid_bounty.id, 4000,
            'cad',
        )
        _make_story(
            sample_paid_bounty.id, sample_hunter.id, 'referrer',
            'Best referral I ever made.', match_id=match.id,
        )
    resp = client.get('/wins/')
    assert resp.status_code == 200
    assert sample_paid_bounty.title.encode() in resp.data
    assert b'Earned' in resp.data
    assert b'CA$4,000.00' in resp.data
    assert b'Referrer' in resp.data


def test_wall_amount_falls_back_to_reward(
    client, app, sample_paid_bounty, sample_user, sample_hunter
):
    """No payout recorded yet — the wall falls back to the need's
    reward as the amount earned."""
    with app.app_context():
        match = _make_match(
            sample_paid_bounty.id, sample_hunter.id, sample_user.id
        )
        _make_story(
            sample_paid_bounty.id, sample_user.id, 'poster',
            'Great hire, smooth process.', match_id=match.id,
        )
    resp = client.get('/wins/')
    assert resp.status_code == 200
    assert b'CA$5,000.00' in resp.data


def test_wall_no_amount_for_free_need(
    client, app, sample_bounty, sample_user, sample_hunter
):
    """A free need with no payout shows no amount — nothing earned
    is shown as nothing, not $0."""
    with app.app_context():
        match = _make_match(
            sample_bounty.id, sample_hunter.id, sample_user.id
        )
        _make_story(
            sample_bounty.id, sample_user.id, 'poster',
            'A community hire, no money involved.', match_id=match.id,
        )
    resp = client.get('/wins/')
    assert resp.status_code == 200
    assert b'A community hire, no money involved.' in resp.data
    assert b'Earned' not in resp.data


def test_wall_empty_state(client):
    """No stories yet — the wall says so."""
    resp = client.get('/wins/')
    assert resp.status_code == 200
    assert b'No wins yet' in resp.data


# ---------------------------------------------------------------- submission


def test_new_story_requires_login(client, sample_bounty):
    """Anonymous users are sent to the login page."""
    resp = client.get(f'/wins/new/{sample_bounty.id}')
    assert resp.status_code == 302
    assert '/auth/login' in resp.headers['Location']


def test_new_story_unknown_bounty_404(logged_in_user):
    """A bounty that does not exist is a 404, not a 403."""
    resp = logged_in_user.get('/wins/new/does-not-exist')
    assert resp.status_code == 404


def test_new_story_form_shows_bounty_title(
    logged_in_user, app, sample_bounty, sample_user, sample_hunter
):
    """The form names the need the story is about."""
    _completed_setup(app, sample_bounty, sample_hunter, sample_user)
    resp = logged_in_user.get(f'/wins/new/{sample_bounty.id}')
    assert resp.status_code == 200
    assert sample_bounty.title.encode() in resp.data
    assert b'Your story' in resp.data


def test_poster_can_add_story(
    logged_in_user, app, sample_bounty, sample_user, sample_hunter
):
    """The poster adds one story in the poster role."""
    _completed_setup(app, sample_bounty, sample_hunter, sample_user)
    resp = logged_in_user.post(
        f'/wins/new/{sample_bounty.id}',
        data={'role': 'poster', 'body': 'Found our engineer here.'},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'now on the wins wall' in resp.data
    assert b'Found our engineer here.' in resp.data
    with app.app_context():
        story = WinStory.query.filter_by(
            bounty_id=sample_bounty.id, role='poster'
        ).first()
        assert story is not None
        assert story.author_id == sample_user.id
        assert story.body == 'Found our engineer here.'


def test_referrer_can_add_story(
    logged_in_hunter, app, sample_bounty, sample_user, sample_hunter
):
    """The referrer adds one story in the referrer role."""
    _completed_setup(app, sample_bounty, sample_hunter, sample_user)
    resp = logged_in_hunter.post(
        f'/wins/new/{sample_bounty.id}',
        data={'role': 'referrer', 'body': 'My friend got the job!'},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'now on the wins wall' in resp.data
    with app.app_context():
        story = WinStory.query.filter_by(
            bounty_id=sample_bounty.id, role='referrer'
        ).first()
        assert story is not None
        assert story.author_id == sample_hunter.id


def test_hired_story_by_participant(
    logged_in_user, app, sample_bounty, sample_user, sample_hunter
):
    """The hired candidate is not a platform user (candidates exist
    only as pitch teasers), so a participant in the hire — here the
    poster — shares the hired person's side in the 'hired' role."""
    _completed_setup(app, sample_bounty, sample_hunter, sample_user)
    resp = logged_in_user.post(
        f'/wins/new/{sample_bounty.id}',
        data={
            'role': 'hired',
            'body': 'She started Monday and loves the team already.',
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'now on the wins wall' in resp.data
    with app.app_context():
        story = WinStory.query.filter_by(
            bounty_id=sample_bounty.id, role='hired'
        ).first()
        assert story is not None
        assert story.author_id == sample_user.id


def test_second_story_same_role_blocked(
    logged_in_user, app, sample_bounty, sample_user, sample_hunter
):
    """One story per role per bounty: a second poster story is
    refused and only the first one exists."""
    _completed_setup(app, sample_bounty, sample_hunter, sample_user)
    logged_in_user.post(
        f'/wins/new/{sample_bounty.id}',
        data={'role': 'poster', 'body': 'First poster story.'},
    )
    resp = logged_in_user.post(
        f'/wins/new/{sample_bounty.id}',
        data={'role': 'poster', 'body': 'Second poster story.'},
    )
    assert resp.status_code == 200
    with app.app_context():
        stories = WinStory.query.filter_by(
            bounty_id=sample_bounty.id, role='poster'
        ).all()
        assert len(stories) == 1
        assert stories[0].body == 'First poster story.'


def test_no_roles_left_redirects_to_wall(
    logged_in_user, app, sample_bounty, sample_user, sample_hunter
):
    """Once a participant has shared every story they can (poster
    + hired), the form redirects to the wall with an explanation."""
    _completed_setup(app, sample_bounty, sample_hunter, sample_user)
    logged_in_user.post(
        f'/wins/new/{sample_bounty.id}',
        data={'role': 'poster', 'body': 'Poster story.'},
    )
    logged_in_user.post(
        f'/wins/new/{sample_bounty.id}',
        data={'role': 'hired', 'body': 'Hired story.'},
    )
    resp = logged_in_user.get(
        f'/wins/new/{sample_bounty.id}', follow_redirects=True
    )
    assert resp.status_code == 200
    assert b'already shared every story' in resp.data


def test_unrelated_user_forbidden(
    client, login_as, app, sample_bounty, sample_user, sample_hunter,
    second_user
):
    """A user who took no part in the hire gets a 403 on the form
    and on submit — and no story is created."""
    _completed_setup(app, sample_bounty, sample_hunter, sample_user)
    login_as(client, 'second@example.com')
    resp = client.get(f'/wins/new/{sample_bounty.id}')
    assert resp.status_code == 403
    resp = client.post(
        f'/wins/new/{sample_bounty.id}',
        data={'role': 'poster', 'body': 'I was not involved.'},
    )
    assert resp.status_code == 403
    with app.app_context():
        assert WinStory.query.filter_by(
            bounty_id=sample_bounty.id
        ).count() == 0


def test_story_requires_completed_match(
    logged_in_user, app, sample_bounty, sample_user, sample_hunter
):
    """No dual-confirmed match on the need — even the poster gets a
    403. Stories are for completed hires only."""
    with app.app_context():
        _make_match(
            sample_bounty.id, sample_hunter.id, sample_user.id,
            confirmed=False,
        )
    resp = logged_in_user.get(f'/wins/new/{sample_bounty.id}')
    assert resp.status_code == 403


def test_role_choice_restricted(
    logged_in_hunter, app, sample_bounty, sample_user, sample_hunter
):
    """The referrer cannot file a story in the poster role — the
    role choice is validated server-side, not just in the form."""
    _completed_setup(app, sample_bounty, sample_hunter, sample_user)
    resp = logged_in_hunter.post(
        f'/wins/new/{sample_bounty.id}',
        data={'role': 'poster', 'body': 'Pretending to be the poster.'},
    )
    assert resp.status_code == 200
    with app.app_context():
        assert WinStory.query.filter_by(
            bounty_id=sample_bounty.id
        ).count() == 0


def test_story_body_too_long(
    logged_in_user, app, sample_bounty, sample_user, sample_hunter
):
    """A story over 1000 characters is refused by the form."""
    _completed_setup(app, sample_bounty, sample_hunter, sample_user)
    resp = logged_in_user.post(
        f'/wins/new/{sample_bounty.id}',
        data={'role': 'poster', 'body': 'x' * 1001},
    )
    assert resp.status_code == 200
    with app.app_context():
        assert WinStory.query.filter_by(
            bounty_id=sample_bounty.id
        ).count() == 0


# ---------------------------------------------------------------- model


def test_invalid_role_rejected(app, sample_bounty, sample_user):
    """Only poster / referrer / hired are valid roles."""
    with app.app_context():
        with pytest.raises(ValueError, match='role must be one of'):
            WinStory(
                bounty_id=sample_bounty.id,
                author_id=sample_user.id,
                role='spectator',
                body='Hello.',
            )


def test_body_too_long_rejected_at_model(
    app, sample_bounty, sample_user
):
    """The model guard rejects bodies over 1000 characters even
    when constructed directly."""
    with app.app_context():
        with pytest.raises(ValueError, match='at most 1000'):
            WinStory(
                bounty_id=sample_bounty.id,
                author_id=sample_user.id,
                role='poster',
                body='x' * 1001,
            )


def test_unique_role_per_bounty(
    app, sample_bounty, sample_user, sample_hunter
):
    """The database constraint backs the one-per-role-per-bounty
    rule: a second story in the same role raises IntegrityError."""
    with app.app_context():
        _make_story(
            sample_bounty.id, sample_user.id, 'poster',
            'First poster story.',
        )
        db.session.add(WinStory(
            bounty_id=sample_bounty.id,
            author_id=sample_hunter.id,
            role='poster',
            body='Second poster story.',
        ))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_eligible_roles(
    app, sample_bounty, sample_user, sample_hunter, second_user
):
    """Role eligibility: the poster may write poster + hired, the
    referrer referrer + hired, an unrelated user nothing. No
    completed match — nobody may write anything."""
    with app.app_context():
        assert eligible_roles(sample_bounty, sample_user.id) == []
        _make_match(
            sample_bounty.id, sample_hunter.id, sample_user.id
        )
        assert eligible_roles(sample_bounty, sample_user.id) == [
            'poster', 'hired',
        ]
        assert eligible_roles(sample_bounty, sample_hunter.id) == [
            'referrer', 'hired',
        ]
        assert eligible_roles(sample_bounty, second_user.id) == []
