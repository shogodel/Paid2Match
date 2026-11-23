"""Phase 1 design modernization tests.

Covers the behaviour the redesign added, not the redesign's pixels:
AJAX JSON endpoints for like/comment/share/follow/delete, the in-feed
composer (including referral-need creation and bounty attachment),
the new-count pill endpoint, the relative-time filter, and the
rendered output of the new nav/theme/homepage/profile shell
(bottom tab bar, anti-flash script, live-text wordmark, de-branded
homepage, tabbed profiles).
"""
from datetime import datetime, timedelta, timezone

from extensions import db
from models.bounty import Bounty
from models.post import Comment, Post

JSON_HEADERS = {
    'X-Requested-With': 'XMLHttpRequest',
    'Accept': 'application/json',
}


# ---------------------------------------------------------------- AJAX likes


class TestAjaxLikes:
    def test_ajax_like_toggle_returns_json(
            self, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        url = f'/social/posts/{sample_post.id}/like'

        r = client.post(url, headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.is_json
        assert r.json == {'liked': True, 'like_count': 1}

        r = client.post(url, headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json == {'liked': False, 'like_count': 0}

    def test_ajax_like_requires_auth(self, client, sample_post):
        r = client.post(
            f'/social/posts/{sample_post.id}/like', headers=JSON_HEADERS)
        assert r.status_code == 401
        assert r.is_json
        assert r.json['error'] == 'authentication required'

    def test_plain_form_like_still_redirects(
            self, client, login_as, second_user, sample_post):
        """A plain (non-AJAX) POST keeps the old redirect behaviour."""
        login_as(client, 'second@example.com', 'SecondPass123!')
        r = client.post(f'/social/posts/{sample_post.id}/like')
        assert r.status_code == 302
        assert not r.is_json


# ------------------------------------------------------------ AJAX comments


class TestAjaxComments:
    def test_ajax_comment_returns_comment_json(
            self, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        r = client.post(
            f'/social/posts/{sample_post.id}/comment',
            data={'body': 'Great post!'},
            headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json['ok'] is True
        assert r.json['comment']['body'] == 'Great post!'
        assert r.json['comment']['author_name'] == 'Second User'
        assert r.json['comment_count'] == 1

    def test_ajax_comment_validation_error_is_json(
            self, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        r = client.post(
            f'/social/posts/{sample_post.id}/comment',
            data={'body': ''},
            headers=JSON_HEADERS)
        assert r.status_code == 400
        assert r.json['ok'] is False
        assert 'errors' in r.json

    def test_comments_json_endpoint(
            self, app, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.post(
            f'/social/posts/{sample_post.id}/comment',
            data={'body': 'First comment'},
            headers=JSON_HEADERS)
        r = client.get(f'/social/posts/{sample_post.id}/comments')
        assert r.status_code == 200
        assert r.json['comment_count'] == 1
        assert r.json['comments'][0]['body'] == 'First comment'
        assert 'detail_url' in r.json


# -------------------------------------------------------------- AJAX share


class TestAjaxShare:
    def test_ajax_share_returns_json_and_is_unique(
            self, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        url = f'/social/posts/{sample_post.id}/share'
        r = client.post(url, data={'comment': ''}, headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json == {'ok': True, 'shared': True, 'share_count': 1}

        # A second share is rejected but reports the current state.
        r = client.post(url, data={'comment': ''}, headers=JSON_HEADERS)
        assert r.status_code == 400
        assert r.json['shared'] is True
        assert r.json['share_count'] == 1


# ------------------------------------------------------------- AJAX follow


class TestAjaxFollow:
    def test_ajax_follow_is_idempotent(
            self, client, logged_in_user, second_user):
        url = f'/social/follow/{second_user.id}'
        r = client.post(url, headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json == {'ok': True, 'following': True, 'followers_count': 1}

        r = client.post(url, headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json['followers_count'] == 1

        r = client.post(
            f'/social/unfollow/{second_user.id}', headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json == {'ok': True, 'following': False, 'followers_count': 0}

    def test_ajax_follow_requires_auth(self, client, second_user):
        r = client.post(
            f'/social/follow/{second_user.id}', headers=JSON_HEADERS)
        assert r.status_code == 401


# ------------------------------------------------------------- AJAX delete


class TestAjaxDelete:
    def test_ajax_delete_own_post(
            self, app, client, logged_in_user, sample_post):
        r = client.post(
            f'/social/posts/{sample_post.id}/delete', headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json == {'ok': True, 'post_id': sample_post.id}
        with app.app_context():
            assert db.session.get(Post, sample_post.id) is None

    def test_ajax_delete_other_users_post_is_forbidden(
            self, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        r = client.post(
            f'/social/posts/{sample_post.id}/delete', headers=JSON_HEADERS)
        assert r.status_code == 403
        assert r.json['ok'] is False


# ------------------------------------------------------- new-count endpoint


class TestFeedNewCount:
    def test_new_count_sees_fresh_posts(
            self, app, client, logged_in_user, sample_user, second_user):
        from models.post import Post as PostModel
        # The sample user follows the second user; a post by the second
        # user after `since` must be counted.
        client.post(f'/social/follow/{second_user.id}')
        with app.app_context():
            since = datetime.now(timezone.utc).replace(tzinfo=None)
            db.session.add(PostModel(
                author_id=second_user.id, post_type='update',
                body='Fresh post for the pill.'))
            db.session.commit()
        r = client.get(
            '/social/feed/new-count',
            query_string={'since': since.isoformat()})
        assert r.status_code == 200
        assert r.json['count'] == 1

    def test_new_count_bad_since_returns_zero(self, client, logged_in_user):
        r = client.get(
            '/social/feed/new-count', query_string={'since': 'not-a-date'})
        assert r.status_code == 200
        assert r.json['count'] == 0


# ----------------------------------------------------------------- composer


class TestComposer:
    def test_composer_creates_update_and_returns_card(
            self, app, client, logged_in_user):
        r = client.post(
            '/social/posts/new',
            data={'post_type': 'update', 'body': 'Composer post via test'},
            headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json['ok'] is True
        assert 'post-card' in r.json['html']
        assert 'Composer post via test' in r.json['html']
        with app.app_context():
            post = db.session.get(Post, r.json['post_id'])
            assert post is not None
            assert post.post_type == 'update'

    def test_composer_need_free_creates_bounty_and_post(
            self, app, client, logged_in_user):
        r = client.post(
            '/social/posts/new',
            data={
                'post_type': 'need_free',
                'title': 'Need a Python mentor',
                'body': 'Looking for a senior Python mentor for a few hours a week.',
                'location': 'Remote',
            },
            headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json['ok'] is True
        with app.app_context():
            post = db.session.get(Post, r.json['post_id'])
            assert post.post_type == 'need_free'
            assert post.bounty_id is not None
            bounty = db.session.get(Bounty, post.bounty_id)
            assert bounty.title == 'Need a Python mentor'
            assert bounty.is_free is True

    def test_composer_need_paid_returns_redirect_url(
            self, app, client, logged_in_user):
        r = client.post(
            '/social/posts/new',
            data={
                'post_type': 'need_paid',
                'title': 'Need a senior engineer',
                'body': 'Hiring a senior engineer, bounty attached.',
                'reward_amount': '500',
                'location': 'Montreal',
            },
            headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json['ok'] is True
        assert 'secure-payment' in r.json['redirect']

    def test_composer_attach_opportunity_links_bounty(
            self, app, client, logged_in_user, sample_bounty):
        r = client.post(
            '/social/posts/new',
            data={
                'post_type': 'update',
                'body': 'Sharing one of my open opportunities.',
                'bounty_id': str(sample_bounty.id),
            },
            headers=JSON_HEADERS)
        assert r.status_code == 200
        assert 'Senior Python Engineer Needed for Startup' in r.json['html']
        with app.app_context():
            post = db.session.get(Post, r.json['post_id'])
            assert str(post.bounty_id) == str(sample_bounty.id)

    def test_composer_attach_other_users_bounty_ignored(
            self, app, client, logged_in_user, sample_bounty, login_as,
            second_user):
        """Attaching a bounty you do not own must silently not attach."""
        login_as(client, 'second@example.com', 'SecondPass123!')
        r = client.post(
            '/social/posts/new',
            data={
                'post_type': 'update',
                'body': 'Trying to attach someone else\u2019s bounty.',
                'bounty_id': str(sample_bounty.id),
            },
            headers=JSON_HEADERS)
        assert r.status_code == 200
        with app.app_context():
            post = db.session.get(Post, r.json['post_id'])
            assert post.bounty_id is None

    def test_composer_validation_error_is_json(
            self, client, logged_in_user):
        r = client.post(
            '/social/posts/new',
            data={'post_type': 'update', 'body': ''},
            headers=JSON_HEADERS)
        assert r.status_code == 400
        assert r.json['ok'] is False
        assert 'errors' in r.json


# --------------------------------------------------------- relative-time filter


class TestRelativeTimeFilter:
    def test_relativetime_values(self, app):
        from flask import render_template_string
        with app.app_context():
            now = datetime.now(timezone.utc)
            f = app.jinja_env.filters['relativetime']
            assert f(now) == 'now'
            assert f(now - timedelta(minutes=5)) == '5m'
            assert f(now - timedelta(hours=2)) == '2h'
            assert f(now - timedelta(days=1)) == 'Yesterday'
            assert f(now - timedelta(days=3)) in (
                'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
            assert f(None) == ''
            # And through the template engine, as the cards use it.
            # A request context is needed: the notification context
            # processor reads current_user, which is None outside one.
            with app.test_request_context():
                rendered = render_template_string(
                    "{{ ts|relativetime }}", ts=now - timedelta(minutes=30))
                assert rendered == '30m'


# ------------------------------------------------------- nav / theme rendering


class TestNavAndThemeRendering:
    def test_bottom_tab_bar_present_when_logged_in(self, client, logged_in_user):
        html = client.get('/social/feed').data.decode()
        assert 'bottom-tabbar' in html
        assert 'tab-post' in html
        assert '/social/feed#composer' in html

    def test_bottom_tab_bar_absent_when_anonymous(self, client):
        html = client.get('/').data.decode()
        assert 'bottom-tabbar' not in html

    def test_theme_toggle_and_antiflash_present(self, client, logged_in_user):
        html = client.get('/').data.decode()
        assert 'p2m-theme' in html
        assert 'data-bs-theme' in html
        assert 'theme-toggle' in html or 'js-theme-toggle' in html

    def test_live_text_wordmark(self, client):
        html = client.get('/').data.decode()
        assert 'paid<span class="brand-2">2</span>match' in html

    def test_feed_layout_and_composer_present(self, client, logged_in_user):
        html = client.get('/social/feed').data.decode()
        assert 'feed-layout' in html
        assert 'composer-card' in html
        assert 'new-posts-pill' in html
        assert 'post-card' in html or 'Nothing here yet' in html

    def test_og_meta_tags_present(self, client):
        html = client.get('/').data.decode()
        assert 'property="og:title"' in html
        assert 'property="og:image"' in html
        assert 'name="twitter:card"' in html


# ---------------------------------------------------- homepage de-branding


class TestHomepageDebranding:
    def test_no_zero_counters_and_no_proudly(self, client):
        html = client.get('/').data.decode()
        for gone in (
            'Total Users', 'Total Paid Out', 'Successful Matches',
            'Proudly Canadian', 'Proudly made', 'coast to coast',
            'Des humains',
        ):
            assert gone not in html, gone
        assert 'The referral network for hiring' in html
        assert 'Made in Canada' in html

    def test_only_two_leaves_remain(self, client):
        """The plan's end state: footer leaf + pledge-band leaf only."""
        html = client.get('/').data.decode()
        assert html.count('maple-leaf') == 2


# ------------------------------------------------------------- profile shell


class TestProfileShell:
    def test_own_profile_has_cover_tabs_and_no_duplicate_stats(
            self, client, logged_in_user, sample_post):
        r = client.get('/profile/')
        assert r.status_code == 200
        html = r.data.decode()
        assert 'profile-cover-band' in html
        assert 'profile-tabs' in html
        assert 'tab-posts' in html
        assert 'tab-referrals' in html
        assert 'tab-about' in html
        assert 'Hello network' in html  # Posts tab shows the user's posts
        assert 'Your numbers' not in html
        assert 'Activity Stats' not in html
        assert 'data-followers-count' in html

    def test_public_profile_has_ajax_follow_pill(
            self, client, logged_in_user, second_user):
        html = client.get(f'/profile/{second_user.id}').data.decode()
        assert 'profile-cover-band' in html
        assert 'btn-follow' in html
        assert f'data-follow-url="/social/follow/{second_user.id}"' in html
        assert f'data-unfollow-url="/social/unfollow/{second_user.id}"' in html
