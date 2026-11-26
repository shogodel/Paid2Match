"""Security audit 2026-11-25 — regression tests for the new findings.

Each test pins one fix from the audit25-security workstream:
- F1 open redirect via scheme-only URLs in auth _is_safe_url
- F2 open redirect via backslash in staked/video _safe_next
- F3 stored javascript: URL in the pledge website (no URL validator)
- F4 DM thread POST injecting messages into a referral negotiation
- F5 SSRF in the linked-account bio-link/code page fetch
- F6 user-picker API leaking every user's email address
- F7 Connect account creation without idempotency key / race fallback
- F8 admin-bootstrap form missing its CSRF token
- F9 app sets no security headers of its own
- F10 unbounded ?limit= on the public bounties API
"""
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from extensions import db
from models.message import Message
from models.user import User


# ---------------------------------------------------------------- F1


class TestAuthSafeUrl:
    def test_rejects_scheme_only_open_redirects(self, app):
        from blueprints.auth.routes import _is_safe_url
        with app.test_request_context('/', base_url='http://localhost'):
            for evil in (
                'http:/evil.com',
                'http:evil.com',
                'https:/evil.com',
                'https:evil.com',
                'http:///evil.com',
                '//evil.com',
                '/\\evil.com',
                'javascript:alert(1)',
                'http://localhost@evil.com',
            ):
                assert _is_safe_url(evil) is False, evil

    def test_accepts_same_origin_targets(self, app):
        from blueprints.auth.routes import _is_safe_url
        with app.test_request_context('/', base_url='http://localhost'):
            assert _is_safe_url('/board') is True
            assert _is_safe_url('/needs/abc?x=1') is True
            assert _is_safe_url('http://localhost/board') is True


# ---------------------------------------------------------------- F2


class TestSafeNextBackslash:
    @pytest.mark.parametrize('mod', [
        'blueprints.staked_vouches.routes',
        'blueprints.video_vouches.routes',
    ])
    def test_backslash_redirect_returns_default(self, app, mod):
        import importlib
        module = importlib.import_module(mod)
        with app.test_request_context('/', base_url='http://localhost'):
            assert module._safe_next('/\\evil.com', '/fallback') == '/fallback'
            assert module._safe_next('/valid', '/fallback') == '/valid'


# ---------------------------------------------------------------- F3


class TestPledgeWebsiteValidator:
    def test_javascript_url_rejected(self, app):
        from blueprints.social.forms import PledgeForm
        with app.test_request_context(
            '/social/pledges/new', method='POST',
            data={'company_name': 'Acme',
                  'website': 'javascript:alert(document.domain)'},
        ):
            assert PledgeForm().validate() is False

    def test_https_url_accepted(self, app):
        from blueprints.social.forms import PledgeForm
        with app.test_request_context(
            '/social/pledges/new', method='POST',
            data={'company_name': 'Acme',
                  'website': 'https://acme.example.com'},
        ):
            assert PledgeForm().validate() is True


# ---------------------------------------------------------------- F4


class TestThreadContextParticipation:
    def test_outsider_cannot_inject_into_referral_thread(
        self, client, app, login_as, sample_user, sample_hunter,
        sample_pitch, second_user,
    ):
        """A user who is neither the pitch's hunter nor the need's
        poster must not be able to tag a DM with that pitch id — the
        message would show up on the referral detail page for the
        referrer, poster and candidate."""
        login_as(client, 'second@example.com')
        r = client.post(
            f'/messages/thread/{sample_user.id}',
            data={'content': 'I am not part of this deal.',
                  'pitch_id': sample_pitch.id,
                  'bounty_id': sample_pitch.bounty_id},
        )
        assert r.status_code in (302, 403)
        with app.app_context():
            assert Message.query.filter_by(
                pitch_id=sample_pitch.id,
                sender_id=second_user.id,
            ).count() == 0

    def test_outsider_injection_refused_as_json(
        self, client, app, login_as, sample_user,
        sample_pitch, second_user,
    ):
        login_as(client, 'second@example.com')
        r = client.post(
            f'/messages/thread/{sample_user.id}',
            data={'content': 'Injected.',
                  'pitch_id': sample_pitch.id},
            headers={'X-Requested-With': 'XMLHttpRequest',
                     'Accept': 'application/json'},
        )
        assert r.status_code == 403
        assert r.get_json()['ok'] is False

    def test_hunter_and_poster_can_still_use_pitch_context(
        self, client, app, login_as, sample_user, sample_hunter,
        sample_pitch,
    ):
        login_as(client, 'hunter@example.com')
        r = client.post(
            f'/messages/thread/{sample_user.id}',
            data={'content': 'Let us discuss the referral.',
                  'pitch_id': sample_pitch.id,
                  'bounty_id': sample_pitch.bounty_id},
        )
        assert r.status_code == 302
        with app.app_context():
            assert Message.query.filter_by(
                pitch_id=sample_pitch.id,
                sender_id=sample_hunter.id,
            ).count() == 1

    def test_outsider_cannot_tag_bounty_only_context(
        self, client, app, login_as, sample_user,
        sample_bounty, second_user,
    ):
        login_as(client, 'second@example.com')
        r = client.post(
            f'/messages/thread/{sample_user.id}',
            data={'content': 'About your need.',
                  'bounty_id': sample_bounty.id},
        )
        assert r.status_code in (302, 403)
        with app.app_context():
            assert Message.query.filter_by(
                bounty_id=sample_bounty.id,
                sender_id=second_user.id,
            ).count() == 0


# ---------------------------------------------------------------- F5


def _resp(status=200, text='', headers=None):
    return SimpleNamespace(
        status_code=status, text=text, headers=headers or {},
    )


class TestFetchPageSsrfGuard:
    def test_rejects_private_and_metadata_ips(self, app):
        from utils.linked_accounts import _fetch_page
        for url in (
            'http://169.254.169.254/latest/meta-data/',
            'http://127.0.0.1:8000/admin',
            'http://10.0.0.5/',
            'http://192.168.1.1/',
            'http://100.109.149.54/',
            'http://[::1]/',
            'http://localhost:5000/',
            'ftp://example.com/file',
        ):
            with patch('utils.linked_accounts.requests.get') as get:
                text, error = _fetch_page(url)
                assert text is None, url
                assert 'cannot be fetched' in error, url
                get.assert_not_called()

    def test_rejects_redirect_to_private_ip(self, app):
        from utils.linked_accounts import _fetch_page
        with patch(
            'utils.linked_accounts.requests.get',
            return_value=_resp(
                status=302,
                headers={'Location': 'http://169.254.169.254/latest/meta-data/'},
            ),
        ) as get:
            text, error = _fetch_page('https://example.com/profile')
            assert text is None
            assert 'cannot be fetched' in error
            assert get.call_count == 1

    def test_public_url_still_fetches(self, app):
        from utils.linked_accounts import _fetch_page
        with patch(
            'utils.linked_accounts.requests.get',
            return_value=_resp(text='<p>hello</p>'),
        ):
            text, error = _fetch_page('https://example.com/github/user')
            assert (text, error) == ('<p>hello</p>', None)

    def test_bio_link_check_rejects_internal_url(self, app, sample_user):
        from models.linked_account import LinkedAccount
        from utils.linked_accounts import verify_bio_link
        with app.app_context():
            account = LinkedAccount(
                user_id=sample_user.id, platform='github',
                verification_method='bio_link',
                url='http://169.254.169.254/latest/meta-data/',
            )
            db.session.add(account)
            db.session.commit()
            with patch('utils.linked_accounts.requests.get') as get:
                ok, error = verify_bio_link(
                    account, 'https://paid2match.work/profile/x',
                )
                assert ok is False
                assert 'cannot be fetched' in error
                get.assert_not_called()


# ---------------------------------------------------------------- F6


class TestUserPickerNoEmailLeak:
    def test_api_users_returns_no_email(
        self, logged_in_user, client, sample_hunter,
    ):
        r = logged_in_user.get('/messages/api/users?q=Hunter')
        data = r.get_json()
        assert any(u['id'] == sample_hunter.id for u in data)
        assert all('email' not in u for u in data)

    def test_api_users_still_matches_by_known_email(
        self, logged_in_user, client, sample_hunter,
    ):
        r = logged_in_user.get('/messages/api/users?q=hunter@example')
        data = r.get_json()
        assert any(u['id'] == sample_hunter.id for u in data)


# ---------------------------------------------------------------- F7


class TestConnectAccountIdempotency:
    def test_create_uses_deterministic_idempotency_key(self, app, sample_user):
        from models.admin_settings import AdminSettings
        from utils.payments import create_connect_account
        fake_account = SimpleNamespace(id='acct_123')
        with app.app_context():
            AdminSettings.set('STRIPE_SECRET_KEY', 'sk_test_fake')
        with app.app_context(), patch(
            'utils.payments.stripe.Account.create',
            return_value=fake_account,
        ) as create:
            account, error = create_connect_account(sample_user)
            assert error is None
            assert account.stripe_account_id == 'acct_123'
            _, kwargs = create.call_args
            assert kwargs['idempotency_key'] == f'connect-{sample_user.id}'

    def test_second_setup_returns_existing_without_new_stripe_call(
        self, app, sample_user,
    ):
        from models.connect_account import ConnectAccount
        from utils.payments import create_connect_account
        with app.app_context():
            db.session.add(ConnectAccount(
                user_id=sample_user.id,
                stripe_account_id='acct_existing',
                onboarding_status='pending',
            ))
            db.session.commit()
            with patch(
                'utils.payments.stripe.Account.create',
            ) as create:
                account, error = create_connect_account(sample_user)
                assert error is None
                assert account.stripe_account_id == 'acct_existing'
                create.assert_not_called()


# ---------------------------------------------------------------- F8


class TestCreateAdminFormHasCsrfToken:
    def test_bootstrap_form_carries_csrf_token(
        self, client, app, monkeypatch,
    ):
        monkeypatch.setenv('ADMIN_SETUP_TOKEN', 'test-token-123')
        r = client.post(
            '/admin/create-admin',
            data={'token': 'test-token-123'},
        )
        assert r.status_code == 200
        assert b'name="csrf_token"' in r.data


# ---------------------------------------------------------------- F9


class TestSecurityHeaders:
    def test_headers_present_on_response(self, client):
        r = client.get('/')
        assert r.headers.get('X-Content-Type-Options') == 'nosniff'
        assert r.headers.get('X-Frame-Options') == 'SAMEORIGIN'
        assert (
            r.headers.get('Referrer-Policy')
            == 'strict-origin-when-cross-origin'
        )


# ---------------------------------------------------------------- F10


class TestBountiesApiLimitClamp:
    def test_negative_limit_does_not_mean_unlimited(
        self, client, sample_bounty, sample_paid_bounty,
    ):
        r = client.get('/needs/api?limit=-1')
        assert r.status_code == 200
        assert len(r.get_json()) == 1

    def test_huge_limit_is_capped(self, client, sample_bounty):
        r = client.get('/needs/api?limit=100000')
        assert r.status_code == 200
        assert len(r.get_json()) <= 100
