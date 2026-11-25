"""Tests for video vouches — 30-second video pitches and vouches.

Uploads go to a tmp dir (VIDEO_VOUCH_UPLOAD_FOLDER override), never
the real upload folder. Auth is OAuth-only, so tests log in via the
session (see conftest login_as).
"""
import io
import os
import shutil
import subprocess

import pytest

from extensions import db
from models.video_vouch import VideoVouch


FAKE_MP4 = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 200


@pytest.fixture
def upload_dir(app, tmp_path):
    """Point the video-vouch upload folder at a tmp dir."""
    folder = tmp_path / 'video-vouches'
    app.config['VIDEO_VOUCH_UPLOAD_FOLDER'] = str(folder)
    return folder


def _upload(client, **fields):
    """POST a video upload. Pass video=(bytes, filename) to override;
    follow_redirects is a client option, not a form field."""
    video = fields.pop('video', (io.BytesIO(FAKE_MP4), 'pitch.mp4'))
    follow = fields.pop('follow_redirects', False)
    data = {'video': video}
    data.update(fields)
    return client.post(
        '/video-vouches/upload',
        data=data,
        content_type='multipart/form-data',
        follow_redirects=follow,
    )


def _make_real_video(path, seconds):
    """A real mp4 of `seconds` seconds (silent test pattern)."""
    subprocess.run(
        ['ffmpeg', '-y', '-v', 'error',
         '-f', 'lavfi', '-i',
         f'testsrc=duration={seconds}:size=64x64:rate=10',
         '-t', str(seconds), '-pix_fmt', 'yuv420p',
         '-c:v', 'libx264', str(path)],
        check=True, timeout=120,
    )


@pytest.fixture(scope='module')
def real_video_bytes(tmp_path_factory):
    """Bytes of a real 2-second mp4, generated once per module.

    Success-path uploads need a real video: when ffprobe is
    installed, a fake mp4 (FAKE_MP4) is rejected as unreadable, so
    the happy-path tests cannot use it anymore."""
    if not shutil.which('ffmpeg'):
        pytest.skip('needs ffmpeg')
    path = tmp_path_factory.mktemp('video') / 'short.mp4'
    _make_real_video(path, 2)
    return path.read_bytes()


@pytest.fixture
def no_ffprobe(monkeypatch):
    """Pretend ffprobe is not installed — the fail-open deploy
    caveat (the duration check is skipped, uploads are accepted)."""
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, 'which',
        lambda cmd, *args, **kwargs: (
            None if cmd == 'ffprobe'
            else real_which(cmd, *args, **kwargs)
        ),
    )


# ---------------------------------------------------------------- model


class TestVideoVouchModel:
    def test_pitch_without_bounty_rejected(self, app, sample_user):
        with app.app_context():
            with pytest.raises(ValueError, match='needs the need'):
                VideoVouch(kind='pitch', user_id=sample_user.id)

    def test_vouch_without_target_rejected(self, app, sample_user):
        with app.app_context():
            with pytest.raises(ValueError, match='needs the person'):
                VideoVouch(kind='vouch', user_id=sample_user.id)

    def test_bad_kind_rejected(self, app, sample_user):
        with app.app_context():
            with pytest.raises(ValueError, match='Invalid video vouch kind'):
                VideoVouch(kind='clip', user_id=sample_user.id)

    def test_bad_status_rejected(self, app, sample_user, sample_bounty):
        with app.app_context():
            with pytest.raises(ValueError, match='Invalid video vouch status'):
                VideoVouch(
                    kind='pitch', user_id=sample_user.id,
                    bounty_id=sample_bounty.id, filename='x.mp4',
                    file_size=1, status='bogus',
                )

    def test_valid_pitch_and_vouch_save(
            self, app, sample_user, second_user, sample_bounty):
        with app.app_context():
            pitch = VideoVouch(
                kind='pitch', user_id=sample_user.id,
                bounty_id=sample_bounty.id, filename='a.mp4',
                original_filename='pitch.mp4', file_size=1234,
            )
            vouch = VideoVouch(
                kind='vouch', user_id=sample_user.id,
                target_user_id=second_user.id, filename='b.mp4',
                file_size=2345, duration_seconds=24.5,
            )
            db.session.add_all([pitch, vouch])
            db.session.commit()
            assert VideoVouch.query.count() == 2
            assert pitch.status == 'active'
            assert vouch.to_dict()['duration_seconds'] == 24.5


# ---------------------------------------------------------------- upload


class TestUpload:
    def test_upload_pitch_happy_path(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, real_video_bytes):
        login_as(client, 'hunter@example.com')
        r = _upload(client, kind='pitch', bounty_id=sample_bounty.id,
                    video=(io.BytesIO(real_video_bytes), 'pitch.mp4'),
                    next=f'/needs/{sample_bounty.url_slug}')
        assert r.status_code == 302
        with app.app_context():
            vouch = VideoVouch.query.first()
            assert vouch is not None
            assert vouch.kind == 'pitch'
            assert vouch.user_id == sample_hunter.id
            assert vouch.bounty_id == sample_bounty.id
            assert vouch.target_user_id is None
            assert vouch.status == 'active'
            assert vouch.original_filename == 'pitch.mp4'
            assert vouch.file_size == len(real_video_bytes)
            assert vouch.duration_seconds == pytest.approx(2, abs=0.5)
            assert os.path.isfile(upload_dir / vouch.filename)

    def test_upload_vouch_happy_path(
            self, app, client, login_as, sample_user, second_user,
            upload_dir, real_video_bytes):
        login_as(client, 'testuser@example.com')
        r = _upload(client, kind='vouch', target_user_id=second_user.id,
                    video=(io.BytesIO(real_video_bytes), 'vouch.mp4'),
                    next=f'/profile/{second_user.handle}')
        assert r.status_code == 302
        with app.app_context():
            vouch = VideoVouch.query.first()
            assert vouch is not None
            assert vouch.kind == 'vouch'
            assert vouch.user_id == sample_user.id
            assert vouch.target_user_id == second_user.id
            assert vouch.bounty_id is None
            assert os.path.isfile(upload_dir / vouch.filename)

    def test_upload_wrong_extension_rejected(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir):
        login_as(client, 'hunter@example.com')
        r = _upload(
            client, kind='pitch', bounty_id=sample_bounty.id,
            video=(io.BytesIO(b'not a video'), 'notes.txt'),
            next='/profile/', follow_redirects=True)
        assert r.status_code == 200
        assert b'Only MP4, WebM and MOV videos are allowed.' in r.data
        with app.app_context():
            assert VideoVouch.query.count() == 0
        assert not upload_dir.exists() or not any(upload_dir.iterdir())

    def test_upload_oversize_rejected(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir):
        # Shrink the limit so the test need not send 50 MB.
        app.config['VIDEO_VOUCH_MAX_BYTES'] = 1024
        login_as(client, 'hunter@example.com')
        r = _upload(
            client, kind='pitch', bounty_id=sample_bounty.id,
            video=(io.BytesIO(b'x' * 2048), 'big.mp4'),
            next='/profile/', follow_redirects=True)
        assert r.status_code == 200
        assert b'too large' in r.data
        with app.app_context():
            assert VideoVouch.query.count() == 0

    @pytest.mark.skipif(
        not shutil.which('ffprobe'), reason='needs ffprobe')
    def test_upload_unreadable_video_rejected(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir):
        """A file ffprobe cannot parse is not a video: rejected,
        not stored — even though its extension is .mp4."""
        login_as(client, 'hunter@example.com')
        r = _upload(
            client, kind='pitch', bounty_id=sample_bounty.id,
            video=(io.BytesIO(FAKE_MP4), 'pitch.mp4'),
            next='/profile/', follow_redirects=True)
        assert r.status_code == 200
        assert b'not a playable video' in r.data
        with app.app_context():
            assert VideoVouch.query.count() == 0
        assert not upload_dir.exists() or not any(upload_dir.iterdir())

    def test_upload_accepted_when_ffprobe_missing(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, no_ffprobe):
        """Without ffprobe the check is skipped (fail open, by
        design — see docs/VIDEO_VOUCH_UPLOADS.md): a fake mp4 is
        accepted with an unknown duration."""
        login_as(client, 'hunter@example.com')
        r = _upload(client, kind='pitch', bounty_id=sample_bounty.id,
                    next='/profile/')
        assert r.status_code == 302
        with app.app_context():
            vouch = VideoVouch.query.first()
            assert vouch is not None
            assert vouch.duration_seconds is None

    def test_upload_over_16mb_reaches_route(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, no_ffprobe):
        """The per-request 50MB override actually applies: a 17MB
        upload clears the 16MB global cap and reaches the route
        (ffprobe mocked away so the fake bytes are accepted)."""
        login_as(client, 'hunter@example.com')
        big = b'\x00' * (17 * 1024 * 1024)
        r = _upload(
            client, kind='pitch', bounty_id=sample_bounty.id,
            video=(io.BytesIO(big), 'big.mp4'),
            next='/profile/', follow_redirects=True)
        assert r.status_code == 200
        # The success flash (not the 413 handler's 'too large'
        # flash) proves the upload reached the route.
        assert b'Your video pitch is live.' in r.data
        with app.app_context():
            vouch = VideoVouch.query.first()
            assert vouch is not None
            assert vouch.file_size == len(big)

    def test_upload_unauthenticated_blocked(
            self, app, client, sample_bounty, upload_dir):
        r = _upload(client, kind='pitch', bounty_id=sample_bounty.id)
        assert r.status_code == 302
        assert '/auth/login' in r.headers['Location']
        with app.app_context():
            assert VideoVouch.query.count() == 0

    def test_upload_pitch_without_bounty_rejected(
            self, app, client, login_as, sample_hunter, upload_dir):
        login_as(client, 'hunter@example.com')
        r = _upload(client, kind='pitch', next='/profile/',
                    follow_redirects=True)
        assert b'Pick the need you are pitching for.' in r.data
        with app.app_context():
            assert VideoVouch.query.count() == 0

    def test_upload_vouch_for_self_rejected(
            self, app, client, login_as, sample_user, upload_dir):
        login_as(client, 'testuser@example.com')
        r = _upload(client, kind='vouch', target_user_id=sample_user.id,
                    next='/profile/', follow_redirects=True)
        assert b'You cannot vouch for yourself.' in r.data
        with app.app_context():
            assert VideoVouch.query.count() == 0

    def test_upload_pitch_for_own_bounty_rejected(
            self, app, client, login_as, sample_user, sample_bounty,
            upload_dir):
        login_as(client, 'testuser@example.com')
        r = _upload(client, kind='pitch', bounty_id=sample_bounty.id,
                    next='/profile/', follow_redirects=True)
        assert b'You cannot pitch for your own need.' in r.data
        with app.app_context():
            assert VideoVouch.query.count() == 0

    @pytest.mark.skipif(
        not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
        reason='needs ffmpeg + ffprobe')
    def test_upload_too_long_video_rejected(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, tmp_path):
        video_path = tmp_path / 'long.mp4'
        _make_real_video(video_path, 31)
        login_as(client, 'hunter@example.com')
        with open(video_path, 'rb') as f:
            r = _upload(client, kind='pitch', bounty_id=sample_bounty.id,
                        video=(io.BytesIO(f.read()), 'long.mp4'),
                        next='/profile/', follow_redirects=True)
        assert b'keep it to 30 seconds or less' in r.data
        with app.app_context():
            assert VideoVouch.query.count() == 0
        assert not any(upload_dir.iterdir())

    @pytest.mark.skipif(
        not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
        reason='needs ffmpeg + ffprobe')
    def test_upload_short_real_video_accepted(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, tmp_path):
        video_path = tmp_path / 'short.mp4'
        _make_real_video(video_path, 2)
        login_as(client, 'hunter@example.com')
        with open(video_path, 'rb') as f:
            r = _upload(client, kind='pitch', bounty_id=sample_bounty.id,
                        video=(io.BytesIO(f.read()), 'short.mp4'),
                        next='/profile/', follow_redirects=True)
        assert b'Your video pitch is live.' in r.data
        with app.app_context():
            vouch = VideoVouch.query.first()
            assert vouch is not None
            assert vouch.duration_seconds == pytest.approx(2, abs=0.5)


# ---------------------------------------------------------------- serve


class TestServe:
    def test_serve_returns_file(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, real_video_bytes):
        login_as(client, 'hunter@example.com')
        _upload(client, kind='pitch', bounty_id=sample_bounty.id,
                video=(io.BytesIO(real_video_bytes), 'pitch.mp4'))
        with app.app_context():
            filename = VideoVouch.query.first().filename
        # Playback is public — no login needed.
        anon = app.test_client()
        r = anon.get(f'/video-vouches/file/{filename}')
        assert r.status_code == 200
        assert r.data == real_video_bytes
        assert r.content_type.startswith('video/')

    def test_serve_unknown_filename_404s(self, app, client, upload_dir):
        r = client.get('/video-vouches/file/' + '0' * 32 + '.mp4')
        assert r.status_code == 404

    def test_serve_rejects_non_uuid_filename(self, app, client, upload_dir):
        assert client.get('/video-vouches/file/notes.txt').status_code == 404
        assert client.get(
            '/video-vouches/file/..%2F..%2Fsecret.mp4').status_code == 404


# ---------------------------------------------------------------- delete


class TestDelete:
    def _uploaded(self, app, client, login_as, sample_hunter,
                  sample_bounty, upload_dir, real_video_bytes):
        login_as(client, 'hunter@example.com')
        _upload(client, kind='pitch', bounty_id=sample_bounty.id,
                video=(io.BytesIO(real_video_bytes), 'pitch.mp4'))
        with app.app_context():
            vouch = VideoVouch.query.first()
            return vouch.id, vouch.filename

    def test_delete_by_owner(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, real_video_bytes):
        vouch_id, filename = self._uploaded(
            app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, real_video_bytes)
        assert os.path.isfile(upload_dir / filename)
        r = client.post(f'/video-vouches/{vouch_id}/delete',
                        data={'next': '/profile/'}, follow_redirects=True)
        assert r.status_code == 200
        assert b'Video removed.' in r.data
        with app.app_context():
            assert db.session.get(VideoVouch, vouch_id).status == 'removed'
        assert not os.path.exists(upload_dir / filename)
        # A removed vouch is never served again.
        assert client.get(f'/video-vouches/file/{filename}').status_code == 404

    def test_delete_by_other_user_forbidden(
            self, app, client, login_as, sample_hunter, sample_bounty,
            sample_user, upload_dir, real_video_bytes):
        vouch_id, filename = self._uploaded(
            app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, real_video_bytes)
        login_as(client, 'testuser@example.com')
        r = client.post(f'/video-vouches/{vouch_id}/delete',
                        data={'next': '/profile/'})
        assert r.status_code == 403
        with app.app_context():
            assert db.session.get(VideoVouch, vouch_id).status == 'active'
        assert os.path.isfile(upload_dir / filename)

    def test_delete_by_admin(
            self, app, client, login_as, sample_hunter, sample_bounty,
            sample_admin, upload_dir, real_video_bytes):
        vouch_id, filename = self._uploaded(
            app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, real_video_bytes)
        login_as(client, 'admin@example.com')
        r = client.post(f'/video-vouches/{vouch_id}/delete',
                        data={'next': '/profile/'})
        assert r.status_code == 302
        with app.app_context():
            assert db.session.get(VideoVouch, vouch_id).status == 'removed'
        assert not os.path.exists(upload_dir / filename)

    def test_delete_unauthenticated_blocked(
            self, app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, real_video_bytes):
        vouch_id, _filename = self._uploaded(
            app, client, login_as, sample_hunter, sample_bounty,
            upload_dir, real_video_bytes)
        # Clear Flask-Login's cached g._login_user: the test's app
        # context is shared, so a cached hunter would shadow this
        # anonymous request (see conftest login_as).
        from flask import g
        g.pop('_login_user', None)
        anon = app.test_client()
        r = anon.post(f'/video-vouches/{vouch_id}/delete')
        assert r.status_code == 302
        assert '/auth/login' in r.headers['Location']


# ---------------------------------------------------------------- UI


class TestVideoVouchUI:
    def test_own_profile_shows_pitch_section(
            self, app, logged_in_user, sample_user):
        r = logged_in_user.get('/profile/')
        assert r.status_code == 200
        html = r.data.decode()
        assert 'My video pitches' in html
        assert 'Video vouches' in html
        assert 'No video pitches yet.' in html

    def test_public_profile_shows_vouch_form(
            self, app, client, login_as, sample_user, second_user):
        login_as(client, 'testuser@example.com')
        r = client.get(f'/profile/{second_user.handle}')
        assert r.status_code == 200
        html = r.data.decode()
        assert 'Video pitches' in html
        assert 'Vouch for this person on camera' in html
        assert '/video-vouches/upload' in html

    def test_bounty_detail_shows_video_pitch_form(
            self, app, client, login_as, sample_hunter, sample_bounty):
        login_as(client, 'hunter@example.com')
        r = client.get(f'/needs/{sample_bounty.url_slug}')
        assert r.status_code == 200
        html = r.data.decode()
        assert 'Pitch by video' in html
        assert '/video-vouches/upload' in html

    def test_bounty_detail_poster_sees_video_pitches(
            self, app, client, login_as, sample_hunter, sample_user,
            sample_bounty, upload_dir, real_video_bytes):
        login_as(client, 'hunter@example.com')
        _upload(client, kind='pitch', bounty_id=sample_bounty.id,
                video=(io.BytesIO(real_video_bytes), 'pitch.mp4'))
        login_as(client, 'testuser@example.com')
        r = client.get(f'/needs/{sample_bounty.url_slug}')
        assert r.status_code == 200
        html = r.data.decode()
        assert 'Video pitches' in html
        assert '<video controls' in html
