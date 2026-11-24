"""Network match — "Your network can close this".

For a need (bounty), suggest the viewer's own connections most likely
to know the right person, using the follow/vouch graph: the graph does
the hunting. E.g. "Marc knows 3 electricians" becomes a panel on the
need's detail page listing the viewer's connections closest to the
right person.

Candidates
----------
Users the viewer follows + users who follow the viewer + users the
viewer has endorsed/vouched for (deduped). The viewer and the need's
poster are never suggested, and neither are deleted accounts.

Scoring (weights)
-----------------
Skill overlap is the highest weight — it is what makes a connection
actually close to the right person:

  +10  per skill the connection lists that matches a skill the need
       asks for, or the need's category (case-insensitive; a match
       is an exact match or a substring match either way, so
       "trades" matches "Skilled trades" and "tech" matches
       "Technology" — but the shorter side must be at least 3
       characters, so a 2-letter skill never matches by substring);
       capped at +30 (3 skills).
  +2   per endorsement/vouch the connection has received, capped
       at +10 (5 vouches).
  +0/+3/+6/+10  by trust level — Newcomer / Member / Trusted /
       Ambassador (see utils/trust.py).
  +3   when the connection's city matches the need's location city
       (first comma-separated part, case-insensitive).

Only connections scoring above 0 are suggested, best first, up to
``limit``. A connection with no skill overlap, no vouches, no trust
standing and no city match has nothing to say about this need, so
they are not shown.

Each suggestion carries short translated reason chips explaining the
match ("Knows Python", "3 vouches", "Trusted member", "Same city").
"""
from flask_babel import gettext as _, ngettext

from extensions import db
from models.endorsement import Endorsement
from models.follow import Follow
from models.profile import Profile
from models.user import User
from utils.trust import trust_level

__all__ = ['suggest_connections_for_need']

# Scoring weights (see the module docstring).
SKILL_MATCH_POINTS = 10
SKILL_MATCH_MAX_POINTS = 30
ENDORSEMENT_POINTS = 2
ENDORSEMENT_MAX_POINTS = 10
TRUST_LEVEL_POINTS = {
    'Newcomer': 0,
    'Member': 3,
    'Trusted': 6,
    'Ambassador': 10,
}
SAME_CITY_POINTS = 3
# A substring match only counts when the shorter side is at least
# this long — otherwise a 2-letter skill like "IT" would match
# "Editor", "Writer", and half the dictionary.
MIN_SUBSTRING_LEN = 3


def _profile_skills(profile):
    """The profile's comma-separated skills as stripped, non-empty names."""
    if not profile or not profile.skills:
        return []
    return [s.strip() for s in profile.skills.split(',') if s.strip()]


def _wanted_terms(bounty):
    """What the need is looking for, lowercased: its skills plus its
    category key and label (a connection skilled in the need's
    category is close to the right person even when no listed skill
    matches)."""
    terms = [s.lower() for s in bounty.skill_list]
    if bounty.category:
        terms.append(bounty.category.lower())
        label = bounty.category_label
        if label:
            terms.append(str(label).lower())
    return terms


def _terms_match(skill, term):
    """True when a profile skill and a wanted term are the same skill:
    equal, or one contains the other (with the short-side guard)."""
    skill = skill.lower()
    term = term.lower()
    if skill == term:
        return True
    if min(len(skill), len(term)) < MIN_SUBSTRING_LEN:
        return False
    return skill in term or term in skill


def _matched_skills(profile_skills, wanted_terms):
    """Profile skills (original casing) matching any wanted term."""
    return [
        skill for skill in profile_skills
        if any(_terms_match(skill, term) for term in wanted_terms)
    ]


def _city(location):
    """The city part of a location ("Montreal, QC" -> "montreal")."""
    if not location:
        return None
    city = location.split(',')[0].strip().lower()
    return city or None


def _candidate_ids(viewer_id, poster_id):
    """IDs of the viewer's connections: both directions of the follow
    graph plus everyone the viewer endorsed. Viewer and poster are
    excluded here, not at render time, so they can never leak in."""
    ids = set()
    ids.update(
        row[0]
        for row in db.session.query(Follow.followed_id).filter(
            Follow.follower_id == viewer_id
        )
    )
    ids.update(
        row[0]
        for row in db.session.query(Follow.follower_id).filter(
            Follow.followed_id == viewer_id
        )
    )
    ids.update(
        row[0]
        for row in db.session.query(Endorsement.endorsee_id).filter(
            Endorsement.endorser_id == viewer_id
        )
    )
    ids.discard(viewer_id)
    ids.discard(poster_id)
    return ids


def suggest_connections_for_need(viewer, bounty, limit=5):
    """Suggest the viewer's connections closest to the right person
    for this need.

    Returns a list of ``{'user', 'score', 'reasons'}`` dicts, best
    first, up to ``limit``. ``reasons`` are short translated strings
    explaining the match. Returns [] when the viewer has no network
    (or no connection scores above 0).
    """
    candidate_ids = _candidate_ids(viewer.id, bounty.poster_id)
    if not candidate_ids:
        return []

    candidates = User.query.filter(
        User.id.in_(candidate_ids),
        User.is_deleted == False,  # noqa: E712 (SQLAlchemy boolean comparison)
    ).all()
    if not candidates:
        return []

    wanted_terms = _wanted_terms(bounty)
    bounty_city = _city(bounty.location)

    suggestions = []
    for candidate in candidates:
        profile = Profile.query.filter_by(user_id=candidate.id).first()
        skills = _profile_skills(profile)
        matched = _matched_skills(skills, wanted_terms)
        vouches = Endorsement.query.filter_by(endorsee_id=candidate.id).count()
        level, _score, _breakdown = trust_level(candidate)

        score = 0
        reasons = []

        skill_points = min(
            len(matched) * SKILL_MATCH_POINTS, SKILL_MATCH_MAX_POINTS
        )
        if skill_points:
            score += skill_points
            # Show at most 3 skill chips — the same cap as the score.
            reasons.extend(
                _('Knows %(skill)s', skill=skill) for skill in matched[:3]
            )

        vouch_points = min(vouches * ENDORSEMENT_POINTS, ENDORSEMENT_MAX_POINTS)
        if vouch_points:
            score += vouch_points
            reasons.append(
                ngettext(
                    '%(count)d vouch', '%(count)d vouches', vouches,
                    count=vouches,
                )
            )

        trust_points = TRUST_LEVEL_POINTS.get(level, 0)
        if trust_points:
            score += trust_points
            if level in ('Trusted', 'Ambassador'):
                reasons.append(_('Trusted member'))

        candidate_city = _city(profile.location) if profile else None
        if bounty_city and candidate_city and bounty_city == candidate_city:
            score += SAME_CITY_POINTS
            reasons.append(_('Same city'))

        if score > 0:
            suggestions.append({
                'user': candidate,
                'score': score,
                'reasons': reasons,
            })

    suggestions.sort(key=lambda s: (-s['score'], s['user'].full_name))
    return suggestions[:limit]
