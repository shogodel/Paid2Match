"""Google sign-in helpers ("Continue with Google").

Hand-rolled OAuth 2.0 authorization-code flow with plain ``requests`` —
the same pattern as utils/linked_accounts.py (no OAuth library in
requirements.txt). Client id/secret come from the environment only
(config.py) — never hardcoded, never stored in the database, never
logged.

Flow: /auth/google/login redirects to Google's authorize endpoint with
a random state token (stored in the session); Google redirects back to
/auth/google/callback with a code; we exchange the code for an access
token and fetch the user's profile from Google's userinfo endpoint.
Google has already verified the email, so accounts created or matched
this way are marked email_verified.

All HTTP calls use a short timeout and are mocked in tests — no network
calls in the suite.
"""
import logging
from urllib.parse import urlencode

import requests
from flask import current_app

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 10

GOOGLE_AUTHORIZE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'
GOOGLE_SCOPE = 'openid email profile'


def google_available():
    """Whether Google sign-in is configured (both id and secret set)."""
    return bool(
        current_app.config.get('GOOGLE_CLIENT_ID')
        and current_app.config.get('GOOGLE_CLIENT_SECRET')
    )


def google_authorize_url(redirect_uri, state):
    """The Google authorize URL, or None when Google sign-in is not
    configured."""
    client_id = current_app.config.get('GOOGLE_CLIENT_ID')
    if not client_id:
        return None
    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': GOOGLE_SCOPE,
        'state': state,
    }
    return f'{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}'


def exchange_google_code(code, redirect_uri):
    """Exchange an authorization code for an access token. Returns
    (token, error)."""
    try:
        resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                'client_id': current_app.config.get('GOOGLE_CLIENT_ID'),
                'client_secret': current_app.config.get('GOOGLE_CLIENT_SECRET'),
                'code': code,
                'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code',
            },
            headers={'Accept': 'application/json'},
            timeout=FETCH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('Google token exchange failed: %s', exc)
        return None, 'Could not reach Google — please try again.'
    if resp.status_code != 200:
        return None, 'Google rejected the sign-in — please try again.'
    token = resp.json().get('access_token')
    if not token:
        return None, 'Google did not return an access token.'
    return token, None


def fetch_google_userinfo(access_token):
    """Fetch the signed-in user's profile from Google. Returns
    (data, error) — data holds at least 'email' and usually 'name'
    and 'email_verified'."""
    try:
        resp = requests.get(
            GOOGLE_USERINFO_URL,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json',
            },
            timeout=FETCH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('Google userinfo fetch failed: %s', exc)
        return None, 'Could not reach Google — please try again.'
    if resp.status_code != 200:
        return None, 'Could not read your profile from Google.'
    return resp.json(), None
