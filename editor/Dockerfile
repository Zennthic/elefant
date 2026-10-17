FROM python:3.11.17-slim-trixie

# git: storage backend  |  libxml2/xmlsec1: python3-saml (SAML 2.0)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        libxml2-dev \
        libxmlsec1-dev \
        libxmlsec1-openssl \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Run as a fixed, non-root UID/GID (10001). The number matters more than the
# name: the fix-perms step in docker-compose.yml and the k8s init containers
# chown existing (root-owned) volumes to exactly this, and k8s' runAsUser and
# fsGroup name it too. Change all of them together or not at all.
RUN groupadd --system --gid 10001 elefant \
 && useradd --system --uid 10001 --gid 10001 --home-dir /home/elefant --create-home --shell /usr/sbin/nologin elefant

# Volumes from before the image went non-root are root-owned; the fix-perms
# step chowns them, but git refuses a repo whose owner is not the caller
# ("dubious ownership") and that check would otherwise fail closed for the
# length of one deploy. These are the container's own mounts, nothing else.
RUN git config --system --add safe.directory '*'

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py config_db.py deployment_mode.py auth.py acl.py storage.py backup_crypto.py people.py restore_jobs.py appearance.py login_throttle.py reset_password.py auth_helpers.py internal_service.py wiki_storage.py wiki_pages.py org_backup.py orgs.py git_sources.py ./
COPY routers/ routers/
COPY static/ static/
COPY reset-password /usr/local/bin/reset-password
RUN chmod +x /usr/local/bin/reset-password \
 && mkdir -p /docs /data /backend_sync \
 && chown elefant:elefant /docs /data /backend_sync

USER 10001:10001

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]


