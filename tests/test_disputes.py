"""M2 dispute filing tests — user-facing filing on a match + the admin
resolve flow with an outcome (upheld/dismissed)."""
from extensions import db
from models import Match
from models.bounty import Bounty
from models.dispute import Dispute
from models.notification import Notification
from models.pitch import Pitch


def _free_bounty(app, poster):
    bounty = Bounty(
        poster_id=poster.id, title='Free need', description='x' * 60,
        bounty_type='recruitment', bounty_direction='seeking_opportunity',
        reward_amount=None, status='open', payment_status='released',
        location='Remote',
    )
    db.session.add(bounty)
    db.session.commit()
    db.session.refresh(bounty)
    return bounty


def _confirmed_match(app, bounty, hunter, poster):
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
    db.session.commit()
    db.session.refresh(match)
    return match


class TestFileDispute:
    def test_dispute_form_renders(self, client, logged_in_hunter, app,
                                  sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        resp = client.get(f'/needs/matches/{match.id}/dispute')
        assert resp.status_code == 200
        assert b'File a dispute' in resp.data

    def test_file_dispute_creates_open_dispute(self, client, logged_in_hunter,
                                               app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        resp = client.post(f'/needs/matches/{match.id}/dispute',
                           data={'reason': 'The poster never paid the reward.'},
                           follow_redirects=True)
        assert b'Dispute filed' in resp.data
        dispute = Dispute.query.filter_by(match_id=match.id).first()
        assert dispute is not None
        assert dispute.status == 'open'
        assert dispute.raised_by_id == sample_hunter.id
        # The other party is notified.
        notes = Notification.query.filter_by(
            recipient_id=sample_user.id, type='dispute_opened'
        ).all()
        assert len(notes) == 1

    def test_file_dispute_requires_reason(self, client, logged_in_hunter, app,
                                          sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        resp = client.post(f'/needs/matches/{match.id}/dispute',
                           data={'reason': 'bad'},
                           follow_redirects=True)
        assert b'at least 10 characters' in resp.data
        assert Dispute.query.filter_by(match_id=match.id).count() == 0

    def test_one_open_dispute_per_match(self, client, logged_in_hunter, app,
                                        sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        data = {'reason': 'The poster never paid the reward.'}
        client.post(f'/needs/matches/{match.id}/dispute', data=data,
                    follow_redirects=True)
        resp = client.post(f'/needs/matches/{match.id}/dispute',
                           data={'reason': 'Another complaint entirely.'},
                           follow_redirects=True)
        assert b'already an open dispute' in resp.data
        assert Dispute.query.filter_by(match_id=match.id).count() == 1

    def test_resolved_dispute_allows_new_filing(self, client, logged_in_hunter,
                                                app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        dispute = Dispute(match_id=match.id, raised_by_id=sample_hunter.id,
                          reason='First complaint, resolved.', status='resolved',
                          outcome='dismissed')
        db.session.add(dispute)
        db.session.commit()
        resp = client.post(f'/needs/matches/{match.id}/dispute',
                           data={'reason': 'A new problem has come up.'},
                           follow_redirects=True)
        assert b'Dispute filed' in resp.data
        assert Dispute.query.filter_by(match_id=match.id).count() == 2

    def test_non_party_cannot_file(self, client, login_as, app, sample_user,
                                   sample_hunter, second_user):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        login_as(client, 'second@example.com', 'SecondPass123!')
        resp = client.post(f'/needs/matches/{match.id}/dispute',
                           data={'reason': 'I have nothing to do with this.'},
                           follow_redirects=True)
        assert b'Access denied' in resp.data
        assert Dispute.query.filter_by(match_id=match.id).count() == 0


class TestResolveDispute:
    def _open_dispute(self, app, sample_user, sample_hunter):
        bounty = _free_bounty(app, sample_user)
        match = _confirmed_match(app, bounty, sample_hunter, sample_user)
        dispute = Dispute(match_id=match.id, raised_by_id=sample_hunter.id,
                          reason='The poster never paid the reward.')
        db.session.add(dispute)
        db.session.commit()
        db.session.refresh(dispute)
        return dispute

    def test_admin_resolve_requires_outcome(self, client, logged_in_admin, app,
                                            sample_user, sample_hunter):
        dispute = self._open_dispute(app, sample_user, sample_hunter)
        resp = client.post(f'/admin/disputes/{dispute.id}/resolve',
                           data={'resolution': 'Looked into it.'},
                           follow_redirects=True)
        assert b'Choose an outcome' in resp.data
        assert db.session.get(Dispute, dispute.id).status == 'open'

    def test_admin_resolve_upheld(self, client, logged_in_admin, app,
                                  sample_user, sample_hunter):
        dispute = self._open_dispute(app, sample_user, sample_hunter)
        resp = client.post(f'/admin/disputes/{dispute.id}/resolve',
                           data={'resolution': 'Poster was at fault.',
                                 'outcome': 'upheld'},
                           follow_redirects=True)
        assert b'Dispute resolved' in resp.data
        dispute = db.session.get(Dispute, dispute.id)
        assert dispute.status == 'resolved'
        assert dispute.outcome == 'upheld'
        assert dispute.resolution == 'Poster was at fault.'
        assert dispute.resolved_at is not None

    def test_admin_resolve_dismissed(self, client, logged_in_admin, app,
                                     sample_user, sample_hunter):
        dispute = self._open_dispute(app, sample_user, sample_hunter)
        client.post(f'/admin/disputes/{dispute.id}/resolve',
                    data={'resolution': 'No evidence.', 'outcome': 'dismissed'},
                    follow_redirects=True)
        dispute = db.session.get(Dispute, dispute.id)
        assert dispute.status == 'resolved'
        assert dispute.outcome == 'dismissed'
