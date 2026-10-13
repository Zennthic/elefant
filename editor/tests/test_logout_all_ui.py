"""The "log out all sessions" control is wired up on the profile page.

Ordinary logout now only ends the session in front of it, which makes this the
only way for a user to recover a lost device — so it has to be reachable, and
it has to sign the caller out afterwards rather than leaving them on a page
whose every request will 401.
"""
from pathlib import Path

import pytest

PROFILE = Path(__file__).resolve().parent.parent / "static" / "profile.html"


@pytest.fixture(scope="module")
def html():
    return PROFILE.read_text()


def test_button_exists_and_calls_the_endpoint(html):
    assert "logoutAllSessions()" in html
    assert "/api/me/logout-all" in html


def test_it_sits_by_the_password_controls(html):
    """Where a user goes looking after something has gone wrong."""
    assert html.index('id="password-card"') < html.index("logoutAllSessions()")
    assert html.index("logoutAllSessions()") < html.index("Two-factor authentication")


def test_it_confirms_first(html):
    """It ends the caller's own session too — not a click to make by accident."""
    fn = html[html.index("async function logoutAllSessions()"):]
    fn = fn[:fn.index("\n}\n")]
    assert "confirm(" in fn


def test_it_redirects_to_login_afterwards(html):
    fn = html[html.index("async function logoutAllSessions()"):]
    fn = fn[:fn.index("\n}\n")]
    assert "/login" in fn


def test_it_is_not_hidden_for_sso_users(html):
    """The password card is hidden for non-local auth; sessions belong to
    everyone, so the sessions card must not be inside it."""
    card = html[html.index('id="password-card"'):html.index("<!-- ── Sessions card")]
    assert "logoutAllSessions" not in card
