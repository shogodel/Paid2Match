"""Video vouches — 30-second video pitches and vouches.

A candidate pitches on camera for a referral need (kind='pitch');
a referrer vouches for someone on camera (kind='vouch'). Files are
stored under random uuid names, so playback URLs are unguessable
and the serve route needs no login.
"""
import os
import re
import shutil
import subprocess
import uuid

from flask import (
    Blueprint, abort, current_app, flash, redirect, request,
    send_from_directory, url_for,
)
from flask_babel import gettext as _
from flask_login import login_required, current_user
from werkzeug.exceptions import RequestEntityTooLarge

from extensions import db, get_or_404
from models.bounty import Bounty
from models.user import User
from models.video_vouch import VideoVouch


video_vouches_bp = Blueprint('video_vouches', __name__)

# Stored filenames are always <32 hex chars>.<ext> — the regex keeps
# the serve route from ever accepting a path traversal or a foreign
# file from the upload folder.
_FILENAME_RE = re.compile(r'^[0-9a-f]{32}\.(mp4|webm|mov)$')


def _safe_next(value, default):
    """Same-origin redirect target only: a relative path (never
    protocol-relative //evil.com), or an absolute URL on this host
    (e.g. the referrer) reduced to its path."""
    if not value:
        return default
    if value.startswith('/') and not value.startswith('//'):
        return value
    from urllib.parse import urlparse
    parsed = urlparse(value)
    if parsed.netloc and parsed.netloc == request.host:
        path = parsed.path or '/'
        return path + (f'?{parsed.query}' if parsed.query else '')
    return default


def _upload_folder():
    folder = current_app.config['VIDEO_VOUCH_UPLOAD_FOLDER']
    os.makedirs(folder, exist_ok=True)
    return folder


def _probe_duration_seconds(path):
    """Video duration in seconds via ffprobe, or None when ffprobe
    is unavailable or the file carries no duration metadata (some
    containers report N/A — the file parsed, the length is just
    unknown).

    Raises ValueError when ffprobe IS available but cannot read the
    file at all: the upload is not a playable video, so the route
    rejects it instead of storing garbage under a .mp4 name. The
    fail-open case is only a missing ffprobe — the Dockerfile
    installs ffmpeg, so that should not happen on the live image
    (see docs/VIDEO_VOUCH_UPLOADS.md).
    """
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        current_app.logger.warning(
            'ffprobe not found — video duration not enforced server-side')
        return None
    try:
        result = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        current_app.logger.warning(
            'ffprobe failed to run on %s — upload rejected', path)
        raise ValueError('ffprobe could not read the file')
    if result.returncode != 0:
        # ffprobe exits non-zero when it cannot parse the container
        # at all — a text file renamed .mp4, a truncated upload, or
        # any other non-video. Not a video, not a vouch.
        current_app.logger.warning(
            'ffprobe could not read %s — upload rejected', path)
        raise ValueError('ffprobe could not read the file')
    text = result.stdout.strip()
    if text in ('', 'N/A'):
        # Readable container, no duration recorded (e.g. a
        # browser-recorded webm): accept, duration unknown.
        return None
    try:
        return float(text)
    except ValueError:
        current_app.logger.warning(
            'ffprobe returned an unparsable duration for %s — '
            'upload rejected', path)
        raise ValueError('ffprobe could not read the file')


@video_vouches_bp.before_request
def _raise_upload_limit():
    """The global MAX_CONTENT_LENGTH is 16MB; video uploads get the
    video-vouch limit (50MB) on this blueprint's upload route only.
    Flask 3 lets a request override the config value per request."""
    if request.endpoint == 'video_vouches.upload':
        request.max_content_length = current_app.config[
            'VIDEO_VOUCH_MAX_BYTES']


@video_vouches_bp.errorhandler(RequestEntityTooLarge)
@video_vouches_bp.errorhandler(413)
def _too_large(error):
    """A video over the size limit is rejected by Flask before the
    route runs — turn the bare 413 into a flash + redirect back."""
    flash(_('That video is too large. Videos must be under 50 MB.'), 'danger')
    target = _safe_next(request.referrer, url_for('profile.view'))
    return redirect(target)


@video_vouches_bp.route('/upload', methods=['POST'])
@login_required
def upload():
    """POST /video-vouches/upload — save a 30-second pitch or vouch.

    Form fields: video (file), kind ('pitch'|'vouch'), bounty_id
    (pitch), target_user_id (vouch), next (redirect target).
    """
    fallback = url_for('profile.view')
    next_url = _safe_next(request.form.get('next'), fallback)
    kind = request.form.get('kind')

    if kind not in VideoVouch.KINDS:
        flash(_('Choose whether this is a pitch or a vouch.'), 'danger')
        return redirect(next_url)

    bounty = None
    target = None
    if kind == 'pitch':
        bounty_id = request.form.get('bounty_id')
        bounty = db.session.get(Bounty, bounty_id) if bounty_id else None
        if not bounty:
            flash(_('Pick the need you are pitching for.'), 'danger')
            return redirect(next_url)
        if bounty.poster_id == current_user.id:
            flash(_('You cannot pitch for your own need.'), 'danger')
            return redirect(next_url)
    else:  # vouch
        target_id = request.form.get('target_user_id')
        target = db.session.get(User, target_id) if target_id else None
        if not target:
            flash(_('Pick the person you are vouching for.'), 'danger')
            return redirect(next_url)
        if target.id == current_user.id:
            flash(_('You cannot vouch for yourself.'), 'danger')
            return redirect(next_url)

    file = request.files.get('video')
    if not file or not file.filename:
        flash(_('Choose a video file first.'), 'danger')
        return redirect(next_url)

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    allowed = current_app.config['VIDEO_VOUCH_ALLOWED_EXTENSIONS']
    if ext not in allowed:
        flash(_('Only MP4, WebM and MOV videos are allowed.'), 'danger')
        return redirect(next_url)

    # Size check (defense in depth — Flask already enforces the same
    # limit via _raise_upload_limit, but the check also guards the
    # route when the limit is configured differently).
    file.stream.seek(0, os.SEEK_END)
    file_size = file.stream.tell()
    file.stream.seek(0)
    max_bytes = current_app.config['VIDEO_VOUCH_MAX_BYTES']
    if file_size > max_bytes:
        flash(_('That video is too large. Videos must be under 50 MB.'), 'danger')
        return redirect(next_url)

    # Random uuid name — unguessable, and the original name never
    # touches the filesystem (no secure_filename needed).
    filename = f'{uuid.uuid4().hex}.{ext}'
    folder = _upload_folder()
    path = os.path.join(folder, filename)
    file.save(path)

    try:
        duration = _probe_duration_seconds(path)
    except ValueError:
        # ffprobe is available and could not read the file: not a
        # playable video. Delete the saved file and refuse.
        os.remove(path)
        flash(
            _('That file is not a playable video. Upload an MP4, '
              'WebM or MOV video file.'),
            'danger',
        )
        return redirect(next_url)
    max_seconds = current_app.config['VIDEO_VOUCH_MAX_SECONDS']
    if duration is not None and duration > max_seconds:
        os.remove(path)
        flash(
            _('That video is %(seconds)s seconds — keep it to %(max)s seconds or less.',
              seconds=int(duration), max=max_seconds),
            'danger',
        )
        return redirect(next_url)

    vouch = VideoVouch(
        kind=kind,
        user_id=current_user.id,
        target_user_id=target.id if target else None,
        bounty_id=bounty.id if bounty else None,
        filename=filename,
        original_filename=file.filename,
        duration_seconds=duration,
        file_size=file_size,
    )
    db.session.add(vouch)
    db.session.commit()

    if kind == 'pitch':
        flash(_('Your video pitch is live.'), 'success')
    else:
        flash(_('Your video vouch is live.'), 'success')
    return redirect(next_url)


@video_vouches_bp.route('/file/<filename>')
def serve(filename):
    """GET /video-vouches/file/<filename> — public playback. The
    filename is a random uuid, so the URL is unguessable; the row
    must still exist and be active (a removed vouch's file is gone,
    and a lookup miss 404s either way)."""
    if not _FILENAME_RE.match(filename):
        abort(404)
    vouch = VideoVouch.query.filter_by(
        filename=filename, status='active'
    ).first()
    if not vouch:
        abort(404)
    folder = current_app.config['VIDEO_VOUCH_UPLOAD_FOLDER']
    return send_from_directory(folder, filename)


@video_vouches_bp.route('/<vouch_id>/delete', methods=['POST'])
@login_required
def delete(vouch_id):
    """POST /video-vouches/<id>/delete — owner or admin only. The
    row is kept as status 'removed' (so references don't dangle) and
    the file is deleted from disk."""
    vouch = get_or_404(VideoVouch, vouch_id)
    if current_user.id != vouch.user_id and current_user.role != 'admin':
        abort(403)
    vouch.status = 'removed'
    db.session.commit()
    folder = current_app.config['VIDEO_VOUCH_UPLOAD_FOLDER']
    try:
        os.remove(os.path.join(folder, vouch.filename))
    except FileNotFoundError:
        pass
    flash(_('Video removed.'), 'success')
    fallback = url_for('profile.view')
    return redirect(_safe_next(request.form.get('next'), fallback))
