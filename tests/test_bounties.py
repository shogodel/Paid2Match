"""Tests for bounties blueprint."""


def _make_bounty(app, poster, title, bounty_type='recruitment',
                 location='Remote', reward_amount=None):
    """A second bounty on the board (slug/shortcode auto-generated)."""
    from extensions import db
    from models.bounty import Bounty
    with app.app_context():
        bounty = Bounty(
            poster_id=poster.id,
            title=title,
            description='A bounty created for board filter tests. '
                        'Detailed enough to pass validation.',
            bounty_type=bounty_type,
            bounty_direction='seeking_opportunity',
            reward_amount=reward_amount,
            status='open',
            payment_status='released',
            location=location,
            success_criteria='Candidate accepts offer and starts work.'
        )
        db.session.add(bounty)
        db.session.commit()
        db.session.refresh(bounty)
        return bounty


class TestBountyBoard:
    def test_board_loads(self, client, sample_bounty):
        r = client.get('/needs/')
        assert r.status_code == 200
        assert b'Senior Python Engineer' in r.data

    def test_board_filters_by_type(self, client, app, sample_user,
                                   sample_bounty):
        _make_bounty(app, sample_user,
                     'Plumber Needed For Bathroom Refit Job',
                     bounty_type='services')
        r = client.get('/needs/?bounty_type=recruitment')
        assert r.status_code == 200
        assert b'Senior Python Engineer' in r.data
        assert b'Plumber Needed For Bathroom Refit Job' not in r.data
        r = client.get('/needs/?bounty_type=services')
        assert r.status_code == 200
        assert b'Plumber Needed For Bathroom Refit Job' in r.data
        assert b'Senior Python Engineer' not in r.data

    def test_board_filters_by_location(self, client, app, sample_user,
                                       sample_bounty):
        _make_bounty(app, sample_user,
                     'Toronto Chef Needed For Bistro Opening',
                     location='Toronto, ON')
        r = client.get('/needs/?location=Remote')
        assert r.status_code == 200
        assert b'Senior Python Engineer' in r.data
        assert b'Toronto Chef Needed For Bistro Opening' not in r.data
        r = client.get('/needs/?location=Toronto')
        assert r.status_code == 200
        assert b'Toronto Chef Needed For Bistro Opening' in r.data
        assert b'Senior Python Engineer' not in r.data

    def test_board_shows_free_badge(self, client, sample_bounty):
        # The free sample bounty renders a 'Free' reward span; the
        # bare word 'Free' also appears in the filter chip, so assert
        # on the span, not the word.
        r = client.get('/needs/')
        assert r.status_code == 200
        assert b'bounty-reward">Free</span>' in r.data


class TestBountyDetail:
    def test_detail_loads(self, client, sample_bounty):
        r = client.get(f'/needs/{sample_bounty.url_slug}')
        assert r.status_code == 200
        assert b'Senior Python Engineer' in r.data

    def test_detail_404(self, client):
        r = client.get('/needs/nonexistent-id-12345')
        assert r.status_code == 404


class TestBountyPost:
    def test_post_requires_login(self, client, sample_bounty):
        r = client.get('/needs/post')
        assert r.status_code == 302

    def test_post_page_loads(self, logged_in_user, client):
        r = logged_in_user.get('/needs/post')
        assert r.status_code == 200

    def test_post_free_bounty(self, logged_in_user, client, app, sample_user):
        r = logged_in_user.post('/needs/post', data={
            'title': 'A Brand New Free Bounty For Testing Purposes',
            'description': 'This is a detailed description for a free bounty that should pass all validations and be created successfully as an open bounty.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'seeking_opportunity',
            'reward_amount': '0',
            'is_free': '1',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert '/needs/' in r.location

    def test_post_paid_bounty(self, logged_in_user, client, app, sample_user):
        r = logged_in_user.post('/needs/post', data={
            'title': 'A Paid Bounty For Recruitment Here',
            'description': 'This bounty has a paid reward and goes live immediately. Owner can secure later.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'offering_opportunity',
            'reward_amount': '2500',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert '/needs/' in r.location

    def test_post_title_too_short(self, logged_in_user, client):
        r = logged_in_user.post('/needs/post', data={
            'title': 'Short',
            'description': 'This description is long enough to pass validation checks.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'seeking_opportunity',
        })
        assert r.status_code == 200
        assert b'between 10 and 200' in r.data


class TestBountyEdit:
    def test_edit_page_loads(self, logged_in_user, client, app, sample_bounty):
        with app.app_context():
            from models.user import User
            user = User.query.first()
            bounty = sample_bounty
            bounty.poster_id = user.id
            from extensions import db
            db.session.commit()
        r = logged_in_user.get(f'/needs/{sample_bounty.id}/edit')
        assert r.status_code == 200
        assert b'Edit Referral Need' in r.data

    def test_edit_updates_bounty(self, logged_in_user, client, app, sample_bounty):
        with app.app_context():
            from models.user import User
            user = User.query.first()
            bounty = sample_bounty
            bounty.poster_id = user.id
            from extensions import db
            db.session.commit()
        r = logged_in_user.post(f'/needs/{sample_bounty.id}/edit', data={
            'title': 'Updated Bounty Title',
            'description': 'Updated description for the bounty.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'seeking_opportunity',
            'reward_amount': '5000',
            'location': 'Updated Location',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'Updated Bounty Title' in r.data

    def test_edit_access_denied(self, logged_in_admin, client, app,
                                sample_admin, sample_bounty):
        # Un-skipped 2026-11-24 audit: the fixtures were never the
        # problem — an admin who is not the poster is denied like
        # anyone else (the route has no admin exemption).
        r = logged_in_admin.get(f'/needs/{sample_bounty.id}/edit',
                                follow_redirects=True)
        assert r.status_code == 200
        assert b'Access denied' in r.data
        r = logged_in_admin.post(f'/needs/{sample_bounty.id}/edit', data={
            'title': 'Hijacked Title Change Attempt Here',
            'description': 'Updated description for the bounty.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'seeking_opportunity',
            'reward_amount': '5000',
            'location': 'Updated Location',
        }, follow_redirects=True)
        assert b'Access denied' in r.data
        with app.app_context():
            from extensions import db
            from models.bounty import Bounty
            db.session.expire_all()
            bounty = db.session.get(Bounty, sample_bounty.id)
            assert bounty.title == 'Senior Python Engineer Needed for Startup'


class TestBountyPitch:
    def test_pitch_requires_login(self, client, sample_bounty):
        r = client.post(f'/needs/{sample_bounty.id}/pitch')
        assert r.status_code == 302

    def test_pitch_not_own_bounty(self, logged_in_user, client, sample_bounty):
        r = logged_in_user.post(f'/needs/{sample_bounty.id}/pitch', data={
            'candidate_teaser': 'I have extensive experience in Python and Flask development and I am very interested in this role.',
        })
        assert r.status_code == 302

    def test_pitch_submission(self, logged_in_hunter, client, app, sample_bounty):
        r = logged_in_hunter.post(f'/needs/{sample_bounty.id}/pitch', data={
            'candidate_teaser': 'I have extensive experience in Python and Flask development. I have worked with Django, SQLAlchemy, and various database systems. I am available immediately.',
        }, follow_redirects=False)
        assert r.status_code == 302

    def test_pitch_duplicate(self, logged_in_hunter, client, app, sample_bounty, sample_pitch):
        r = logged_in_hunter.post(f'/needs/{sample_bounty.id}/pitch', data={
            'candidate_teaser': 'Another pitch from the same hunter user should be rejected.',
        })
        assert r.status_code == 302


class TestRecruitmentBoard:
    def test_recruitment_board(self, client, sample_bounty):
        r = client.get('/needs/recruitment')
        assert r.status_code == 200
        assert b'Recruitment' in r.data



class TestBountyApi:
    def test_api_returns_json(self, client, sample_bounty):
        r = client.get('/needs/api')
        assert r.status_code == 200
        assert r.is_json
        data = r.get_json()
        assert isinstance(data, list)

    def test_api_limit(self, client, sample_bounty):
        r = client.get('/needs/api?limit=1')
        assert r.status_code == 200
        data = r.get_json()
        assert len(data) <= 1


class TestPitchResponse:
    def test_respond_interested(self, logged_in_user, client, app, sample_pitch, sample_bounty):
        r = logged_in_user.post(
            f'/needs/pitches/{sample_pitch.id}/respond',
            data={'response': 'interested'},
            follow_redirects=False
        )
        assert r.status_code == 302
        assert '/messages/' in r.location

    def test_respond_pass(self, logged_in_user, client, app, sample_pitch, sample_bounty):
        r = logged_in_user.post(
            f'/needs/pitches/{sample_pitch.id}/respond',
            data={'response': 'pass'},
            follow_redirects=False
        )
        assert r.status_code == 302
        assert f'/needs/{sample_bounty.url_slug}' in r.location

    def test_respond_requires_poster(self, logged_in_hunter, client, sample_pitch):
        r = logged_in_hunter.post(
            f'/needs/pitches/{sample_pitch.id}/respond',
            data={'response': 'interested'}
        )
        assert r.status_code == 302

    def test_respond_invalid_response(self, logged_in_user, client, sample_pitch):
        r = logged_in_user.post(
            f'/needs/pitches/{sample_pitch.id}/respond',
            data={'response': 'invalid'}
        )
        assert r.status_code == 302

class TestBountyVisibility:
    """M0: paid bounties stay hidden until payment is secured; free ones show."""

    def _make_bounty(self, app, sample_user, title, reward, payment_status, is_free=False):
        from models.bounty import Bounty
        from extensions import db
        with app.app_context():
            bounty = Bounty(
                poster_id=sample_user.id,
                title=title,
                description='A detailed description for this test bounty, long enough to pass any validation.',
                bounty_type='recruitment',
                bounty_direction='seeking_opportunity',
                reward_amount=reward,
                status='open',
                payment_status=payment_status,
                is_free=is_free,
                location='Montreal',
            )
            db.session.add(bounty)
            db.session.commit()
            db.session.refresh(bounty)
            return bounty

    def test_unsecured_paid_bounty_hidden_from_board(self, client, app, sample_user):
        self._make_bounty(app, sample_user, 'Hidden Unsecured Paid Need XYZ', 5000, 'unsecured')
        r = client.get('/needs/')
        assert r.status_code == 200
        assert b'Hidden Unsecured Paid Need XYZ' not in r.data

    def test_secured_paid_bounty_visible_on_board(self, client, app, sample_user):
        self._make_bounty(app, sample_user, 'Visible Secured Paid Need XYZ', 5000, 'secured')
        r = client.get('/needs/')
        assert r.status_code == 200
        assert b'Visible Secured Paid Need XYZ' in r.data

    def test_free_bounty_visible_on_board(self, client, app, sample_user):
        self._make_bounty(app, sample_user, 'Visible Free Need XYZ', 0, 'unsecured', is_free=True)
        r = client.get('/needs/')
        assert r.status_code == 200
        assert b'Visible Free Need XYZ' in r.data

    def test_unsecured_paid_bounty_hidden_from_recruitment_board(self, client, app, sample_user):
        self._make_bounty(app, sample_user, 'Hidden Recruitment Need XYZ', 5000, 'unsecured')
        r = client.get('/needs/recruitment')
        assert r.status_code == 200
        assert b'Hidden Recruitment Need XYZ' not in r.data

    def test_unsecured_detail_redirects_non_poster(self, client, app, sample_user):
        bounty = self._make_bounty(app, sample_user, 'Hidden Detail Need XYZ', 5000, 'unsecured')
        r = client.get(f'/needs/{bounty.url_slug}', follow_redirects=False)
        assert r.status_code == 302
        assert '/needs/' in r.location

    def test_unsecured_detail_loads_for_poster(self, logged_in_user, client, app, sample_user):
        bounty = self._make_bounty(app, sample_user, 'Poster Detail Need XYZ', 5000, 'unsecured')
        r = logged_in_user.get(f'/needs/{bounty.url_slug}')
        assert r.status_code == 200
        assert b'Poster Detail Need XYZ' in r.data

    def test_pitch_rejected_on_unsecured_bounty(self, logged_in_hunter, client, app, sample_user, sample_hunter):
        bounty = self._make_bounty(app, sample_user, 'No Pitch Need XYZ', 5000, 'unsecured')
        r = logged_in_hunter.post(f'/needs/{bounty.id}/pitch', data={
            'candidate_teaser': 'I have extensive experience in Python and Flask development and I am very interested in this role.',
        }, follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            from models.pitch import Pitch
            assert Pitch.query.filter_by(bounty_id=bounty.id).count() == 0


class TestRecruitmentBoardFilters:
    """M0: recruitment_board() ignored _apply_filters() — filters did nothing."""

    def test_location_filter_works(self, client, app, sample_user):
        from models.bounty import Bounty
        from extensions import db
        with app.app_context():
            for title, city in [('Montreal Need XYZ', 'Montreal'), ('Toronto Need XYZ', 'Toronto')]:
                db.session.add(Bounty(
                    poster_id=sample_user.id, title=title,
                    description='A detailed description for this test bounty, long enough.',
                    bounty_type='recruitment', bounty_direction='seeking_opportunity',
                    reward_amount=None, status='open', payment_status='unsecured',
                    is_free=True, location=city,
                ))
            db.session.commit()
        r = client.get('/needs/recruitment?location=Montreal')
        assert r.status_code == 200
        assert b'Montreal Need XYZ' in r.data
        assert b'Toronto Need XYZ' not in r.data


class TestPostPaidBountyRedirect:
    """M0: posting a paid bounty must go to secure-payment, not upgrades."""

    def test_post_paid_bounty_redirects_to_secure_payment(self, logged_in_user, client):
        r = logged_in_user.post('/needs/post', data={
            'title': 'A Paid Bounty Redirect Test Here',
            'description': 'This bounty has a paid reward and should redirect to the payment page after creation.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'seeking_opportunity',
            'reward_amount': '2500',
        }, follow_redirects=False)
        assert r.status_code == 302
        assert '/secure-payment' in r.location
