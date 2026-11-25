# Paid2Match — Docker image (mirrors the droplet's deploy; see DEPLOYMENT.md).
# The live server builds this image with `docker compose build paid2match`
# (compose project at /home/bogdan/apps/docker-compose.yaml on the droplet).
FROM python:3.12-slim
WORKDIR /app
# ffmpeg (ffprobe) for server-side enforcement of the 30s video-vouch limit.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Compile translations at image build: .po files are the source of truth,
# .mo files are build artifacts. (Committed .mo files are also in the repo
# so a plain checkout works without a build step.)
RUN pybabel compile -d translations
# Serve through wsgi:app, NOT app:create_app(): the factory defaults to
# the DEVELOPMENT config when FLASK_ENV is unset (DEBUG on, secure
# session cookies off), while wsgi.py defaults to production. Depending
# on an undocumented FLASK_ENV=production in the container env for the
# live site to run the right config is a footgun; the image must be
# production by default. FLASK_ENV still overrides when set (staging).
CMD ["gunicorn", "-b", "0.0.0.0:8000", "wsgi:app"]
