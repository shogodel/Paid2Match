"""Nice URLs: /needs/<slug>-<shortcode> and /profile/<handle>.

Needs moved from /bounties/<uuid> to /needs/<slug>-<shortcode>;
profiles moved from /profile/<uuid> to /profile/<handle>. Old
/bounties/* URLs 301 to their /needs/* equivalents.
"""
import os

import pytest
from sqlalchemy import text

from extensions import db
from models.bounty import Bounty
from models.user import User
from utils.slugs import (
    handle_from_name,
    parse_slug_code,
    slugify,
)


def _make_user(app, email, full_name):
    """A user with no profile (so /profile/setup is reachable)."""
    from werkzeug.security import generate_password_hash
    with app.app_context():
        user = User(
            email=email,
            full_name=full_name,
            password_hash=generate_password_hash('TestPass123!'),
            email_verified=True,
        )
        db.session.add(user)
        db.session.commit()
        db.session.refresh(user)
        return user


def _setup_data(**overrides):
    data = {
        'profile_type': 'worker',
        'display_name': 'Test Profile',
    }
    data.update(overrides)
    return data


# ------------------------------------------------------------------ slugify


class TestSlugify:
    def test_accents_transliterated(self):
        assert slugify('Électricien à Montréal') == 'electricien-a-montreal'
        assert slugify('Niño Señor') == 'nino-senor'
        assert slugify('Ünïcodé Täst') == 'unicode-tast'

    def test_empty_and_blank_titles_become_need(self):
        assert slugify('') == 'need'
        assert slugify('   ') == 'need'
        assert slugify(None) == 'need'
        assert slugify('!!!') == 'need'

    def test_separators_collapse(self):
        assert slugify('Senior   Python---Engineer!!') == 'senior-python-engineer'
        assert slugify('  Trim Me  ') == 'trim-me'

    def test_long_titles_trimmed_to_about_80_chars(self):
        slug = slugify('Word ' * 40)
        assert len(slug) <= 80
        assert not slug.endswith('-')
        assert slug == 'word-' * 15 + 'word'

    def test_parse_slug_code(self):
        assert parse_slug_code('electricien-a-montreal-x1y2z3') == (
            'electricien-a-montreal', 'x1y2z3')
        assert parse_slug_code('no-code-here') == (None, None)
        assert parse_slug_code('abc-12345') == (None, None)
        assert parse_slug_code('plain') == (None, None)


# ------------------------------------------------- model-level generation


class TestModelGeneration:
    def test_slug_and_shortcode_generated_on_insert(self, app, sample_bounty):
        assert sample_bounty.slug == 'senior-python-engineer-needed-for-startup'
        assert len(sample_bounty.shortcode) == 6
        assert sample_bounty.url_slug == (
            f'{sample_bounty.slug}-{sample_bounty.shortcode}'
        )

    def test_same_title_collisions_get_distinct_shortcodes(
        self, app, sample_user
    ):
        with app.app_context():
            bounties = [
                Bounty(
                    poster_id=sample_user.id,
                    title='Same Title Here',
                    description='x' * 60,
                    bounty_type='recruitment',
                    bounty_direction='seeking_opportunity',
                    status='open',
                    payment_status='released',
                )
                for _ in range(3)
            ]
            db.session.add_all(bounties)
            db.session.commit()
            slugs = {b.slug for b in bounties}
            codes = {b.shortcode for b in bounties}
            assert slugs == {'same-title-here'}
            assert len(codes) == 3

    def test_slug_stable_when_title_edited(self, app, sample_bounty):
        with app.app_context():
            bounty = db.session.get(Bounty, sample_bounty.id)
            old_slug = bounty.slug
            bounty.title = 'A Completely Different Title'
            db.session.commit()
            assert bounty.slug == old_slug

    def test_handle_generated_from_full_name(self, app, sample_user):
        assert sample_user.handle == 'test.user'

    def test_same_name_collisions_get_numbered_handles(self, app):
        users = [
            _make_user(app, f'dup{i}@example.com', 'Dup Name')
            for i in range(3)
        ]
        assert [u.handle for u in users] == [
            'dup.name', 'dup.name2', 'dup.name3',
        ]

    def test_handle_from_name_edge_cases(self):
        assert handle_from_name('Élise Côté') == 'elise.cote'
        assert handle_from_name('Jo') == 'jox'
        assert handle_from_name('') == 'user'
        assert handle_from_name('!!!') == 'user'
        assert len(handle_from_name('A' * 50)) == 30


# --------------------------------------------------------- handle validation


class TestHandleValidation:
    @pytest.mark.parametrize('bad', [
        'Test.User',      # uppercase
        'BAD',            # uppercase
        'ab',             # too short
        'a' * 31,         # too long
        'bad handle',     # space
        'bad/handle',     # slash
        'bad@handle',     # @
        'électricien',    # non-ASCII
    ])
    def test_invalid_handles_rejected_by_model(self, app, bad):
        # The app fixture imports every model (via the blueprints)
        # so the mappers configure even when this class runs alone.
        with pytest.raises(ValueError):
            User(email='x@example.com', full_name='X Y', handle=bad)

    @pytest.mark.parametrize('good', [
        'test.user', 'a-b_c.d9', 'abc', 'a' * 30, 'remus.b',
    ])
    def test_valid_handles_accepted_by_model(self, app, good):
        user = User(email='x@example.com', full_name='X Y', handle=good)
        assert user.handle == good

    @pytest.mark.parametrize('reserved', [
        # /profile/* static routes — a handle here would be
        # unreachable (the static segment wins the match).
        'setup', 'edit', 'linked', 'verify-request', 'users',
        # Top-level route prefixes and app words.
        'admin', 'api', 'static', 'auth', 'needs', 'wins', 'pledge',
        'intros', 'login', 'support', 'paid2match',
    ])
    def test_reserved_handles_rejected_by_model(self, app, reserved):
        with pytest.raises(ValueError, match='reserved'):
            User(email='x@example.com', full_name='X Y',
                 handle=reserved)

    @pytest.mark.parametrize('name,expected', [
        ('Admin', 'admin2'),
        ('Setup', 'setup2'),
        ('Needs', 'needs2'),
    ])
    def test_handle_generation_skips_reserved(self, name, expected):
        assert handle_from_name(name) == expected

    def test_auto_generated_handle_skips_reserved(self, app):
        """A user actually named 'Admin' gets 'admin2' at insert —
        never the reserved 'admin'."""
        user = _make_user(app, 'admin-named@example.com', 'Admin')
        assert user.handle == 'admin2'

    def test_duplicate_handle_rejected_by_database(
        self, app, sample_user
    ):
        from sqlalchemy.exc import IntegrityError
        with app.app_context():
            db.session.add(User(
                email='copy@example.com',
                full_name='Copy Cat',
                handle=sample_user.handle,
                email_verified=True,
            ))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()


# ------------------------------------------------------------ detail routes


class TestNeedDetailUrls:
    def test_canonical_url_loads(self, client, sample_bounty):
        r = client.get(f'/needs/{sample_bounty.url_slug}')
        assert r.status_code == 200
        assert b'Senior Python Engineer' in r.data

    def test_wrong_slug_redirects_to_canonical(self, client, sample_bounty):
        r = client.get(
            f'/needs/wrong-slug-{sample_bounty.shortcode}',
            follow_redirects=False,
        )
        assert r.status_code == 301
        assert r.location.endswith(f'/needs/{sample_bounty.url_slug}')

    def test_bare_uuid_redirects_to_canonical(self, client, sample_bounty):
        r = client.get(f'/needs/{sample_bounty.id}', follow_redirects=False)
        assert r.status_code == 301
        assert r.location.endswith(f'/needs/{sample_bounty.url_slug}')

    def test_unknown_shortcode_404s(self, client):
        assert client.get('/needs/nothing-here-abcdef').status_code == 404
        assert client.get('/needs/not-a-slug-code').status_code == 404

    def test_board_and_sub_routes_live_under_needs(
        self, client, logged_in_user, sample_bounty
    ):
        assert client.get('/needs/').status_code == 200
        assert client.get('/needs/recruitment').status_code == 200
        assert client.get('/needs/services').status_code == 200
        assert client.get('/needs/api').status_code == 200
        r = logged_in_user.get(f'/needs/{sample_bounty.id}/edit')
        assert r.status_code == 200


# ------------------------------------------------------- old-URL redirects


class TestLegacyBountiesRedirects:
    def test_board_redirects(self, client):
        r = client.get('/bounties/', follow_redirects=False)
        assert r.status_code == 301
        assert r.location.endswith('/needs/')

    def test_detail_redirects_to_slug_url(self, client, sample_bounty):
        r = client.get(f'/bounties/{sample_bounty.id}', follow_redirects=False)
        assert r.status_code == 301
        assert r.location.endswith(f'/needs/{sample_bounty.url_slug}')

    def test_edit_redirects(self, client, sample_bounty):
        r = client.get(
            f'/bounties/{sample_bounty.id}/edit', follow_redirects=False
        )
        assert r.status_code == 301
        assert r.location.endswith(f'/needs/{sample_bounty.id}/edit')

    def test_simple_routes_redirect(self, client):
        for old, new in [
            ('post', 'post'),
            ('recruitment', 'recruitment'),
            ('services', 'services'),
            ('api', 'api'),
            ('check-expired', 'check-expired'),
            ('my-invitations', 'my-invitations'),
        ]:
            r = client.get(f'/bounties/{old}', follow_redirects=False)
            assert r.status_code == 301, old
            assert r.location.endswith(f'/needs/{new}'), old

    def test_pitch_and_match_routes_redirect(
        self, client, sample_bounty, sample_pitch
    ):
        r = client.post(
            f'/bounties/{sample_bounty.id}/pitch', follow_redirects=False
        )
        assert r.status_code == 301
        assert r.location.endswith(f'/needs/{sample_bounty.id}/pitch')
        r = client.post(
            f'/bounties/pitches/{sample_pitch.id}/respond',
            follow_redirects=False,
        )
        assert r.status_code == 301
        assert r.location.endswith(
            f'/needs/pitches/{sample_pitch.id}/respond'
        )

    def test_unknown_paths_fall_back_to_board(self, client):
        for path in [
            '/bounties/does-not-exist',
            '/bounties/whatever/nested/deep',
            '/bounties/00000000-0000-0000-0000-000000000000',
            '/bounties/00000000-0000-0000-0000-000000000000/edit',
        ]:
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 301, path
            assert r.location.endswith('/needs/'), path

    def test_query_string_survives_redirect(self, client):
        r = client.get('/bounties/?free=1', follow_redirects=False)
        assert r.status_code == 301
        assert r.location.endswith('/needs/?free=1')

    def test_stripe_webhook_stays_live_at_old_url(self, client):
        # NOT a redirect: Stripe POSTs must reach the handler. With
        # payments disabled the handler answers 400 — the point is
        # it is not a 301.
        r = client.post('/bounties/stripe-webhook', data=b'{}')
        assert r.status_code == 400
        r = client.post('/needs/stripe-webhook', data=b'{}')
        assert r.status_code == 400


# ------------------------------------------------------------ profile URLs


class TestProfileHandleUrls:
    def test_handle_url_loads(self, client, sample_user):
        r = client.get(f'/profile/{sample_user.handle}')
        assert r.status_code == 200
        assert b'Test User' in r.data

    def test_uuid_url_redirects_to_handle(self, client, sample_user):
        r = client.get(f'/profile/{sample_user.id}', follow_redirects=False)
        assert r.status_code == 301
        assert r.location.endswith(f'/profile/{sample_user.handle}')

    def test_unknown_handle_404s(self, client):
        assert client.get('/profile/no-such-handle').status_code == 404

    def test_uuid_redirect_follows_handle_change(
        self, client, app, sample_user
    ):
        with app.app_context():
            user = db.session.get(User, sample_user.id)
            user.handle = 'renamed.user'
            db.session.commit()
        r = client.get(f'/profile/{sample_user.id}', follow_redirects=False)
        assert r.status_code == 301
        assert r.location.endswith('/profile/renamed.user')
        # The old handle no longer resolves.
        assert client.get('/profile/test.user').status_code == 404
        assert client.get('/profile/renamed.user').status_code == 200

    def test_specific_profile_routes_not_swallowed(
        self, client, logged_in_user, sample_user
    ):
        # /profile/setup, /edit, /linked and /users/.../endorse are
        # static routes — the <handle> rule must not capture them.
        assert logged_in_user.get('/profile/edit').status_code in (200, 302)
        assert logged_in_user.get('/profile/linked').status_code == 200
        assert client.get('/profile/setup').status_code == 302  # login wall

    def test_mention_links_use_handles(self, app, second_user):
        from flask import render_template_string
        from utils.mentions import link_mentions
        with app.app_context():
            with app.test_request_context():
                out = render_template_string(
                    '{{ t|mention_links }}', t='Hi @Second')
                assert f'/profile/{second_user.handle}' in out
                assert second_user.id not in out


# ------------------------------------------------------- setup form handle


class TestSetupFormHandle:
    def test_setup_prefills_generated_handle(
        self, client, app, login_as
    ):
        user = _make_user(app, 'prefill@example.com', 'Prefill Person')
        login_as(client, 'prefill@example.com')
        html = client.get('/profile/setup').data.decode()
        assert 'value="prefill.person"' in html

    def test_setup_saves_changed_handle(self, client, app, login_as):
        user = _make_user(app, 'change@example.com', 'Change Me')
        login_as(client, 'change@example.com')
        r = client.post('/profile/setup', data=_setup_data(
            handle='changed.handle',
        ), follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(User, user.id).handle == 'changed.handle'

    def test_setup_keeps_handle_when_blank(
        self, client, app, login_as
    ):
        user = _make_user(app, 'blank@example.com', 'Blank Keeper')
        login_as(client, 'blank@example.com')
        r = client.post('/profile/setup', data=_setup_data(handle=''),
                        follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(User, user.id).handle == 'blank.keeper'

    def test_setup_rejects_taken_handle(
        self, client, app, login_as, sample_user
    ):
        _make_user(app, 'taken@example.com', 'Taken Taker')
        login_as(client, 'taken@example.com')
        r = client.post('/profile/setup', data=_setup_data(
            handle=sample_user.handle,
        ))
        assert r.status_code == 200
        assert b'That handle is already taken.' in r.data

    def test_setup_rejects_reserved_handle(
        self, client, app, login_as
    ):
        _make_user(app, 'reserved@example.com', 'Reserved Taker')
        login_as(client, 'reserved@example.com')
        r = client.post('/profile/setup', data=_setup_data(
            handle='admin',
        ))
        assert r.status_code == 200
        assert b'That handle is reserved.' in r.data

    def test_setup_handle_race_returns_friendly_error(
        self, client, app, login_as, sample_user, monkeypatch
    ):
        """A handle taken between the form's uniqueness check and
        the commit is caught by the database constraint — the route
        answers with the friendly 'already taken' message, not a
        500. (The form check is patched out to simulate the race.)"""
        from blueprints.profile.forms import SetupForm
        monkeypatch.setattr(
            SetupForm, 'validate_handle', lambda self, field: None
        )
        user = _make_user(app, 'race@example.com', 'Race Taker')
        login_as(client, 'race@example.com')
        r = client.post('/profile/setup', data=_setup_data(
            handle=sample_user.handle,
        ))
        assert r.status_code == 200
        assert b'That handle is already taken.' in r.data
        with app.app_context():
            assert db.session.get(User, user.id).handle == 'race.taker'
            from models.profile import Profile
            assert Profile.query.filter_by(
                user_id=user.id).count() == 0

    def test_setup_rejects_bad_handle_format(
        self, client, app, login_as
    ):
        _make_user(app, 'badfmt@example.com', 'Bad Format')
        login_as(client, 'badfmt@example.com')
        r = client.post('/profile/setup', data=_setup_data(
            handle='Bad Handle!',
        ))
        assert r.status_code == 200
        # The form error renders (not just the hint text, which is
        # always on the page).
        assert b'text-danger' in r.data
        assert 'Handles use 3–30'.encode() in r.data


# ------------------------------------------------------- no hardcoded URLs


class TestNoHardcodedBountiesUrls:
    def test_templates_have_no_hardcoded_bounties_urls(self):
        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates',
        )
        offenders = []
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                with open(path, encoding='utf-8') as f:
                    for lineno, line in enumerate(f, start=1):
                        if '/bounties/' in line:
                            offenders.append(f'{path}:{lineno}')
        assert not offenders, (
            'hardcoded /bounties/ URLs remain in templates: '
            + ', '.join(offenders)
        )


# ------------------------------------------------------- migration backfill


class TestMigrationBackfill:
    def test_nice_urls_migration_backfills_existing_rows(self, tmp_path):
        """Upgrade to the Phase 3 revision, insert rows the old way
        (no slug/shortcode/handle columns exist yet), then upgrade
        to head: every row must come out with values filled."""
        import flask_migrate
        from app import create_app

        db_file = tmp_path / 'nice_urls_backfill.db'
        # The URI must reach create_app() via config_overrides:
        # Flask-SQLAlchemy creates its engines eagerly in
        # db.init_app(), so setting SQLALCHEMY_DATABASE_URI on the
        # app afterwards is silently ignored and this test would
        # run against (and leak rows into) the shared test DB.
        test_app = create_app('testing', config_overrides={
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_file}',
        })
        with test_app.app_context():
            flask_migrate.upgrade(revision='phase3_discovery_trust')
            db.session.execute(text(
                "INSERT INTO users (id, email, full_name, role) VALUES "
                "('u1', 'a@example.com', 'Élise Côté', 'user'), "
                "('u2', 'b@example.com', 'Élise Côté', 'user')"
            ))
            db.session.execute(text(
                "INSERT INTO bounties (id, poster_id, title, description, "
                "bounty_type, bounty_direction, status, payment_status) "
                "VALUES "
                "('b1', 'u1', 'Électricien à Montréal', 'desc', "
                "'recruitment', 'seeking_opportunity', 'open', 'released'), "
                "('b2', 'u1', 'Électricien à Montréal', 'desc', "
                "'recruitment', 'seeking_opportunity', 'open', 'released')"
            ))
            db.session.commit()

            flask_migrate.upgrade()
            rows = db.session.execute(text(
                'SELECT slug, shortcode FROM bounties ORDER BY id'
            )).all()
            assert [r[0] for r in rows] == [
                'electricien-a-montreal', 'electricien-a-montreal',
            ]
            assert rows[0][1] != rows[1][1]
            assert all(len(r[1]) == 6 for r in rows)
            handles = db.session.execute(text(
                'SELECT handle FROM users ORDER BY id'
            )).all()
            assert [h[0] for h in handles] == [
                'elise.cote', 'elise.cote2',
            ]
