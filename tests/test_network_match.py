"""Tests for the network-match suggestions ("Your network can close
this") — utils/network_match.py + the panel on the bounty detail page.

Fixtures: sample_user is the need's poster, sample_hunter is the
viewer, second_user lists Python/Sales (Montreal) and third_user lists
Recruiting (Laval).
"""
import pytest
from flask import url_for
from werkzeug.security import generate_password_hash

from extensions import db
from models.bounty import Bounty
from models.endorsement import Endorsement
from models.follow import Follow
from models.profile import Profile
from models.user import User
from utils.network_match import suggest_connections_for_need


def _make_user(app, email, name, skills=None, location=None):
    """A user with a profile, created directly in the DB."""
    with app.app_context():
        user = User(
            email=email,
            full_name=name,
            password_hash=generate_password_hash('Pass123!'),
            role='user',
            is_active=True,
            email_verified=True,
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(Profile(
            user_id=user.id,
            profile_type='worker',
            display_name=name,
            skills=skills,
            location=location,
            reputation_score=0,
        ))
        db.session.commit()
        db.session.refresh(user)
        return user


def _make_bounty(app, poster, skills, location, category='tech'):
    """A free, open, publicly visible need."""
    with app.app_context():
        bounty = Bounty(
            poster_id=poster.id,
            title='Python developer needed',
            description='Looking for a Python developer for a short contract.',
            bounty_type='recruitment',
            bounty_direction='seeking_opportunity',
            category=category,
            skills=skills,
            reward_amount=None,
            status='open',
            payment_status='released',
            location=location,
        )
        db.session.add(bounty)
        db.session.commit()
        db.session.refresh(bounty)
        return bounty


def _follow(app, follower, followed):
    with app.app_context():
        db.session.add(Follow(
            follower_id=follower.id, followed_id=followed.id,
        ))
        db.session.commit()


def _endorse(app, endorser, endorsee, skill):
    with app.app_context():
        db.session.add(Endorsement(
            endorser_id=endorser.id, endorsee_id=endorsee.id, skill=skill,
        ))
        db.session.commit()


def _detail_url(app, bounty_id):
    """The bounty detail URL via url_for (never a hardcoded path —
    the /needs prefix may move). url_for needs a request context
    outside a request, so build it in a test one."""
    with app.app_context():
        bounty = db.session.get(Bounty, bounty_id)
        slug_code = bounty.url_slug
    with app.test_request_context():
        return url_for('bounties.detail', slug_code=slug_code)


@pytest.fixture
def network_bounty(app, sample_user):
    """A Montreal need asking for Python."""
    return _make_bounty(
        app, sample_user, skills='Python, Flask', location='Montreal, QC',
    )


def test_skill_match_outranks_no_match(
    app, sample_user, sample_hunter, second_user, third_user, network_bounty,
):
    """A connection whose skills match the need ranks above one whose
    skills don't (third_user only scores via a vouch)."""
    _follow(app, sample_hunter, second_user)
    _follow(app, sample_hunter, third_user)
    _endorse(app, sample_user, third_user, 'Recruiting')

    suggestions = suggest_connections_for_need(sample_hunter, network_bounty)

    assert [s['user'].id for s in suggestions] == [
        second_user.id, third_user.id,
    ]
    assert suggestions[0]['score'] > suggestions[1]['score']
    assert 'Knows Python' in suggestions[0]['reasons']
    assert 'Same city' in suggestions[0]['reasons']
    assert '1 vouch' in suggestions[1]['reasons']


def test_endorsed_outranks_unendorsed_with_equal_skills(
    app, sample_user, sample_hunter, second_user, network_bounty,
):
    """Two connections with the same skill match: the vouched one
    ranks higher. The need is Remote so neither gets a city bonus."""
    fourth_user = _make_user(
        app, 'fourth@example.com', 'Fourth User',
        skills='Python', location='Toronto, ON',
    )
    remote_bounty = _make_bounty(
        app, sample_user, skills='Python', location='Remote',
    )
    _follow(app, sample_hunter, second_user)
    _follow(app, sample_hunter, fourth_user)
    _endorse(app, sample_user, fourth_user, 'Python')

    suggestions = suggest_connections_for_need(sample_hunter, remote_bounty)

    assert [s['user'].id for s in suggestions] == [
        fourth_user.id, second_user.id,
    ]
    assert '1 vouch' in suggestions[0]['reasons']


def test_viewer_and_poster_never_suggested(
    app, sample_user, sample_hunter, second_user, network_bounty,
):
    """The viewer and the poster are excluded even when the viewer
    follows / endorses the poster and (defensively) follows themself."""
    _follow(app, sample_hunter, sample_user)
    _follow(app, sample_hunter, second_user)
    _follow(app, sample_hunter, sample_hunter)
    _endorse(app, sample_hunter, sample_user, 'Hiring')

    suggestions = suggest_connections_for_need(sample_hunter, network_bounty)

    ids = [s['user'].id for s in suggestions]
    assert ids == [second_user.id]
    assert sample_user.id not in ids
    assert sample_hunter.id not in ids


def test_follower_and_endorsed_are_candidates(
    app, sample_user, sample_hunter, second_user, third_user, network_bounty,
):
    """Candidates come from both follow directions and from people the
    viewer endorsed — not just from who the viewer follows."""
    _follow(app, second_user, sample_hunter)  # follows the viewer
    _endorse(app, sample_hunter, third_user, 'Recruiting')  # viewer vouched
    _endorse(app, sample_user, third_user, 'Recruiting')

    suggestions = suggest_connections_for_need(sample_hunter, network_bounty)

    assert [s['user'].id for s in suggestions] == [
        second_user.id, third_user.id,
    ]


def test_empty_network_returns_empty(
    app, sample_hunter, network_bounty,
):
    """A viewer with no follows and no endorsements gets []."""
    assert suggest_connections_for_need(sample_hunter, network_bounty) == []


def test_limit_is_respected(
    app, sample_user, sample_hunter, second_user, third_user, network_bounty,
):
    _follow(app, sample_hunter, second_user)
    _follow(app, sample_hunter, third_user)
    _endorse(app, sample_user, third_user, 'Recruiting')

    suggestions = suggest_connections_for_need(
        sample_hunter, network_bounty, limit=1,
    )

    assert len(suggestions) == 1
    assert suggestions[0]['user'].id == second_user.id


def test_detail_page_shows_panel(
    app, client, login_as, sample_hunter, second_user, network_bounty,
):
    """Logged-in viewer with a matching connection sees the panel."""
    _follow(app, sample_hunter, second_user)
    login_as(client, sample_hunter.email)

    url = _detail_url(app, network_bounty.id)
    response = client.get(url)

    assert response.status_code == 200
    assert b'People in your network who could help' in response.data
    assert b'Second User' in response.data
    assert b'Knows Python' in response.data


def test_detail_page_hides_panel_without_suggestions(
    app, client, login_as, sample_hunter, network_bounty,
):
    """Logged-in viewer with no network: no panel on the detail page."""
    login_as(client, sample_hunter.email)

    url = _detail_url(app, network_bounty.id)
    response = client.get(url)

    assert response.status_code == 200
    assert b'People in your network who could help' not in response.data


def test_detail_page_hides_panel_for_anonymous(
    app, client, sample_hunter, second_user, network_bounty,
):
    """Anonymous visitors never see the panel, even when the viewer
    would have had a matching connection."""
    _follow(app, sample_hunter, second_user)

    url = _detail_url(app, network_bounty.id)
    response = client.get(url)

    assert response.status_code == 200
    assert b'People in your network who could help' not in response.data
