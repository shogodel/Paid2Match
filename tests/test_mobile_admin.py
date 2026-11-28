"""Regression tests for the mobile admin pass (branch mobile-admin-friendly).

Admin tables become labelled stacked cards below 768px instead of
clipping off the right edge of the phone (html/body use overflow-x:
clip, so the clipped columns were unreachable, not scrollable).
The admin section nav is mobile-only (d-lg-none) so desktop is
unchanged.
"""
import os


class TestAdminMobileTables:
    def test_users_table_is_stacked_with_labels(self, logged_in_admin, client, sample_user):
        r = logged_in_admin.get('/admin/users')
        assert r.status_code == 200
        body = r.data.decode()
        assert 'table-stack-mobile' in body
        # Every cell carries its label for the stacked-card layout.
        assert 'data-label="Name"' in body
        assert 'data-label="Email"' in body
        assert 'data-label="Email verified"' in body
        assert 'data-label="Founding"' in body
        assert 'data-label="Actions"' in body
        # No inline display styles left to fight the stacked CSS.
        assert 'style="display:inline"' not in body

    def test_bounties_table_is_stacked_with_labels(self, logged_in_admin, client, sample_bounty):
        r = logged_in_admin.get('/admin/bounties')
        assert r.status_code == 200
        body = r.data.decode()
        assert 'table-stack-mobile' in body
        assert 'data-label="Title"' in body
        assert 'data-label="Actions"' in body
        assert 'style="display:inline"' not in body

    def test_payouts_tables_stacked_and_filter_wraps(
            self, logged_in_admin, client, app, sample_bounty, sample_pitch):
        from extensions import db
        from models.match import Match
        from models.payout import Payout
        with app.app_context():
            match = Match(
                bounty_id=sample_bounty.id,
                pitch_id=sample_pitch.id,
                hunter_id=sample_pitch.hunter_id,
                employer_id=sample_bounty.poster_id,
                hunter_confirmed=True,
                employer_confirmed=True,
                payout_amount=4000,
                platform_fee=1000,
                payout_status='pending',
            )
            db.session.add(match)
            db.session.flush()
            db.session.add(Payout(
                match_id=match.id,
                referrer_id=sample_pitch.hunter_id,
                bounty_id=sample_bounty.id,
                kind='referral',
                gross_amount=5000,
                platform_fee=1000,
                amount=4000,
                method='stripe',
                status='pending',
            ))
            db.session.commit()
        r = logged_in_admin.get('/admin/payouts')
        assert r.status_code == 200
        body = r.data.decode()
        assert 'table-stack-mobile' in body
        assert 'admin-filter-group' in body
        assert 'data-label="Manual payout"' in body
        assert 'data-label="Referrer"' in body

    def test_pledges_table_is_stacked(self, logged_in_admin, client, app, sample_user):
        from extensions import db
        from models.endorsement import Pledge
        with app.app_context():
            db.session.add(Pledge(user_id=sample_user.id, company_name='Test Co Inc'))
            db.session.commit()
        r = logged_in_admin.get('/admin/pledges')
        assert r.status_code == 200
        body = r.data.decode()
        assert 'table-stack-mobile' in body
        assert 'data-label="Company"' in body
        assert 'data-label="Actions"' in body

    def test_linked_accounts_table_is_stacked(self, logged_in_admin, client, app, sample_user):
        from extensions import db
        from models.linked_account import LinkedAccount
        with app.app_context():
            db.session.add(LinkedAccount(
                user_id=sample_user.id, platform='linkedin',
                handle='testuser', url='https://www.linkedin.com/in/testuser',
                status='pending', verification_method='manual',
                manual_review=True))
            db.session.commit()
        r = logged_in_admin.get('/admin/linked-accounts')
        assert r.status_code == 200
        body = r.data.decode()
        assert 'table-stack-mobile' in body
        assert 'data-label="Platform"' in body
        assert 'data-label="Actions"' in body


class TestAdminMobileNav:
    def test_admin_pages_show_section_nav(self, logged_in_admin, client):
        r = logged_in_admin.get('/admin/')
        assert r.status_code == 200
        body = r.data.decode()
        assert 'admin-subnav' in body
        assert 'admin-page' in body
        for label in ('Users', 'Opportunities', 'Disputes', 'Payouts',
                      'Reports', 'Verifications', 'Pledges',
                      'Employer Pledges', 'Linked Accounts', 'Settings'):
            assert label in body

    def test_non_admin_never_sees_admin_nav(self, logged_in_user, client):
        r = logged_in_user.get('/')
        assert r.status_code == 200
        assert 'admin-subnav' not in r.data.decode()

    def test_dashboard_tables_scroll_and_header_complete(
            self, logged_in_admin, client, sample_bounty):
        r = logged_in_admin.get('/admin/')
        assert r.status_code == 200
        body = r.data.decode()
        # Summary tables scroll inside their cards at 320px.
        assert 'table-responsive' in body
        # The Recent Opportunities header matches its four columns.
        assert '<th>Type</th>' in body


class TestAdminMobileCss:
    def test_stacked_table_and_subnav_rules_exist(self, app):
        css_path = os.path.join(app.static_folder, 'css', 'main.css')
        with open(css_path) as f:
            css = f.read()
        assert '.table-stack-mobile' in css
        assert 'data-label' in css
        assert '.admin-subnav' in css
        assert '.admin-page .btn' in css
        assert 'min-height: 44px' in css
        assert '#activityChart' in css
        assert 'max-width: 100%' in css

    def test_service_worker_cache_bumped(self, client):
        r = client.get('/sw.js')
        assert r.status_code == 200
        assert 'p2m-v6' in r.data.decode()


class TestAdminLinkInMobileMenu:
    """The Admin link used to live only in the desktop avatar dropdown
    (d-none d-lg-block), so on a phone there was no way to reach /admin
    at all — the mobile hamburger menu had every account link except
    Admin. Regression: the mobile menu carries it for admins only."""

    def test_admin_sees_admin_link_in_mobile_menu(
            self, logged_in_admin):
        r = logged_in_admin.get('/')
        assert r.status_code == 200
        body = r.data.decode()
        assert 'class="nav-link" href="/admin/"' in body

    def test_regular_user_sees_no_admin_link(
            self, logged_in_user):
        r = logged_in_user.get('/')
        assert r.status_code == 200
        assert '/admin' not in r.data.decode()


class TestLongUnbrokenTextWraps:
    """Audit 2026-11-25: a 62-char unbroken word, a 140-char bare URL
    or a 200-char bounty title has no spaces to wrap at, so with the
    default overflow-wrap it ran past the card edge and was silently
    sliced (html/body use overflow-x: clip). overflow-wrap:anywhere —
    not break-word — is what these need: anywhere also makes the
    min-content of a FLEX ITEM breakable, so a long name/title inside
    a flex row can shrink instead of forcing the row wider than the
    viewport. Regression: the rules and the row classes exist."""

    def test_wrapping_rules_exist(self, app):
        css_path = os.path.join(app.static_folder, 'css', 'main.css')
        with open(css_path) as f:
            css = f.read()
        for selector in (
            '.post-meta',
            '.post-author-name',
            '.bounty-nested-title',
            '.profile-name',
            '.profile-headline',
            '.profile-meta-line',
            '.hiring-strip-list a',
            '.messages-view .list-group-item p',
            '.break-anywhere',
        ):
            assert selector in css, selector
        assert 'overflow-wrap: anywhere' in css
        assert '.hiring-strip-list li { min-width: 0; }' in css

    def test_people_and_search_rows_can_shrink(self, app):
        root = app.root_path
        for template in (
            'templates/social/people.html',
            'templates/social/search.html',
        ):
            with open(os.path.join(root, template)) as f:
                html = f.read()
            # Text column shrinks; Follow button keeps its size.
            assert 'd-flex align-items-center gap-2 min-w-0' in html, template
            assert 'min-w-0 break-anywhere' in html, template
            assert 'btn-follow btn-primary btn-sm flex-shrink-0' in html, template
