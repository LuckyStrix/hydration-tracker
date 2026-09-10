# Hydration tracker.
#
# Built for Docker Desktop on Windows, but there is nothing Windows-specific in
# here -- the host-side details that matter live in docker-compose.yml and the
# README, chiefly that the database belongs in a named volume rather than a
# bind-mounted host directory.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl is here for the healthcheck below and nothing else; the application
# makes no outbound calls that need it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code change does not reinstall the dependency tree.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps -e .

# Runs as a non-root user. The volume has to be owned by it, which is why the
# directory is created and chowned here rather than left to the mount.
RUN useradd --create-home --uid 10001 hydration \
 && mkdir -p /data /backups \
 && chown -R hydration:hydration /data /backups /app
USER hydration

ENV HYDRATION_DATA_DIR=/data \
    HYDRATION_BACKUP_DIR=/backups \
    HYDRATION_PORT=8080

EXPOSE 8080

# Hits a route that touches the database, so "healthy" means the whole path
# works rather than just that a socket is open.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["hydration", "serve"]
