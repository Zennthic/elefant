"""A 429 has to say which ceiling refused it, and when it really clears.

Reported from production: Elefant started answering 429 and nothing in the
response said why. The limiter has TWO ceilings — per-minute and per-day —
and it answered both with the same sentence and a hardcoded
`Retry-After: 60`.

For the per-minute ceiling that header is right. For the per-day one it is a
lie with a cost: a well-behaved client retries every 60 seconds for hours, so
a quota that has been quietly spent presents as a hard outage, and the person
holding it cannot tell a burst from an exhausted budget. That is the question
they actually need answered, and it was the one piece of information the
response withheld.

These pin the reporting. The ceilings themselves are policy and live on the
token row; nothing here asserts what they should be.
"""
import time

import pytest

import main


@pytest.fixture(autouse=True)
def clean_counters():
    main._rate_counters.clear()
    yield
    main._rate_counters.clear()


@pytest.fixture
def limiting_on(monkeypatch):
    """Switch the limiter on, as a deployment that wants a ceiling would.

    It is OFF by default now, so every test about what a refusal SAYS has to
    turn it on first — which is itself the clearest statement of the default.
    """
    monkeypatch.setattr(main, "RATE_LIMITING_ON", True)
    monkeypatch.setattr(main, "_RATE_WINDOWS", (
        ("rpm", 60, "rate_limit_rpm", 60, "per-minute"),
        ("rpd", 86400, "rate_limit_rpd", 1000, "per-day"),
    ))


def _info(rpm=60, rpd=1000):
    return {"rate_limit_rpm": rpm, "rate_limit_rpd": rpd}


def _spend(token, n, info):
    for _ in range(n):
        assert main._check_rate_limit(token, info) is None


# ── the allowed path is unchanged ──────────────────────────────────────────

def test_a_request_under_both_ceilings_is_allowed(limiting_on, clean_counters):
    assert main._check_rate_limit("tok", _info()) is None


def test_the_limiter_still_actually_limits(limiting_on, clean_counters):
    """The reporting change must not have widened the gate."""
    _spend("tok", 5, _info(rpm=5))
    assert main._check_rate_limit("tok", _info(rpm=5)) is not None


def test_each_token_has_its_own_budget(limiting_on, clean_counters):
    _spend("tok-a", 3, _info(rpm=3))
    assert main._check_rate_limit("tok-a", _info(rpm=3)) is not None
    assert main._check_rate_limit("tok-b", _info(rpm=3)) is None


# ── which ceiling refused, and for how long ────────────────────────────────

def test_the_per_minute_ceiling_names_itself(limiting_on, clean_counters):
    _spend("tok", 3, _info(rpm=3))
    refusal = main._check_rate_limit("tok", _info(rpm=3))
    assert refusal["window"] == "per-minute"
    assert refusal["limit"] == 3 and refusal["used"] == 3
    assert 0 < refusal["retry_after"] <= 61


def test_the_per_day_ceiling_names_itself_and_does_not_say_sixty(limiting_on, clean_counters):
    """The bug, in one assertion: a spent daily budget used to come back as
    "retry in 60 seconds"."""
    _spend("tok", 4, _info(rpm=1000, rpd=4))
    refusal = main._check_rate_limit("tok", _info(rpm=1000, rpd=4))
    assert refusal["window"] == "per-day"
    assert refusal["retry_after"] > 60, "a daily ceiling cannot clear in a minute"
    assert refusal["retry_after"] <= 86401


def test_when_both_are_blown_the_binding_one_is_reported(limiting_on, clean_counters):
    """Reporting the per-minute window here would send the caller back in 60
    seconds to be refused again by the ceiling that had not moved."""
    _spend("tok", 5, _info(rpm=1000, rpd=1000))
    refusal = main._check_rate_limit("tok", _info(rpm=5, rpd=5))
    assert refusal["window"] == "per-day"
    assert refusal["retry_after"] > 60


def test_retry_after_shrinks_as_the_window_ages(limiting_on, clean_counters):
    """It is computed from the oldest entry in the window, not from a constant
    — so a caller that waits is told less each time rather than the same 60."""
    main._rate_counters["tok"]["rpm"] = [time.time() - 45] * 3
    main._rate_counters["tok"]["rpd"] = [time.time() - 45] * 3
    refusal = main._check_rate_limit("tok", _info(rpm=3, rpd=1000))
    assert refusal["window"] == "per-minute"
    assert refusal["retry_after"] <= 16, "45s into a 60s window, ~15s should remain"


# ── refusals must not feed the window ──────────────────────────────────────

def test_a_refused_request_is_not_counted(limiting_on, clean_counters):
    """Otherwise a client retrying hard holds its own window permanently full
    and never recovers — the limiter would punish the retry, not the load."""
    _spend("tok", 3, _info(rpm=3))
    before = len(main._rate_counters["tok"]["rpm"])
    for _ in range(25):
        main._check_rate_limit("tok", _info(rpm=3))
    assert len(main._rate_counters["tok"]["rpm"]) == before


def test_the_window_frees_up_once_entries_age_out(limiting_on, clean_counters):
    main._rate_counters["tok"]["rpm"] = [time.time() - 61] * 10
    main._rate_counters["tok"]["rpd"] = [time.time() - 61] * 10
    assert main._check_rate_limit("tok", _info(rpm=3)) is None


# ── what reaches the caller ────────────────────────────────────────────────

def test_the_response_carries_the_detail_a_person_needs(limiting_on, clean_counters):
    """A bare "Rate limit exceeded" is what made this undiagnosable from the
    outside — it is the same string whichever ceiling fired."""
    _spend("tok", 2, _info(rpm=1000, rpd=2))
    refusal = main._check_rate_limit("tok", _info(rpm=1000, rpd=2))
    for field in ("window", "limit", "used", "retry_after"):
        assert field in refusal, f"the refusal does not carry {field}"


def test_the_source_sends_those_fields_and_a_computed_retry_after():
    """Guards the wiring rather than the calculation: the fields are useless
    if the middleware still answers with a constant."""
    src = (main.__file__ and open(main.__file__).read()) or ""
    assert 'b"retry-after", b"60"' not in src, "the hardcoded Retry-After is back"
    assert 'str(refusal["retry_after"]).encode()' in src
    assert '"window": refusal["window"]' in src
    # And the operator gets a server-side trace naming the token and ceiling.
    assert "Rate limited token" in src


def test_the_message_says_the_budget_is_shared_per_token():
    """The first thing to check when a limit is hit that nobody expected: it
    is one budget per token, not per session, so concurrent agents share it."""
    src = open(main.__file__).read()
    assert "per TOKEN" in src


# ── off by default ─────────────────────────────────────────────────────────
# The ceilings were on at 60/1000 with no way to see or change them on the
# path that mattered, and that caused the incident. They are now a control a
# deployment opts into, not one it discovers.

def test_rate_limiting_is_off_unless_a_deployment_asks_for_it():
    assert main.RATE_LIMITING_ON is False, (
        "rate limiting defaults to ON again. It counts HTTP requests rather "
        "than cost, resets on every deploy, and charges protocol overhead "
        "against a usage budget — a deployment should choose it deliberately."
    )
    assert main.RATE_LIMIT_RPM == 0 and main.RATE_LIMIT_RPD == 0


def test_nothing_is_refused_while_it_is_off(clean_counters):
    for _ in range(500):
        assert main._check_rate_limit("tok", _info(rpm=1, rpd=1)) is None


def test_a_token_carrying_its_own_ceiling_is_still_not_limited(clean_counters):
    """The env var is the MASTER. Existing ApiToken rows carry 60/1000 in
    their columns; if those still applied, "off" would not be off for the
    tokens most likely to be in use."""
    assert main._check_rate_limit("tok", _info(rpm=1, rpd=1)) is None
    assert main._check_rate_limit("tok", _info(rpm=1, rpd=1)) is None


def test_a_disabled_limiter_records_nothing(clean_counters):
    """_rate_counters is keyed by token hash and never evicted, so appending
    while switched off would be a slow leak in a long-lived process for no
    benefit whatsoever."""
    for _ in range(50):
        main._check_rate_limit("tok", _info())
    assert not main._rate_counters, "the disabled limiter is still accumulating"


def test_one_window_can_be_enabled_without_the_other(monkeypatch, clean_counters):
    """A deployment that wants a burst ceiling should not be forced into a
    daily quota to get one."""
    monkeypatch.setattr(main, "RATE_LIMITING_ON", True)
    monkeypatch.setattr(main, "_RATE_WINDOWS", (
        ("rpm", 60, "rate_limit_rpm", 3, "per-minute"),
        ("rpd", 86400, "rate_limit_rpd", 0, "per-day"),   # off
    ))
    for _ in range(3):
        assert main._check_rate_limit("tok", {}) is None
    refusal = main._check_rate_limit("tok", {})
    assert refusal["window"] == "per-minute"
    assert not main._rate_counters["tok"]["rpd"], "the off window still counted"


def test_an_oauth_session_no_longer_claims_a_ceiling_it_cannot_have():
    """to_validate_dict() used to hand back a hardcoded 60/1000 that read like
    configuration and was not: no column backs it, no UI shows it, and nothing
    short of a code edit could change it. That is what made the incident
    undiagnosable."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "editor" / "config_db.py"
    body = src.read_text()
    start = body.index("def to_validate_dict")
    chunk = body[start:body.index("\n    def ", start + 10)]
    assert '"rate_limit_rpm": 60' not in chunk
    assert '"rate_limit_rpd": 1000' not in chunk
