# Paid2Match Deployment Configuration

## Docker deploy (current)

The live site runs as a Docker container on the droplet (compose project at
`/home/bogdan/apps/docker-compose.yaml`, service `paid2match`, image built
from `./Paid2Match`). The image has no volume mounts — code is baked in at
build. Deploy:

```bash
cd /home/bogdan/apps/Paid2Match
git pull
docker compose build paid2match
docker compose up -d paid2match
docker compose exec paid2match flask db upgrade
```

The repo `Dockerfile` runs `pybabel compile -d translations` at image build,
so translations are always compiled from the current `.po` files.

> **One-time warning:** the droplet's checkout used to carry an *untracked*
> `Dockerfile` (created directly on the server). The first `git pull` after
> this file lands in the repo will fail with "untracked working tree file
> would be overwritten" unless that server-side file is removed first
> (`rm /home/bogdan/apps/Paid2Match/Dockerfile` — it is identical in content
> to the repo version apart from the `pybabel compile` step).

The uWSGI/Nginx notes below are the older setup and are kept for reference.

## Overview
> STALE SECTION: the notes below describe the pre-Docker uWSGI/Nginx
> setup, kept for reference only. The live deploy is Docker +
> Gunicorn (see "Docker deploy" above); uWSGI is NOT in
> requirements.txt (it cannot build on Python 3.12) and the Dockerfile
> serves the app with Gunicorn. Do not re-add uWSGI.

## Configuration Files

### 1. uWSGI Configuration (`uwsgi.ini`)
- **Socket**: `/tmp/paid2match.sock` (Unix socket for Nginx communication)
- **Workers**: 4 processes with 2 threads each
- **App**: `wsgi:app` (Flask application)
- **Python path**: Includes virtual environment site-packages
- **Timeouts**: 300 seconds for requests
- **Logging**: `/tmp/uwsgi.log`

### 2. Nginx Configuration (`/etc/nginx/sites-enabled/paid2match`)
- **Protocol**: uwsgi (via `uwsgi_pass`)
- **Socket**: `unix:/tmp/paid2match.sock`
- **SSL**: Let's Encrypt certificates configured
- **Static files**: Served directly by Nginx from `/root/paid2match/static`
- **Uploads**: Served directly by Nginx from `/root/paid2match/uploads`

### 3. WSGI Entry Point (`wsgi.py`)
- Uses `create_app()` with environment from `FLASK_ENV` variable
- Defaults to 'production' if not specified

## Installed Components

### Python Packages (requirements.txt)
- **uWSGI**: 2.0.31 (replaced Gunicorn 25.3.0)
- **Flask**: 3.1.3
- **Flask extensions**: Babel, Bcrypt, Login, Migrate, SQLAlchemy, WTF
- **Database**: PostgreSQL (psycopg2-binary)
- **Payments**: Stripe
- **Other**: Pillow (image processing), python-dotenv

### Application Structure
- **Blueprints**: auth, bounties, profile, messages, admin
- **Models**: User, Profile, Bounty, Pitch, Match, Message, Dispute, etc.
- **Templates**: Jinja2 templates in `/root/paid2match/templates/`
- **Static files**: CSS, JS, uploads in `/root/paid2match/static/`

## Management Commands

### Start uWSGI
```bash
cd /root/paid2match
pkill -f uwsgi; sleep 1
venv/bin/uwsgi --ini uwsgi.ini --daemonize /tmp/uwsgi_start.log
```

### Stop uWSGI
```bash
cat /tmp/uwsgi.pid | xargs kill -9 2>/dev/null
# or
pkill -f uwsgi
```

### Reload uWSGI (graceful)
```bash
cat /tmp/uwsgi.pid | xargs kill -HUP
```

### Check Status
```bash
ps aux | grep uwsgi | grep -v grep
ls -la /tmp/paid2match.sock
curl -I https://paid2match.work/
```

### View Logs
```bash
tail -f /tmp/uwsgi.log           # uWSGI logs
tail -f /var/log/nginx/error.log # Nginx error logs
tail -f /var/log/nginx/access.log # Nginx access logs
```

### Restart Nginx
```bash
nginx -t && nginx -s reload
```

## Environment Variables
Set in `.env` file or system environment:
- `FLASK_ENV`: 'production' (default), 'development', 'testing'
- `DATABASE_URL`: PostgreSQL connection string
- `SECRET_KEY`: Flask secret key (required — production refuses to
  boot without one)
- Stripe keys are NOT env vars: they live in AdminSettings in the
  database (Admin > Settings). Setting `STRIPE_SECRET_KEY` in the
  environment configures nothing.

## Best Practices Implemented
1. **Unix socket** instead of TCP port (better performance)
2. **Nginx serves static files** directly (not through Python)
3. **Proper timeouts** configured (300s for uploads/processing)
4. **Logging** enabled for debugging
5. **Multiple workers** for concurrency
6. **SSL/TLS** with Let's Encrypt
7. **Security headers** configured in Nginx

## Migration from Gunicorn
- Removed: `gunicorn==25.3.0` from requirements.txt
- Removed: `gunicorn_config.py`
- Added: `uwsgi==2.0.31` to requirements.txt
- Added: `uwsgi.ini` configuration file
- Updated: `wsgi.py` for better environment handling
- Updated: Nginx to use `uwsgi_pass` instead of `proxy_pass`

## Verification
```bash
# Site should return HTTP 200
curl -I https://paid2match.work/

# Socket file should exist
ls -la /tmp/paid2match.sock

# uWSGI processes should be running
ps aux | grep uwsgi
```

## Troubleshooting
1. **Socket file not created**: Check `/tmp/uwsgi.log` for Python import errors
2. **502 Bad Gateway**: Verify uWSGI is running and socket permissions (666)
3. **Static files not loading**: Check Nginx config `alias` paths
4. **Database errors**: Verify `DATABASE_URL` in environment

## Scheduled Jobs

### Weekly digest email (Phase 3)
`flask send-weekly-digest` emails every active user with a verified
email a plain-text digest: the week's top posts (Trending ranking),
new people to follow, new referral needs in their spaces, and their
week's stats (karma earned, new followers, likes received). The
command prints a summary (`N sent, M skipped`) and never raises on a
send failure.

Cron line (Mondays at 08:00 UTC — run inside the app container):
```cron
0 8 * * 1 cd /home/bogdan/apps && docker compose exec -T paid2match flask send-weekly-digest
```

**Mail is NOT configured on the live container** (no `MAIL_*` env
vars), so the digest currently sends nothing there: the command runs,
logs a warning per user, and reports `0 sent, N skipped`. That is
expected until SMTP is configured (see `EMAIL_SETUP.md`) — do not
work around it, and do not invent mail credentials. Once `MAIL_SERVER`
is set, the same command starts sending with no code change.

Links in the digest are built against `P2M_BASE_URL` (default
`https://paid2match.work`); the CLI has no request to take the host
from, so set it if the site ever moves.
