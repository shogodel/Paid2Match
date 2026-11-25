"""SEO tests — robots.txt and the dynamic sitemap.xml.

The sitemap must list exactly what a stranger can actually see:
public static pages, seeded spaces, open publicly-visible needs
(the board's own filter), public handle profiles, and public posts.
Login-gated areas, private profiles, expired needs, unsecured paid
needs, and posts linked to them must never appear.
"""
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from extensions import db
from models.bounty import Bounty
from models.post import Post
from models.profile import Profile
from models.space import seed_spaces
from models.user import User


def _make_user(email, full_name, privacy_level=0, with_profile=True):
    """Create a user (handle auto-generated from full_name) with an
    optional profile at the given privacy level."""
    user = User(
        email=email,
        full_name=full_name,
        password_hash=generate_password_hash('TestPass123!'),
        role='user',
        is_active=True,
        email_verified=True,
    )
    db.session.add(user)
    db.session.flush()
    if with_profile:
        profile = Profile(
            user_id=user.id,
            profile_type='independent',
            display_name=full_name,
            privacy_level=privacy_level,
        )
        db.session.add(profile)
    db.session.commit()
    db.session.refresh(user)
    return user


def _make_bounty(poster_id, title, reward_amount=None,
                 payment_status='unsecured', deadline=None,
                 status='open', is_free=False):
    bounty = Bounty(
        poster_id=poster_id,
        title=title,
        description='A test bounty for the sitemap.',
        bounty_type='recruitment',
        bounty_direction='seeking_opportunity',
        reward_amount=reward_amount,
        payment_status=payment_status,
        deadline=deadline,
        status=status,
        is_free=is_free,
        location='Laval, QC',
    )
    db.session.add(bounty)
    db.session.commit()
    db.session.refresh(bounty)
    return bounty


def _make_post(author_id, body='A public test post.', bounty_id=None,
               is_hidden=False):
    post = Post(
        author_id=author_id,
        post_type='update',
        body=body,
        bounty_id=bounty_id,
        is_hidden=is_hidden,
    )
    db.session.add(post)
    db.session.commit()
    db.session.refresh(post)
    return post


def _sitemap(client):
    resp = client.get('/sitemap.xml')
    assert resp.status_code == 200
    assert resp.content_type.startswith('application/xml')
    return resp.get_data(as_text=True)


class TestRobotsTxt:
    def test_robots_txt_served(self, client):
        resp = client.get('/robots.txt')
        assert resp.status_code == 200
        assert resp.content_type.startswith('text/plain')
        body = resp.get_data(as_text=True)
        assert 'Sitemap: https://paid2match.work/sitemap.xml' in body
        for disallowed in (
            '/admin/', '/auth/', '/messages/', '/payments/',
            '/profile/linked', '/social/notifications',
            '/social/analytics', '/referrals/', '/stakes/',
            '/video-vouches/',
        ):
            assert f'Disallow: {disallowed}' in body


class TestSitemapStaticUrls:
    def test_static_urls_and_seeded_spaces(self, client, app):
        with app.app_context():
            seed_spaces()
            db.session.commit()
        body = _sitemap(client)
        assert '<loc>https://paid2match.work/</loc>' in body
        for path in (
            '/needs/', '/wins/', '/pledge/', '/intros/',
            '/social/spaces', '/social/people',
            '/social/spaces/tech', '/social/spaces/trades',
            '/social/spaces/montreal',
        ):
            assert f'https://paid2match.work{path}' in body

    def test_no_private_or_legacy_urls(self, client, app):
        with app.app_context():
            seed_spaces()
            db.session.commit()
            user = _make_user('pub@example.com', 'Public Person')
        body = _sitemap(client)
        for forbidden in ('/admin/', '/auth/', '/messages/', '/bounties/'):
            assert forbidden not in body
        # Handles are listed; UUIDs never are.
        assert f'/profile/{user.handle}' in body
        assert f'/profile/{user.id}' not in body


class TestSitemapNeeds:
    def test_lists_open_secured_need(self, client, app):
        with app.app_context():
            user = _make_user('poster@example.com', 'Poster Person')
            bounty = _make_bounty(
                user.id, 'Need a plumber in Laval',
                reward_amount=500, payment_status='secured',
            )
            slug = bounty.url_slug
        body = _sitemap(client)
        assert f'https://paid2match.work/needs/{slug}' in body

    def test_lists_open_free_need(self, client, app):
        with app.app_context():
            user = _make_user('free@example.com', 'Free Poster')
            bounty = _make_bounty(
                user.id, 'Community help wanted in Laval',
                reward_amount=None, is_free=True,
            )
            slug = bounty.url_slug
        body = _sitemap(client)
        assert f'https://paid2match.work/needs/{slug}' in body

    def test_excludes_expired_need(self, client, app):
        with app.app_context():
            user = _make_user('old@example.com', 'Old Poster')
            bounty = _make_bounty(
                user.id, 'Expired need nobody should see',
                reward_amount=500, payment_status='secured',
                deadline=datetime.now(timezone.utc) - timedelta(days=1),
            )
            slug = bounty.url_slug
        body = _sitemap(client)
        assert f'/needs/{slug}' not in body

    def test_excludes_unsecured_paid_need(self, client, app):
        with app.app_context():
            user = _make_user('unsec@example.com', 'Unsecured Poster')
            bounty = _make_bounty(
                user.id, 'Unsecured paid need in Laval',
                reward_amount=500, payment_status='unsecured',
            )
            slug = bounty.url_slug
        body = _sitemap(client)
        assert f'/needs/{slug}' not in body


class TestSitemapProfiles:
    def test_lists_public_handle_profile(self, client, app):
        with app.app_context():
            user = _make_user('handle@example.com', 'Handle Person')
        body = _sitemap(client)
        assert f'https://paid2match.work/profile/{user.handle}' in body

    def test_excludes_private_profile(self, client, app):
        with app.app_context():
            user = _make_user(
                'private@example.com', 'Private Person',
                privacy_level=2,
            )
        body = _sitemap(client)
        assert f'/profile/{user.handle}' not in body

    def test_excludes_user_without_profile(self, client, app):
        with app.app_context():
            user = _make_user(
                'noprofile@example.com', 'No Profile',
                with_profile=False,
            )
        body = _sitemap(client)
        assert f'/profile/{user.handle}' not in body


class TestSitemapPosts:
    def test_lists_public_post(self, client, app):
        with app.app_context():
            user = _make_user('author@example.com', 'Author Person')
            post = _make_post(user.id)
            post_id = post.id
        body = _sitemap(client)
        assert f'https://paid2match.work/social/posts/{post_id}' in body

    def test_excludes_hidden_post(self, client, app):
        with app.app_context():
            user = _make_user('hidden@example.com', 'Hidden Author')
            post = _make_post(user.id, is_hidden=True)
            post_id = post.id
        body = _sitemap(client)
        assert f'/social/posts/{post_id}' not in body

    def test_excludes_post_linked_to_unsecured_bounty(self, client, app):
        with app.app_context():
            user = _make_user('linked@example.com', 'Linked Author')
            bounty = _make_bounty(
                user.id, 'Unsecured paid need for a post',
                reward_amount=500, payment_status='unsecured',
            )
            post = _make_post(user.id, bounty_id=bounty.id)
            post_id = post.id
        body = _sitemap(client)
        assert f'/social/posts/{post_id}' not in body

    def test_lists_post_linked_to_secured_bounty(self, client, app):
        with app.app_context():
            user = _make_user('seclink@example.com', 'Secured Author')
            bounty = _make_bounty(
                user.id, 'Secured need for a post',
                reward_amount=500, payment_status='secured',
            )
            post = _make_post(user.id, bounty_id=bounty.id)
            post_id = post.id
        body = _sitemap(client)
        assert f'https://paid2match.work/social/posts/{post_id}' in body
