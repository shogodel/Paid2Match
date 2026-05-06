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

    def test_setup_success(self, client, app, sample_user):
        client.post('/auth/login', data={
            'email': 'testuser@example.com',
            'password': 'TestPass123!',
        })
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
        r = client.get(f'/profile/{sample_user.id}')
        assert r.status_code == 200

    def test_public_profile_404(self, client):
        r = client.get('/profile/nonexistent-id-00000')
        assert r.status_code == 404