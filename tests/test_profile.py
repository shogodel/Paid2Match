"""Tests for profile blueprint."""
import pytest


class TestProfileSetup:
    def test_setup_requires_login(self, client):
        r = client.get('/profile/setup')
        assert r.status_code == 302

    def test_setup_page_loads(self, logged_in_user, client, sample_user, app):
        with app.app_context():
            from models.profile import Profile
            from extensions import db
            p = Profile.query.filter_by(user_id=sample_user.id).first()
            db.session.delete(p)
            db.session.commit()
        r = logged_in_user.get('/profile/setup')
        assert r.status_code == 200

    def test_setup_success(self, client, app, sample_user, login_as):
        login_as(client, 'testuser@example.com')
        r = client.post('/profile/setup', data={
            'profile_type': 'worker',
            'display_name': 'Test Profile',
            'bio': 'Experienced developer.',
            'location': 'Remote',
            'skills': 'Python, Flask',
        }, follow_redirects=False)
        assert r.status_code == 302

    def test_setup_redirects_existing(self, logged_in_user, client, sample_user):
        r = logged_in_user.get('/profile/setup')
        assert r.status_code in (200, 302)


class TestProfileView:
    def test_view_requires_login(self, client):
        r = client.get('/profile/')
        assert r.status_code == 302

    def test_view_page_loads(self, logged_in_user, client, sample_user):
        r = logged_in_user.get('/profile/')
        assert r.status_code == 200
        assert b'Test User' in r.data

    def test_view_shows_reputation(self, logged_in_user, client, sample_user, app):
        r = logged_in_user.get('/profile/')
        assert r.status_code == 200

    def test_view_shows_activity_counts(self, logged_in_user, client, sample_pitch, sample_user):
        r = logged_in_user.get('/profile/')
        assert r.status_code == 200

    def test_view_shows_my_bounties(self, logged_in_user, client, sample_bounty, app, sample_user):
        with app.app_context():
            from models.user import User
            from models.bounty import Bounty
            from extensions import db
            user = User.query.first()
            bounty = Bounty.query.first()
            if bounty:
                bounty.poster_id = user.id
                db.session.commit()
        r = logged_in_user.get('/profile/')
        assert r.status_code == 200

    def test_view_shows_my_pitches(self, logged_in_user, client, sample_pitch, app, sample_hunter):
        r = logged_in_user.get('/profile/')
        assert r.status_code == 200

    def test_view_shows_my_matches(self, logged_in_user, client, app, sample_user, sample_bounty, sample_hunter):
        with app.app_context():
            from models.user import User
            from models.bounty import Bounty
            from models.pitch import Pitch
            from models.match import Match
            from extensions import db
            from datetime import datetime, timezone
            user = User.query.first()
            bounty = Bounty.query.first()
            hunter = User.query.filter(User.id != user.id).first()
            if bounty and hunter:
                pitch = Pitch.query.filter_by(bounty_id=bounty.id).first()
                if pitch:
                    match = Match(
                        bounty_id=bounty.id,
                        pitch_id=pitch.id,
                        hunter_id=pitch.hunter_id,
                        employer_id=user.id,
                        hunter_confirmed=True,
                        employer_confirmed=True,
                        confirmed_at=datetime.now(timezone.utc)
                    )
                    db.session.add(match)
                    db.session.commit()
        r = logged_in_user.get('/profile/')
        assert r.status_code == 200


class TestProfileEdit:
    def test_edit_requires_login(self, client):
        r = client.get('/profile/edit')
        assert r.status_code == 302

    def test_edit_page_loads(self, logged_in_user, client, sample_user):
        r = logged_in_user.get('/profile/edit')
        assert r.status_code in (200, 302)

    def test_edit_updates_profile(self, logged_in_user, client, sample_user, app):
        r = logged_in_user.post('/profile/edit', data={
            'profile_type': 'worker',
            'display_name': 'Updated Name',
            'bio': 'Updated bio text.',
            'location': 'Remote',
            'company_name': '',
            'skills': 'Python, SQL',
            'website': '',
        }, follow_redirects=False)
        assert r.status_code in (200, 302)


class TestProfilePublic:
    def test_public_profile_loads(self, client, sample_user, app):
        with app.app_context():
            from models.profile import Profile
            from extensions import db
            profile = Profile.query.filter_by(user_id=sample_user.id).first()
            if not profile:
                profile = Profile(
                    user_id=sample_user.id,
                    profile_type='worker',
                    display_name='Test User',
                    headline='Test Headline',
                    bio='Test bio'
                )
                db.session.add(profile)
                db.session.commit()
        r = client.get(f'/profile/{sample_user.handle}')
        assert r.status_code == 200

    def test_public_profile_404(self, client):
        r = client.get('/profile/nonexistent-id-00000')
        assert r.status_code == 404


class TestBioEscaping:
    """The bio is user-controlled text. It used to render with
    `| replace('\\n', '<br>') | safe`, which skipped escaping
    entirely — a <script> tag in a bio ran on every visitor's
    profile view (stored XSS). The nl2br filter escapes first and
    only then turns newlines into <br>."""

    _EVIL_BIO = ('Hello\n<script>alert("pwned")</script>'
                 '<img src=x onerror=alert(2)>')

    def _set_bio(self, app, user_id, bio):
        with app.app_context():
            from extensions import db
            from models.profile import Profile
            profile = Profile.query.filter_by(user_id=user_id).first()
            profile.bio = bio
            db.session.commit()

    def test_public_profile_escapes_bio(self, client, sample_user, app):
        self._set_bio(app, sample_user.id, self._EVIL_BIO)
        r = client.get(f'/profile/{sample_user.handle}')
        assert r.status_code == 200
        assert b'<script>alert("pwned")' not in r.data
        assert b'<img src=x' not in r.data
        assert b'&lt;script&gt;' in r.data
        assert b'&lt;img src=x onerror=alert(2)&gt;' in r.data
        # Newlines still render as line breaks.
        assert b'Hello<br>&lt;script&gt;' in r.data

    def test_own_profile_view_escapes_bio(
            self, logged_in_user, client, sample_user, app):
        self._set_bio(app, sample_user.id, self._EVIL_BIO)
        r = logged_in_user.get('/profile/')
        assert r.status_code == 200
        assert b'<script>alert("pwned")' not in r.data
        assert b'<img src=x' not in r.data
        assert b'&lt;script&gt;' in r.data

    def test_nl2br_filter(self, app):
        nl2br = app.jinja_env.filters['nl2br']
        assert str(nl2br(None)) == ''
        assert str(nl2br('')) == ''
        assert str(nl2br('a\nb')) == 'a<br>b'
        assert str(nl2br('<b>x</b>')) == '&lt;b&gt;x&lt;/b&gt;'

class TestProfileLinkedInFields:
    """M0: the LinkedIn-style fields must actually persist (they were
    silently discarded before — setup even crashed with a TypeError)."""

    def _fresh_verified_user(self, app):
        """Create a verified user with NO profile and return (user, password)."""
        from werkzeug.security import generate_password_hash
        from models.user import User
        from extensions import db
        with app.app_context():
            user = User(
                email='fresh@example.com',
                full_name='Fresh User',
                password_hash=generate_password_hash('FreshPass123!'),
                role='user',
                is_active=True,
                email_verified=True
            )
            db.session.add(user)
            db.session.commit()
            return user.id

    def test_setup_saves_linkedin_fields(self, client, app, login_as):
        self._fresh_verified_user(app)
        login_as(client, 'fresh@example.com')
        r = client.post('/profile/setup', data={
            'profile_type': 'worker',
            'display_name': 'Fresh User',
            'headline': 'Senior Python Developer',
            'bio': 'Experienced developer.',
            'location': 'Montreal',
            'company_name': 'Acme',
            'skills': 'Python, Flask',
            'website': 'https://example.com',
            'linkedin_url': 'https://linkedin.com/in/fresh',
            'github_url': 'https://github.com/fresh',
            'twitter_handle': '@fresh',
            'remote_ok': 'y',
            'relocation_assistance': 'y',
        }, follow_redirects=False)
        assert r.status_code == 302

        with app.app_context():
            from models.profile import Profile
            from models.user import User
            user = User.query.filter_by(email='fresh@example.com').first()
            profile = Profile.query.filter_by(user_id=user.id).first()
            assert profile is not None
            assert profile.headline == 'Senior Python Developer'
            assert profile.linkedin_url == 'https://linkedin.com/in/fresh'
            assert profile.github_url == 'https://github.com/fresh'
            assert profile.twitter_handle == '@fresh'
            assert profile.remote_ok is True
            assert profile.relocation_assistance is True

    def test_edit_saves_linkedin_fields(self, logged_in_user, client, sample_user, app):
        r = logged_in_user.post('/profile/edit', data={
            'profile_type': 'worker',
            'display_name': 'Test User',
            'headline': 'Edited Headline',
            'bio': 'Bio.',
            'location': 'Remote',
            'company_name': '',
            'skills': 'Python',
            'website': '',
            'linkedin_url': 'https://linkedin.com/in/edited',
            'github_url': 'https://github.com/edited',
            'twitter_handle': '@edited',
            'remote_ok': 'y',
            'privacy_level': '0',
        }, follow_redirects=False)
        assert r.status_code in (200, 302)

        with app.app_context():
            from models.profile import Profile
            profile = Profile.query.filter_by(user_id=sample_user.id).first()
            assert profile.headline == 'Edited Headline'
            assert profile.linkedin_url == 'https://linkedin.com/in/edited'
            assert profile.github_url == 'https://github.com/edited'
            assert profile.twitter_handle == '@edited'
            assert profile.remote_ok is True


class TestPublicProfileTrustCard:
    """The single Trust card at the top of the public profile
    (templates/profile/public.html, id="trust-card")."""

    def _trust_card(self, data):
        """The Trust card's HTML, sliced out of the page."""
        start = data.find(b'id="trust-card"')
        assert start != -1, 'Trust card missing from public profile'
        end = data.find(b'profile-tabs', start)
        return data[start:end if end != -1 else len(data)]

    def _add_match(self, app, sample_user, sample_hunter,
                   sample_bounty, sample_pitch):
        """A dual-confirmed match with the profile owner as hunter."""
        from datetime import datetime, timezone
        from extensions import db
        from models.match import Match
        with app.app_context():
            match = Match(
                bounty_id=sample_bounty.id,
                pitch_id=sample_pitch.id,
                hunter_id=sample_user.id,
                employer_id=sample_hunter.id,
                hunter_confirmed=True,
                employer_confirmed=True,
                confirmed_at=datetime.now(timezone.utc),
            )
            db.session.add(match)
            db.session.commit()

    def _add_endorsement(self, app, sample_user, sample_hunter):
        """A real vouch for the profile owner (inserted directly —
        the 250-karma gate in give_endorsement is not under test)."""
        from extensions import db
        from models.endorsement import Endorsement
        with app.app_context():
            db.session.add(Endorsement(
                endorser_id=sample_hunter.id,
                endorsee_id=sample_user.id,
                skill='Python',
            ))
            db.session.commit()

    def _patch_trust_signals(self, monkeypatch, rate=0.8,
                             avg=(6.0, 3)):
        """average_response_time / format_response_time live in
        utils.trust — monkeypatch them in per their contract:
        (median_hours, count) and a median-hours formatter, so the
        card rendering is tested in isolation from the query."""
        monkeypatch.setattr(
            'utils.trust.response_rate', lambda user_id: rate)
        monkeypatch.setattr(
            'utils.trust.average_response_time',
            lambda user_id: avg, raising=False)
        monkeypatch.setattr(
            'utils.trust.format_response_time',
            lambda hours: '~%d hours' % hours, raising=False)

    def test_trust_card_renders_all_signals(
            self, client, app, sample_user, sample_hunter,
            sample_bounty, sample_pitch, monkeypatch):
        self._add_match(app, sample_user, sample_hunter,
                        sample_bounty, sample_pitch)
        self._add_endorsement(app, sample_user, sample_hunter)
        monkeypatch.setattr(
            'blueprints.profile.routes.ratings_summary',
            lambda user_id, reviews=None: (4.5, 3, 1.0))
        self._patch_trust_signals(monkeypatch)
        r = client.get(f'/profile/{sample_user.handle}')
        assert r.status_code == 200
        card = self._trust_card(r.data)
        # Score + level.
        assert b'Trust:' in card
        assert b'/100' in card
        # Completed referrals (matches_count) + vouches.
        assert b'Referrals completed' in card
        assert card.count(b'<span class="stat-number">1</span>') == 2
        assert b'vouch' in card
        # Rating average + count.
        assert b'4.5/5' in card
        assert b'3 reviews' in card
        # Response time directly next to the response rate, one line.
        assert (b'Responds in ~6 hours \xc2\xb7 answers 80% '
                b'of referrals') in card
        # Breakdown list under the score.
        assert b'Why this score:' in card
        assert b'Identity:' in card
        assert b'Transactions:' in card
        assert b'Reliability:' in card
        assert b'Community:' in card
        assert b'Tenure:' in card

    def test_trust_card_empty_state(self, client, sample_user):
        # Fresh user: no matches, no ratings, no pitches received.
        r = client.get(f'/profile/{sample_user.handle}')
        assert r.status_code == 200
        card = self._trust_card(r.data)
        assert b'No history yet' in card
        assert b'Responds in' not in card
        assert b'answers' not in card
        # Never fake zeros.
        assert b'~0 hours' not in r.data
        assert b'0%' not in r.data

    def test_response_time_never_without_response_rate(
            self, client, app, sample_user, sample_hunter,
            sample_bounty, sample_pitch, monkeypatch):
        # The user HAS history (a completed referral) and a fast
        # average — but no response rate. The time must not appear.
        self._add_match(app, sample_user, sample_hunter,
                        sample_bounty, sample_pitch)
        self._patch_trust_signals(monkeypatch, rate=None)
        r = client.get(f'/profile/{sample_user.handle}')
        assert r.status_code == 200
        card = self._trust_card(r.data)
        assert b'Referrals completed' in card
        assert b'~6 hours' not in card
        assert b'Responds in' not in card

    def test_response_rate_without_response_time(
            self, client, sample_user, monkeypatch):
        # Rate with no answered pitches: the rate shows on its own.
        self._patch_trust_signals(monkeypatch, avg=None)
        r = client.get(f'/profile/{sample_user.handle}')
        assert r.status_code == 200
        card = self._trust_card(r.data)
        assert b'Answers 80% of referrals' in card
        assert b'Responds in' not in card

    def test_breakdown_list_under_score(self, client, sample_user):
        r = client.get(f'/profile/{sample_user.handle}')
        assert r.status_code == 200
        card = self._trust_card(r.data)
        assert card.find(b'Trust:') < card.find(b'Why this score:')
        assert card.find(b'Why this score:') < card.find(b'Identity:')
        assert b'Identity:' in card
        assert b'/20' in card
        assert b'Transactions:' in card
        assert b'/40' in card
        assert b'Reliability:' in card
        assert b'Community:' in card
        assert b'Tenure:' in card
        assert b'/10' in card
        # No penalties for a fresh user — the line stays hidden.
        assert b'Penalties:' not in card


class TestProfileCompletion:
    """M0: completion_percentage must be able to reach 100 (it capped at 70)."""

    def test_completion_reaches_100(self, app):
        from models.profile import Profile
        with app.app_context():
            profile = Profile(
                user_id='x', profile_type='worker',
                display_name='Name', headline='Headline', bio='Bio',
                location='Montreal', avatar_url='/uploads/avatars/a.png',
                skills='Python', website='https://example.com',
                linkedin_url='https://linkedin.com/in/x',
                github_url='https://github.com/x',
                twitter_handle='@x',
            )
            assert profile.completion_percentage == 100

    def test_completion_partial(self, app):
        from models.profile import Profile
        with app.app_context():
            profile = Profile(
                user_id='x', profile_type='worker', display_name='Name')
            assert profile.completion_percentage == 15
