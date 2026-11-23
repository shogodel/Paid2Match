"""@mentions (Phase 2): @Name in post/comment text links to the
profile and notifies the mentioned user.

Users have no username field — mentions resolve against display names
and full names. A token matches when it equals a user's display name
or full name with spaces removed (case-insensitive), or the first word
of either ("@Second" mentions "Second User"). Ambiguous tokens resolve
to the first match by display name; unmatched tokens render as plain
text and notify nobody.

The mention index is built once per request (flask.g) from all active
users — one query per request, cheap at this scale.
"""
import re

from flask import g, url_for
from markupsafe import Markup, escape

from extensions import db
from models.profile import Profile
from models.user import User
from utils.notifications import notify

MENTION_RE = re.compile(r'@([A-Za-z\u00c0-\u00ff0-9_][A-Za-z\u00c0-\u00ff0-9_.\-]*)')


def _normalized(name):
    """Lowercase, spaces removed — the form tokens are matched against."""
    return (name or '').lower().replace(' ', '')


def _mention_index():
    """{token: user_id} for every active user, cached on flask.g."""
    if getattr(g, '_mention_index', None) is None:
        index = {}
        rows = db.session.query(User, Profile).join(
            Profile, Profile.user_id == User.id, isouter=True,
        ).filter(
            User.is_active == True,  # noqa: E712
            User.is_deleted == False,  # noqa: E712
        ).order_by(Profile.display_name, User.full_name).all()
        for user, profile in rows:
            names = [user.full_name]
            if profile and profile.display_name:
                names.append(profile.display_name)
            for name in names:
                for token in {_normalized(name), _normalized(name.split(' ')[0])}:
                    if token and token not in index:
                        index[token] = user.id
        g._mention_index = index
    return g._mention_index


def find_mentions(text):
    """Mentioned users in text, in order, deduplicated: a list of
    (token, user_id). Unmatched tokens are skipped."""
    if not text:
        return []
    index = _mention_index()
    found = []
    seen = set()
    for match in MENTION_RE.finditer(text):
        token = match.group(1)
        user_id = index.get(token.lower())
        if user_id and user_id not in seen:
            seen.add(user_id)
            found.append((token, user_id))
    return found


def notify_mentions(text, actor_id, actor_name, link_url, exclude_ids=None):
    """Notify every user @mentioned in text (in-app only — 'mention'
    is not an email type). The actor is never notified about their own
    mention, and ids in exclude_ids are skipped (they already got a
    notification for this same event, e.g. the post author on a
    comment). Does not commit — the caller commits."""
    exclude = set(exclude_ids or set())
    for _token, user_id in find_mentions(text):
        if user_id == actor_id or user_id in exclude:
            continue
        notify(
            user_id, 'mention',
            f'{actor_name} mentioned you',
            text[:120], actor_id=actor_id, link_url=link_url,
        )


def link_mentions(text):
    """Jinja filter: escape text, then turn resolved @mentions into
    links to the mentioned user's profile. Unmatched @tokens stay as
    plain text. Returns Markup (already escaped — safe to render)."""
    if not text:
        return Markup('')
    index = _mention_index()

    def _replace(match):
        token = match.group(1)
        user_id = index.get(token.lower())
        if not user_id:
            return match.group(0)
        url = url_for('profile.public_profile', user_id=user_id)
        return (
            f'<a href="{escape(url)}" class="mention-link">'
            f'@{escape(token)}</a>'
        )

    return Markup(MENTION_RE.sub(_replace, escape(text)))
