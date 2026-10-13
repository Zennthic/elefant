# Rebuild marker: bump to force CI to rebuild + redeploy this image when the
# deployed mcp is stale relative to main.py — e.g. shipping the auth-header
# forwarding read fix that the previous build (broken on the macOS runner)
# never deployed.
FROM python:3.11.17-slim-trixie

# Run as a fixed, non-root UID/GID (10001). The number matters more than the
# name: the fix-perms step in docker-compose.yml and the k8s init containers
# chown existing (root-owned) volumes to exactly this, and k8s' runAsUser and
# fsGroup name it too. Change all of them together or not at all.
RUN groupadd --system --gid 10001 elefant \
 && useradd --system --uid 10001 --gid 10001 --home-dir /home/elefant --create-home --shell /usr/sbin/nologin elefant

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

COPY main.py .

USER 10001:10001

EXPOSE 8003

CMD ["python", "main.py"]


