"""Security audit 2026-11-27 — regression tests for the new findings.

- S1 SSRF bypass via non-decimal IP literals in the linked-account
  fetch guard (hex / octal / short-form / single-integer hosts
  resolve to loopback but ipaddress does not parse them).
- S2 /messages/api/conversations still returned every conversation
  partner's email (the picker endpoint was fixed in audit25, this
  one was missed).
- S3 linked-account page fetch had no response-size cap (unbounded
  body read into worker memory).
- S4 social search passed LIKE wildcards unescaped ('%' matched
  everyone, as the picker did before audit25).
- S5 referral form choices rendered followed users' emails into the
  page (same email-exposure class as audit25 F6).
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from extensions import db
from models.follow import Follow


# ---------------------------------------------------------------- S1


class TestSsrfAlternateIpNotations:
    @pytest.mark.parametrize('url', [
        'http://0x7f.0.0.1/admin',
        'http://0x7f000001/admin',
        'http://2130706433/admin',
        'http://0177.0.0.1/admin',
        'http://127.1/admin',
        'http://0/admin',
        'http://0xC0.0xA8.0x01.0x01/',
        'http://3232235777/',
        'http://0xA9.0xFE.0xA9.0xFE/latest/meta-data/',
    ])
    def test_rejects_non_decimal_loopback_and_private_ips(self, app, url):
        from utils.linked_accounts import _is_safe_fetch_url
        assert _is_safe_fetch_url(url) is False, url

    def test_fetch_never_called_for_hex_loopback(self, app):
        from utils.linked_accounts import _fetch_page
        with patch('utils.linked_accounts.requests.get') as get:
            text, error = _fetch_page('http://0x7f.0.0.1:8000/admin')
            assert text is None
            assert 'cannot be fetched' in error
            get.assert_not_called()

    def test_ip_literal_helper(self, app):
        from utils.linked_accounts import _ip_literal
        assert str(_ip_literal('0x7f.0.0.1')) == '127.0.0.1'
        assert str(_ip_literal('2130706433')) == '127.0.0.1'
        assert str(_ip_literal('0177.0.0.1')) == '127.0.0.1'
        assert _ip_literal('example.com') is None
        assert _ip_literal('github.com') is None

    def test_public_url_still_allowed(self, app):
        from utils.linked_accounts import _is_safe_fetch_url
        assert _is_safe_fetch_url('https://example.com/profile') is True
        assert _is_safe_fetch_url('https://github.com/someone') is True


# ---------------------------------------------------------------- S2


class TestApiConversationsNoEmailLeak:
    def test_conversations_return_no_email(
        self, logged_in_user, sample_message, sample_hunter,
    ):
        r = logged_in_user.get('/messages/api/conversations')
        assert r.status_code == 200
        data = r.get_json()
        assert any(u['id'] == sample_hunter.id for u in data)
        assert all('email' not in u for u in data)
        assert all('hunter@example.com' not in str(u) for u in data)

    def test_conversations_still_identify_partner(
        self, logged_in_user, sample_message, sample_hunter,
    ):
        r = logged_in_user.get('/messages/api/conversations')
        partner = next(
            u for u in r.get_json() if u['id'] == sample_hunter.id
        )
        assert partner['name'] == 'Hunter User'
        assert partner['handle']


# ---------------------------------------------------------------- S3


def _stream_resp(chunks, content_length=None, status=200):
    headers = {}
    if content_length is not None:
        headers['Content-Length'] = str(content_length)
    return SimpleNamespace(
        status_code=status,
        headers=headers,
        encoding='utf-8',
        iter_content=lambda chunk_size: iter(chunks),
    )


class TestFetchPageSizeCap:
    def test_refuses_huge_content_length_without_reading_body(self, app):
        from utils.linked_accounts import _fetch_page
        resp = _stream_resp([], content_length=500 * 1024 * 1024)
        with patch(
            'utils.linked_accounts.requests.get', return_value=resp,
        ) as get:
            text, error = _fetch_page('https://example.com/big')
            assert text is None
            assert 'too large' in error
            get.assert_called_once()

    def test_refuses_body_over_cap_despite_small_declared_length(
        self, app,
    ):
        from utils.linked_accounts import MAX_FETCH_BYTES, _fetch_page
        big = b'x' * (MAX_FETCH_BYTES + 1)
        resp = _stream_resp(
            [big[:1024 * 1024], big[1024 * 1024:]],
            content_length=len(big) - 1,  # lying header
        )
        with patch(
            'utils.linked_accounts.requests.get', return_value=resp,
        ):
            text, error = _fetch_page('https://example.com/big')
            assert text is None
            assert 'too large' in error

    def test_small_streamed_page_still_fetches(self, app):
        from utils.linked_accounts import _fetch_page
        resp = _stream_resp([b'<p>hello ', b'world</p>'], content_length=20)
        with patch(
            'utils.linked_accounts.requests.get', return_value=resp,
        ):
            text, error = _fetch_page('https://example.com/profile')
            assert (text, error) == ('<p>hello world</p>', None)


# ---------------------------------------------------------------- S4


class TestSocialSearchWildcardEscape:
    def test_percent_query_matches_nobody(self, client, sample_user):
        r = client.get('/social/search', query_string={'q': '%'})
        assert r.status_code == 200
        assert b'Test User' not in r.data

    def test_underscore_query_matches_nobody(self, client, sample_user):
        r = client.get('/social/search', query_string={'q': '_'})
        assert r.status_code == 200
        assert b'Test User' not in r.data

    def test_plain_query_still_matches(self, client, sample_user):
        r = client.get('/social/search', query_string={'q': 'Test'})
        assert r.status_code == 200
        assert b'Test User' in r.data


# ---------------------------------------------------------------- S5


class TestReferralChoicesNoEmail:
    def test_followed_users_listed_without_email(
        self, app, sample_user, sample_hunter,
    ):
        from blueprints.referrals.routes import _connection_choices
        with app.app_context():
            db.session.add(Follow(
                follower_id=sample_user.id,
                followed_id=sample_hunter.id,
            ))
            db.session.commit()
            choices = _connection_choices(sample_user.id)
        labels = [label for _id, label in choices]
        assert any(_id == sample_hunter.id for _id, _l in choices)
        assert not any('hunter@example.com' in label for label in labels)
        assert any('Hunter User' in label for label in labels)
