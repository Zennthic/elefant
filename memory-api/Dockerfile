FROM python:3.11.17-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

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

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]


