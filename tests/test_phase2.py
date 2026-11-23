"""Phase 2 design modernization tests.

Covers the PWA (manifest, service worker, offline page), the FR/ES
catalog recovery (compiled catalogs, translated homepage, switcher
return-to-page, <html lang>), topic spaces, flair filters and the
user-chosen feed picker, polls, @mentions, the onboarding checklist,
and bookmark/save (the Phase 1 deferral this phase closes).
"""
import gettext
import json
import os
from datetime import datetime, timedelta, timezone

from extensions import db
from models.notification import Notification
from models.poll import Poll, PollVote, create_poll
from models.post import Bookmark, Post
from models.space import Space, SpaceMembership, seed_spaces

JSON_HEADERS = {
    'X-Requested-With': 'XMLHttpRequest',
    'Accept': 'application/json',
}


def _make_post(app, author_id, body, post_type='update', space_id=None,
               created_at=None):
    with app.app_context():
        post = Post(
            author_id=author_id, post_type=post_type, body=body,
            space_id=space_id,
        )
        if created_at is not None:
            post.created_at = created_at
        db.session.add(post)
        db.session.commit()
        db.session.refresh(post)
        return post


def _clear_babel_locale_cache():
    """Drop the per-request locale caches after switching language
    mid-test. Flask-Babel caches the resolved locale on flask.g (and
    the app's own selector caches on g._p2m_locale); g lives on the
    app context, and the test fixture holds one app context open for
    the whole test — so without this reset the first request's locale
    would stick for every later request in the test. In production
    each request gets a fresh g, so this is test-only."""
    from flask import g
    g.pop('_flask_babel', None)
    g.pop('_p2m_locale', None)


# --------------------------------------------------------------------- PWA


class TestPWA:
    def test_manifest_valid_json_with_required_keys(self, client):
        r = client.get('/manifest.json')
        assert r.status_code == 200
        assert r.content_type.startswith('application/manifest+json')
        manifest = json.loads(r.data)
        assert manifest['name'] == 'Paid2Match'
        assert manifest['short_name'] == 'Paid2Match'
        assert manifest['start_url'] == '/'
        assert manifest['scope'] == '/'
        assert manifest['display'] == 'standalone'
        assert manifest['theme_color'] == '#D9481C'
        assert manifest['background_color'] == '#FAF7F4'
        assert manifest['description'] == (
            'The referral network for hiring. '
            'Real people vouching for real people.'
        )
        assert 'proudly' not in manifest['description'].lower()
        sizes = {icon['sizes'] for icon in manifest['icons']}
        assert '192x192' in sizes
        assert '512x512' in sizes
        assert any(
            'maskable' in icon.get('purpose', '')
            for icon in manifest['icons']
        )

    def test_manifest_icons_exist_on_disk(self, app):
        static = os.path.join(app.root_path, 'static')
        for filename in (
            'img/icon-192.png', 'img/icon-512.png',
            'img/icon-maskable-512.png', 'img/apple-touch-icon-180.png',
        ):
            path = os.path.join(static, filename)
            assert os.path.exists(path), filename
            assert os.path.getsize(path) > 1000, filename

    def test_service_worker_served_from_root(self, client):
        r = client.get('/sw.js')
        assert r.status_code == 200
        assert r.content_type.startswith('application/javascript')
        body = r.data.decode()
        assert 'p2m-v2' in body
        assert '/offline' in body
        # Auth/API/POSTs are network-only, never cached.
        assert '/auth/' in body
        assert '/api/' in body

    def test_offline_page_renders(self, client):
        r = client.get('/offline')
        assert r.status_code == 200
        assert 'You are offline' in r.data.decode()

    def test_base_links_manifest_and_theme_color(self, client):
        html = client.get('/').data.decode()
        assert 'manifest.json' in html
        assert 'name="theme-color"' in html
        assert '#D9481C' in html
        assert 'apple-touch-icon' in html
        assert 'apple-mobile-web-app-capable' in html
        assert 'mobile-web-app-capable' in html


# -------------------------------------------------------------------- i18n


class TestI18n:
    def _catalog(self, lang):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'translations', lang, 'LC_MESSAGES', 'messages.mo')
        assert os.path.getsize(path) > 10000, f'{lang} .mo too small'
        with open(path, 'rb') as f:
            return gettext.GNUTranslations(f)

    def test_fr_and_es_catalogs_compile_and_translate(self):
        fr = self._catalog('fr')
        assert fr.gettext('Join the network') == 'Rejoindre le réseau'
        assert fr.gettext('Feed') == 'Fil'
        assert fr.gettext('People') == 'Personnes'
        assert fr.gettext('Save') == 'Enregistrer'
        es = self._catalog('es')
        assert es.gettext('Join the network') == 'Unirse a la red'
        assert es.gettext('People') == 'Personas'
        assert es.gettext('Save') == 'Guardar'
        # Plurals survive compilation.
        assert fr.ngettext('%(num)s comment', '%(num)s comments', 2) == \
            '%(num)s commentaires'

    def test_homepage_differs_in_french(self, client):
        en = client.get('/').data
        assert b'<html lang="en">' in en
        assert b'Join the network' in en
        client.get('/set-language/fr')
        _clear_babel_locale_cache()
        fr = client.get('/').data
        assert fr != en
        assert b'<html lang="fr">' in fr
        assert 'Rejoindre le réseau'.encode() in fr
        assert b'Join the network' not in fr

    def test_homepage_in_spanish(self, client):
        client.get('/set-language/es')
        _clear_babel_locale_cache()
        es = client.get('/').data
        assert b'<html lang="es">' in es
        assert 'Unirse a la red'.encode() in es

    def test_switcher_returns_to_current_page(self, client):
        r = client.get('/set-language/fr?next=/social/spaces')
        assert r.status_code == 302
        assert r.headers['Location'] == '/social/spaces'

    def test_switcher_rejects_open_redirects(self, client):
        r = client.get(
            '/set-language/fr?next=https://evil.example.com/x')
        assert r.status_code == 302
        assert r.headers['Location'] == '/'

    def test_switcher_persists_to_profile_when_logged_in(
            self, app, client, logged_in_user, sample_user):
        client.get('/set-language/fr')
        with app.app_context():
            from models.profile import Profile
            profile = Profile.query.filter_by(
                user_id=sample_user.id).first()
            assert profile.language == 'fr'

    def test_switcher_shows_active_language(
            self, client, logged_in_user):
        client.get('/set-language/fr')
        _clear_babel_locale_cache()
        html = client.get('/').data.decode()
        # The active language carries the check icon + active class.
        assert 'Français' in html
        assert 'dropdown-item active" href="/set-language/fr"' in html

    def test_no_literal_jinja_in_comment_placeholder(
            self, client, logged_in_user, sample_post):
        """Regression: placeholders inside {{ form.field(...) }} calls
        were once written placeholder="{{ _('...') }}", which renders
        the Jinja source literally. They must be _('...') directly."""
        client.get('/set-language/fr')
        _clear_babel_locale_cache()
        html = client.get(f'/social/posts/{sample_post.id}').data.decode()
        assert "{{ _('Write a comment...') }}" not in html
        assert 'Écrire un commentaire…' in html

    def test_pledge_card_heading_translates(
            self, client, logged_in_user):
        client.get('/set-language/fr')
        _clear_babel_locale_cache()
        html = client.get('/social/feed').data.decode()
        assert 'Embauche révisée par des humains' in html
        assert 'Human-reviewed hiring</h5>' not in html

    def test_trust_level_label_translates(
            self, app, client, logged_in_user, sample_user, second_user):
        """Trust levels are logic keys in Python; only the rendered
        label translates, via the level_label filter."""
        _make_post(app, second_user.id, 'Post whose author level shows.')
        client.get('/set-language/fr')
        _clear_babel_locale_cache()
        html = client.get('/social/feed?tab=latest').data.decode()
        assert 'Nouveau venu' in html
        # The logic key itself is untouched.
        from utils.trust import LEVELS, level_label
        assert LEVELS == ['Newcomer', 'Member', 'Trusted', 'Ambassador']
        assert level_label('Nope') == 'Nope'


# ------------------------------------------------------------------ spaces


class TestSpaces:
    def test_seed_spaces_cli_creates_three_and_is_idempotent(
            self, app, runner):
        result = runner.invoke(args=['seed-spaces'])
        assert result.exit_code == 0
        assert 'Seeded 3 space(s)' in result.output
        with app.app_context():
            slugs = sorted(s.slug for s in Space.query.all())
            assert slugs == ['montreal', 'tech', 'trades']
        result = runner.invoke(args=['seed-spaces'])
        assert 'already seeded' in result.output
        with app.app_context():
            assert Space.query.count() == 3

    def test_spaces_directory_lists_seeded_spaces(
            self, app, client, logged_in_user):
        with app.app_context():
            seed_spaces()
            db.session.commit()
        html = client.get('/social/spaces').data.decode()
        assert 'Tech &amp; Software' in html or 'Tech & Software' in html
        assert 'Skilled Trades' in html
        assert 'Montréal &amp; Laval' in html or 'Montréal & Laval' in html

    def test_per_space_feed_filters(
            self, app, client, logged_in_user, sample_user):
        with app.app_context():
            seed_spaces()
            db.session.commit()
            tech = Space.query.filter_by(slug='tech').first()
            trades = Space.query.filter_by(slug='trades').first()
        _make_post(app, sample_user.id, 'Tech space post here.',
                   space_id=tech.id)
        tech_html = client.get('/social/spaces/tech').data.decode()
        assert 'Tech space post here.' in tech_html
        trades_html = client.get('/social/spaces/trades').data.decode()
        assert 'Tech space post here.' not in trades_html
        # Space posts still appear in the main feed.
        feed_html = client.get('/social/feed?tab=latest').data.decode()
        assert 'Tech space post here.' in feed_html

    def test_unknown_space_404s(self, client):
        assert client.get('/social/spaces/nope').status_code == 404

    def test_join_leave_and_notification_level(
            self, app, client, logged_in_user, sample_user):
        with app.app_context():
            seed_spaces()
            db.session.commit()
            tech = Space.query.filter_by(slug='tech').first()
            tech_id = tech.id
        r = client.post('/social/spaces/tech/join')
        assert r.status_code == 302
        with app.app_context():
            m = SpaceMembership.query.filter_by(
                user_id=sample_user.id, space_id=tech_id).first()
            assert m is not None
            assert m.notification_level == 'all'
        # Joining again is idempotent.
        client.post('/social/spaces/tech/join')
        with app.app_context():
            assert SpaceMembership.query.filter_by(
                user_id=sample_user.id, space_id=tech_id).count() == 1
        # Muting persists.
        client.post('/social/spaces/tech/notifications',
                    data={'level': 'muted'})
        with app.app_context():
            m = SpaceMembership.query.filter_by(
                user_id=sample_user.id, space_id=tech_id).first()
            assert m.notification_level == 'muted'
        # Leaving removes the membership.
        client.post('/social/spaces/tech/leave')
        with app.app_context():
            assert SpaceMembership.query.filter_by(
                user_id=sample_user.id, space_id=tech_id).first() is None

    def test_setting_level_implies_joining(
            self, app, client, logged_in_user, sample_user):
        with app.app_context():
            seed_spaces()
            db.session.commit()
            tech = Space.query.filter_by(slug='tech').first()
            tech_id = tech.id
        client.post('/social/spaces/tech/notifications',
                    data={'level': 'mentions'})
        with app.app_context():
            m = SpaceMembership.query.filter_by(
                user_id=sample_user.id, space_id=tech_id).first()
            assert m is not None
            assert m.notification_level == 'mentions'

    def test_invalid_notification_level_rejected(
            self, app, client, logged_in_user, sample_user):
        with app.app_context():
            seed_spaces()
            db.session.commit()
            tech = Space.query.filter_by(slug='tech').first()
            tech_id = tech.id
        client.post('/social/spaces/tech/notifications',
                    data={'level': 'everything'})
        with app.app_context():
            assert SpaceMembership.query.filter_by(
                user_id=sample_user.id, space_id=tech_id).first() is None


# ------------------------------------------- flair + user-chosen feeds


class TestFlairAndFeeds:
    def test_feed_picker_tabs_render(self, client, logged_in_user):
        html = client.get('/social/feed').data.decode()
        assert 'tab=following' in html
        assert 'tab=latest' in html
        assert 'tab=discover' in html
        assert 'flair-chip' in html

    def test_following_vs_latest_differ(
            self, app, client, logged_in_user, sample_user, second_user,
            third_user):
        client.post(f'/social/follow/{second_user.id}')
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _make_post(app, second_user.id, 'Followed author old post.',
                   created_at=now - timedelta(hours=2))
        _make_post(app, third_user.id, 'Unfollowed author new post.',
                   created_at=now - timedelta(minutes=5))
        following = client.get('/social/feed?tab=following').data.decode()
        assert 'Followed author old post.' in following
        assert 'Unfollowed author new post.' not in following
        latest = client.get('/social/feed?tab=latest').data.decode()
        assert 'Followed author old post.' in latest
        assert 'Unfollowed author new post.' in latest
        # Latest is chronological: the newer post comes first.
        assert latest.index('Unfollowed author new post.') < \
            latest.index('Followed author old post.')

    def test_top_tab_excludes_own_posts(
            self, app, client, logged_in_user, sample_user, second_user):
        _make_post(app, sample_user.id, 'My own post for Top check.')
        _make_post(app, second_user.id, 'Someone elses post for Top.')
        top = client.get('/social/feed?tab=discover').data.decode()
        assert 'Someone elses post for Top.' in top
        assert 'My own post for Top check.' not in top

    def test_flair_filter(
            self, app, client, logged_in_user, sample_user, second_user):
        client.post(f'/social/follow/{second_user.id}')
        _make_post(app, second_user.id, 'A question post for flair.',
                   post_type='ask')
        _make_post(app, second_user.id, 'A discussion post for flair.',
                   post_type='update')
        ask = client.get(
            '/social/feed?tab=latest&flair=ask').data.decode()
        assert 'A question post for flair.' in ask
        assert 'A discussion post for flair.' not in ask
        discussion = client.get(
            '/social/feed?tab=latest&flair=discussion').data.decode()
        assert 'A discussion post for flair.' in discussion
        assert 'A question post for flair.' not in discussion
        # An unknown flair value is ignored (both posts show).
        both = client.get(
            '/social/feed?tab=latest&flair=bogus').data.decode()
        assert 'A question post for flair.' in both
        assert 'A discussion post for flair.' in both


# ------------------------------------------------------- Warm Rainbow
# (2026-11-23): colour with a job — flair chips, avatar tones, space
# accents — plus the traced handshake logo mark (sprite #p2m-mark).


class TestWarmRainbow:
    def test_avatar_tone_filter_deterministic_and_bounded(self, app):
        tone = app.jinja_env.filters['avatar_tone']
        assert tone('some-user-id') == tone('some-user-id')
        assert tone(None) in range(8)
        assert tone('') in range(8)
        tones = {tone(f'user-{i}') for i in range(20)}
        assert tones <= set(range(8))
        # md5 mod 8 spreads: 20 ids must not collapse to one tone.
        assert len(tones) >= 3

    def test_feed_flair_chips_carry_flair_classes(
            self, client, logged_in_user):
        html = client.get('/social/feed').data.decode()
        for flair in ('discussion', 'win', 'ask', 'need', 'available',
                      'intro'):
            assert f'flair-chip flair-{flair}' in html, flair

    def test_post_card_type_chip_carries_flair_class(
            self, app, client, logged_in_user, sample_user, second_user):
        client.post(f'/social/follow/{second_user.id}')
        _make_post(app, second_user.id, 'A question post for chip.',
                   post_type='ask')
        _make_post(app, second_user.id, 'A win post for chip.',
                   post_type='win')
        html = client.get('/social/feed?tab=latest').data.decode()
        assert 'post-type-chip flair-ask' in html
        assert 'post-type-chip flair-win' in html
        # The old per-type classes are gone (replaced by flair-*).
        assert 'chip-need' not in html
        assert 'chip-win' not in html
        assert 'chip-ask' not in html

    def test_space_icons_carry_slug_classes(
            self, app, client, logged_in_user):
        with app.app_context():
            seed_spaces()
            db.session.commit()
        html = client.get('/social/spaces').data.decode()
        assert 'space-icon space-tech' in html
        assert 'space-icon space-trades' in html
        assert 'space-icon space-montreal' in html

    def test_avatar_placeholder_carries_tone_class(
            self, client, logged_in_user):
        html = client.get('/social/feed').data.decode()
        assert 'avatar-tone-' in html

    def test_logo_mark_uses_traced_sprite(self, client):
        html = client.get('/').data.decode()
        assert '#p2m-mark' in html
        assert 'brand-mark' in html
        # The hand-drawn blob (mask-slits) is retired.
        assert 'p2m-hand-gaps' not in html


# ------------------------------------------------------------------- polls


class TestPolls:
    def _poll_post(self, app, sample_user):
        post = _make_post(app, sample_user.id, 'Poll post: pick one.')
        with app.app_context():
            poll = create_poll(post, ['Yes', 'No', 'Maybe'])
            db.session.commit()
            db.session.refresh(poll)
            return post, poll

    def test_vote_once_and_results(
            self, app, client, login_as, second_user, sample_user):
        post, poll = self._poll_post(app, sample_user)
        with app.app_context():
            from models.poll import PollOption
            option_ids = [o.id for o in PollOption.query.filter_by(
                poll_id=poll.id).order_by(PollOption.position).all()]
        login_as(client, 'second@example.com', 'SecondPass123!')
        r = client.post(
            f'/social/polls/{poll.id}/vote',
            data={'option_id': option_ids[0]}, headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json['ok'] is True
        assert r.json['already_voted'] is False
        assert r.json['total_votes'] == 1
        assert r.json['voted_option_id'] == option_ids[0]
        results = {o['text']: o for o in r.json['options']}
        assert results['Yes']['votes'] == 1
        assert results['Yes']['percent'] == 100
        assert results['No']['votes'] == 0
        # A second vote is idempotent — same results, no extra vote.
        r = client.post(
            f'/social/polls/{poll.id}/vote',
            data={'option_id': option_ids[1]}, headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json['already_voted'] is True
        assert r.json['total_votes'] == 1
        assert r.json['voted_option_id'] == option_ids[0]
        with app.app_context():
            assert PollVote.query.filter_by(
                poll_id=poll.id, user_id=second_user.id).count() == 1

    def test_vote_invalid_option_is_400(
            self, app, client, login_as, second_user, sample_user):
        post, poll = self._poll_post(app, sample_user)
        login_as(client, 'second@example.com', 'SecondPass123!')
        r = client.post(
            f'/social/polls/{poll.id}/vote',
            data={'option_id': 'not-a-real-option'}, headers=JSON_HEADERS)
        assert r.status_code == 400
        assert r.json['ok'] is False

    def test_vote_requires_auth(
            self, app, client, sample_user):
        post, poll = self._poll_post(app, sample_user)
        r = client.post(
            f'/social/polls/{poll.id}/vote',
            data={'option_id': 'whatever'}, headers=JSON_HEADERS)
        assert r.status_code == 401

    def test_poll_renders_on_post_detail(
            self, app, client, logged_in_user, sample_user):
        post, poll = self._poll_post(app, sample_user)
        html = client.get(f'/social/posts/{post.id}').data.decode()
        assert 'Yes' in html
        assert 'No' in html
        assert 'Maybe' in html

    def test_composer_creates_poll(
            self, app, client, logged_in_user):
        r = client.post(
            '/social/posts/new',
            data={
                'post_type': 'update',
                'body': 'Composer poll post.',
                'poll_options': 'Alpha\nBeta',
            },
            headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json['ok'] is True
        with app.app_context():
            post = db.session.get(Post, r.json['post_id'])
            assert post.poll is not None
            assert sorted(o.text for o in post.poll.options) == [
                'Alpha', 'Beta']


# ---------------------------------------------------------------- mentions


class TestMentions:
    def test_comment_mention_notifies(
            self, app, client, login_as, second_user, third_user,
            sample_post):
        login_as(client, 'third@example.com', 'ThirdPass123!')
        r = client.post(
            f'/social/posts/{sample_post.id}/comment',
            data={'body': '@Second what do you think?'},
            headers=JSON_HEADERS)
        assert r.status_code == 200
        with app.app_context():
            n = Notification.query.filter_by(
                recipient_id=second_user.id, type='mention').first()
            assert n is not None
            assert 'mentioned you' in n.title

    def test_post_mention_notifies(
            self, app, client, logged_in_user, second_user):
        r = client.post(
            '/social/posts/new',
            data={'post_type': 'update',
                  'body': 'Shout-out to @Second for the help!'},
            headers=JSON_HEADERS)
        assert r.status_code == 200
        with app.app_context():
            n = Notification.query.filter_by(
                recipient_id=second_user.id, type='mention').first()
            assert n is not None

    def test_link_mentions_filter_links_and_escapes(self, app, second_user):
        from flask import render_template_string
        from utils.mentions import link_mentions
        with app.app_context():
            with app.test_request_context():
                out = render_template_string(
                    "{{ t|mention_links }}",
                    t='Hi @Second and @Nobody <b>bold</b>')
                assert f'/profile/{second_user.id}' in out
                assert '@Second' in out
                assert 'mention-link' in out
                assert '@Nobody' in out  # unmatched stays plain text
                assert '<b>bold</b>' not in out  # escaped
                assert '&lt;b&gt;' in out
                assert link_mentions('') == ''


# ------------------------------------------------------- bookmark / saved


class TestBookmark:
    def test_ajax_save_toggle(self, client, login_as, second_user,
                              sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        url = f'/social/posts/{sample_post.id}/save'
        r = client.post(url, headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json == {'saved': True}
        r = client.post(url, headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json == {'saved': False}

    def test_save_requires_auth(self, client, sample_post):
        r = client.post(
            f'/social/posts/{sample_post.id}/save', headers=JSON_HEADERS)
        assert r.status_code == 401

    def test_saved_view_lists_saved_posts(
            self, app, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        html = client.get('/social/saved').data.decode()
        assert 'Nothing saved yet' in html
        client.post(f'/social/posts/{sample_post.id}/save',
                    headers=JSON_HEADERS)
        html = client.get('/social/saved').data.decode()
        assert 'Hello network' in html
        assert 'Nothing saved yet' not in html
        client.post(f'/social/posts/{sample_post.id}/save',
                    headers=JSON_HEADERS)
        html = client.get('/social/saved').data.decode()
        assert 'Nothing saved yet' in html

    def test_save_button_on_post_card(
            self, app, client, logged_in_user, sample_user, second_user):
        _make_post(app, second_user.id, 'Post with a save button.')
        html = client.get('/social/feed?tab=latest').data.decode()
        assert 'post-save-btn' in html
        assert 'data-save-url' in html


# -------------------------------------------------------------- onboarding


class TestOnboarding:
    def test_checklist_shows_for_new_user(self, client, logged_in_user):
        html = client.get('/social/feed').data.decode()
        assert 'onboarding-card' in html
        assert 'Three quick steps to get started.' in html
        assert 'Complete your profile' in html
        assert 'Say hello with #intros' in html
        assert 'Follow someone' in html

    def test_dismiss_hides_checklist_for_the_session(
            self, client, logged_in_user):
        r = client.post('/social/onboarding/dismiss',
                        headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json == {'ok': True}
        html = client.get('/social/feed').data.decode()
        assert 'onboarding-card' not in html

    def test_completed_steps_mark_done_and_hide_card(
            self, app, client, logged_in_user, sample_user, second_user):
        from models.profile import Profile
        with app.app_context():
            profile = Profile.query.filter_by(
                user_id=sample_user.id).first()
            profile.headline = 'A headline'
            profile.bio = 'A bio'
            profile.location = 'Montreal'
            profile.avatar_url = '/static/img/logo-mark.svg'
            profile.skills = 'Python'
            profile.website = 'https://example.com'
            profile.linkedin_url = 'https://linkedin.com/in/x'
            profile.github_url = 'https://github.com/x'
            profile.twitter_handle = '@x'
            db.session.commit()
            assert profile.completion_percentage == 100
        _make_post(app, sample_user.id, 'Hello! #intros', post_type='intro')
        client.post(f'/social/follow/{second_user.id}')
        html = client.get('/social/feed').data.decode()
        assert 'onboarding-card' not in html

    def test_onboarding_steps_report_progress(
            self, app, client, logged_in_user, sample_user, second_user):
        from utils.onboarding import get_onboarding_steps
        with app.test_request_context():
            steps = get_onboarding_steps(sample_user)
            assert [s['key'] for s in steps] == [
                'profile', 'intro', 'follow']
            assert all(not s['done'] for s in steps)
        _make_post(app, sample_user.id, 'Hello! #intros', post_type='intro')
        client.post(f'/social/follow/{second_user.id}')
        with app.test_request_context():
            steps = {s['key']: s for s in get_onboarding_steps(sample_user)}
            assert steps['intro']['done'] is True
            assert steps['follow']['done'] is True
            assert steps['profile']['done'] is False
