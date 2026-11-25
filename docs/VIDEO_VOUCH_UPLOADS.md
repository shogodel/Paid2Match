# Video Vouch Uploads — Deploy Notes

Video pitches and vouches are files on disk, not rows in the
database. The database only stores the metadata (`video_vouches`
table) plus the stored filename; the video itself lives under
`VIDEO_VOUCH_UPLOAD_FOLDER` (default
`<app>/instance/uploads/video-vouches`).

## The one thing that will bite you: there is no volume mount

The live `paid2match` container is built from the repo image with
**no volume mounts** — code is baked in at build time. That means
any file written inside the container (including every uploaded
video) is **wiped on the next rebuild/recreate**. Until the volume
below exists, video vouches work but do not survive a deploy.

## What deploy needs (do all four)

### 1. Create the directory on the droplet

On the droplet, as `bogdan`:

```bash
mkdir -p /home/bogdan/apps/uploads/video-vouches
```

The app runs as root inside the container (the image has no `USER`
directive), so no special ownership is needed on the host side —
the container can write to a `bogdan`-owned directory. If the
image ever gains a non-root user, that user needs write access to
this directory (host UID must match the container UID, or the
directory must be group/world-writable — prefer matching the UID).

### 2. Add the volume to docker-compose.yaml

In `/home/bogdan/apps/docker-compose.yaml`, under the `paid2match`
service:

```yaml
services:
  paid2match:
    volumes:
      - ./uploads:/app/instance/uploads
```

This mounts the whole `instance/uploads` tree (video vouches live
at `instance/uploads/video-vouches` inside it), so future
upload features under `instance/uploads` are covered too. Then
recreate the container:

```bash
cd /home/bogdan/apps
docker compose up -d paid2match
```

### 3. Set the env var (optional — only if the folder moves)

The default already points at `<app>/instance/uploads/video-vouches`,
which is where the volume above lands, so **no env var is needed
for the standard setup**. Only set one to use a different path:

```bash
VIDEO_VOUCH_UPLOAD_FOLDER=/app/instance/uploads/video-vouches
```

(in the container's environment — e.g. the compose `environment:`
block or the `.env` file compose reads). If set, it must be a path
inside a mounted volume, or uploads are wiped on rebuild just the
same.

### 4. Back the directory up

`/home/bogdan/apps/uploads/` is user data now, like the Postgres
volume. Include it in whatever backs up the droplet (or at least
copy it before any rebuild that touches the compose file).

## Permissions notes

- Stored filenames are random uuids (`<32 hex chars>.mp4`) — the
  playback URL (`/video-vouches/file/<filename>`) is unguessable,
  so the serve route needs no login. Do not "fix" this by moving
  uploads under `/static/` or another publicly listed directory:
  the folder must not be listable, and the app serves the files.
- Uploaded files are written mode `644` by the app process; the
  directory needs write permission for the container user (root
  today — see step 1).
- The app never executes uploaded files and never uses the
  original filename on disk — only the uuid name.

## ffprobe (server-side duration enforcement)

The 30-second limit is enforced server-side with `ffprobe`
(part of ffmpeg). The `Dockerfile` installs ffmpeg, so the live
image has it — the upload route picks up `ffprobe` from `PATH`
automatically. (The client-side check in
`templates/partials/video_vouch_form.html` stays as the first
line of defence either way.)

Behaviour when ffprobe is missing (e.g. a local checkout without
ffmpeg): the duration check is skipped (a warning is logged) and
the upload is accepted — fail open, so uploads keep working. Do
not rely on this in production: without ffprobe there is no
server-side limit at all.

Behaviour when ffprobe is present but cannot read the file (a
non-video renamed `.mp4`, a truncated upload): the upload is
**rejected** — fail closed. A file ffprobe cannot parse is not a
playable video, so it is never stored as a vouch. A readable
container whose duration ffprobe reports as `N/A` (some
browser-recorded videos carry no duration metadata) is accepted
with an unknown duration.

## Limits (config.py)

| Setting | Default |
|---|---|
| `VIDEO_VOUCH_MAX_SECONDS` | 30 |
| `VIDEO_VOUCH_MAX_BYTES` | 50 MB |
| `VIDEO_VOUCH_ALLOWED_EXTENSIONS` | mp4, webm, mov |

The upload route raises Flask's per-request content limit to
`VIDEO_VOUCH_MAX_BYTES` for the upload endpoint only; the global
16 MB `MAX_CONTENT_LENGTH` still applies everywhere else.

## Deleting

Deleting a vouch (owner or admin) sets its row to status
`removed` and deletes the file from disk. Removed rows are never
served again, even if a file somehow survives.
