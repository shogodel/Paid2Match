"""M1 tests: notifications — helper, route hooks, unread badge, mark read."""
import pytest

from extensions import db
from models.notification import Notification
from models.pitch import Pitch
from utils.notifications import notify, unread_count

TEASER = (
    'I have 5 years of Python experience and have worked with Django and '
    'Flask extensively. I am available immediately and excited about this.'
)


class TestNotifyHelper:
    def test_notify_creates_notification(self, app, sample_user, second_user):
        n = notify(second_user.id, 'like', 'Someone liked your post',
                   actor_id=sample_user.id)
        db.session.commit()
        assert n is not None
        assert Notification.query.filter_by(
            recipient_id=second_user.id, type='like').count() == 1
        assert unread_count(second_user.id) == 1
        assert unread_count(sample_user.id) == 0

    def test_notify_never_notifies_actor_about_own_action(self, app, sample_user):
        n = notify(sample_user.id, 'like', 'Self like',
                   actor_id=sample_user.id)
        assert n is None
        assert Notification.query.filter_by(recipient_id=sample_user.id).count() == 0

    def test_notify_key_event_degrades_gracefully_without_mail(self, app, sample_user, second_user):
        # Testing config has MAIL_SERVER='localhost' with no real server;
        # the in-app notification must still land and nothing may raise.
        n = notify(second_user.id, 'referral_new', 'New referral',
                   actor_id=sample_user.id)
        db.session.commit()
        assert n is not None
        assert Notification.query.filter_by(
            recipient_id=second_user.id, type='referral_new').count() == 1


class TestRouteHooks:
    def test_pitch_notifies_poster(self, client, logged_in_hunter, sample_bounty, sample_user):
        client.post(f'/needs/{sample_bounty.id}/pitch', data={
            'candidate_teaser': TEASER,
        }, follow_redirects=True)
        assert Notification.query.filter_by(
            recipient_id=sample_user.id, type='referral_new').first() is not None

    def test_accept_notifies_referrer(self, client, logged_in_user, sample_pitch, sample_hunter):
        client.post(f'/needs/pitches/{sample_pitch.id}/respond',
                    data={'response': 'interested'}, follow_redirects=True)
        assert Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='referral_accepted').first() is not None

    def test_reject_notifies_referrer(self, client, logged_in_user, sample_pitch, sample_hunter):
        client.post(f'/needs/pitches/{sample_pitch.id}/respond',
                    data={'response': 'pass'}, follow_redirects=True)
        assert Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='referral_rejected').first() is not None

    def test_message_notifies_recipient(self, client, logged_in_user, sample_hunter):
        client.post('/messages/compose', data={
            'recipient_email': 'hunter@example.com',
            'content': 'Hello, I wanted to ask about your referral.',
        }, follow_redirects=True)
        n = Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='message_new').first()
        assert n is not None
        assert 'Test User' in n.title

    def test_reply_notifies_original_sender(self, client, login_as, sample_hunter, sample_message, sample_user):
        login_as(client, 'hunter@example.com', 'HunterPass123!')
        client.post(f'/messages/{sample_message.id}/reply', data={
            'content': 'Thanks for the message, let us talk soon.',
        }, follow_redirects=True)
        assert Notification.query.filter_by(
            recipient_id=sample_user.id, type='message_new').first() is not None

    def test_dual_confirm_notifies_both(self, client, login_as, sample_pitch,
                                        sample_hunter, sample_user):
        # Poster accepts, then both confirm.
        login_as(client, 'testuser@example.com', 'TestPass123!')
        client.post(f'/needs/pitches/{sample_pitch.id}/respond',
                    data={'response': 'interested'}, follow_redirects=True)
        login_as(client, 'hunter@example.com', 'HunterPass123!')
        client.post(f'/needs/pitches/{sample_pitch.id}/confirm-match', follow_redirects=True)
        login_as(client, 'testuser@example.com', 'TestPass123!')
        client.post(f'/needs/pitches/{sample_pitch.id}/confirm-match', follow_redirects=True)
        assert Notification.query.filter_by(
            recipient_id=sample_hunter.id, type='match_confirmed').first() is not None
        assert Notification.query.filter_by(
            recipient_id=sample_user.id, type='match_confirmed').first() is not None


class TestNotificationCenter:
    def test_unread_badge_in_navbar(self, client, logged_in_user, app, sample_user, second_user):
        notify(sample_user.id, 'follow', 'Second started following you',
               actor_id=second_user.id)
        db.session.commit()
        resp = client.get('/social/feed')
        assert resp.status_code == 200
        # Phase 1 nav: the unread count rides on the bell as a
        # terracotta pill (nav-badge) and on the mobile tab bar
        # (tab-badge), replacing the old bg-danger Bootstrap badge.
        assert b'nav-badge' in resp.data
        assert b'tab-badge' in resp.data
        assert b'>1<' in resp.data

    def test_mark_one_read_follows_link(self, client, logged_in_user, app, sample_user, second_user):
        n = notify(sample_user.id, 'like', 'Someone liked your post',
                   actor_id=second_user.id, link_url='/social/feed')
        db.session.commit()
        n_id = n.id
        resp = client.post(f'/social/notifications/{n_id}/read')
        assert resp.status_code == 302
        assert resp.location.endswith('/social/feed')
        assert db.session.get(Notification, n_id).is_read is True

    def test_mark_all_read(self, client, logged_in_user, app, sample_user, second_user):
        notify(sample_user.id, 'like', 'Like one', actor_id=second_user.id)
        notify(sample_user.id, 'comment', 'Comment one', actor_id=second_user.id)
        db.session.commit()
        assert unread_count(sample_user.id) == 2
        client.post('/social/notifications/read-all', follow_redirects=True)
        assert unread_count(sample_user.id) == 0

    def test_notifications_page_lists_them(self, client, logged_in_user, app, sample_user, second_user):
        notify(sample_user.id, 'follow', 'Second started following you',
               actor_id=second_user.id)
        db.session.commit()
        resp = client.get('/social/notifications')
        assert resp.status_code == 200
        assert b'Second started following you' in resp.data
