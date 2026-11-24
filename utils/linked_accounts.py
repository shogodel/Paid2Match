"""Linked-account verification service (M3, §2).

Three methods, strongest first:
- OAuth (LinkedIn, GitHub): the user signs in with the other platform.
  Client id/secret come from the environment only (config.py) — never
  hardcoded, never stored in the database.
- Bio-link check (rel=me model): the user puts their Paid2Match
  profile URL in the other account's bio; we fetch that page and look
  for the link back.
- Unique code: the user posts a short code (P2M-XXXXX); we fetch the
  page and look for it. When the fetch fails (Instagram/TikTok often
  block server fetches), the user asks for manual review and the
  founder checks it from the admin queue.

All HTTP calls go through ``requests`` with a short timeout and are
mocked in tests — no network calls in the suite. Verification expires
after 90 days; ``refresh_linked_account_expiry`` flips stale rows to
'expired' (called on the linked-accounts page, not in the trust
computation — that stays a pure read).
"""
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from flask import current_app, url_for

from extensions import db
from models import utc_now
from models.follow import Follow
from models.linked_account import (
    OAUTH_PLATFORMS,
    REVERIFY_DAYS,
    LinkedAccount,
    generate_verification_code,
)

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 10
FETCH_HEADERS = {
    'User-Agent': 'Paid2Match verification (https://paid2match.work)',
}

OAUTH_ENDPOINTS = {
    'github': {
        'authorize': 'https://github.com/login/oauth/authorize',
        'token': 'https://github.com/login/oauth/access_token',
        'profile': 'https://api.github.com/user',
        'scope': 'read:user',
    },
    'linkedin': {
        'authorize': 'https://www.linkedin.com/oauth/v2/authorization',
        'token': 'https://www.linkedin.com/oauth/v2/accessToken',
        'profile': 'https://api.linkedin.com/v2/userinfo',
        'scope': 'openid profile',
    },
}

# GitHub sign-in ("Continue with GitHub") needs the user's email, which
# the profile endpoint only returns when it is public. The /user/emails
# endpoint lists the (private) addresses — but it requires the
# user:email scope, which the account-linking flow does not ask for.
GITHUB_EMAILS_URL = 'https://api.github.com/user/emails'
GITHUB_SIGNIN_SCOPE = 'read:user user:email'


def _oauth_config(platform):
    """(client_id, client_secret) from config, or (None, None)."""
    prefix = platform.upper()
    return (
        current_app.config.get(f'{prefix}_CLIENT_ID'),
        current_app.config.get(f'{prefix}_CLIENT_SECRET'),
    )


def oauth_available(platform):
    """Whether OAuth is configured for this platform."""
    client_id, client_secret = _oauth_config(platform)
    return bool(client_id and client_secret)


def oauth_authorize_url(platform, redirect_uri, state, scope=None):
    """The provider's authorize URL, or None when OAuth is not
    configured for this platform. ``scope`` overrides the platform's
    default (sign-in asks GitHub for ``user:email`` too, so a hidden
    email can still be read for login)."""
    if platform not in OAUTH_PLATFORMS:
        return None
    client_id, _secret = _oauth_config(platform)
    if not client_id:
        return None
    endpoints = OAUTH_ENDPOINTS[platform]
    params = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'state': state,
        'scope': scope or endpoints['scope'],
    }
    if platform == 'linkedin':
        params['response_type'] = 'code'
    return f"{endpoints['authorize']}?{urlencode(params)}"


def exchange_oauth_code(platform, code, redirect_uri):
    """Exchange an authorization code for an access token. Returns
    (token, error)."""
    _client_id, client_secret = _oauth_config(platform)
    client_id, _ = _oauth_config(platform)
    endpoints = OAUTH_ENDPOINTS[platform]
    try:
        resp = requests.post(
            endpoints['token'],
            data={
                'client_id': client_id,
                'client_secret': client_secret,
                'code': code,
                'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code',
            },
            headers={'Accept': 'application/json'},
            timeout=FETCH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('OAuth token exchange failed for %s: %s', platform, exc)
        return None, 'Could not reach the provider — please try again.'
    if resp.status_code != 200:
        return None, 'The provider rejected the sign-in — please try again.'
    token = resp.json().get('access_token')
    if not token:
        return None, 'The provider did not return an access token.'
    return token, None


def fetch_oauth_profile(platform, access_token):
    """Fetch the signed-in user's handle + profile URL from the
    provider. Returns (handle, url, error)."""
    endpoints = OAUTH_ENDPOINTS[platform]
    try:
        resp = requests.get(
            endpoints['profile'],
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json',
            },
            timeout=FETCH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('OAuth profile fetch failed for %s: %s', platform, exc)
        return None, None, 'Could not reach the provider — please try again.'
    if resp.status_code != 200:
        return None, None, 'Could not read your profile from the provider.'
    data = resp.json()
    if platform == 'github':
        return data.get('login'), data.get('html_url'), None
    # LinkedIn userinfo: sub is the stable id, name the display name.
    handle = data.get('preferred_username') or data.get('name')
    return handle, None, None


def fetch_github_user(access_token):
    """Fetch the full GitHub /user payload for sign-in (login, name,
    email — the email is null when the user hides it). Returns
    (data, error)."""
    try:
        resp = requests.get(
            OAUTH_ENDPOINTS['github']['profile'],
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json',
            },
            timeout=FETCH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('GitHub user fetch failed: %s', exc)
        return None, 'Could not reach GitHub — please try again.'
    if resp.status_code != 200:
        return None, 'Could not read your profile from GitHub.'
    return resp.json(), None


def fetch_github_primary_email(access_token):
    """The user's primary verified GitHub email — the fallback for
    sign-in when the public profile hides the email. Returns
    (email, error); email is None when the account has no verified
    address (not an error — the caller decides what to do)."""
    try:
        resp = requests.get(
            GITHUB_EMAILS_URL,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json',
            },
            timeout=FETCH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('GitHub emails fetch failed: %s', exc)
        return None, 'Could not reach GitHub — please try again.'
    if resp.status_code != 200:
        return None, 'Could not read your email from GitHub.'
    for entry in resp.json() or []:
        if entry.get('primary') and entry.get('verified'):
            return entry.get('email'), None
    return None, None


def _fetch_page(url):
    """Fetch a page's text, or (None, error)."""
    try:
        resp = requests.get(url, headers=FETCH_HEADERS, timeout=FETCH_TIMEOUT)
    except requests.RequestException as exc:
        return None, f'Could not fetch the page ({exc.__class__.__name__}).'
    if resp.status_code != 200:
        return None, f'The page returned HTTP {resp.status_code}.'
    return resp.text, None


def verify_bio_link(account, profile_url):
    """Check whether the account's page links back to the user's
    Paid2Match profile (rel=me model — a plain link back counts too).
    Returns (ok, error)."""
    if not account.url:
        return False, 'Add the URL of your profile on that platform first.'
    text, error = _fetch_page(account.url)
    if error:
        return False, error
    if profile_url.lower() in text.lower():
        return True, None
    return False, (
        'We could not find your Paid2Match profile link on that page — '
        'make sure the link is in your bio and your profile is public.'
    )


def verify_code(account):
    """Check whether the account's page contains the verification code.
    Returns (ok, error)."""
    if not account.url:
        return False, 'Add the URL of your profile on that platform first.'
    if not account.verification_code:
        return False, 'No verification code was issued for this link.'
    text, error = _fetch_page(account.url)
    if error:
        return False, error
    if account.verification_code in text:
        return True, None
    return False, (
        'We could not find the code on that page — make sure it is in '
        'your bio or a public post.'
    )


def mark_verified(account):
    """Mark an account verified (90-day expiry). Does not commit."""
    now = utc_now()
    account.status = 'verified'
    account.verified_at = now
    account.expires_at = now + timedelta(days=REVERIFY_DAYS)
    account.manual_review = False
    account.admin_note = None
    db.session.flush()
    return account


def refresh_linked_account_expiry():
    """Flip verified accounts past their 90-day expiry to 'expired'.
    Returns how many flipped. Does not commit."""
    now = utc_now()
    stale = LinkedAccount.query.filter(
        LinkedAccount.status == 'verified',
        LinkedAccount.expires_at.isnot(None),
        LinkedAccount.expires_at < now,
    ).all()
    for account in stale:
        account.status = 'expired'
    return len(stale)


def verified_linked_count(user_id):
    """Currently-verified linked accounts for a user (expired ones
    don't count)."""
    accounts = LinkedAccount.query.filter_by(
        user_id=user_id, status='verified'
    ).all()
    return sum(1 for a in accounts if a.is_currently_verified)


def get_or_create_link(user_id, platform, method, url=None, handle=None):
    """The user's link row for a platform, creating it when needed.
    A rejected/expired row is reused (reset to pending) so re-linking
    works. Code-method rows get a fresh code. Does not commit."""
    account = LinkedAccount.query.filter_by(
        user_id=user_id, platform=platform
    ).first()
    if account is None:
        account = LinkedAccount(
            user_id=user_id,
            platform=platform,
            verification_method=method,
        )
        db.session.add(account)
    elif account.status in ('rejected', 'expired'):
        account.status = 'pending'
        account.verified_at = None
        account.expires_at = None
        account.admin_note = None
        account.manual_review = False
    account.verification_method = method
    if url is not None:
        account.url = url
    if handle is not None:
        account.handle = handle
    if method == 'code' and not account.verification_code:
        account.verification_code = generate_verification_code()
    db.session.flush()
    return account


def visible_linked_accounts(user_id, viewer_id):
    """The user's linked accounts the viewer may see (§2.4 visibility
    rules). Only verified-or-pending accounts are listed — rejected
    ones are the owner's business."""
    accounts = LinkedAccount.query.filter_by(user_id=user_id).filter(
        LinkedAccount.status.in_(['pending', 'verified', 'expired'])
    ).all()
    if viewer_id == user_id:
        return accounts
    follows = False
    if viewer_id:
        follows = Follow.query.filter_by(
            follower_id=viewer_id, followed_id=user_id
        ).first() is not None
    return [a for a in accounts if a.visible_to(viewer_id, follows)]


def oauth_state_token():
    """A CSRF state token for the OAuth round-trip (stored in session)."""
    return secrets.token_urlsafe(24)
