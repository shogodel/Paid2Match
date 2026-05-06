# Paid2Match Deployment Configuration

## Overview
Gunicorn has been replaced with **uWSGI** for better performance and integration with Nginx.

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
- `SECRET_KEY`: Flask secret key
- `STRIPE_SECRET_KEY`: Stripe API key

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
