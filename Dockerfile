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
CMD ["gunicorn", "-b", "0.0.0.0:8000", "app:create_app()"]
