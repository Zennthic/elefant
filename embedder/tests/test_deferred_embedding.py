"""Restore the data now, embed it later.

On a small box the bundled CPU model is most of a restore's wall-clock time,
and it is the part an operator most wants to move: get the org's data back in
minutes, then embed overnight — or after pointing the deployment at a faster
backend.

That trade is only acceptable because a memory's content lives in the Qdrant
PAYLOAD and the vector is derived from it. Nothing is lost by deferring; the
memory is simply unreachable by semantic search until the vector lands. The
flag is what makes that state countable, findable after a restart, and
clearable exactly once — so the tests here are mostly about the flag.
"""
import pytest


@pytest.fixture
def qd(monkeypatch):
    """A Qdrant stand-in that remembers points and can filter on the flag."""
    import main

    class _Pt:
        def __init__(self, pid, vector, payload):
            self.id, self.vector, self.payload = pid, vector, payload

    class _Count:
        def __init__(self, n):
            self.count = n

    class _Q:
        def __init__(self):
            self.points = {}
            self.embed_calls = 0

        def upsert(self, collection_name, points):
            for p in points:
                self.points[p.id] = _Pt(p.id, p.vector, dict(p.payload))

        def _pending(self):
            return [p for p in self.points.values()
                    if p.payload.get(main.PENDING_EMBED_KEY)]

        def count(self, collection_name, count_filter=None, exact=True):
            return _Count(len(self._pending()) if count_filter is not None
                          else len(self.points))

        def scroll(self, collection_name, scroll_filter=None, limit=64,
                   with_payload=True, with_vectors=False):
            return self._pending()[:limit], None

        def delete(self, collection_name, points_selector=None):
            self.points.clear()

        def get_collections(self):
            class _C:
                collections = []
            return _C()

    fake = _Q()
    monkeypatch.setattr(main, "qdrant", lambda: fake)

    def _embed(text):
        fake.embed_calls += 1
        return [0.5] * main._vector_size
    monkeypatch.setattr(main, "get_embedding", _embed)
    return fake


def _create(content="a memory", embed=True, slug="acme"):
    import main
    body = main.MemoryCreate(content=content, tenant_slug=slug, embed=embed)

    class _Req:
        headers = {}
        cookies = {}
    # The tenant comes from the token in production; here it is given.
    import unittest.mock as mock
    with mock.patch.object(main, "_auth_ctx", return_value=None), \
         mock.patch.object(main, "_resolve_tenant_slug", side_effect=lambda r, s: s or slug):
        return main.create_memory(body, _Req())


# ── writing one ────────────────────────────────────────────────────────────

def test_embedding_is_on_by_default(qd):
    payload = _create()
    import main
    assert qd.embed_calls == 1
    assert main.PENDING_EMBED_KEY not in payload


def test_deferring_skips_the_model_entirely(qd):
    """This is the whole point: the slow call is not made."""
    _create(embed=False)
    assert qd.embed_calls == 0


def test_a_deferred_memory_is_flagged_and_still_holds_its_content(qd):
    import main
    payload = _create(content="the thing to remember", embed=False)
    assert payload[main.PENDING_EMBED_KEY] is True
    assert payload["content"] == "the thing to remember"
    stored = list(qd.points.values())[0]
    assert stored.payload["content"] == "the thing to remember", \
        "content must survive in the payload — it is what the later pass embeds from"


def test_a_deferred_memory_gets_a_placeholder_of_the_right_width(qd):
    """Qdrant will not store a point whose vector is the wrong dimension, so
    the placeholder has to match the collection, not be empty."""
    import main
    _create(embed=False)
    stored = list(qd.points.values())[0]
    assert len(stored.vector) == main._vector_size
    assert set(stored.vector) == {0.0}


# ── counting the debt ──────────────────────────────────────────────────────

def test_pending_counts_only_the_deferred_ones(qd):
    import main
    _create(content="a", embed=False)
    _create(content="b", embed=False)
    _create(content="c", embed=True)
    assert main.count_pending_embeddings("acme") == 2


def test_a_tenant_that_never_deferred_has_no_debt(qd):
    import main
    _create(embed=True)
    assert main.count_pending_embeddings("acme") == 0


# ── paying it off ──────────────────────────────────────────────────────────

def test_the_pass_embeds_everything_pending_and_clears_the_flag(qd):
    import main
    _create(content="a", embed=False)
    _create(content="b", embed=False)
    main.embed_pending_memories("acme")

    assert qd.embed_calls == 2
    assert main.count_pending_embeddings("acme") == 0
    for p in qd.points.values():
        assert main.PENDING_EMBED_KEY not in p.payload
        assert set(p.vector) != {0.0}, "the placeholder was never replaced"


def test_the_pass_leaves_already_embedded_memories_alone(qd):
    import main
    _create(content="already done", embed=True)
    qd.embed_calls = 0
    main.embed_pending_memories("acme")
    assert qd.embed_calls == 0


def test_content_is_what_gets_embedded(qd, monkeypatch):
    import main
    seen = []
    monkeypatch.setattr(main, "get_embedding",
                        lambda t: (seen.append(t), [0.5] * main._vector_size)[1])
    _create(content="the exact words", embed=False)
    main.embed_pending_memories("acme")
    assert seen == ["the exact words"]


def test_a_failure_leaves_the_rest_pending_for_a_later_run(qd, monkeypatch):
    """Resumability is the property that makes this safe to defer at all: an
    interrupted pass must not lose track of what is still owed."""
    import main
    _create(content="a", embed=False)
    _create(content="b", embed=False)

    def broken(_text):
        raise RuntimeError("embedding backend is down")
    monkeypatch.setattr(main, "get_embedding", broken)

    main.embed_pending_memories("acme")          # must not raise out
    assert main.count_pending_embeddings("acme") == 2, \
        "a failed pass cleared the flag on work it never did"
    assert main._pending_embed_state["state"] == "error"


def test_a_partial_failure_keeps_what_succeeded(qd, monkeypatch):
    import main
    _create(content="good", embed=False)
    _create(content="bad", embed=False)

    def sometimes(text):
        if text == "bad":
            raise RuntimeError("nope")
        return [0.5] * main._vector_size
    monkeypatch.setattr(main, "get_embedding", sometimes)

    main.embed_pending_memories("acme")
    assert main.count_pending_embeddings("acme") == 1
    # And a later run, once the backend is healthy, finishes the job.
    monkeypatch.setattr(main, "get_embedding", lambda t: [0.5] * main._vector_size)
    main.embed_pending_memories("acme")
    assert main.count_pending_embeddings("acme") == 0


def test_a_point_that_stays_flagged_does_not_loop_forever(qd, monkeypatch):
    """The pass re-queries the pending set each round, so termination depends
    on that set shrinking. A point that somehow keeps its flag — a write that
    silently didn't take — must end the pass, not spin the CPU until someone
    notices."""
    import main
    _create(content="sticky", embed=False)
    # Embed successfully, but never let the flag clear.
    monkeypatch.setattr(qd, "upsert", lambda collection_name, points: None)
    main.embed_pending_memories("acme")          # must return
    assert main._pending_embed_state["state"] == "done"


def test_the_run_reports_progress_a_ui_can_show(qd):
    import main
    for i in range(3):
        _create(content=f"m{i}", embed=False)
    main.embed_pending_memories("acme")
    state = main._pending_embed_state
    assert state["state"] == "done"
    assert state["processed"] == 3 and state["total"] == 3
    assert state["tenant_slug"] == "acme"
