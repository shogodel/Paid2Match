"""Audit 2026-11-27 (correctness workstream) probe tests.

Hunting: bad ids / bad query params must not 500; pagination edge
cases; form validation; empty states; 403 handler; reputation
formatter boundaries.
"""
import uuid


class TestBadIds:
    def test_need_bad_id_404_not_500(self, client):
        for bad in ('not-a-uuid', '123', str(uuid.uuid4())):
            resp = client.get(f'/needs/{bad}')
            assert resp.status_code in (404, 302), (bad, resp.status_code)

    def test_need_expired_bad_id(self, client):
        resp = client.get('/needs/not-a-uuid/expired')
        assert resp.status_code in (404, 302), resp.status_code

    def test_post_detail_bad_id(self, client):
        resp = client.get('/social/posts/not-a-uuid')
        assert resp.status_code in (404, 302), resp.status_code

    def test_public_profile_unknown_handle_404(self, client):
        resp = client.get('/profile/does-not-exist-handle')
        assert resp.status_code in (404, 302), resp.status_code

    def test_thread_bad_user_404(self, client, logged_in_user):
        resp = client.get(f'/messages/thread/{uuid.uuid4()}')
        assert resp.status_code in (404, 302), resp.status_code


class TestBadQueryParams:
    def test_api_bounties_limit_clamped(self, client):
        for q in ('?limit=0', '?limit=-5', '?limit=100000', '?limit=abc'):
            resp = client.get(f'/needs/api{q}')
            assert resp.status_code == 200, (q, resp.status_code)
            assert len(resp.get_json()) <= 100, q

    def test_bounties_list_bad_page(self, client):
        for q in ('?page=0', '?page=-1', '?page=abc', '?page=99999'):
            resp = client.get(f'/needs/{q}')
            assert resp.status_code == 200, (q, resp.status_code)

    def test_feed_bad_page(self, client, logged_in_user):
        for q in ('?page=0', '?page=-1', '?page=abc', '?page=99999'):
            resp = client.get(f'/social/feed{q}')
            assert resp.status_code == 200, (q, resp.status_code)

    def test_notifications_bad_page(self, client, logged_in_user):
        for q in ('?page=0', '?page=-1', '?page=abc', '?page=99999'):
            resp = client.get(f'/social/notifications{q}')
            assert resp.status_code == 200, (q, resp.status_code)

    def test_search_empty_query(self, client, logged_in_user):
        resp = client.get('/social/search?q=')
        assert resp.status_code == 200, resp.status_code
        assert b'None' not in resp.data
        resp = client.get('/social/search?q=zzzznohitszzzz')
        assert resp.status_code == 200, resp.status_code
        assert b'None' not in resp.data


class TestEmptyStates:
    def test_empty_feed_renders(self, client, logged_in_user):
        resp = client.get('/social/feed')
        assert resp.status_code == 200
        assert b'None' not in resp.data

    def test_empty_inbox_renders(self, client, logged_in_user):
        resp = client.get('/messages/')
        assert resp.status_code == 200
        assert b'None' not in resp.data

    def test_empty_notifications_renders(self, client, logged_in_user):
        resp = client.get('/social/notifications')
        assert resp.status_code == 200
        assert b'None' not in resp.data


class TestForbiddenHandler:
    def test_admin_as_plain_user_is_403(self, client, logged_in_user):
        resp = client.get('/admin/')
        assert resp.status_code in (302, 403), resp.status_code

    def test_403_renders_branded_page_not_bare_werkzeug(
            self, app, client, logged_in_user, sample_user, second_user):
        """Deleting someone else's video vouch aborts 403 — the user
        must see the site's own 403 page (base template chrome),
        not the bare Werkzeug 'Forbidden' page: there are 7
        abort(403) call sites but no 403 handler or template."""
        from extensions import db
        from models.video_vouch import VideoVouch
        with app.app_context():
            vouch = VideoVouch(
                user_id=second_user.id, kind='vouch',
                target_user_id=sample_user.id,
                filename='x.mp4', status='active', file_size=123)
            db.session.add(vouch)
            db.session.commit()
            vouch_id = vouch.id
        resp = client.post(f'/video-vouches/{vouch_id}/delete')
        assert resp.status_code == 403, resp.status_code
        assert b'Forbidden' in resp.data
        # Branded page: renders inside base.html, not Werkzeug default.
        assert b'Paid2Match' in resp.data


class TestBoardNegativePage:
    def test_negative_page_shows_first_page_not_tail(
            self, app, client, sample_user, sample_bounty):
        """?page=-1 on the board must not serve a slice of the tail:
        Python's negative slicing turns start=-24/end=-12 into real
        items. With 15 open needs, page=-1 must show the first page
        (or an empty page), never items labelled page -1."""
        from extensions import db
        from models.bounty import Bounty
        with app.app_context():
            for i in range(29):
                db.session.add(Bounty(
                    poster_id=sample_user.id,
                    title=f'Extra need number {i:02d} here',
                    description='A perfectly ordinary need description.',
                    bounty_type='recruitment',
                    bounty_direction='seeking_opportunity',
                    status='open', payment_status='released'))
            db.session.commit()
        page1 = client.get('/needs/?page=1')
        page_neg = client.get('/needs/?page=-1')
        assert page1.status_code == 200
        assert page_neg.status_code == 200
        # A clamped negative page renders exactly the first page
        # (ignoring the og:url meta tag, which echoes the request
        # URL by design).
        def _strip_og_url(body):
            import re
            return re.sub(rb'og:url" content="[^"]*"', b'', body)
        assert _strip_og_url(page_neg.data) == _strip_og_url(page1.data)

    def test_board_next_link_points_to_page_two(
            self, app, client, sample_user, sample_bounty):
        """The board's fake pagination object must carry prev_num /
        next_num: the template's Next link uses them, and without
        them it rendered href="?page=" — Next reloaded page 1."""
        from extensions import db
        from models.bounty import Bounty
        with app.app_context():
            for i in range(13):
                db.session.add(Bounty(
                    poster_id=sample_user.id,
                    title=f'Extra need number {i:02d} here',
                    description='A perfectly ordinary need description.',
                    bounty_type='recruitment',
                    bounty_direction='seeking_opportunity',
                    status='open', payment_status='released'))
            db.session.commit()
        body = client.get('/needs/?page=1').data
        assert b'href="/needs/?page=2"' in body
        assert b'href="/needs/?page="' not in body

    def test_notify_to_nonexistent_user_creates_nothing(self, app):
        """notify() to an id with no User row must not leave an
        orphan notification nobody can ever read (same spirit as
        the deleted-user guard)."""
        import uuid
        from unittest.mock import patch
        from models.notification import Notification
        from utils.notifications import notify
        with app.app_context():
            ghost = str(uuid.uuid4())
            with patch('utils.notifications.send_simple_email') as mail:
                result = notify(ghost, 'message_new', 'Hi', 'Hello')
            assert result is None
            assert Notification.query.filter_by(
                recipient_id=ghost).first() is None
            mail.assert_not_called()


class TestResponseTimeFormatter:
    def test_almost_a_day_never_says_24_hours(self):
        from utils.trust import format_response_time
        assert format_response_time(23.6) == '~1 day'
        assert format_response_time(23.4) == '~23 hours'
        assert format_response_time(24.0) == '~1 day'
        assert format_response_time(47.9) == '~2 days'

    def test_negative_never_claims_under_an_hour(self):
        from utils.trust import format_response_time
        # A negative median can only come from bad data; it must not
        # be presented as a fast response.
        assert format_response_time(-5) is None or 'under an hour' not in str(format_response_time(-5))
