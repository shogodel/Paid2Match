"""Onboarding checklist (Phase 2): the LinkedIn-style 3-step card a
new user sees on the feed until all three steps are done (or they
dismiss it for the session).

Steps: complete the profile (the M0 completion_percentage, surfaced
— its math is unchanged), say hello with an #intros post, follow
someone. The profile strength meter itself already lives in the feed
rail and on the profile page; this module only decides the checklist.
"""
from flask import url_for
from flask_babel import lazy_gettext as _l

from models.follow import Follow
from models.post import Post
from models.profile import Profile


def get_onboarding_steps(user):
    """The three welcome steps for this user: each is a dict with key,
    label, hint, done, and url. Call inside a request (url_for)."""
    profile = Profile.query.filter_by(user_id=user.id).first()
    profile_done = bool(profile and profile.completion_percentage >= 100)
    has_intro = Post.query.filter_by(
        author_id=user.id, post_type='intro', is_hidden=False,
    ).first() is not None
    follows_someone = Follow.query.filter_by(
        follower_id=user.id,
    ).first() is not None
    return [
        {
            'key': 'profile',
            'label': _l('Complete your profile'),
            'hint': _l('Add a photo, headline, and bio — profiles with a photo get seen first.'),
            'done': profile_done,
            'url': url_for('profile.edit'),
        },
        {
            'key': 'intro',
            'label': _l('Say hello with #intros'),
            'hint': _l('Introduce yourself: what you do, what you are looking for.'),
            'done': has_intro,
            'url': url_for('social.feed') + '#composer',
        },
        {
            'key': 'follow',
            'label': _l('Follow someone'),
            'hint': _l('Follow people you know — their posts fill your feed.'),
            'done': follows_someone,
            'url': url_for('social.people'),
        },
    ]
