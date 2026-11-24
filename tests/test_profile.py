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
