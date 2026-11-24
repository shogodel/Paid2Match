"""Tests for the Resend HTTPS API email path (utils/email.py).

DigitalOcean blocks outbound SMTP ports on the droplet, so sending goes
through the Resend API when RESEND_API_KEY is set, and falls back to
SMTP (Flask-Mail) otherwise.
"""
from unittest.mock import MagicMock, patch

from utils.email import RESEND_API_URL, send_simple_email


def _enable_resend(app):
    app.config['RESEND_API_KEY'] = 're_test_key'
    app.config['MAIL_DEFAULT_SENDER'] = 'support@paid2match.work'


def test_resend_api_success(app):
    """API key set + Resend returns 200 -> True, correct request."""
    _enable_resend(app)
    mock_resp = MagicMock(status_code=200)
    with patch('utils.email.requests.post', return_value=mock_resp) as mock_post:
        assert send_simple_email('user@example.com', 'Hi', 'Body') is True
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == RESEND_API_URL
    assert kwargs['headers'] == {'Authorization': 'Bearer re_test_key'}
    assert kwargs['json'] == {
        'from': 'support@paid2match.work',
        'to': ['user@example.com'],
        'subject': 'Hi',
        'text': 'Body',
    }
    assert kwargs['timeout'] == 30


def test_resend_api_rejection_returns_false(app):
    """Resend returns a non-200 status -> False."""
    _enable_resend(app)
    mock_resp = MagicMock(status_code=422, text='{"message":"invalid"}')
    with patch('utils.email.requests.post', return_value=mock_resp):
        assert send_simple_email('user@example.com', 'Hi', 'Body') is False


def test_resend_api_network_error_returns_false(app):
    """requests raises -> False (no exception leaks to the caller)."""
    import requests

    _enable_resend(app)
    with patch('utils.email.requests.post', side_effect=requests.RequestException('boom')):
        assert send_simple_email('user@example.com', 'Hi', 'Body') is False


def test_resend_preferred_over_smtp(app):
    """Both API key and MAIL_SERVER set -> API used, SMTP untouched."""
    _enable_resend(app)
    app.config['MAIL_SERVER'] = 'smtp.example.com'
    mock_resp = MagicMock(status_code=200)
    with (
        patch('utils.email.requests.post', return_value=mock_resp) as mock_post,
        patch('utils.email.mail.send') as mock_send,
    ):
        assert send_simple_email('user@example.com', 'Hi', 'Body') is True
    mock_post.assert_called_once()
    mock_send.assert_not_called()


def test_smtp_fallback_without_api_key(app):
    """No API key + MAIL_SERVER set -> Flask-Mail SMTP path used."""
    app.config['RESEND_API_KEY'] = None
    app.config['MAIL_SERVER'] = 'smtp.example.com'
    with (
        patch('utils.email.requests.post') as mock_post,
        patch('utils.email.mail.send') as mock_send,
    ):
        assert send_simple_email('user@example.com', 'Hi', 'Body') is True
    mock_post.assert_not_called()
    mock_send.assert_called_once()


def test_neither_configured_returns_false(app):
    """No API key and no MAIL_SERVER -> False, nothing attempted."""
    app.config['RESEND_API_KEY'] = None
    app.config['MAIL_SERVER'] = None
    with (
        patch('utils.email.requests.post') as mock_post,
        patch('utils.email.mail.send') as mock_send,
    ):
        assert send_simple_email('user@example.com', 'Hi', 'Body') is False
    mock_post.assert_not_called()
    mock_send.assert_not_called()
