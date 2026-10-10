"""Board people: the directory, resolution, and the display records a card's
reporter and participants render as.

The editor owns identity, so this module owns the promise the whole attribute
rests on: a card's people are REAL accounts. That means resolution is strict
(an unresolvable name is refused, an ambiguous one is refused rather than
guessed) while description is total (a reference always comes back with
something to render, including one whose account is gone).

Exercised against a real config DB — these are joins over user_accounts and
api_tokens, and a mocked session would assert the mock rather than the query.
"""
import re
import uuid

import pytest

import config_db
import main
from served_routes import served_paths
from routers import boards
import people as board_people


ORG_SLUG = "people-test-org"


@pytest.fixture
def org(monkeypatch):
    """A tenant with two members, an agent token, and a token linked to a user.

    Torn down by id rather than by truncating: other modules share this
    session-scoped SQLite file, and a test that empties the tables would break
    whichever ran after it.
    """
    session = next(config_db.get_db_session())
    tenant = config_db.Tenant(id=str(uuid.uuid4()), name="People Test", slug=ORG_SLUG)
    session.add(tenant)
    jon = config_db.UserAccount(
        id=str(uuid.uuid4()), org_id=tenant.id, username="jon",
        full_username=f"jon@{ORG_SLUG}.local", display_name="Jon Smith",
        email="jon@example.com", firstname="Jon", lastname="Smith")
    jan = config_db.UserAccount(
        id=str(uuid.uuid4()), org_id=tenant.id, username="jan",
        full_username=f"jan@{ORG_SLUG}.local", firstname="Jan", lastname="Novak",
        is_active=False)
    routine = config_db.ApiToken(
        id=str(uuid.uuid4()), name="Dispatch routine", token_hash=uuid.uuid4().hex * 2,
        tenant_id=tenant.id, source="claude_code")
    jons_laptop = config_db.ApiToken(
        id=str(uuid.uuid4()), name="Jon's laptop", token_hash=uuid.uuid4().hex * 2,
        tenant_id=tenant.id, user_id=jon.id, source="claude_code")
    session.add_all([jon, jan, routine, jons_laptop])
    session.commit()
    yield {"session": session, "tenant": tenant, "jon": jon, "jan": jan,
           "routine": routine, "jons_laptop": jons_laptop}
    for row in (jon, jan, routine, jons_laptop, tenant):
        session.delete(row)
    session.commit()
    session.close()


# ── reference shape ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("user:abc123", ("user", "abc123")),
    ("agent:abc123", ("agent", "abc123")),
    ("USER:abc123", ("user", "abc123")),
    ("  user:abc123  ", ("user", "abc123")),
])
def test_parse_ref_accepts_principal_references(value, expected):
    assert board_people.parse_ref(value) == expected


@pytest.mark.parametrize("value", [
    "", None, "jon", "user:", ":abc", "team:abc", "user:abc/def",
    "user:abc def", "user:" + "x" * 200,
])
def test_parse_ref_rejects_everything_else(value):
    """A name is not a reference. If these parsed, the field would quietly
    become free text with a colon in it — exactly what it replaces."""
    assert board_people.parse_ref(value) is None
    assert board_people.is_ref(value) is False


# ── the directory ──────────────────────────────────────────────────────────

def test_directory_lists_members_and_agent_accounts(org):
    refs = {p["ref"] for p in board_people.directory(org["session"], ORG_SLUG)}
    assert f"user:{org['jon'].id}" in refs
    assert f"user:{org['jan'].id}" in refs
    assert f"agent:{org['routine'].id}" in refs


def test_directory_omits_a_token_that_belongs_to_a_user(org):
    """A token linked to a person IS that person — the agent is holding their
    credential. Listing it separately would offer one human twice, once under
    a name nobody would recognise as them."""
    refs = {p["ref"] for p in board_people.directory(org["session"], ORG_SLUG)}
    assert f"agent:{org['jons_laptop'].id}" not in refs


def test_directory_omits_an_expired_token(org):
    """An expired token cannot act, so offering it as a NEW participant would
    be offering a dead identity. An existing reference to it still describes."""
    from datetime import datetime, timedelta
    session = org["session"]
    dead = config_db.ApiToken(
        id=str(uuid.uuid4()), name="Retired routine", token_hash=uuid.uuid4().hex * 2,
        tenant_id=org["tenant"].id, source="automation",
        expires_at=datetime.utcnow() - timedelta(days=1))
    session.add(dead)
    session.commit()
    try:
        refs = {p["ref"] for p in board_people.directory(session, ORG_SLUG)}
        assert f"agent:{dead.id}" not in refs
        described = board_people.describe(session, ORG_SLUG, [f"agent:{dead.id}"])
        assert described[f"agent:{dead.id}"]["known"] is True
    finally:
        session.delete(dead)
        session.commit()


def test_directory_of_an_unknown_tenant_is_empty(org):
    assert board_people.directory(org["session"], "no-such-tenant") == []


def test_directory_does_not_cross_tenants(org):
    """The reference is a join key. A directory that reached into another org
    would let one board attribute a card to somebody who cannot see it."""
    session = org["session"]
    other = config_db.Tenant(id=str(uuid.uuid4()), name="Other", slug="people-test-other")
    stranger = config_db.UserAccount(
        id=str(uuid.uuid4()), org_id=other.id, username="stranger",
        full_username="stranger@other.local", display_name="Stranger")
    session.add_all([other, stranger])
    session.commit()
    try:
        refs = {p["ref"] for p in board_people.directory(session, ORG_SLUG)}
        assert f"user:{stranger.id}" not in refs
        # And the same reference must not resolve from the other side either.
        out = board_people.resolve(session, ORG_SLUG, ["stranger"])
        assert out["resolved"] == {}
        assert "stranger" in out["errors"]
    finally:
        session.delete(stranger)
        session.delete(other)
        session.commit()


# ── display: what a card renders ───────────────────────────────────────────

def test_display_prefers_the_display_name_then_the_full_name(org):
    session = org["session"]
    out = board_people.describe(
        session, ORG_SLUG, [f"user:{org['jon'].id}", f"user:{org['jan'].id}"])
    assert out[f"user:{org['jon'].id}"]["display"] == "Jon Smith"
    # Jan has no display_name, so first + last is the next best thing.
    assert out[f"user:{org['jan'].id}"]["display"] == "Jan Novak"


def test_a_deactivated_member_still_resolves_and_is_marked(org):
    """Retaining the historical reference is the specified behaviour: the card
    keeps saying who raised it after they leave, flagged rather than blanked."""
    rec = board_people.describe(
        org["session"], ORG_SLUG, [f"user:{org['jan'].id}"])[f"user:{org['jan'].id}"]
    assert rec["known"] is True
    assert rec["active"] is False
    assert rec["display"] == "Jan Novak"


def test_a_reference_with_no_account_left_still_renders(org):
    """The hard-delete case. The card still points at a specific principal, so
    it renders as a former member rather than disappearing — blanking it would
    be the "unresolve it" behaviour this design rejected."""
    gone = f"user:{uuid.uuid4()}"
    rec = board_people.describe(org["session"], ORG_SLUG, [gone])[gone]
    assert rec["known"] is False
    assert rec["active"] is False
    assert rec["display"] == "Former member"
    assert rec["ref"] == gone


def test_describe_answers_for_every_reference_asked_about(org):
    """Total by construction, so a caller can render without branching."""
    refs = [f"user:{org['jon'].id}", "user:missing", "not-a-reference"]
    out = board_people.describe(org["session"], ORG_SLUG, refs)
    assert set(out) == set(refs)


def test_describe_of_nothing_is_nothing(org):
    assert board_people.describe(org["session"], ORG_SLUG, []) == {}
    assert board_people.describe(org["session"], ORG_SLUG, ["", None]) == {}


def test_an_agent_is_described_as_an_agent(org):
    ref = f"agent:{org['routine'].id}"
    rec = board_people.describe(org["session"], ORG_SLUG, [ref])[ref]
    assert rec["kind"] == "agent"
    assert rec["display"] == "Dispatch routine"
    assert rec["source"] == "claude_code"


# ── resolution: the write gate ─────────────────────────────────────────────

def test_resolve_accepts_the_names_a_person_would_type(org):
    session, jon = org["session"], org["jon"]
    for typed in ["jon", "JON", "Jon Smith", "jon@example.com",
                  f"jon@{ORG_SLUG}.local", jon.id, f"user:{jon.id}"]:
        out = board_people.resolve(session, ORG_SLUG, [typed])
        assert out["errors"] == {}, f"{typed!r} should resolve"
        assert out["resolved"][typed] == f"user:{jon.id}"


def test_resolve_accepts_an_agent_by_name_and_by_reference(org):
    session, routine = org["session"], org["routine"]
    for typed in ["Dispatch routine", f"agent:{routine.id}", routine.id]:
        out = board_people.resolve(session, ORG_SLUG, [typed])
        assert out["resolved"][typed] == f"agent:{routine.id}", f"{typed!r} should resolve"


def test_resolve_refuses_a_name_that_matches_nobody_and_says_who_exists(org):
    """An error that says "no" without saying "who, then" is how a surface with
    no picker becomes unusable."""
    out = board_people.resolve(org["session"], ORG_SLUG, ["nobody-here"])
    assert out["resolved"] == {}
    msg = out["errors"]["nobody-here"]
    assert "nobody-here" in msg
    assert "Jon Smith" in msg


def test_resolve_refuses_a_well_formed_reference_to_nobody(org):
    """Shape is not existence. Accepting this would store a reference nothing
    can resolve, which is the free-text failure wearing a colon."""
    ghost = f"user:{uuid.uuid4()}"
    out = board_people.resolve(org["session"], ORG_SLUG, [ghost])
    assert out["resolved"] == {}
    assert "no member or agent account has that id" in out["errors"][ghost]


def test_resolve_refuses_an_ambiguous_name_rather_than_guessing(org):
    """Two people answering to one name is the case where guessing puts the
    wrong person on a card — and a wrong name is worse than a refused one."""
    session = org["session"]
    twin = config_db.UserAccount(
        id=str(uuid.uuid4()), org_id=org["tenant"].id, username="joseph",
        full_username=f"joseph@{ORG_SLUG}.local", display_name="jon")
    session.add(twin)
    session.commit()
    try:
        out = board_people.resolve(session, ORG_SLUG, ["jon"])
        assert out["resolved"] == {}
        assert "matches 2 accounts" in out["errors"]["jon"]
    finally:
        session.delete(twin)
        session.commit()


def test_resolve_does_not_match_on_a_prefix(org):
    """Exact matching only. "jo" resolving to whichever of Jon and Jan sorted
    first is the silent-wrong-person failure this field exists to remove."""
    out = board_people.resolve(org["session"], ORG_SLUG, ["jo"])
    assert out["resolved"] == {}
    assert "jo" in out["errors"]


def test_resolve_of_nothing_is_nothing(org):
    out = board_people.resolve(org["session"], ORG_SLUG, ["", None])
    assert out == {"resolved": {}, "errors": {}}


def test_resolve_reports_per_input_so_one_bad_name_is_identifiable(org):
    out = board_people.resolve(org["session"], ORG_SLUG, ["jon", "ghost"])
    assert out["resolved"]["jon"] == f"user:{org['jon'].id}"
    assert set(out["errors"]) == {"ghost"}


# ── the routes ─────────────────────────────────────────────────────────────

def test_every_editor_module_is_copied_into_the_image():
    """The Dockerfile names each module by hand, so a new one that is not added
    there is missing from the image and the container crash-loops on import —
    a failure that cannot happen until deploy, long after every test is green.

    Deliberately checks the whole directory rather than this one module: the
    next module added has the same trap waiting for it.
    """
    from pathlib import Path
    editor_dir = Path(main.__file__).resolve().parent
    dockerfile = (editor_dir / "Dockerfile").read_text()
    copied = " ".join(line for line in dockerfile.splitlines()
                      if line.startswith("COPY") and ".py" in line)
    missing = [p.name for p in sorted(editor_dir.glob("*.py"))
               if p.name not in copied]
    assert not missing, (
        f"{missing} live in editor/ but are not COPYed into the image. "
        f"The container will fail on import at startup.")


def test_people_routes_are_registered():
    paths = served_paths(main.app)
    assert "/api/board/people" in paths
    assert "/api/internal/board/people" in paths


def test_internal_people_endpoint_requires_the_peer_token():
    """It names accounts, so it gets the same gate as the rest of
    /api/internal — a workload on the internal network must not read the
    staff list unauthenticated."""
    import inspect
    src = inspect.getsource(boards.internal_board_people)
    assert "_require_internal_service(request)" in src


def test_token_validation_declares_which_identity_space_the_id_is_in():
    """An ApiToken id is durable and can BE an agent account; an OAuth token's
    id is derived from the token hash and changes at every login. A caller
    deriving a principal must be able to tell them apart without guessing from
    the shape of an id."""
    import inspect
    src = inspect.getsource(main._resolve_bearer_token)
    assert 'info["principal_kind"] = "api"' in src
    assert 'info["principal_kind"] = "oauth"' in src


# ── enrichment onto board payloads ─────────────────────────────────────────

def test_card_people_refs_collects_reporter_and_participants_once():
    cards = [
        {"reporter": "user:a", "participants": ["user:b", "user:c"]},
        {"reporter": "user:b", "participants": ["user:a"]},
        {"reporter": "", "participants": []},
    ]
    assert boards._card_people_refs(cards) == ["user:a", "user:b", "user:c"]


def test_card_people_refs_of_an_empty_board_is_empty():
    assert boards._card_people_refs([]) == []
    assert boards._card_people_refs([{}]) == []


def test_enrich_people_never_fails_a_card_read(monkeypatch):
    """A card must render when identity lookup is having a bad day. The
    references stay exactly as they are and the UI falls back to showing
    them — worse to read, never wrong."""
    def boom(*a, **kw):
        raise RuntimeError("directory down")
    monkeypatch.setattr(board_people, "describe", boom)
    data = {"cards": [{"slug": "c", "reporter": "user:a", "participants": []}]}
    out = boards._enrich_people(data, ORG_SLUG, None)
    assert out["people"] == {}
    assert out["cards"][0]["reporter"] == "user:a"


def test_enrich_people_handles_a_single_card_payload(org):
    data = {"card": {"slug": "c", "reporter": f"user:{org['jon'].id}", "participants": []}}
    out = boards._enrich_people(data, ORG_SLUG, org["session"])
    assert out["people"][f"user:{org['jon'].id}"]["display"] == "Jon Smith"


def test_card_create_stamps_the_session_user_as_the_actor():
    """A card added in the UI is reported by the person adding it unless they
    say otherwise — and if they do say otherwise they are still on the card."""
    import inspect
    src = inspect.getsource(boards.board_card_create)
    assert '"actor": f"user:{claims.get(\'uid\')}" if claims.get("uid") else ""' in src


def test_card_reads_are_enriched_with_people():
    import inspect
    for fn in (boards.board_cards_list, boards.board_card_get, boards.board_card_update):
        assert "_enrich_people(" in inspect.getsource(fn), fn.__name__


def test_card_list_forwards_the_people_filters():
    import inspect
    src = inspect.getsource(boards.board_cards_list)
    assert '"reporter": reporter' in src
    assert '"participant": participant' in src
    assert '"person": person' in src


def test_the_board_ui_cannot_send_a_filter_this_route_drops():
    """FastAPI ignores a query param the signature does not declare, so a
    filter the UI sends and the editor never declared is dropped in SILENCE:
    the board shows a filter chip over an unfiltered board, and nothing
    anywhere errors.

    That is a cross-service check on purpose. Both halves are individually
    correct in that failure — only their disagreement is the bug, so it can
    only be caught from a test that reads both.
    """
    import inspect
    from pathlib import Path
    boards_js = Path(main.__file__).resolve().parents[1] / "memory-ui" / "static" / "boards.js"
    if not boards_js.exists():
        pytest.skip("memory-ui is not checked out beside the editor")
    head = boards_js.read_text().split("const cardParams = ", 1)
    assert len(head) == 2, "boards.js no longer builds its card query in cardParams()"
    tail = head[1]
    # Two shapes, because the arrow function has worn both: a concise body
    # returning the object directly, `() => ({ ... })`, and a block body that
    # parses the search string first and then `return { ... };`. Anchoring on
    # only one of them is how this guard goes blind during a refactor while
    # still reporting green.
    if tail.lstrip().startswith("() => ({"):
        obj = tail.split("({", 1)[1].split("})", 1)[0]
    else:
        assert "return {" in tail, "cardParams() no longer returns the card query object"
        obj = tail.split("return {", 1)[1].split("};", 1)[0]
    sent = set(re.findall(r"(\w+):", obj))
    declared = set(inspect.signature(boards.board_cards_list).parameters)
    missing = sorted(sent - declared)
    assert not missing, (
        f"the board UI sends {missing}, which /api/board/cards does not "
        f"declare — FastAPI will drop them and the board will render as if "
        f"the filter were never applied.")
