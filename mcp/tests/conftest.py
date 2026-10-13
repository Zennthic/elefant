"""Test-time fixtures for mcp/.

The MCP server is a FastMCP app wrapped in an ASGI bearer-token
middleware.  Pure unit tests of the helpers (_auth_headers,
_override_key, etc.) just need sys.path + env vars; tool tests need
the BearerAuthMiddleware bypassed via a fake validated-token context.
"""
import os
import sys
from pathlib import Path

import pytest


# ── sys.path setup ────────────────────────────────────────────────────────
_MCP_DIR = Path(__file__).resolve().parent.parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))


# ── Env vars main.py reads at import time ────────────────────────────────
os.environ.setdefault("EMBEDDER_URL", "http://test-embedder:8001")
os.environ.setdefault("MEMORY_API_URL", "http://test-memory-api:8002")
os.environ.setdefault("EDITOR_URL", "http://test-editor:8080")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-service-token-cafef00d")
os.environ.setdefault("EXTERNAL_MCP_URL", "https://test.example.com/mcp")
os.environ.setdefault("EXTERNAL_EDITOR_URL", "https://test.example.com/editor")


# ── Shaped objects for ctx-shaped tests ──────────────────────────────────

class _FakeRequest:
    def __init__(self, headers=None, scope=None):
        self.headers = headers or {}
        self.scope = scope if scope is not None else {}


class _FakeContext:
    """Mimics the shape MCP tools receive as `ctx`.

    Real ctx is FastMCP's Context with .request_context.request.scope
    holding the ASGI scope BearerAuthMiddleware decorated with the
    token info.  This stub matches that shape exactly so _token,
    _token_hash, _session_id, _tenant_slug all work against it.
    """
    def __init__(self, *, token_info=None, token_hash="testhash", session_id=""):
        scope = {
            "_kb_token": token_info or {},
            "_kb_token_hash": token_hash,
        }
        headers = {"mcp-session-id": session_id} if session_id else {}

        class _Ctx:
            pass
        ctx = _Ctx()
        request = _FakeRequest(headers=headers, scope=scope)
        ctx.request = request
        self.request_context = ctx


@pytest.fixture
def fake_ctx():
    """Factory for a FastMCP-shaped Context with arbitrary token claims."""
    def _make(token_info=None, token_hash="testhash", session_id=""):
        return _FakeContext(
            token_info=token_info, token_hash=token_hash, session_id=session_id,
        )
    return _make
