"""Tests for database models."""
import pytest
from datetime import datetime


class TestUserModel:
    def test_user_creation(self, app, db_session):
        from models.user import User
        from extensions import db
        from werkzeug.security import generate_password_hash
        user = User(
            email='modeltest@example.com',
            full_name='Model Test User',
            password_hash=generate_password_hash('TestPass123!'),
        )
        db_session.add(user)
        db_session.commit()
        assert user.id is not None
        assert user.role == 'user'
        assert user.is_active == True

    def test_user_to_dict(self, app, sample_user):
        with app.app_context():
            d = sample_user.to_dict()
            assert d['email'] == 'testuser@example.com'
            assert d['role'] == 'user'
            assert 'id' in d


class TestBountyModel:
    def test_free_bounty_is_open(self, app, sample_bounty):
        with app.app_context():
            assert sample_bounty.is_free_bounty == True
            assert sample_bounty.display_name == 'Seeking Opportunity — Free'

    def test_paid_bounty_properties(self, app, sample_paid_bounty):
        with app.app_context():
            assert sample_paid_bounty.is_free_bounty == False
            assert 'Seeking Opportunity' in sample_paid_bounty.display_name
            assert '$5,000' in sample_paid_bounty.display_name

    def test_bounty_to_dict(self, app, sample_bounty):
        with app.app_context():
            d = sample_bounty.to_dict()
            assert 'bounty_direction' in d
            assert d['is_free'] == True

    def test_bounty_direction_defaults(self, app, db_session, sample_user):
        from models.bounty import Bounty
        with app.app_context():
            b = Bounty(
                poster_id=sample_user.id,
                title='Default Direction Bounty Test',
                description='This bounty uses the default seeking_opportunity direction as the system default setting.',
                bounty_type='healthcare',
                status='open',
                payment_status='released',
            )
            db_session.add(b)
            db_session.commit()
            assert b.bounty_direction == 'seeking_opportunity'


class TestPitchModel:
    def test_pitch_status_defaults(self, app, sample_pitch):
        with app.app_context():
            assert sample_pitch.status == 'pending'

    def test_pitch_to_dict(self, app, sample_pitch):
        with app.app_context():
            d = sample_pitch.to_dict()
            assert d['status'] == 'pending'
            assert d['hunter_id'] == sample_pitch.hunter_id


class TestMessageModel:
    def test_message_is_read_defaults_false(self, app, sample_message):
        with app.app_context():
            assert sample_message.is_read == False


class TestProfileModel:
    def test_profile_reputation_defaults(self, app, sample_user, db_session):
        with app.app_context():
            from models.profile import Profile
            p = Profile.query.filter_by(user_id=sample_user.id).first()
            assert p.reputation_score == 0

    def test_profile_to_dict(self, app, sample_user):
        with app.app_context():
            from models.profile import Profile
            p = Profile.query.filter_by(user_id=sample_user.id).first()
            assert p is not None
            d = p.to_dict()
            assert 'profile_type' in d
            assert d['user_id'] == sample_user.id


class TestMatchModel:
    def test_match_creation(self, app, db_session, sample_user, sample_hunter, sample_bounty, sample_pitch):
        from models.match import Match
        with app.app_context():
            m = Match(
                bounty_id=sample_bounty.id,
                pitch_id=sample_pitch.id,
                hunter_id=sample_hunter.id,
                employer_id=sample_user.id,
            )
            db_session.add(m)
            db_session.commit()
            assert m.id is not None
            assert m.payout_status == 'pending'


class TestAdminSettingsModel:
    def test_admin_settings_get_set(self, app, db_session):
        from models.admin_settings import AdminSettings
        with app.app_context():
            AdminSettings.set('TEST_KEY', 'test_value')
            assert AdminSettings.get('TEST_KEY') == 'test_value'
            assert AdminSettings.get('MISSING_KEY', 'default') == 'default'

    def test_admin_settings_overwrite(self, app, db_session):
        from models.admin_settings import AdminSettings
        with app.app_context():
            AdminSettings.set('OVERWRITE_KEY', 'first_value')
            AdminSettings.set('OVERWRITE_KEY', 'second_value')
            assert AdminSettings.get('OVERWRITE_KEY') == 'second_value'