"""
Talking to the other Elefant services: the embedder's URL and the shared
secret that marks a request as coming from a peer service.

Moved out of main.py by CQ-04 phase 2, so a router can reach a peer without
importing main. Every Python service keeps its own copy of
_internal_service_headers(); tests/test_internal_service_headers_identical.py
holds the copies to one function.
"""
import hmac
import logging
import os

from fastapi import HTTPException, Request

log = logging.getLogger("editor")

EMBEDDER_URL  = os.environ.get("EMBEDDER_URL", "http://embedder:8001")
MEMORY_API_URL = os.environ.get("MEMORY_API_URL", "http://memory-api:8002")

# Shared secret that identifies this editor instance as a peer Elefant
# service when calling embedder / memory-api directly (org backup snapshot,
# restore, hard-purge). Without it, those internal endpoints fail closed.
# Must match the value set in every other Elefant container's environment.
INTERNAL_SERVICE_TOKEN = os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()
if not INTERNAL_SERVICE_TOKEN:
    log.critical(
        "INTERNAL_SERVICE_TOKEN is not set — admin operations that talk "
        "directly to embedder/memory-api (org backup, restore, purge) will "
        "be REJECTED by peers. Set INTERNAL_SERVICE_TOKEN in your .env "
        "(run: openssl rand -hex 32) and restart every Elefant container."
    )


def _internal_service_headers() -> dict:
    """Headers that identify this request as coming from a peer Elefant
    service. Empty dict when the token isn't configured — peer will reject
    the request, which is the correct fail-closed posture."""
    if not INTERNAL_SERVICE_TOKEN:
        return {}
    return {"X-Internal-Service": INTERNAL_SERVICE_TOKEN}


def _require_internal_service(request: Request) -> None:
    """Reject any request that doesn't carry a valid X-Internal-Service token.

    Peer-service-only endpoints (e.g. /api/internal/config, which returns
    decrypted git credentials) are not routed through nginx, but a workload on
    the internal network or an SSRF that reaches http://editor:8080 directly
    would otherwise read them unauthenticated. Fail closed: an unset token —
    which .env.example marks required — rejects every caller, matching
    memory-api's gate. Constant-time compare avoids leaking the token."""
    presented = (request.headers.get("x-internal-service") or "").strip()
    if not (INTERNAL_SERVICE_TOKEN and presented
            and hmac.compare_digest(presented, INTERNAL_SERVICE_TOKEN)):
        raise HTTPException(status_code=403, detail="internal service token required")
