"""Pytest configuration and shared fixtures for Paid2Match tests."""
import pytest
from app import create_app
from extensions import db
from models.user import User
from models.profile import Profile
from models.bounty import Bounty
from models.pitch import Pitch
from models.message import Message
from models.match import Match
from models.dispute import Dispute
from models.admin_settings import AdminSettings


@pytest.fixture(scope='function')
def app():
    """Create and configure a test app instance."""
    test_app = create_app('testing')
    with test_app.app_context():
        db.create_all()
        yield test_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    """A test client for the app."""
    return app.test_client()


@pytest.fixture
def app_context(app):
    """Push an application context."""
    with app.app_context():
        yield


@pytest.fixture
def runner(app):
    """A test CLI runner for the app."""
    return app.test_cli_runner()


@pytest.fixture
def db_session(app):
    """Provide a database session within app context."""
    with app.app_context():
        yield db.session


@pytest.fixture
def sample_user(app):
    """Create a sample registered user with profile."""
    from werkzeug.security import generate_password_hash
    with app.app_context():
        user = User(
            email='testuser@example.com',
            full_name='Test User',
            password_hash=generate_password_hash('TestPass123!'),
            role='user',
            is_active=True
        )
        db.session.add(user)
        db.session.flush()
        profile = Profile(
            user_id=user.id,
            profile_type='independent',
            display_name='Test User',
            reputation_score=0
        )
        db.session.add(profile)
        db.session.commit()
        db.session.refresh(user)
        return user


@pytest.fixture
def sample_admin(app):
    """Create a sample admin user."""
    from werkzeug.security import generate_password_hash
    with app.app_context():
        user = User(
            email='admin@example.com',
            full_name='Admin User',
            password_hash=generate_password_hash('AdminPass123!'),
            role='admin',
            is_active=True
        )
        db.session.add(user)
        db.session.commit()
        db.session.refresh(user)
        return user


@pytest.fixture
def sample_bounty(app, sample_user):
    """Create a sample bounty (free/open)."""
    with app.app_context():
        bounty = Bounty(
            poster_id=sample_user.id,
            title='Senior Python Engineer Needed for Startup',
            description='Looking for an experienced Python engineer to help build our backend. This is a great opportunity for someone with Django and Flask experience.',
            bounty_type='recruitment',
            bounty_direction='seeking_opportunity',
            reward_amount=None,
            status='open',
            payment_status='released',
            location='Remote',
            success_criteria='Candidate accepts offer and starts work.'
        )
        db.session.add(bounty)
        db.session.commit()
        db.session.refresh(bounty)
        return bounty


@pytest.fixture
def sample_paid_bounty(app, sample_user):
    """Create a sample paid bounty (pending payment)."""
    with app.app_context():
        bounty = Bounty(
            poster_id=sample_user.id,
            title='Senior Engineer Recruitment Bounty',
            description='Hiring a senior engineer with proven track record. Great compensation package available.',
            bounty_type='recruitment',
            bounty_direction='seeking_opportunity',
            reward_amount=5000,
            status='pending',
            payment_status='unsecured',
            location='New York, NY'
        )
        db.session.add(bounty)
        db.session.commit()
        db.session.refresh(bounty)
        return bounty


@pytest.fixture
def sample_hunter(app):
    """Create a sample hunter user."""
    from werkzeug.security import generate_password_hash
    with app.app_context():
        user = User(
            email='hunter@example.com',
            full_name='Hunter User',
            password_hash=generate_password_hash('HunterPass123!'),
            role='user',
            is_active=True
        )
        db.session.add(user)
        db.session.flush()
        profile = Profile(
            user_id=user.id,
            profile_type='worker',
            display_name='Hunter User',
            reputation_score=15
        )
        db.session.add(profile)
        db.session.commit()
        db.session.refresh(user)
        return user


@pytest.fixture
def sample_pitch(app, sample_bounty, sample_hunter):
    """Create a sample pitch."""
    with app.app_context():
        pitch = Pitch(
            bounty_id=sample_bounty.id,
            hunter_id=sample_hunter.id,
            candidate_teaser='I have 5 years of Python experience and have worked with Django and Flask extensively. I am available immediately and excited about this opportunity.',
            status='pending'
        )
        db.session.add(pitch)
        db.session.commit()
        db.session.refresh(pitch)
        return pitch


@pytest.fixture
def sample_message(app, sample_user, sample_hunter):
    """Create a sample message."""
    with app.app_context():
        msg = Message(
            sender_id=sample_user.id,
            recipient_id=sample_hunter.id,
            content='Your pitch looks great! Can we schedule a call?'
        )
        db.session.add(msg)
        db.session.commit()
        db.session.refresh(msg)
        return msg


@pytest.fixture
def logged_in_user(client, sample_user):
    """Log in the sample user and return the client."""
    client.post('/auth/login', data={
        'email': 'testuser@example.com',
        'password': 'TestPass123!'
    })
    return client


@pytest.fixture
def logged_in_hunter(client, sample_hunter):
    """Log in the sample hunter user and return the client."""
    client.post('/auth/login', data={
        'email': 'hunter@example.com',
        'password': 'HunterPass123!'
    })
    return client


@pytest.fixture
def logged_in_admin(client, sample_admin):
    """Log in the admin user and return the client."""
    client.post('/auth/login', data={
        'email': 'admin@example.com',
        'password': 'AdminPass123!'
    })
    return client