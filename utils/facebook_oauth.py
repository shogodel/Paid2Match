"""Facebook sign-in helpers ("Continue with Facebook").

Hand-rolled OAuth 2.0 authorization-code flow with plain ``requests`` —
the same pattern as utils/google_oauth.py (no OAuth library in
requirements.txt). Client id/secret come from the environment only
(config.py) — never hardcoded, never stored in the database, never
logged.

Flow: /auth/facebook/login redirects to Facebook's authorize endpoint
with a random state token (stored in the session); Facebook redirects
back to /auth/facebook/callback with a code; we exchange the code for
an access token (a GET to the token endpoint — Facebook's token
endpoint takes query params, not a POST body) and fetch the user's
profile from the Graph API /me endpoint.

Facebook only returns an email when the user granted the ``email``
permission AND the account has a confirmed email address — people who
signed up with a phone number may have none. With no email the login
fails cleanly (see the callback in blueprints/auth/routes.py); accounts
are never created without an email.

All HTTP calls use a short timeout and are mocked in tests — no network
calls in the suite.
"""
import logging
from urllib.parse import urlencode

import requests
from flask import current_app

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 10

FACEBOOK_AUTHORIZE_URL = 'https://www.facebook.com/v25.0/dialog/oauth'
FACEBOOK_TOKEN_URL = 'https://graph.facebook.com/v25.0/oauth/access_token'
FACEBOOK_USERINFO_URL = 'https://graph.facebook.com/v25.0/me'
FACEBOOK_SCOPE = 'public_profile,email'


def facebook_available():
    """Whether Facebook sign-in is configured (both id and secret set)."""
    return bool(
        current_app.config.get('FACEBOOK_CLIENT_ID')
        and current_app.config.get('FACEBOOK_CLIENT_SECRET')
    )


def facebook_authorize_url(redirect_uri, state):
    """The Facebook authorize URL, or None when Facebook sign-in is not
    configured."""
    client_id = current_app.config.get('FACEBOOK_CLIENT_ID')
    if not client_id:
        return None
    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': FACEBOOK_SCOPE,
        'state': state,
    }
    return f'{FACEBOOK_AUTHORIZE_URL}?{urlencode(params)}'


def exchange_facebook_code(code, redirect_uri):
    """Exchange an authorization code for an access token. Returns
    (token, error). Facebook's token endpoint is a GET with query
    params and returns JSON."""
    try:
        resp = requests.get(
            FACEBOOK_TOKEN_URL,
            params={
                'client_id': current_app.config.get('FACEBOOK_CLIENT_ID'),
                'client_secret': current_app.config.get('FACEBOOK_CLIENT_SECRET'),
                'redirect_uri': redirect_uri,
                'code': code,
            },
            headers={'Accept': 'application/json'},
            timeout=FETCH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('Facebook token exchange failed: %s', exc)
        return None, 'Could not reach Facebook — please try again.'
    if resp.status_code != 200:
        return None, 'Facebook rejected the sign-in — please try again.'
    token = resp.json().get('access_token')
    if not token:
        return None, 'Facebook did not return an access token.'
    return token, None


def fetch_facebook_userinfo(access_token):
    """Fetch the signed-in user's profile from Facebook. Returns
    (data, error) — data holds 'id', 'name', and 'email' when Facebook
    shares one (it may not — see the module docstring)."""
    try:
        resp = requests.get(
            FACEBOOK_USERINFO_URL,
            params={'fields': 'id,name,email'},
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json',
            },
            timeout=FETCH_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('Facebook userinfo fetch failed: %s', exc)
        return None, 'Could not reach Facebook — please try again.'
    if resp.status_code != 200:
        return None, 'Could not read your profile from Facebook.'
    return resp.json(), None
