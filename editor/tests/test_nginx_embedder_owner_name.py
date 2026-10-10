"""SEC-03: every nginx location that proxies to the embedder must blank
X-Auth-Owner-Name.

The embedder honors that header as a memory-visibility scope hint. It is meant
to be set only by the internal MCP call; nginx must strip it on all browser-
facing routes. A missing strip on the `/embedder/` catch-all let a browser
client spoof it and read/edit another user's owner-tagged memories.
"""
import re
from pathlib import Path

_NGINX_CONF = Path(__file__).resolve().parents[2] / "nginx" / "nginx.conf"


def _location_blocks(text: str):
    """Yield each `location ... { ... }` body (brace-depth aware)."""
    for m in re.finditer(r"location\s+([^\{]+)\{", text):
        i = m.end()
        depth = 1
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        yield m.group(1).strip(), text[m.end():i]


def test_every_embedder_location_blanks_owner_name():
    text = _NGINX_CONF.read_text()
    offenders = [
        name for name, body in _location_blocks(text)
        if "proxy_pass" in body and "http://embedder" in body
        and 'proxy_set_header   X-Auth-Owner-Name ""' not in body
        and 'X-Auth-Owner-Name ""' not in body
    ]
    assert not offenders, (
        f"nginx locations proxy to the embedder without blanking "
        f"X-Auth-Owner-Name: {offenders} — SEC-03 regression"
    )
