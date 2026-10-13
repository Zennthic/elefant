"""The domain-verification CNAME target sits under a domain the operator owns.

Until 2026-10-13 the default was verify.elefant.app, a domain Zennthic does
not own. Elefant only compares the CNAME target as a string, so it never
had to resolve, but customers put it in their public DNS and it reads as a
service of whoever owns that name. The default now follows the deployment's
own public domain, and no file in the tree names elefant.app.
"""
import subprocess
from pathlib import Path

import orgs
import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("ELEFANT_DOMAIN_VERIFY_HOST", "ELEFANT_PUBLIC_DOMAIN", "EDITOR_EXTERNAL_URL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_default_follows_the_public_domain(clean_env):
    clean_env.setenv("ELEFANT_PUBLIC_DOMAIN", "Wiki.Example.com")
    assert orgs._configured_verify_host() == "verify.wiki.example.com"


def test_default_falls_back_to_the_editor_url_host(clean_env):
    """Compose passes EDITOR_EXTERNAL_URL and not ELEFANT_PUBLIC_DOMAIN."""
    clean_env.setenv("EDITOR_EXTERNAL_URL", "https://wiki.example.com:8443/editor")
    assert orgs._configured_verify_host() == "verify.wiki.example.com"


def test_public_domain_wins_over_the_editor_url(clean_env):
    clean_env.setenv("ELEFANT_PUBLIC_DOMAIN", "a.example.com")
    clean_env.setenv("EDITOR_EXTERNAL_URL", "https://b.example.com/editor")
    assert orgs._configured_verify_host() == "verify.a.example.com"


def test_an_explicit_setting_wins(clean_env):
    clean_env.setenv("ELEFANT_PUBLIC_DOMAIN", "a.example.com")
    clean_env.setenv("ELEFANT_DOMAIN_VERIFY_HOST", " Verify.Ops.Example.net. ")
    assert orgs._configured_verify_host() == "verify.ops.example.net"


def test_with_nothing_set_the_default_cannot_be_registered_by_anyone(clean_env):
    """RFC 2606 reserves .invalid: it never resolves and nobody can own it."""
    assert orgs._configured_verify_host() == "verify.elefant.invalid"


def test_the_target_is_token_dot_host(clean_env):
    clean_env.setenv("ELEFANT_PUBLIC_DOMAIN", "wiki.example.com")
    assert orgs._configured_verify_host() == "verify.wiki.example.com"
    assert orgs._verify_target("abc123").endswith("." + orgs.ELEFANT_DOMAIN_VERIFY_HOST)
    assert orgs._verify_host("acme.com") == "elefant-verify.acme.com"


def test_nothing_in_the_tree_names_elefant_dot_app():
    """A default that names a domain we do not own is the thing this guards.
    Tracked text files only; this file is the one allowed mention."""
    files = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                           check=True).stdout.split(b"\0")
    offenders = []
    for raw in files:
        if not raw:
            continue
        path = REPO / raw.decode()
        if path == Path(__file__) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "elefant.app" in text:
            offenders.append(raw.decode())
    assert not offenders, offenders
