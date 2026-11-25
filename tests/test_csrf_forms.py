"""CSRF coverage for POST forms.

Production enables Flask-WTF CSRF (WTF_CSRF_ENABLED=True in the base
config) while TestingConfig disables it — so a POST form without a
token passes every test and 400s in production. In Nov 2026, 43 of
90 POST forms across the templates had no token at all (admin
actions, delete-account, bounty close/confirm, reply, payments,
linked accounts, referral consent, notifications, delete-post...).

These tests lock the fix in two ways: a static scan of every
template (no POST form may lack a token) and live requests with
CSRF switched on (a token taken from the rendered page must make
the POST succeed).
"""
import os
import re

import pytest

from extensions import db
from models.admin_settings import AdminSettings
from models.user import User

FORM_RE = re.compile(r'<form\b[^>]*>(.*?)</form>', re.S | re.I)
# WTForms hidden_tag() renders id/name/type before value; the hand-written
# inputs put value right after name — allow attributes in between.
TOKEN_RE = re.compile(r'name="csrf_token"[^>]*?value="([^"]+)"')


def _enable_csrf(app):
    """Flip CSRF on the way production has it. CSRFProtect reads the
    flag per request, so this works after app creation."""
    app.config['WTF_CSRF_ENABLED'] = True
    app.config['WTF_CSRF_SSL_STRICT'] = False


def _form_token(html):
    match = TOKEN_RE.search(html)
    assert match, 'no csrf_token input found in rendered page'
    return match.group(1)


class TestTemplateCsrfCoverage:
    def test_every_post_form_has_a_csrf_token(self, app):
        """Every <form method="POST"> in every template must carry a
        csrf_token input (or a FlaskForm hidden_tag, which includes
        one). JS-only forms with no method attribute POST through
        main.js, which sends the X-CSRFToken header."""
        templates_dir = os.path.join(app.root_path, 'templates')
        missing = []
        checked = 0
        for root, _dirs, files in os.walk(templates_dir):
            for name in sorted(files):
                if not name.endswith('.html'):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding='utf-8') as f:
                    text = f.read()
                for match in FORM_RE.finditer(text):
                    block = match.group(0)
                    tag = re.match(r'<form\b[^>]*>', block, re.I).group(0)
                    method = re.search(r'method=["\']?(\w+)', tag, re.I)
                    if not method or method.group(1).upper() != 'POST':
                        continue
                    checked += 1
                    if 'csrf_token' not in block and 'hidden_tag' not in block:
                        line = text[:match.start()].count('\n') + 1
                        missing.append(f'{path}:{line}: {tag}')
        assert checked > 0, 'scan found no POST forms at all'
        assert not missing, (
            'POST forms without a CSRF token (they 400 in production):\n'
            + '\n'.join(missing)
        )


class TestCsrfEnabledPosts:
    def test_read_all_notifications_rejects_no_token(self, app, client,
                                                     logged_in_user):
        _enable_csrf(app)
        resp = client.post('/social/notifications/read-all')
        assert resp.status_code == 400

    def test_read_all_notifications_works_with_rendered_token(
            self, app, client, logged_in_user, sample_user):
        _enable_csrf(app)
        # The read-all form only renders when there is something
        # unread — create one notification first.
        from models.notification import Notification
        with app.app_context():
            db.session.add(Notification(
                recipient_id=sample_user.id, type='like',
                title='Someone liked your post'))
            db.session.commit()
        page = client.get('/social/notifications')
        assert page.status_code == 200
        token = _form_token(page.data.decode())
        resp = client.post('/social/notifications/read-all',
                           data={'csrf_token': token})
        assert resp.status_code == 302

    def test_delete_account_form_posts_with_token(
            self, app, client, logged_in_user):
        _enable_csrf(app)
        page = client.get('/auth/delete-account')
        assert page.status_code == 200
        token = _form_token(page.data.decode())
        # Wrong confirmation: the route rejects it with a redirect,
        # not a 400 — the form itself must get through CSRF.
        resp = client.post('/auth/delete-account',
                           data={'csrf_token': token,
                                 'confirmation': 'not-delete'})
        assert resp.status_code == 302
        with app.app_context():
            user = User.query.filter_by(
                email='testuser@example.com').first()
            assert user is not None
            assert user.is_active is True

    def test_admin_deactivate_form_posts_with_token(
            self, app, client, logged_in_admin, sample_user):
        _enable_csrf(app)
        page = client.get('/admin/users')
        assert page.status_code == 200
        token = _form_token(page.data.decode())
        resp = client.post(
            f'/admin/users/{sample_user.id}/deactivate',
            data={'csrf_token': token})
        assert resp.status_code == 302
        with app.app_context():
            assert db.session.get(User, sample_user.id).is_active is False

    def test_settings_reveal_accepts_configured_header(
            self, app, client, logged_in_admin):
        # templates/admin/settings.html sends the token as an
        # X-CSRFToken header; Flask-WTF must accept it.
        _enable_csrf(app)
        with app.app_context():
            AdminSettings.set('STRIPE_SECRET_KEY', 'sk_test_reveal')
        page = client.get('/admin/settings')
        assert page.status_code == 200
        token = _form_token(page.data.decode())
        resp = client.post('/admin/settings/reveal',
                           data={'key': 'STRIPE_SECRET_KEY'},
                           headers={'X-CSRFToken': token})
        assert resp.status_code == 200
        assert resp.get_json()['value'] == 'sk_test_reveal'

    def test_payment_forms_carry_tokens(self, app, client, logged_in_user):
        _enable_csrf(app)
        pro = client.get('/payments/pro')
        assert pro.status_code == 200
        assert TOKEN_RE.search(pro.data.decode()), \
            'subscribe/cancel Pro forms have no CSRF token'
        bumps = client.get('/payments/bumps')
        assert bumps.status_code == 200
        assert TOKEN_RE.search(bumps.data.decode()), \
            'buy bump pack form has no CSRF token'
