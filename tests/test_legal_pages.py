"""Legal pages tests — /privacy, /terms, /cookie-policy load, carry
their titles, link from the footer, and state the current (OAuth-only,
no-password) product. The privacy page is also the URL Meta links to
from the Facebook app's data-deletion field, so it must name the
support address and the in-app deletion flow."""


class TestLegalPagesLoad:
    def test_privacy_page(self, client):
        resp = client.get('/privacy')
        assert resp.status_code == 200
        assert b'Privacy Policy' in resp.data
        assert b'Last updated: November 23, 2026' in resp.data
        assert b'support@paid2match.work' in resp.data
        # OAuth-only product: no password wording anywhere on the page.
        assert b'password' not in resp.data.lower()
        # Meta data-deletion requirement: the page explains deletion.
        assert b'Data deletion' in resp.data
        assert b'DELETE' in resp.data

    def test_terms_page(self, client):
        resp = client.get('/terms')
        assert resp.status_code == 200
        assert b'Terms of Service' in resp.data
        assert b'Last updated: November 23, 2026' in resp.data
        assert b'support@paid2match.work' in resp.data
        assert b'password' not in resp.data.lower()
        assert b'$50 CAD' in resp.data

    def test_cookie_policy_page(self, client):
        resp = client.get('/cookie-policy')
        assert resp.status_code == 200
        assert b'Cookie Policy' in resp.data
        assert b'Last updated: November 23, 2026' in resp.data
        assert b'support@paid2match.work' in resp.data
        assert b'password' not in resp.data.lower()
        # All three sign-in providers are named.
        assert b'Continue with Google' in resp.data
        assert b'Continue with GitHub' in resp.data
        assert b'Continue with Facebook' in resp.data

    def test_legal_pages_are_public(self, client):
        # No login wall: anonymous visitors (and Meta's reviewers) can
        # read them.
        for path in ('/privacy', '/terms', '/cookie-policy'):
            resp = client.get(path)
            assert resp.status_code == 200


class TestLegalFooterLinks:
    def test_footer_links_point_at_real_routes(self, client):
        resp = client.get('/')
        assert resp.status_code == 200
        assert b'href="/privacy"' in resp.data
        assert b'href="/terms"' in resp.data
        assert b'href="/cookie-policy"' in resp.data
        # The Help link added by the support build is untouched.
        assert b'href="/help"' in resp.data
