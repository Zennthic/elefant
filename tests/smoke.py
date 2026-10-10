"""Cross-service smoke tests.

Hits a live Elefant deployment (BASE_URL, required) and asserts the
cross-service auth contract works end-to-end.  Unit tests cannot see a
deploy-time misconfiguration: if the editor's INTERNAL_SERVICE_TOKEN env
var is missing, /api/admin/memory/compact/config returns 502.  A smoke
test that just calls that endpoint after deploy fails loud immediately.

Run:
    BASE_URL=https://elefant.example.com \\
    SMOKE_SESSION_COOKIE=<paste-elefant_session-value> \\
        python -m pytest tests/smoke.py -m smoke -q

In CI (the post-deploy job in build-deploy.yml), BASE_URL points at
the freshly deployed stage host and SMOKE_SESSION_COOKIE comes from a
dedicated CI service account whose cookie is rotated regularly.
SMOKE_REQUIRE_AUTH=1 turns a missing cookie from a skip into a failure (see
test_authenticated_smoke_is_armed below). CI does not set it for now: a
session cookie expires after 24 hours, so requiring one halted every deploy.

IMPORTANT: this file runs IMMEDIATELY after `docker compose up -d`,
which returns the moment containers START rather than when each app
is actually serving.  The editor takes 10-30s to finish its startup
hooks (backend readiness, S3 setup, scheduler init).  The `wait_for_
stack_ready` session-scoped fixture below polls until the stack is
actually responsive before the test functions run — otherwise smoke
fails for transient deploy-timing reasons rather than real bugs.
"""
import os
import time
import pytest
import httpx


# No default: smoke must never quietly point at somebody else's deployment.
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
if not BASE_URL:
    raise RuntimeError(
        "tests/smoke.py needs BASE_URL set to the deployment under test, "
        "e.g. BASE_URL=https://elefant.example.com"
    )
COOKIE   = os.environ.get("SMOKE_SESSION_COOKIE", "")
# Set by CI, never needed by hand. Without it a missing cookie skips the
# authenticated half, which is what you want at a terminal and exactly what
# went unnoticed in CI: 21 skips, exit 0, a green job.
REQUIRE_AUTH = os.environ.get("SMOKE_REQUIRE_AUTH", "") == "1"
WAIT_FOR_READY_SECONDS = int(os.environ.get("SMOKE_READY_WAIT", "120"))


def _session_cookie() -> dict:
    """Format the elefant_session cookie for httpx."""
    return {"elefant_session": COOKIE} if COOKIE else {}


def _need_cookie():
    """Skip cookie-bearing tests if no SMOKE_SESSION_COOKIE was set —
    half the smoke covers anonymous endpoints (/.well-known) and runs
    even without auth, so partial smoke is still useful."""
    if not COOKIE:
        pytest.skip("SMOKE_SESSION_COOKIE not set — skipping authenticated smoke")


# ── Wait for stack ready (runs once before any test) ────────────────────

@pytest.fixture(scope="session", autouse=True)
def wait_for_stack_ready():
    """Block until the stack is responsive, or fail loud after a
    timeout.  This is the first thing the smoke job does — without it,
    every transient post-deploy startup delay turns into a CI failure
    that looks like a real bug.

    Uses the OAuth protected-resource metadata endpoint as the readiness
    probe because:
      1. It's served by MCP, which depends on editor + memory-api +
         embedder — if it's 200 the whole stack is at least talking.
      2. It's anonymous (no cookie needed).
      3. It returns structured JSON, so a "warming up" cache page or
         a 200-with-HTML-error wouldn't fool us.

    Times out after SMOKE_READY_WAIT seconds (default 120).
    """
    deadline = time.time() + WAIT_FOR_READY_SECONDS
    last_err = None
    poll_interval = 3
    while time.time() < deadline:
        try:
            r = httpx.get(
                f"{BASE_URL}/.well-known/oauth-protected-resource",
                timeout=8,
            )
            if r.status_code == 200 and "resource" in r.json():
                # Probe the editor-served endpoint too — MCP can be up
                # before the editor finishes booting.
                r2 = httpx.get(
                    f"{BASE_URL}/.well-known/oauth-authorization-server",
                    timeout=8,
                )
                if r2.status_code == 200 and "authorization_endpoint" in r2.json():
                    print(f"\n==> stack is ready after "
                          f"{int(time.time() - (deadline - WAIT_FOR_READY_SECONDS))}s")
                    return
                last_err = f"editor AS metadata: {r2.status_code}"
            else:
                last_err = f"MCP discovery: {r.status_code}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(poll_interval)

    pytest.fail(
        f"Stack never became ready in {WAIT_FOR_READY_SECONDS}s; "
        f"last probe: {last_err}.  If this is a real outage, check "
        f"`docker logs elefant-editor-1` and the CI deploy step.  If "
        f"the deploy needs more time, raise SMOKE_READY_WAIT."
    )


# ── Anonymous endpoints ──────────────────────────────────────────────────

@pytest.mark.smoke
def test_oauth_protected_resource_metadata():
    """RFC 9728 — MCP discovery endpoint.  If this 404s, Claude.ai
    can't even start the OAuth flow."""
    r = httpx.get(f"{BASE_URL}/.well-known/oauth-protected-resource", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert "resource" in body
    assert "authorization_servers" in body


@pytest.mark.smoke
def test_oauth_authorization_server_metadata():
    """RFC 8414 — the AS metadata.  Same chain: if this 404s, no OAuth."""
    r = httpx.get(f"{BASE_URL}/.well-known/oauth-authorization-server", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert "authorization_endpoint" in body
    assert "token_endpoint" in body


@pytest.mark.smoke
def test_login_page_renders():
    """Editor serves the login page anonymously.  If this 5xx's, the
    editor crashed; if it 4xx's, traefik / nginx is misrouting."""
    r = httpx.get(f"{BASE_URL}/login", follow_redirects=False, timeout=10)
    # 200 is the happy path; 302→/setup is fine on a fresh stack with
    # no users yet.
    assert r.status_code in (200, 302), \
        f"login page failed: {r.status_code} {r.text[:200]}"


# ── The shared theme ─────────────────────────────────────────
# Every colour on every page now comes from ONE generated stylesheet, and no
# page carries a palette of its own. So if these assets are not reachable the
# failure is not "a bit off" — the page renders with no colours at all. They
# are checked ANONYMOUSLY on purpose: the people who see /login, a /share/
# viewer or the /graph embed are logged out, and the catch-all location would
# hand them a login redirect instead of the stylesheet.

@pytest.mark.smoke
@pytest.mark.parametrize("path,kind,marker", [
    ("/appearance.css", "css",        "data-palette='indigo'"),
    ("/elefant-ui.css", "css",        "var(--font-sans)"),
    ("/theme.js",       "javascript", "elefant-palette"),
])
def test_theme_assets_are_served_to_a_logged_out_visitor(path, kind, marker):
    r = httpx.get(f"{BASE_URL}{path}", follow_redirects=False, timeout=10)
    assert r.status_code == 200, (
        f"{path} -> {r.status_code}; a logged-out page would render unstyled "
        "(is the public nginx location missing?)"
    )
    assert kind in r.headers.get("content-type", ""), \
        f"{path} served as {r.headers.get('content-type')!r}"
    assert marker in r.text, f"{path} is not the generated theme asset"


@pytest.mark.smoke
def test_palette_list_is_served_and_complete():
    """The SPA's palette picker is built from this file, and it is the same
    file appearance.css is generated from."""
    r = httpx.get(f"{BASE_URL}/palettes.json", follow_redirects=False, timeout=10)
    assert r.status_code == 200, f"/palettes.json -> {r.status_code}"
    palettes = r.json()
    ids = [p["id"] for p in palettes]
    assert "indigo" in ids and len(ids) >= 2, ids
    css = httpx.get(f"{BASE_URL}/appearance.css", timeout=10).text
    for pid in ids:
        for mode in ("light", "dark"):
            assert f"html[data-theme='{mode}'][data-palette='{pid}']" in css, \
                f"palette {pid!r} has no {mode} block in the deployed appearance.css"


@pytest.mark.smoke
def test_login_page_uses_the_shared_theme():
    """Proves the deployed pages are the redesigned ones: they link the shared
    sheet by absolute path, and define no palette of their own."""
    r = httpx.get(f"{BASE_URL}/login", follow_redirects=True, timeout=10)
    assert r.status_code == 200
    html = r.text
    assert 'href="/appearance.css"' in html, "the login page does not load the shared theme"
    assert 'src="/theme.js"' in html
    assert "--accent:" not in html, "the login page still defines its own palette"


# ── Cookie-bearing endpoints (require SMOKE_SESSION_COOKIE) ──────────────

@pytest.mark.smoke
def test_authenticated_smoke_is_armed():
    """The guard for everything below. Each cookie-bearing test skips on its
    own when there is no cookie, and a skip exits 0, so with the secret unset
    the job reports green while half the suite never runs. That is how the
    authenticated half went dark in CI for weeks.

    So: when CI says the cookie is required, a missing one FAILS here. And
    whenever a cookie is present, it must still be a live session. An expired
    or rotated-out cookie otherwise surfaces as a scatter of unrelated 401s,
    and this names the cause instead."""
    if not COOKIE:
        if REQUIRE_AUTH:
            pytest.fail(
                "SMOKE_SESSION_COOKIE is empty, so the authenticated smoke would "
                "skip and read as green. Set the SMOKE_SESSION_COOKIE repository "
                "secret to the smoke service account's elefant_session cookie "
                "(Settings > Secrets and variables > Actions)."
            )
        pytest.skip("SMOKE_SESSION_COOKIE not set — skipping authenticated smoke")
    r = httpx.get(f"{BASE_URL}/api/me", cookies=_session_cookie(),
                  timeout=10, follow_redirects=False)
    assert r.status_code == 200, (
        f"SMOKE_SESSION_COOKIE is set but {BASE_URL} does not accept it "
        f"(/api/me answered {r.status_code}). The session has probably expired "
        f"or been revoked: sign in as the smoke service account and replace "
        f"the SMOKE_SESSION_COOKIE secret."
    )
    me = r.json()
    print(f"\n==> authenticated smoke runs as {me.get('username') or me.get('email')!r} "
          f"(role {me.get('role')!r})")


@pytest.mark.smoke
def test_compactor_admin_config_reachable():
    """The cross-service token check.  /api/admin/memory/compact/config goes
    editor -> memory-api with the internal-service header.  If the
    editor's INTERNAL_SERVICE_TOKEN env is missing OR memory-api's
    isn't either, this returns 502 'memory-api unreachable'.

    Anonymous version: editor's /api/auth/check fronts every /api/admin
    route, so without a session this returns 401, which proves only
    nginx is up.  The cookie version proves the editor -> memory-api
    path works end-to-end."""
    _need_cookie()
    r = httpx.get(
        f"{BASE_URL}/api/admin/memory/compact/config",
        cookies=_session_cookie(), timeout=15,
    )
    assert r.status_code == 200, \
        f"compactor config unreachable: {r.status_code} {r.text[:300]}"


@pytest.mark.smoke
def test_compactor_admin_stats_reachable():
    """Sibling endpoint — exercises memory-api compact/stats which goes
    through the same internal-service-token gate."""
    _need_cookie()
    r = httpx.get(
        f"{BASE_URL}/api/admin/memory/compact/stats",
        cookies=_session_cookie(), timeout=15,
    )
    assert r.status_code == 200, \
        f"compactor stats unreachable: {r.status_code} {r.text[:300]}"


@pytest.mark.smoke
def test_per_user_token_list_reachable():
    """Exercises editor's session decode + DB query.  If sessions are
    broken or the user record is gone, returns 401 not 200."""
    _need_cookie()
    r = httpx.get(
        f"{BASE_URL}/api/me/tokens",
        cookies=_session_cookie(), timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert "tokens" in body


@pytest.mark.smoke
def test_per_user_oauth_sessions_decompose_scope():
    """Confirms the PR #69 fix shipped — GET /api/me/oauth-sessions
    now includes individual perm booleans, not just the 'mcp' string."""
    _need_cookie()
    r = httpx.get(
        f"{BASE_URL}/api/me/oauth-sessions",
        cookies=_session_cookie(), timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert "sessions" in body
    # If the user has any OAuth sessions, every row must carry the
    # decomposed permission booleans (else the new edit modal can't
    # render correctly).
    for s in body["sessions"]:
        for key in ("wiki_read", "wiki_write", "memory_read", "memory_write"):
            assert key in s, f"OAuth session row missing {key}: {s}"


# ── Tenant-isolation smoke (anonymous direct probe) ─────────────────────

@pytest.mark.smoke
def test_anonymous_request_with_tenant_slug_refused():
    """Direct unauthenticated probe of the embedder via nginx.  Anyone
    can read the AI/-tagged public wiki of the default org, but a probe
    for a NAMED tenant without auth headers must NOT silently return
    that tenant's data."""
    r = httpx.get(
        f"{BASE_URL}/api/search/unified",
        params={"q": "test", "tenant_slug": "victim-corp"},
        timeout=10,
        follow_redirects=False,
    )
    # Acceptable outcomes:
    #   401 — nginx auth_request gate refusing anonymously
    #   302 — nginx redirecting to /login (cookie missing)
    #   403 — embedder _resolve_tenant_slug refusing cross-tenant
    # ALL of these mean "we did NOT return another tenant's data".
    # A 200 here is the real bug: data leak.
    assert r.status_code in (302, 401, 403), \
        f"anonymous tenant probe returned {r.status_code}: {r.text[:200]}"
    if r.status_code == 200:
        # Belt-and-suspenders: if a future nginx config makes this 200
        # with an empty body or login page, that's fine.  Actual content
        # is the bug.
        assert "victim" not in r.text.lower()


# ── nginx route coverage (anonymous) ────────────────────────────────────
# The bug class this catches: a new /api/* endpoint family ships in the
# editor (and passes every unit test, because unit tests hit the app
# directly) but nginx has no matching `location` block.  In the browser
# the request then falls through nginx's catch-all `location /` to the
# memory-ui static SPA, which serves its own 404 page — so the feature is
# dead end-to-end even though the editor code is correct.  The wiki
# History panel once 404'd this way: the editor had /api/wiki/* but
# nginx didn't route it.  A unit test can't see this; only a probe through
# the real nginx can.

# Routes that MUST be proxied to an app (not swallowed by the SPA catch-all).
# Each is a representative member of an /api/* family.  Anonymously these
# hit nginx's auth_request gate, so the *expected* answer is a redirect to
# login or a 401 — never a raw 404.  Keep this list in lockstep with the
# `location /api/...` blocks in nginx/nginx.conf: when you add a new
# /api/* family, add nginx routing AND a line here.
_NGINX_ROUTED_API_PATHS = [
    "/api/wiki/history?path=index.md",      # wiki version history (the regression)
    "/api/wiki/revision?path=index.md&id=1",
    "/api/file?path=index.md",              # core editor file read
    "/api/tree",                            # wiki tree
    "/api/directory?path=AI",               # directory listing
    "/api/me/tokens",                       # per-user token list
    "/api/admin/memory/compact/config",     # editor -> memory-api internal hop
    "/api/board/cards?repo=zennthic/elefant",  # board card repo filter
    "/api/board/people",                    # board people directory
]


# ── Feature smoke: the board card repo attribute ───────────────────────
#
# Per the house standard, a feature ships with a smoke test that drives the
# real deployed behaviour rather than a unit-level stand-in. Both tests below
# are deliberately READ-ONLY or refusal-only: a smoke suite that runs on every
# deploy must not leave a trail of scratch cards on the board it is testing.
# There is no card deletion (archive is the terminal state), so a create-and-
# clean-up test would silently accumulate archived litter forever.


@pytest.mark.smoke
def test_board_repo_filter_returns_only_that_repo():
    """The repo attribute exists so one board can carry several projects and
    still be sliced apart. This asserts the slice actually holds in the
    deployed stack: every card the filter returns really does carry that repo.

    An empty result is a legitimate pass — a board with no cards for this repo
    is not a bug, and asserting a non-empty list would couple the smoke suite
    to the contents of somebody's board.
    """
    _need_cookie()
    r = httpx.get(
        f"{BASE_URL}/api/board/cards",
        params={"repo": "zennthic/elefant"},
        cookies=_session_cookie(), timeout=15,
    )
    assert r.status_code == 200, f"board card list failed: {r.status_code} {r.text[:200]}"
    cards = r.json().get("cards", [])
    wrong = [c for c in cards if (c.get("repo") or "").lower() != "zennthic/elefant"]
    assert not wrong, (
        "the repo filter returned cards belonging to another repo (or none at "
        f"all): {[(c.get('slug'), c.get('repo')) for c in wrong][:5]}"
    )


@pytest.mark.smoke
def test_board_refuses_a_repo_that_is_not_a_repo():
    """The canonicalizer is what keeps the filter above meaningful: one
    repository must be exactly one stored value, so anything that is not a
    GitHub repo reference is refused rather than stored.

    This drives the whole path — nginx, the editor proxy passing `repo`
    through, and memory-api's validator — and it is safe to run on every
    deploy precisely BECAUSE it is refused: a 400 creates no card, so the
    test leaves nothing behind. A 201 here is the real failure, and it would
    mean junk is reaching the column and the board has grown a card that no
    filter will ever match.
    """
    _need_cookie()
    r = httpx.post(
        f"{BASE_URL}/api/board/cards",
        json={"title": "smoke: invalid repo must be refused",
              "slug": "smoke-invalid-repo-must-be-refused",
              "repo": "not a repo"},
        cookies=_session_cookie(), timeout=15,
    )
    assert r.status_code != 201, (
        "a card with a malformed repo was CREATED. The repo validator is not "
        "running in the deployed stack, so junk can reach the field and that "
        "card now matches no repo filter. Check that memory-api is on the "
        "image carrying _clean_repo and that the editor forwards `repo`."
    )
    assert r.status_code == 400, (
        f"expected 400 for a malformed repo, got {r.status_code}: {r.text[:200]}")


@pytest.mark.smoke
def test_board_cards_carry_immutable_numbers():
    """Every card the deployed board returns has a number, and they are unique.

    The number is the card's permanent handle — quoted as "#33" in a wiki page
    or a GitHub issue, and the thing its permalink is built from. Two failures
    are worth catching on the real stack rather than in a unit test:

      missing    the startup backfill did not run, so existing cards have no
                 number and the UI renders no chip. A unit test cannot see
                 this: the backfill only fires against a real database, once.
      duplicate  two cards share a number, which means the counter was reset
                 or rebuilt from the cards table. Every reference to the older
                 one silently now reads as the newer.

    Read-only, so it leaves no trace on the board it is testing.
    """
    _need_cookie()
    r = httpx.get(
        f"{BASE_URL}/api/board/cards",
        cookies=_session_cookie(), timeout=15,
    )
    assert r.status_code == 200, f"board cards failed: {r.status_code} {r.text[:200]}"
    cards = r.json().get("cards", [])
    if not cards:
        pytest.skip("no cards on this board to check")

    missing = [c.get("slug") for c in cards if c.get("number") is None]
    assert not missing, (
        f"{len(missing)} card(s) have no number, e.g. {missing[:5]}. The "
        f"startup backfill did not run — check memory-api is on the image "
        f"carrying _backfill_card_numbers and that it reached the database."
    )

    numbers = [c["number"] for c in cards]
    dupes = {n for n in numbers if numbers.count(n) > 1}
    assert not dupes, (
        f"card numbers {sorted(dupes)} are used by more than one card. The "
        f"counter has been reset or is being derived from the cards table; "
        f"every existing reference to those numbers is now ambiguous."
    )


# ── Feature smoke: the card due date ──────────────────────────────────
#
# Read-only or refusal-only, like the repo smoke above, for the same reason:
# a suite that runs on every deploy must not leave scratch cards behind.


@pytest.mark.smoke
def test_board_overdue_filter_returns_only_late_open_cards():
    """`overdue` is the filter a person or an agent reaches for to ask "what
    is late". Every card it returns must carry a due date before today and
    must not be closed. An empty answer is a pass: a board with nothing late
    is not a bug. A 200 whose cards carry no `due` at all means the editor
    dropped the filter, or memory-api is on an image without the field."""
    _need_cookie()
    r = httpx.get(
        f"{BASE_URL}/api/board/cards",
        params={"overdue": "true"},
        cookies=_session_cookie(), timeout=15,
    )
    assert r.status_code == 200, f"overdue filter failed: {r.status_code} {r.text[:200]}"
    import datetime as _dt
    # A day of slack either side, since the server decides "today" in UTC.
    tomorrow = (_dt.datetime.now(_dt.timezone.utc).date() + _dt.timedelta(days=1)).isoformat()
    wrong = [(c.get("slug"), c.get("due")) for c in r.json().get("cards", [])
             if not c.get("due") or c["due"] >= tomorrow or c.get("closed")]
    assert not wrong, f"the overdue filter returned cards that are not overdue: {wrong[:5]}"


@pytest.mark.smoke
def test_board_due_window_filter_holds():
    """`due_after` / `due_before` slice by date, inclusive, and leave out
    cards with no due date."""
    _need_cookie()
    r = httpx.get(
        f"{BASE_URL}/api/board/cards",
        params={"due_after": "2000-01-01", "due_before": "2999-12-31"},
        cookies=_session_cookie(), timeout=15,
    )
    assert r.status_code == 200, f"due window filter failed: {r.status_code} {r.text[:200]}"
    undated = [c.get("slug") for c in r.json().get("cards", []) if not c.get("due")]
    assert not undated, (
        "the due-date window returned cards with no due date, so the filter is "
        f"not reaching memory-api: {undated[:5]}")


@pytest.mark.smoke
def test_board_refuses_a_due_date_that_is_not_a_date():
    """A due date is a real calendar day or nothing. Refused rather than
    stored, so this creates no card; a 201 is the failure."""
    _need_cookie()
    r = httpx.post(
        f"{BASE_URL}/api/board/cards",
        json={"title": "smoke: invalid due date must be refused",
              "slug": "smoke-invalid-due-date-must-be-refused",
              "due": "next friday"},
        cookies=_session_cookie(), timeout=15,
    )
    assert r.status_code != 201, (
        "a card with a malformed due date was CREATED. Either the editor is "
        "not forwarding `due`, or memory-api is on an image without _clean_due.")
    assert r.status_code == 400, (
        f"expected 400 for a malformed due date, got {r.status_code}: {r.text[:200]}")


# ── Feature smoke: the card people attributes ──────────────────────────
#
# Read-only or refusal-only, for the same reason as the repo tests above: a
# suite that runs on every deploy must not leave scratch cards on the board it
# is testing, and archive is the terminal state so there is no cleaning up.


@pytest.mark.smoke
def test_board_people_directory_resolves_real_accounts():
    """The directory is the promise the whole attribute rests on: what the
    picker offers is what the store will accept.

    Two failures worth catching on the real stack. An EMPTY directory means the
    editor resolved the session's tenant to an org with no members, which
    cannot be true of the org whose cookie just authenticated — the usual cause
    is the board and the identity tables disagreeing about which tenant this
    is. A directory whose entries carry no reference is one the pickers cannot
    submit and the store cannot match.
    """
    _need_cookie()
    r = httpx.get(f"{BASE_URL}/api/board/people",
                  cookies=_session_cookie(), timeout=15)
    assert r.status_code == 200, f"people directory failed: {r.status_code} {r.text[:200]}"
    people = r.json().get("people", [])
    assert people, (
        "the board's people directory is empty, but the session that just "
        "authenticated belongs to an org with at least one member. The board "
        "and the identity tables are disagreeing about which tenant this is."
    )
    bad = [p for p in people if not (p.get("ref") or "").split(":")[0] in ("user", "agent")]
    assert not bad, (
        f"{len(bad)} directory entries carry no principal reference, e.g. "
        f"{[p.get('display') for p in bad][:5]}. A picker cannot submit these "
        f"and the store cannot match them."
    )


@pytest.mark.smoke
def test_board_refuses_a_reporter_who_is_not_a_real_account():
    """A card's people are resolved accounts, not free text — so a name that
    matches nobody is refused rather than stored.

    Safe on every deploy precisely BECAUSE it is refused: a 400 creates no
    card, so the test leaves nothing behind. A 201 here is the real failure,
    and it means the field has quietly become free text with a colon in it —
    a card claiming a person no query can resolve, which is worse than a card
    naming nobody, because a blank field is visibly missing and a junk one is
    not.
    """
    _need_cookie()
    r = httpx.post(
        f"{BASE_URL}/api/board/cards",
        json={"title": "smoke: unresolvable reporter must be refused",
              "slug": "smoke-unresolvable-reporter-must-be-refused",
              "reporter": "no-such-person-smoke-test"},
        cookies=_session_cookie(), timeout=15,
    )
    assert r.status_code != 201, (
        "a card naming a person who does not exist was CREATED. The resolver "
        "is not running in the deployed stack — check that memory-api can "
        "reach the editor's /api/internal/board/people with a matching "
        "INTERNAL_SERVICE_TOKEN."
    )
    assert r.status_code == 400, (
        f"expected 400 for an unresolvable reporter, got {r.status_code}: {r.text[:200]}")


@pytest.mark.smoke
def test_board_cards_name_their_people_rather_than_only_referencing_them():
    """Every reference a card carries comes back with a display record.

    The card stores an opaque reference and the NAME is resolved at read time,
    so this is the join that has to hold across two services and two databases.
    When it breaks, nothing errors — the board simply renders `user:9f2c…` on
    every chip, which is the unreadable thing the whole attribute replaced.

    Read-only, and an empty board is a legitimate pass.
    """
    _need_cookie()
    r = httpx.get(f"{BASE_URL}/api/board/cards",
                  cookies=_session_cookie(), timeout=15)
    assert r.status_code == 200, f"board cards failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    cards, people = data.get("cards", []), data.get("people", {})
    refs = {ref for c in cards
            for ref in [c.get("reporter") or ""] + list(c.get("participants") or []) if ref}
    if not refs:
        pytest.skip("no cards on this board name anybody yet")
    unnamed = [ref for ref in refs if not (people.get(ref) or {}).get("display")]
    assert not unnamed, (
        f"{len(unnamed)} principal reference(s) came back with no display "
        f"record, e.g. {sorted(unnamed)[:5]}. The editor's people enrichment "
        f"is not running, so the board will render raw references."
    )


@pytest.mark.smoke
def test_board_touch_drag_must_be_armed_by_hold():
    """The deployed boards.js arms a touch drag by press-and-hold.

    On a phone, a column taller than the screen could not be scrolled: the
    finger drag was read as a card move, so scrolling reordered cards and a
    sideways slip changed a card's column — which is a stage change, and the
    dispatch loop reads column position as a scope signal.  The fix is
    entirely client-side, so no API call can prove it landed; what CAN be
    checked after a deploy is that the asset nginx is actually serving is the
    fixed one.  A stale cached layer or a memory-ui image that did not
    rebuild would leave the old gesture live with everything else green.

    A real touch gesture is out of reach from here — that part still needs a
    human on a real device, which is why the hold duration was tuned by feel
    rather than derived.
    """
    _need_cookie()
    r = httpx.get(f"{BASE_URL}/boards.js", cookies=_session_cookie(), timeout=15)
    assert r.status_code == 200, (
        f"/boards.js did not serve: {r.status_code} {r.text[:200]}")
    body = r.text
    # memory-ui ships as a plain nginx static copy with no runtime build step,
    # so the served file is the source file and these are literal.  They are
    # identifiers rather than values on purpose: the hold duration and wobble
    # tolerance still want tuning against a real device, and a smoke test that
    # pinned `400` would fail on the tuning rather than on a bad deploy.
    for marker in ("delayOnTouchOnly: true", "touchStartThreshold", "TOUCH_ARM_MS"):
        assert marker in body, (
            f"the deployed boards.js has no `{marker}` — the served asset "
            f"predates the touch-arming fix, so dragging to scroll a tall "
            f"column still moves cards. memory-ui did not rebuild, or nginx "
            f"is serving a stale layer."
        )


@pytest.mark.smoke
def test_wiki_reader_anchors_headings_and_builds_a_toc():
    """The deployed wiki reader gives every heading an anchor.

    `page.md#heading` links, the hover "#" beside a heading and the table of
    contents all live in memory-ui's index.html, and no API call exercises
    them, so what can be checked after a deploy is that the page nginx serves
    is the one that carries them. The slug rule itself is pinned by the unit
    suites (editor, mcp, memory-ui) against tests/fixtures/heading-anchors.json.
    """
    _need_cookie()
    r = httpx.get(f"{BASE_URL}/", cookies=_session_cookie(), timeout=15,
                  follow_redirects=False)
    assert r.status_code == 200, (
        f"the wiki app did not serve: {r.status_code} {r.text[:200]}")
    body = r.text
    for marker in ("function headingSlug", "function addHeadingAnchors",
                   "function scrollToWikiAnchor", "function renderWikiToc",
                   "'wiki-h-'", ".heading-anchor::before"):
        assert marker in body, (
            f"the deployed index.html has no `{marker}` — the served app "
            f"predates heading anchors, so `page.md#heading` links land at "
            f"the top of the page and there is no table of contents. "
            f"memory-ui did not rebuild, or nginx is serving a stale layer."
        )


@pytest.mark.smoke
def test_wiki_page_header_pins_flush_to_the_top():
    """The deployed wiki page header sticks to the pane's top edge.

    A viewer holding the bar drops its top padding and the bar sticks at a
    plain `top: 0`, so no strip is left above it for page text to show
    through. The earlier negative-offset fix still left a few pixels. The fix is CSS in memory-ui's index.html; the geometry
    is pinned by memory-ui/test/wiki-page-header.mjs, so what is checked after
    a deploy is that nginx serves the page carrying it.
    """
    _need_cookie()
    r = httpx.get(f"{BASE_URL}/", cookies=_session_cookie(), timeout=15,
                  follow_redirects=False)
    assert r.status_code == 200, (
        f"the wiki app did not serve: {r.status_code} {r.text[:200]}")
    body = r.text
    for marker in ("#tab-wiki #wiki-viewer:has(> .wiki-sticky-bar) { padding-top: 0; }",
                   "box-shadow: 0 -8px 0 var(--bg);"):
        assert marker in body, (
            f"the deployed index.html has no `{marker}` — the served app "
            f"predates the sticky header fix, so page text still shows above "
            f"the pinned Edit/Share/Lock bar. memory-ui did not rebuild, or "
            f"nginx is serving a stale layer."
        )


@pytest.mark.smoke
def test_board_card_editor_accepts_a_newline_and_a_pasted_screenshot():
    """The deployed boards.js lets Enter through, and saves what code writes.

    Issue #142.  Three input bugs in the card editor, all of them
    invisible to the API:

      * the inline-mention typeahead consumed EVERY Enter, so the description
        textarea could never reach a second line — and therefore could not hold
        a bullet list, a numbered list or a fenced block either, which is how
        one bug arrived as four reports;
      * a pasted screenshot was written with `textarea.value = …`, which fires
        no `input` event, and `input` is the only thing that schedules the
        auto-save, so the upload succeeded and the markdown was discarded on
        close;
      * the comment composer had no paste handler at all.

    Same shape and the same reasoning as the touch-drag check above: the fix is
    entirely client-side, so no API call can prove it landed.  What can be
    checked after a deploy is that the asset nginx actually serves is the fixed
    one — a memory-ui image that did not rebuild, or a stale nginx layer, would
    leave all three live with the rest of the pipeline green.

    The behaviour itself is covered by memory-ui/test/card-editor-input.mjs,
    which drives the real handlers in jsdom.  This only asks whether that file
    is the one being served.
    """
    _need_cookie()
    r = httpx.get(f"{BASE_URL}/boards.js", cookies=_session_cookie(), timeout=15)
    assert r.status_code == 200, (
        f"/boards.js did not serve: {r.status_code} {r.text[:200]}")
    body = r.text

    # memory-ui ships as a plain nginx static copy with no runtime build step,
    # so the served file IS the source file and these are literal.  Each is a
    # line that only the fixed handler has; the tempting check — "no
    # preventDefault straight after the Enter branch" — matches the wiki-link
    # input too, which is a single-line field where consuming Enter is correct.
    for marker, why in (
        ("const it = open && active >= 0 ? items[active] : null;",
         "the typeahead still acts on Enter with its list closed, which is the "
         "unconditional preventDefault() that left the card description unable "
         "to reach a second line"),
        ("if (o.onEnter) { e.preventDefault(); close(); return o.onEnter(); }",
         "Enter is not falling through to the browser on a field that never "
         "asked for it, so a textarea still gets no newline"),
        ("fireInput",
         "a programmatic write to a card field fires no input event, so a "
         "pasted screenshot and a picked @mention are still lost on close"),
        ("$('b-ec-comment-body')?.addEventListener('paste'",
         "the comment composer is still not wired for a pasted image"),
    ):
        assert marker in body, (
            f"the deployed boards.js has no `{marker}` — {why}. memory-ui did "
            f"not rebuild, or nginx is serving a stale layer."
        )


@pytest.mark.smoke
@pytest.mark.parametrize("path", _NGINX_ROUTED_API_PATHS)
def test_api_route_reaches_app_through_nginx(path):
    """Anonymous probe of each /api/* family through the real nginx.

    A 404 here is the bug: nginx has no `location` for this prefix, so the
    request fell through to the memory-ui SPA catch-all (which returns its
    own 404 page).  The fix is a `location` block in nginx/nginx.conf, not
    anything in the editor.  Any of 302/401/403 means nginx routed it to
    the app and the auth gate answered — which is all this test asserts.
    """
    r = httpx.get(f"{BASE_URL}{path}", follow_redirects=False, timeout=10)
    assert r.status_code != 404, (
        f"{path} returned 404 — nginx has no `location` for it, so the "
        f"request fell through to the SPA catch-all.  Add a `location` "
        f"block in nginx/nginx.conf (mirror the /api/file block).  "
        f"Body: {r.text[:200]}"
    )
    # Auth-gated routes redirect to login (302) or refuse (401/403)
    # anonymously.  We don't assert a single code because different
    # families gate differently (auth_request 401 vs. @login_redirect
    # 302 vs. tenant 403) — the point is purely "nginx routed it, the app
    # answered, it is NOT a stray 404".
    assert r.status_code in (302, 401, 403), (
        f"{path} should be auth-gated through nginx (302 login / 401 / "
        f"403), got {r.status_code}: {r.text[:200]}"
    )


# ── Feature smoke: the org admin console loads (CQ-04 phase 5) ───────────
#
# Phase 5 moved users, teams, groups and the org's domains out of the
# editor's main.py into routers. The route table tests prove the routes are
# still declared; this proves the deployed console can still load them.
# Read-only. It needs an admin session: an ordinary user is refused these
# routes by design, so it skips rather than fails when the smoke cookie is not
# an admin's.


@pytest.mark.smoke
def test_org_admin_console_loads_users_teams_and_groups():
    _need_cookie()
    who = httpx.get(f"{BASE_URL}/api/admin/whoami", cookies=_session_cookie(), timeout=10)
    assert who.status_code == 200, f"whoami: {who.status_code} {who.text[:200]}"
    if who.json().get("role") not in ("org_admin", "platform_admin"):
        pytest.skip("SMOKE_SESSION_COOKIE is not an org or platform admin's session")
    for path, key in (("/api/admin/users", "users"), ("/api/admin/teams", "teams"),
                      ("/api/admin/groups", "groups"), ("/api/admin/org/domains", "domains")):
        r = httpx.get(f"{BASE_URL}{path}", cookies=_session_cookie(), timeout=10)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"
        assert isinstance(r.json().get(key), list), f"{path} has no `{key}` list: {r.text[:200]}"


# ── Feature smoke: the git sources admin page loads (CQ-04 phase 6) ──────
#
# Phase 6 moved the /api/admin/git-sources routes out of the editor's main.py
# into routers/git_sources.py. The route table tests prove the routes are
# still declared; this proves the deployed admin page can still list the
# org's sources and read their live sync state from the embedder. Read-only.
# It needs an admin session, and skips rather than fails without one.


@pytest.mark.smoke
def test_git_sources_admin_lists_sources_and_sync_state():
    _need_cookie()
    who = httpx.get(f"{BASE_URL}/api/admin/whoami", cookies=_session_cookie(), timeout=10)
    assert who.status_code == 200, f"whoami: {who.status_code} {who.text[:200]}"
    if who.json().get("role") not in ("org_admin", "platform_admin"):
        pytest.skip("SMOKE_SESSION_COOKIE is not an org or platform admin's session")
    r = httpx.get(f"{BASE_URL}/api/admin/git-sources", cookies=_session_cookie(), timeout=10)
    assert r.status_code == 200, f"/api/admin/git-sources: {r.status_code} {r.text[:200]}"
    sources = r.json().get("sources")
    assert isinstance(sources, list), f"no `sources` list: {r.text[:200]}"
    st = httpx.get(f"{BASE_URL}/api/admin/git-sources/status",
                   cookies=_session_cookie(), timeout=10)
    assert st.status_code == 200, f"/api/admin/git-sources/status: {st.status_code} {st.text[:200]}"
    assert isinstance(st.json(), dict), f"sync state is not an object: {st.text[:200]}"
    ids = {s["id"] for s in sources}
    stray = sorted(set(st.json()) - ids)
    assert not stray, f"sync state names sources outside this org: {stray[:5]}"


# ── Feature smoke: local sign-in and API tokens (CQ-04 phase 7) ─────────
#
# Phase 7 moved login, MFA, the invite flow and the API token routes out of
# the editor's main.py into routers/local_auth.py and routers/tokens.py. The
# route table tests prove the routes are still declared; these prove the
# deployed stack still reaches them through nginx and they still behave.


@pytest.mark.smoke
def test_local_sign_in_routes_still_answer():
    """Anonymous and side-effect free: a wrong password is refused, and the
    MFA pages send a visitor with no pending login back to /login."""
    user = f"smoke-nobody-{int(time.time())}@invalid.test"
    r = httpx.post(f"{BASE_URL}/login", data={"username": user, "password": "not-a-password"},
                   follow_redirects=False, timeout=15)
    assert r.status_code == 401, f"POST /login: {r.status_code} {r.text[:200]}"
    assert "Invalid username or password" in r.text
    for path in ("/mfa", "/setup-mfa"):
        r = httpx.get(f"{BASE_URL}{path}", follow_redirects=False, timeout=10)
        assert r.status_code == 302, f"{path}: {r.status_code} {r.text[:200]}"
        assert "/login" in r.headers.get("location", ""), f"{path} -> {r.headers.get('location')}"


@pytest.mark.smoke
def test_self_service_token_create_edit_and_revoke():
    """Mints a scope-less personal token, widens one scope, then revokes it.
    The token expires in a day and is revoked in `finally`, so a failed run
    leaves nothing usable behind."""
    _need_cookie()
    from urllib.parse import urlparse
    u = urlparse(BASE_URL)
    headers = {"Origin": f"{u.scheme}://{u.netloc}"}
    kw = dict(cookies=_session_cookie(), headers=headers, timeout=15)
    body = {"name": "smoke: CQ-04 phase 7 (auto-revoked)", "expires_days": 1,
            "memory_read": False, "memory_write": False, "wiki_read": False,
            "wiki_write": False, "graph_read": False,
            "board_read": False, "board_write": False}
    r = httpx.post(f"{BASE_URL}/api/me/tokens", json=body, **kw)
    assert r.status_code == 201, f"create: {r.status_code} {r.text[:200]}"
    tok = r.json()
    try:
        assert tok.get("token"), "the raw token was not returned once on create"
        assert tok.get("source") == "user_self_service"
        r = httpx.patch(f"{BASE_URL}/api/me/tokens/{tok['id']}", json={"board_read": True}, **kw)
        assert r.status_code == 200, f"edit: {r.status_code} {r.text[:200]}"
        assert r.json().get("board_read") is True
        assert r.json().get("wiki_read") is False
    finally:
        r = httpx.delete(f"{BASE_URL}/api/me/tokens/{tok['id']}", **kw)
    assert r.status_code == 204, f"revoke: {r.status_code} {r.text[:200]}"
    listed = httpx.get(f"{BASE_URL}/api/me/tokens", cookies=_session_cookie(), timeout=10)
    assert tok["id"] not in {t["id"] for t in listed.json()["tokens"]}


# ── Feature smoke: git sources report a real index (#136) ────────────────
#
# The failure this covers was invisible to every other check in the suite:
# sync recorded success, the containers were healthy, CI was green, and 13 of
# 14 repos were silently unsearchable. What made it undetectable was that the
# two signals an agent actually uses, the file count and the file tree, were
# both derived from a clone path the reader could not resolve, so "nothing
# there" and "cannot find it" came back looking identical.
#
# These drive the deployed endpoints an agent's list_sources and source_tree
# really go through, and are read-only.


@pytest.mark.smoke
def test_git_sources_carry_an_org_slug():
    """The field whose absence caused the outage.

    Clones live at /git-sources/<org_slug>/<source_id>/. Without org_slug on
    the payload, source_tree looked under /git-sources/default/<id>, missed,
    and reported a cloned repo as never synced. Asserting the field is present
    catches a regression at the point it is introduced rather than after a
    sync migrates the next clone out of the legacy path.
    """
    _need_cookie()
    r = httpx.get(f"{BASE_URL}/embedder/api/git-sources",
                  cookies=_session_cookie(), timeout=20)
    assert r.status_code == 200, f"git source list failed: {r.status_code} {r.text[:200]}"
    sources = r.json().get("sources", [])
    if not sources:
        pytest.skip("no git sources configured on this deployment")
    missing = [s.get("name") or s.get("id") for s in sources if not s.get("org_slug")]
    assert not missing, (
        f"these sources came back with no org_slug: {missing[:5]}. "
        f"/api/internal/git-sources has stopped emitting the field, so the "
        f"embedder cannot resolve their clones, so source_tree will answer "
        f"'Source has not been synced yet' for repos that are cloned and current."
    )


@pytest.mark.smoke
def test_a_synced_git_source_can_have_its_tree_read():
    """The end-to-end shape of the bug: a source reporting a successful sync
    whose tree could not be read.

    Only sources that claim to have synced are checked. A source that has
    genuinely never synced is entitled to 404, and asserting otherwise would
    fail on a freshly provisioned stack rather than on a regression.
    """
    _need_cookie()
    r = httpx.get(f"{BASE_URL}/embedder/api/git-sources",
                  cookies=_session_cookie(), timeout=20)
    assert r.status_code == 200, f"git source list failed: {r.status_code} {r.text[:200]}"
    synced = [s for s in r.json().get("sources", []) if s.get("last_synced_at")]
    if not synced:
        pytest.skip("no synced git sources on this deployment")

    unreadable = []
    for s in synced:
        t = httpx.get(f"{BASE_URL}/embedder/api/git-sources/{s['id']}/tree",
                      cookies=_session_cookie(), timeout=30)
        if t.status_code != 200:
            unreadable.append((s.get("name"), t.status_code, t.text[:120]))
    assert not unreadable, (
        "these sources report a successful sync but their checkout cannot be "
        f"read back: {unreadable[:5]}. That is the #136 shape: the clone is "
        f"on the volume and the reader is looking in the wrong directory."
    )


@pytest.mark.smoke
def test_a_synced_git_source_reports_the_files_it_has():
    """file_count must be the source's total in the index, not the delta of
    whatever the last sync happened to touch.

    Reporting the delta is why 13 healthy repos advertised `files: 0`: any
    sync with no upstream changes indexes nothing and used to report exactly
    that. A source whose tree has files and whose sync succeeded must not come
    back claiming zero.
    """
    _need_cookie()
    r = httpx.get(f"{BASE_URL}/embedder/api/git-sources",
                  cookies=_session_cookie(), timeout=20)
    assert r.status_code == 200, f"git source list failed: {r.status_code} {r.text[:200]}"
    synced = [s for s in r.json().get("sources", []) if s.get("last_synced_at")]
    if not synced:
        pytest.skip("no synced git sources on this deployment")

    empty = []
    for s in synced:
        t = httpx.get(f"{BASE_URL}/embedder/api/git-sources/{s['id']}/tree",
                      cookies=_session_cookie(), timeout=30)
        if t.status_code != 200:
            continue                      # covered by the tree test above
        if t.json().get("file_count", 0) > 0 and not (s.get("file_count") or 0):
            empty.append((s.get("name"), s.get("file_count")))
    assert not empty, (
        f"these sources have files in their checkout but report none: {empty[:5]}. "
        f"Either file_count has gone back to reporting the per-run delta, or "
        f"the sync is recording success without indexing anything."
    )


# ── Feature smoke: an agent can embed an image in a page (#328) ──────────
#
# The read half (get_wiki_page inlining images) already existed; the card
# added the write half. This drives the deployed MCP endpoint exactly as an
# agent does: upload a PNG, write a page that references it with the path
# the tool hands back, and read the page back with the image inline. The
# page, the image and the token are all removed afterwards.

_PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
            "DwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _mcp_result(r: httpx.Response) -> dict:
    """A JSON-RPC reply, whether it came back as JSON or as an SSE stream."""
    assert r.status_code == 200, f"MCP: {r.status_code} {r.text[:200]}"
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        import json as _json
        for line in r.text.splitlines():
            if line.startswith("data:"):
                msg = _json.loads(line[5:].strip())
                if "result" in msg or "error" in msg:
                    return msg
        raise AssertionError(f"no JSON-RPC reply in the stream: {r.text[:200]}")
    return r.json()


class _Mcp:
    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}",
                  "Accept": "application/json, text/event-stream"}
        self.n = 0
        init = self._rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "elefant-smoke", "version": "1"}})
        assert "result" in init, f"initialize: {init}"
        if init.get("_sid"):
            self.h["mcp-session-id"] = init["_sid"]
        httpx.post(f"{BASE_URL}/mcp", headers=self.h, timeout=15,
                   json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _rpc(self, method: str, params: dict) -> dict:
        self.n += 1
        r = httpx.post(f"{BASE_URL}/mcp", headers=self.h, timeout=30,
                       json={"jsonrpc": "2.0", "id": self.n, "method": method,
                             "params": params})
        msg = _mcp_result(r)
        msg["_sid"] = r.headers.get("mcp-session-id")
        return msg

    def call(self, name: str, **args) -> str:
        msg = self._rpc("tools/call", {"name": name, "arguments": args})
        assert "result" in msg, f"{name}: {msg.get('error')}"
        return "".join(c.get("text", "") for c in msg["result"].get("content", []))


@pytest.mark.smoke
def test_an_agent_can_upload_an_image_and_read_it_back_inline():
    _need_cookie()
    import secrets
    from urllib.parse import urlparse
    u = urlparse(BASE_URL)
    kw = dict(cookies=_session_cookie(), headers={"Origin": f"{u.scheme}://{u.netloc}"},
              timeout=15)
    body = {"name": "smoke: wiki_upload_image (auto-revoked)", "expires_days": 1,
            "memory_read": False, "memory_write": False, "wiki_read": True,
            "wiki_write": True, "graph_read": False,
            "board_read": False, "board_write": False}
    r = httpx.post(f"{BASE_URL}/api/me/tokens", json=body, **kw)
    assert r.status_code == 201, f"token create: {r.status_code} {r.text[:200]}"
    tok = r.json()
    folder = f"smoke-upload-image-{secrets.token_hex(4)}"
    mcp = None
    try:
        mcp = _Mcp(tok["token"])
        out = mcp.call("wiki_upload_image", path=f"{folder}/assets/pixel.png",
                       image_base64=_PNG_1PX, content_type="image/png",
                       page=f"{folder}/page.md")
        assert "✅" in out, out
        assert "![pixel](assets/pixel.png)" in out, out
        out = mcp.call("wiki_write", path=f"{folder}/page.md",
                       content="# Smoke\n\n![pixel](assets/pixel.png)\n")
        assert "✅" in out, out
        page = mcp.call("get_wiki_page", path=f"{folder}/page.md")
        assert f"![pixel](data:image/png;base64,{_PNG_1PX})" in page, page[:400]

        # SVG stays refused end to end: it would be served from our origin.
        out = mcp.call("wiki_upload_image", path=f"{folder}/assets/x.svg",
                       image_base64="PHN2Zz48L3N2Zz4=", content_type="image/svg+xml")
        assert "415" in out, out
    finally:
        if mcp is not None:
            try:
                mcp.call("wiki_delete", path=folder, recursive=True)
            except Exception:
                pass
        httpx.delete(f"{BASE_URL}/api/me/tokens/{tok['id']}", **kw)


# ── Feature smoke: the MCP diagram surface (card #411) ───────────────────
#
# The card's acceptance, end to end against the deployed stack and its
# diagram-renderer: create a flowchart and get an image back; patch one label
# and see a one-line change in the page's history; revert and render again;
# and a broken source comes back with its line number. Stage runs the
# renderer (COMPOSE_PROFILES=diagrams in the deploy job), so a missing one is
# a failure here, not a skip. Card #412 adds the always-on diagram-validator:
# a mistake after front matter is reported at its line in the source as sent,
# and the structured result says `saved` and `validated`. The page and the
# token are removed afterwards.

_FLOW = "flowchart TD\n  A[Log in] --> B[Check password]\n  B --> C[Home]"


def _result(mcp: "_Mcp", name: str, **args) -> dict:
    msg = mcp._rpc("tools/call", {"name": name, "arguments": args})
    assert "result" in msg, f"{name}: {msg.get('error')}"
    return msg["result"]


def _content(mcp: "_Mcp", name: str, **args) -> list:
    return _result(mcp, name, **args).get("content", [])


def _text_of(blocks: list) -> str:
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _png_in(blocks: list) -> bool:
    import base64
    imgs = [b for b in blocks if b.get("type") == "image" and b.get("mimeType") == "image/png"]
    return bool(imgs) and base64.b64decode(imgs[0]["data"])[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.smoke
def test_an_agent_can_create_patch_revert_and_render_a_diagram():
    _need_cookie()
    import re
    import secrets
    from urllib.parse import urlparse
    u = urlparse(BASE_URL)
    kw = dict(cookies=_session_cookie(), headers={"Origin": f"{u.scheme}://{u.netloc}"},
              timeout=15)
    body = {"name": "smoke: diagram tools (auto-revoked)", "expires_days": 1,
            "memory_read": False, "memory_write": False, "wiki_read": True,
            "wiki_write": True, "graph_read": False,
            "board_read": False, "board_write": False}
    r = httpx.post(f"{BASE_URL}/api/me/tokens", json=body, **kw)
    assert r.status_code == 201, f"token create: {r.status_code} {r.text[:200]}"
    tok = r.json()
    folder = f"smoke-diagram-{secrets.token_hex(4)}"
    mcp = None
    try:
        mcp = _Mcp(tok["token"])

        # 1. create a flowchart, get a rendered image back
        res = _result(mcp, "diagram_create", name=f"{folder}/flow", source=_FLOW,
                      title="Smoke flow")
        assert res.get("structuredContent", {}).get("validated") is True, res.get("structuredContent")
        out = res.get("content", [])
        text = _text_of(out)
        assert "✅ Created diagram" in text, text
        assert "Validated by Mermaid" in text, f"the renderer did not validate it: {text}"
        assert _png_in(out), f"no PNG came back: {[b.get('type') for b in out]}"
        path = re.search(r"at `([^`]+\.md)`", text).group(1)
        version = re.search(r"version `([0-9a-f]{12})`", text).group(1)

        # 2. patch one label; the page's history shows a one-line change
        out = _content(mcp, "diagram_patch", name=path, if_version=version, edits=[
            {"op": "replace", "old": "B[Check password]", "new": "B[Check password and MFA]"}])
        assert "✅ Patched diagram" in _text_of(out), _text_of(out)
        assert _png_in(out), "no PNG came back from the patch"
        hist = mcp.call("wiki_history", path=path)
        revs = re.findall(r"revision `(\d+)`", hist)
        assert len(revs) >= 2, hist
        newest, first = revs[0], revs[-1]
        diff = mcp.call("wiki_diff", path=path, revision_id=int(first), against_id=int(newest))
        changed = [l for l in diff.splitlines()
                   if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        assert changed == ["-  A[Log in] --> B[Check password]",
                           "+  A[Log in] --> B[Check password and MFA]"], diff

        # A stale version is refused, and nothing is written.
        out = _content(mcp, "diagram_patch", name=path, if_version=version, edits=[
            {"op": "replace", "old": "C[Home]", "new": "C[Dashboard]"}])
        assert "Version conflict" in _text_of(out), _text_of(out)

        # 3. revert restores the previous version, and it renders again
        out = mcp.call("wiki_revert", path=path, revision_id=int(first))
        assert "✅" in out, out
        src = mcp.call("diagram_get", name=path)
        assert "B[Check password]" in src and "MFA" not in src, src
        assert _png_in(_content(mcp, "diagram_render", name=path)), "revert did not re-render"

        # 4. a broken source comes back with its line number, and is not saved
        out = _content(mcp, "diagram_create", name=f"{folder}/broken",
                       source="flowchart TD\n  A --> B[\n  B --> C")
        assert "at line 2 of the source" in _text_of(out), _text_of(out)
        gone = mcp.call("diagram_get", name=path.replace("/flow.md", "/broken.md"))
        assert "No diagram at" in gone, gone

        # 5. after front matter, the line is still the one in the source as sent
        res = _result(mcp, "diagram_create", name=f"{folder}/broken2",
                      source="---\ntitle: T\n---\nflowchart TD\n  A --> B[\n  B --> C")
        assert "at line 5 of the source" in _text_of(res.get("content", [])), res
        sc = res.get("structuredContent") or {}
        assert sc.get("saved") is False and sc.get("validated") is True, sc
        assert (sc.get("error") or {}).get("line") == 5, sc
    finally:
        if mcp is not None:
            try:
                mcp.call("wiki_delete", path=folder, recursive=True)
            except Exception:
                pass
        httpx.delete(f"{BASE_URL}/api/me/tokens/{tok['id']}", **kw)
