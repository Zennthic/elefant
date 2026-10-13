"""Boards: slug derivation, the advisory nudge, request defaults, and the
stage/seed vocabulary. Pure logic — no Postgres needed (the SQL endpoints are
exercised against a real DB in integration, not here)."""
import main


def test_slugify_examples():
    # The proposal's worked example.
    assert main._board_slugify("Apple/iCloud MCP server") == "apple-icloud-mcp-server"
    assert main._board_slugify("  Boards!!  ") == "boards"
    assert main._board_slugify("a---b__c") == "a-b-c"
    assert main._board_slugify("") == ""
    assert main._board_slugify("###") == ""


def test_nudge_warning_threshold():
    """Still works for a tenant that asks for a nudge — it is just not on by
    default any more."""
    assert main._nudge_warning("short", 280) == []
    assert main._nudge_warning("", 280) == []
    long = "x" * 281
    warns = main._nudge_warning(long, 280)
    assert len(warns) == 1 and "281" in warns[0]
    # A zero/negative threshold disables the nudge entirely.
    assert main._nudge_warning(long, 0) == []
    assert main._nudge_warning(long, -1) == []


def test_there_is_no_description_length_by_default():
    """Cards became fully-formed issue tracking, so nothing warns
    about how long a description is unless the tenant asked for it. The knob
    survives; the default does not."""
    assert main.BOARD_NUDGE_DEFAULT == 0
    assert main._nudge_warning("x" * 100_000, main.BOARD_NUDGE_DEFAULT) == []


def test_a_long_description_is_never_refused():
    """It was advisory before and still is — the warning rides in warnings[],
    never as a rejection, so there is no maximum length to hit."""
    import inspect
    src = inspect.getsource(main._nudge_warning)
    assert "HTTPException" not in src
    assert "raise" not in src


def test_card_add_request_defaults():
    req = main.CardAddRequest(title="Test")
    assert req.tenant_slug == "default"
    assert req.body == ""
    assert req.source == "unknown"
    assert req.nudge_chars == main.BOARD_NUDGE_DEFAULT == 0
    assert req.slug is None and req.column is None


def test_stage_vocabulary_and_seed():
    assert main.BOARD_STAGES == [
        "backlog", "selected-for-development", "in-progress",
        "scheduled", "blocked", "review", "done",
    ]
    # A new board gets one column per stage, in board order.
    assert main._SEED_COLUMNS == [
        ("Backlog",     "backlog"),
        ("Up Next",     "selected-for-development"),
        ("In Progress", "in-progress"),
        ("Scheduled",   "scheduled"),
        ("Blocked",     "blocked"),
        ("Review",      "review"),
        ("Done",        "done"),
    ]
    for _, stage in main._SEED_COLUMNS:
        assert stage in main.BOARD_STAGES


def test_the_old_vocabulary_is_still_valid():
    """New defaults are for a NEW board, not a migration of the
    boards people are working on. Every existing column carries a legacy
    stage, so rejecting those would make an existing column unsaveable the
    moment somebody renamed it."""
    assert set(main.LEGACY_BOARD_STAGES) == {
        "spark", "shaping", "designed", "ready",
        "building", "shipped", "dropped",
    }
    assert set(main.VALID_BOARD_STAGES) == set(main.BOARD_STAGES) | set(main.LEGACY_BOARD_STAGES)
    # `blocked` survived the rename unchanged, so it must not be listed twice.
    assert len(main.VALID_BOARD_STAGES) == len(set(main.VALID_BOARD_STAGES))


def test_a_finished_blocker_resolves_under_either_vocabulary():
    """The one place the two vocabularies are reconciled. A blocker that failed
    to resolve would stall every card waiting on it — a consequence, not a
    cosmetic difference, which is why this one case is worth special-casing
    when nothing else is."""
    assert "done" in main.TERMINAL_STAGES       # current
    assert "shipped" in main.TERMINAL_STAGES    # what `done` used to be called
    assert "dropped" in main.TERMINAL_STAGES    # the other way work ends
    assert main._link_resolved("done", None) is True
    assert main._link_resolved("shipped", None) is True
    assert main._link_resolved("in-progress", None) is False
    assert main._link_resolved("building", None) is False
