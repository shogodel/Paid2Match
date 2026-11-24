"""M3 linked-account verification tests — OAuth (mocked), bio-link and
code checks (mocked fetches), manual review queue, 90-day expiry,
trust identity points, visibility rules."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from extensions import db
from models import LinkedAccount, utc_now
from models.follow import Follow
from models.linked_account import generate_verification_code
from utils.linked_accounts import (
    exchange_oauth_code,
    fetch_oauth_profile,
    get_or_create_link,
    mark_verified,
    oauth_authorize_url,
    oauth_available,
    refresh_linked_account_expiry,
    verified_linked_count,
    verify_bio_link,
    verify_code,
    visible_linked_accounts,
)
from utils.trust import identity_points


def _mock_resp(status=200, text='', json_data=None):
    resp = SimpleNamespace(status_code=status, text=text)
    resp.json = lambda: json_data if json_data is not None else {}
    return resp


def _link(user, platform='github', method='bio_link', **kwargs):
    account = get_or_create_link(
        user.id, platform, method,
        url=kwargs.pop('url', f'https://example.com/{platform}/user'),
        handle=kwargs.pop('handle', None),
    )
    for key, value in kwargs.items():
        setattr(account, key, value)
    db.session.commit()
    db.session.refresh(account)
    return account


class TestVerificationCode:
    def test_code_format(self):
        code = generate_verification_code()
        assert code.startswith('P2M-')
        assert len(code) == 9  # P2M- + 5 chars
        # Unambiguous alphabet — no 0/O, 1/I/l.
        for ch in code[4:]:
            assert ch not in '0O1Il'

    def test_code_method_issues_code(self, app, sample_user):
        account = _link(sample_user, 'instagram', 'code')
        assert account.verification_code.startswith('P2M-')


class TestBioLinkCheck:
    def test_bio_link_found(self, app, sample_user):
        account = _link(sample_user)
        profile_url = f'https://paid2match.work/profile/{sample_user.id}'
        with patch(
            'utils.linked_accounts.requests.get',
            return_value=_mock_resp(
                text=f'<a href="{profile_url}">Paid2Match</a>'
            ),
        ):
            ok, error = verify_bio_link(account, profile_url)
        assert (ok, error) == (True, None)

    def test_bio_link_case_insensitive(self, app, sample_user):
        account = _link(sample_user)
        profile_url = f'https://paid2match.work/profile/{sample_user.id}'
        with patch(
            'utils.linked_accounts.requests.get',
            return_value=_mock_resp(text=f'<a href="{profile_url.upper()}">x</a>'),
        ):
            ok, _error = verify_bio_link(account, profile_url)
        assert ok is True

    def test_bio_link_missing(self, app, sample_user):
        account = _link(sample_user)
        with patch(
            'utils.linked_accounts.requests.get',
            return_value=_mock_resp(text='<p>Nothing here.</p>'),
        ):
            ok, error = verify_bio_link(
                account, 'https://paid2match.work/profile/xyz'
            )
        assert ok is False
        assert 'could not find' in error.lower()

    def test_bio_link_fetch_failure(self, app, sample_user):
        import requests
        account = _link(sample_user)
        with patch(
            'utils.linked_accounts.requests.get',
            side_effect=requests.RequestException('blocked'),
        ):
            ok, error = verify_bio_link(account, 'https://x/y')
        assert ok is False
        assert 'Could not fetch' in error


class TestCodeCheck:
    def test_code_found(self, app, sample_user):
        account = _link(sample_user, 'instagram', 'code')
        with patch(
            'utils.linked_accounts.requests.get',
            return_value=_mock_resp(text=f'Bio: {account.verification_code}'),
        ):
            ok, error = verify_code(account)
        assert (ok, error) == (True, None)

    def test_code_missing(self, app, sample_user):
        account = _link(sample_user, 'instagram', 'code')
        with patch(
            'utils.linked_accounts.requests.get',
            return_value=_mock_resp(text='Bio: nothing'),
        ):
            ok, error = verify_code(account)
        assert ok is False
        assert 'could not find the code' in error.lower()


class TestOAuth:
    def test_oauth_unavailable_without_config(self, app):
        assert oauth_available('github') is False
        assert oauth_available('linkedin') is False
        assert oauth_authorize_url('github', 'https://x/cb', 'state') is None

    def test_oauth_authorize_url_with_config(self, app):
        app.config['GITHUB_CLIENT_ID'] = 'gh_id'
        app.config['GITHUB_CLIENT_SECRET'] = 'gh_secret'
        assert oauth_available('github') is True
        url = oauth_authorize_url('github', 'https://x/cb', 'state123')
        assert url.startswith('https://github.com/login/oauth/authorize?')
        assert 'client_id=gh_id' in url
        assert 'state=state123' in url

    def test_exchange_oauth_code(self, app):
        app.config['GITHUB_CLIENT_ID'] = 'gh_id'
        app.config['GITHUB_CLIENT_SECRET'] = 'gh_secret'
        with patch(
            'utils.linked_accounts.requests.post',
            return_value=_mock_resp(json_data={'access_token': 'tok_1'}),
        ) as post:
            token, error = exchange_oauth_code(
                'github', 'code_1', 'https://x/cb'
            )
        assert (token, error) == ('tok_1', None)
        assert post.call_args.args[0] == 'https://github.com/login/oauth/access_token'

    def test_fetch_github_profile(self, app):
        with patch(
            'utils.linked_accounts.requests.get',
            return_value=_mock_resp(json_data={
                'login': 'octocat', 'html_url': 'https://github.com/octocat',
            }),
        ):
            handle, url, error = fetch_oauth_profile('github', 'tok_1')
        assert (handle, url, error) == (
            'octocat', 'https://github.com/octocat', None
        )

    def test_oauth_callback_verifies_link(
        self, app, client, sample_user, login_as
    ):
        app.config['GITHUB_CLIENT_ID'] = 'gh_id'
        app.config['GITHUB_CLIENT_SECRET'] = 'gh_secret'
        login_as(client, sample_user.email, 'TestPass123!')
        # Start the flow — sets the state token in the session.
        resp = client.get('/profile/linked/github/oauth')
        assert resp.status_code == 302
        assert 'github.com/login/oauth/authorize' in resp.headers['Location']
        with client.session_transaction() as sess:
            state = sess['oauth_state']
        with patch(
            'blueprints.profile.routes.exchange_oauth_code',
            return_value=('tok_1', None),
        ), patch(
            'blueprints.profile.routes.fetch_oauth_profile',
            return_value=('octocat', 'https://github.com/octocat', None),
        ):
            resp = client.get(
                f'/profile/linked/github/oauth/callback?code=c1&state={state}'
            )
        assert resp.status_code == 302
        account = LinkedAccount.query.filter_by(
            user_id=sample_user.id, platform='github'
        ).first()
        assert account is not None
        assert account.status == 'verified'
        assert account.handle == 'octocat'
        assert account.is_currently_verified is True

    def test_oauth_callback_rejects_bad_state(
        self, app, client, sample_user, login_as
    ):
        app.config['GITHUB_CLIENT_ID'] = 'gh_id'
        app.config['GITHUB_CLIENT_SECRET'] = 'gh_secret'
        login_as(client, sample_user.email, 'TestPass123!')
        client.get('/profile/linked/github/oauth')
        resp = client.get(
            '/profile/linked/github/oauth/callback?code=c1&state=wrong'
        )
        assert resp.status_code == 302
        assert LinkedAccount.query.filter_by(
            user_id=sample_user.id, platform='github'
        ).first() is None


class TestManualReview:
    def test_request_and_approve(self, app, client, sample_user, sample_admin, login_as):
        account = _link(sample_user, 'instagram', 'bio_link')
        login_as(client, sample_user.email, 'TestPass123!')
        resp = client.post(f'/profile/linked/{account.id}/manual-review')
        assert resp.status_code == 302
        db.session.refresh(account)
        assert account.manual_review is True

        login_as(client, sample_admin.email, 'AdminPass123!')
        resp = client.get('/admin/linked-accounts')
        assert resp.status_code == 200
        assert b'instagram' in resp.data.lower()
        resp = client.post(f'/admin/linked-accounts/{account.id}/approve')
        assert resp.status_code == 302
        db.session.refresh(account)
        assert account.status == 'verified'
        assert account.is_currently_verified is True

    def test_reject_with_note(self, app, client, sample_user, sample_admin, login_as):
        account = _link(sample_user, 'instagram', 'bio_link')
        login_as(client, sample_user.email, 'TestPass123!')
        client.post(f'/profile/linked/{account.id}/manual-review')
        login_as(client, sample_admin.email, 'AdminPass123!')
        resp = client.post(
            f'/admin/linked-accounts/{account.id}/reject',
            data={'admin_note': 'No link found in bio.'},
        )
        assert resp.status_code == 302
        db.session.refresh(account)
        assert account.status == 'rejected'
        assert account.admin_note == 'No link found in bio.'

    def test_rejected_link_can_be_relinked(
        self, app, sample_user
    ):
        account = _link(sample_user, 'instagram', 'bio_link')
        account.status = 'rejected'
        db.session.commit()
        relinked = get_or_create_link(
            sample_user.id, 'instagram', 'code',
            url='https://instagram.com/user',
        )
        db.session.commit()
        assert relinked.id == account.id
        assert relinked.status == 'pending'
        assert relinked.verification_code is not None


class TestExpiry:
    def test_mark_verified_sets_90_day_expiry(self, app, sample_user):
        account = _link(sample_user)
        mark_verified(account)
        db.session.commit()
        assert account.status == 'verified'
        assert (account.expires_at - account.verified_at).days == 90

    def test_refresh_flips_expired(self, app, sample_user):
        account = _link(sample_user)
        mark_verified(account)
        account.expires_at = utc_now() - timedelta(days=1)
        db.session.commit()
        assert account.is_currently_verified is False
        assert verified_linked_count(sample_user.id) == 0
        flipped = refresh_linked_account_expiry()
        db.session.commit()
        assert flipped == 1
        db.session.refresh(account)
        assert account.status == 'expired'

    def test_expired_link_does_not_count_for_trust(self, app, sample_user):
        # Email verified = 5 identity points; 2 verified links = +5.
        assert identity_points(sample_user) == 5
        for platform in ('github', 'linkedin'):
            account = _link(sample_user, platform, 'bio_link')
            mark_verified(account)
        db.session.commit()
        assert verified_linked_count(sample_user.id) == 2
        assert identity_points(sample_user) == 10
        # One expires — the bonus is gone.
        account = LinkedAccount.query.filter_by(
            user_id=sample_user.id, platform='github'
        ).first()
        account.expires_at = utc_now() - timedelta(days=1)
        db.session.commit()
        assert identity_points(sample_user) == 5

    def test_linked_page_refreshes_expiry(
        self, app, client, sample_user, login_as
    ):
        account = _link(sample_user)
        mark_verified(account)
        account.expires_at = utc_now() - timedelta(days=1)
        db.session.commit()
        login_as(client, sample_user.email, 'TestPass123!')
        resp = client.get('/profile/linked')
        assert resp.status_code == 200
        db.session.refresh(account)
        assert account.status == 'expired'


class TestVisibility:
    def test_hidden_link_invisible_but_counts_for_trust(
        self, app, sample_user, second_user
    ):
        account = _link(sample_user, 'github', 'bio_link')
        mark_verified(account)
        account.visibility = 'hidden'
        db.session.commit()
        assert visible_linked_accounts(sample_user.id, second_user.id) == []
        assert verified_linked_count(sample_user.id) == 1

    def test_followers_only_visible_to_followers(
        self, app, sample_user, second_user, third_user
    ):
        account = _link(sample_user, 'github', 'bio_link')
        mark_verified(account)
        account.visibility = 'followers'
        db.session.add(Follow(
            follower_id=second_user.id, followed_id=sample_user.id
        ))
        db.session.commit()
        assert visible_linked_accounts(sample_user.id, second_user.id) == [account]
        assert visible_linked_accounts(sample_user.id, third_user.id) == []
        # The owner always sees their own links.
        assert visible_linked_accounts(sample_user.id, sample_user.id) == [account]

    def test_public_link_visible_to_strangers(
        self, app, sample_user, second_user
    ):
        account = _link(sample_user, 'github', 'bio_link')
        mark_verified(account)
        db.session.commit()
        assert visible_linked_accounts(sample_user.id, second_user.id) == [account]
        assert visible_linked_accounts(sample_user.id, None) == [account]

    def test_rejected_link_never_listed(
        self, app, sample_user, second_user
    ):
        account = _link(sample_user, 'github', 'bio_link')
        account.status = 'rejected'
        db.session.commit()
        assert visible_linked_accounts(sample_user.id, second_user.id) == []
        assert visible_linked_accounts(sample_user.id, sample_user.id) == []


class TestCheckRoute:
    def test_check_bio_link_verifies(
        self, app, client, sample_user, login_as
    ):
        account = _link(sample_user)
        login_as(client, sample_user.email, 'TestPass123!')
        profile_url = f'http://localhost/profile/{sample_user.handle}'
        with patch(
            'blueprints.profile.routes.verify_bio_link',
            return_value=(True, None),
        ) as verify:
            resp = client.post(f'/profile/linked/{account.id}/check')
        assert resp.status_code == 302
        verify.assert_called_once()
        assert verify.call_args.args[1] == profile_url
        db.session.refresh(account)
        assert account.status == 'verified'

    def test_check_other_users_link_denied(
        self, app, client, sample_user, second_user, login_as
    ):
        account = _link(sample_user)
        login_as(client, second_user.email, 'SecondPass123!')
        resp = client.post(f'/profile/linked/{account.id}/check')
        assert resp.status_code == 302
        db.session.refresh(account)
        assert account.status == 'pending'
