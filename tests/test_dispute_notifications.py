"""M3 gap fix — resolving a dispute notifies BOTH parties (M2 only
recorded the outcome)."""
from extensions import db
from models import Match
from models.bounty import Bounty
from models.dispute import Dispute
from models.notification import Notification
from models.pitch import Pitch


def _match_with_dispute(poster, hunter, raiser):
    bounty = Bounty(
        poster_id=poster.id, title='Disputed need', description='x' * 60,
        bounty_type='recruitment', bounty_direction='seeking_opportunity',
        reward_amount=None, status='open', payment_status='released',
        location='Remote',
    )
    db.session.add(bounty)
    db.session.flush()
    pitch = Pitch(bounty_id=bounty.id, hunter_id=hunter.id,
                  candidate_teaser='x' * 60, status='accepted')
    db.session.add(pitch)
    db.session.flush()
    match = Match(
        bounty_id=bounty.id, pitch_id=pitch.id,
        hunter_id=hunter.id, employer_id=poster.id,
        hunter_confirmed=True, employer_confirmed=True,
        payout_status='completed',
    )
    db.session.add(match)
    db.session.flush()
    dispute = Dispute(
        match_id=match.id, raised_by_id=raiser.id,
        reason='The referral was not as described.',
    )
    db.session.add(dispute)
    db.session.commit()
    db.session.refresh(dispute)
    return match, dispute


def _notes_for(user_id, note_type='dispute_resolved'):
    return Notification.query.filter_by(
        recipient_id=user_id, type=note_type
    ).all()


class TestDisputeNotifications:
    def test_resolve_notifies_both_parties_upheld(
        self, app, client, sample_user, sample_hunter, sample_admin, login_as
    ):
        _match, dispute = _match_with_dispute(
            sample_user, sample_hunter, raiser=sample_hunter
        )
        login_as(client, sample_admin.email, 'AdminPass123!')
        resp = client.post(
            f'/admin/disputes/{dispute.id}/resolve',
            data={'outcome': 'upheld', 'resolution': 'Refund issued.'},
        )
        assert resp.status_code == 302
        # The raiser hears it was upheld in their favour...
        raiser_notes = _notes_for(sample_hunter.id)
        assert len(raiser_notes) == 1
        assert 'upheld in your favour' in raiser_notes[0].body
        assert 'Refund issued.' in raiser_notes[0].body
        # ...and the other party hears it was upheld against them.
        other_notes = _notes_for(sample_user.id)
        assert len(other_notes) == 1
        assert 'upheld against you' in other_notes[0].body

    def test_resolve_notifies_both_parties_dismissed(
        self, app, client, sample_user, sample_hunter, sample_admin, login_as
    ):
        _match, dispute = _match_with_dispute(
            sample_user, sample_hunter, raiser=sample_user
        )
        login_as(client, sample_admin.email, 'AdminPass123!')
        resp = client.post(
            f'/admin/disputes/{dispute.id}/resolve',
            data={'outcome': 'dismissed', 'resolution': 'No evidence.'},
        )
        assert resp.status_code == 302
        raiser_notes = _notes_for(sample_user.id)
        assert len(raiser_notes) == 1
        assert 'dismissed' in raiser_notes[0].body
        other_notes = _notes_for(sample_hunter.id)
        assert len(other_notes) == 1
        assert 'dismissed in your favour' in other_notes[0].body

    def test_notifications_are_in_app_only(
        self, app, client, sample_user, sample_hunter, sample_admin, login_as
    ):
        """Dispute notifications are in-app, not emailed (disputes are
        handled in-app per the notification registry)."""
        from models.notification import Notification as NotificationModel
        assert 'dispute_resolved' not in NotificationModel.EMAIL_TYPES
        _match, dispute = _match_with_dispute(
            sample_user, sample_hunter, raiser=sample_hunter
        )
        login_as(client, sample_admin.email, 'AdminPass123!')
        client.post(
            f'/admin/disputes/{dispute.id}/resolve',
            data={'outcome': 'upheld', 'resolution': ''},
        )
        assert len(_notes_for(sample_hunter.id)) == 1
        assert len(_notes_for(sample_user.id)) == 1
