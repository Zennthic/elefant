"""Startup reindex re-embeds only the wiki pages that changed (card #421).

Every embedder start used to re-embed the whole wiki, although the vectors on
the persistent Qdrant volume were still valid. Wiki chunks now carry the
page's content hash, the model that embedded them and the page's chunk count,
and `full_reindex(force=False)` (the startup pass) skips a page whose stamps
match. These tests run `full_reindex` against an in-memory Qdrant and count
the calls to the embedding function.
"""
import json
from types import SimpleNamespace

import pytest


class _FakeQdrant:
    """Just enough of QdrantClient for index_file, the stamp scan and the
    orphan prune: upsert, delete (by id list or by `path` filter), scroll,
    get_collections, plus a dot-product search for comparing results."""

    def __init__(self):
        self.colls: dict[str, dict] = {}

    def upsert(self, collection_name, points):
        store = self.colls.setdefault(collection_name, {})
        for p in points:
            store[p.id] = SimpleNamespace(id=p.id, vector=list(p.vector), payload=dict(p.payload))

    def delete(self, collection_name, points_selector):
        store = self.colls.setdefault(collection_name, {})
        if isinstance(points_selector, list):
            for pid in points_selector:
                store.pop(pid, None)
            return
        cond = points_selector.filter.must[0]
        for pid in [i for i, p in store.items() if p.payload.get(cond.key) == cond.match.value]:
            del store[pid]

    def scroll(self, collection_name, limit, offset=None, with_payload=True, with_vectors=False):
        pts = sorted(self.colls.get(collection_name, {}).values(), key=lambda p: p.id)
        start = offset or 0
        batch = pts[start:start + limit]
        nxt = start + limit if start + limit < len(pts) else None
        return [SimpleNamespace(id=p.id, payload=dict(p.payload)) for p in batch], nxt

    def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name=n) for n in self.colls])

    def search(self, coll, vec, k=5):
        hits = sorted(
            self.colls.get(coll, {}).values(),
            key=lambda p: (-sum(a * b for a, b in zip(p.vector, vec)), p.id),
        )
        return [(p.id, p.payload["path"]) for p in hits[:k]]

    def snapshot(self):
        return {c: {i: (p.vector, p.payload) for i, p in s.items()} for c, s in self.colls.items()}


class _Env:
    def __init__(self, main, monkeypatch, tmp_path):
        self.main = main
        self.q = _FakeQdrant()
        self.calls: list[str] = []
        self.cfg = {"provider": "custom", "model": "model-a"}
        self.docs = tmp_path / "docs"
        self.docs.mkdir()
        self.state_file = tmp_path / "embedding_state.json"

        monkeypatch.setattr(main, "DOCS_DIR", self.docs)
        monkeypatch.setattr(main, "_MULTI_TENANT_DOCS", True)
        monkeypatch.setattr(main, "EMBEDDING_STATE_FILE", self.state_file)
        monkeypatch.setattr(main, "qdrant", lambda: self.q)
        monkeypatch.setattr(main, "_load_embedding_config", lambda: dict(self.cfg))
        monkeypatch.setattr(main, "_activate_backend", self._activate)
        monkeypatch.setattr(main, "ensure_collection", lambda recreate=False: None)
        monkeypatch.setattr(main, "_reconcile_all_docs_from_s3", lambda: 0)
        monkeypatch.setattr(main, "_fetch_git_sources", lambda: [])
        monkeypatch.setattr(main, "get_embedding", self._embed)
        monkeypatch.setattr(main, "_embed_cfg", {})
        monkeypatch.setattr(main, "_vector_size", 4)

    def _activate(self, cfg):
        self.main._embed_cfg = dict(cfg)
        self.main._vector_size = 4
        return False

    def _embed(self, text):
        # Deterministic per text and per model, so a model change really
        # changes the vectors.
        self.calls.append(text)
        h = sum(map(ord, text + self.cfg["model"]))
        return [float((h >> s) % 7) for s in (0, 3, 6, 9)]

    def write(self, rel, body):
        p = self.docs / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def run(self, force):
        self.calls.clear()
        self.main.full_reindex(force=force)
        return len(self.calls)

    def embedded_titles(self):
        return {t.split("\n")[0] for t in self.calls}


@pytest.fixture
def env(monkeypatch, tmp_path):
    import main
    e = _Env(main, monkeypatch, tmp_path)
    e.write("acme/alpha.md", "---\ntitle: Alpha\n---\n# A\n\nalpha one\n\n## A2\n\nalpha two\n")
    e.write("acme/beta.md", "---\ntitle: Beta\n---\n# B\n\nbeta body\n")
    e.write("acme/sub/gamma.md", "---\ntitle: Gamma\n---\n# G\n\ngamma body\n")
    return e


def _search_all(env):
    return [env.q.search("acme_wiki", v) for v in ([1, 0, 0, 0], [0, 1, 2, 3], [5, 5, 1, 0])]


def test_first_run_stamps_every_chunk(env):
    env.run(force=False)
    pts = list(env.q.colls["acme_wiki"].values())
    assert pts
    for p in pts:
        assert len(p.payload["content_sha"]) == 64
        assert p.payload["embed_model"] == "custom/model-a/4"
        assert p.payload["chunk_count"] >= 1


def test_restart_with_no_changes_embeds_nothing(env):
    env.run(force=False)
    before_store = env.q.snapshot()
    before_search = _search_all(env)

    assert env.run(force=False) == 0

    assert env.q.snapshot() == before_store
    assert _search_all(env) == before_search
    st = env.main._reindex_state
    assert st["last_reindex_skipped_unchanged"] == 3
    assert st["last_reindex_docs_indexed"] == 3
    assert st["last_reindex_failed_count"] == 0
    assert st["state"] == "watching"


def test_editing_one_page_re_embeds_only_that_page(env):
    env.run(force=False)
    env.write("acme/beta.md", "---\ntitle: Beta\n---\n# B\n\nbeta body, edited\n")

    assert env.run(force=False) > 0

    assert env.embedded_titles() == {"Beta"}
    assert env.main._reindex_state["last_reindex_skipped_unchanged"] == 2
    beta = [p for p in env.q.colls["acme_wiki"].values() if p.payload["path"] == "acme/beta.md"]
    assert "edited" in beta[0].payload["content"]


def test_same_dimension_model_change_re_embeds_every_page(env):
    env.run(force=False)
    env.cfg["model"] = "model-b"  # same vector size, different model

    env.run(force=False)

    assert env.embedded_titles() == {"Alpha", "Beta", "Gamma"}
    assert env.main._reindex_state["last_reindex_skipped_unchanged"] == 0
    assert {p.payload["embed_model"] for p in env.q.colls["acme_wiki"].values()} == {"custom/model-b/4"}
    # ... and the restart after that is quiet again.
    assert env.run(force=False) == 0


def test_sidecar_naming_another_backend_re_embeds_everything(env):
    env.run(force=False)
    state = json.loads(env.state_file.read_text())
    state["collections"]["acme_wiki"]["model"] = "something-else"
    env.state_file.write_text(json.dumps(state))

    env.run(force=False)

    assert env.embedded_titles() == {"Alpha", "Beta", "Gamma"}
    # The run records the active backend, so the next start skips again.
    assert env.run(force=False) == 0


def test_force_re_embeds_everything(env):
    env.run(force=False)

    env.run(force=True)

    assert env.embedded_titles() == {"Alpha", "Beta", "Gamma"}
    assert env.main._reindex_state["last_reindex_skipped_unchanged"] == 0


def test_force_is_the_default_for_user_triggered_rebuilds(env):
    env.run(force=False)
    env.calls.clear()
    env.main.full_reindex()
    assert env.embedded_titles() == {"Alpha", "Beta", "Gamma"}


def test_page_with_deleted_points_is_re_embedded(env):
    """#83 still holds: a page whose vectors went missing while its file
    stayed is put back, not skipped."""
    env.run(force=False)
    env.q.delete("acme_wiki", [i for i, p in env.q.colls["acme_wiki"].items()
                               if p.payload["path"] == "acme/sub/gamma.md"])

    env.run(force=False)

    assert env.embedded_titles() == {"Gamma"}
    assert any(p.payload["path"] == "acme/sub/gamma.md" for p in env.q.colls["acme_wiki"].values())


def test_page_missing_one_chunk_is_re_embedded(env):
    env.run(force=False)
    alpha = sorted(i for i, p in env.q.colls["acme_wiki"].items() if p.payload["path"] == "acme/alpha.md")
    assert len(alpha) > 1
    env.q.delete("acme_wiki", alpha[:1])

    env.run(force=False)

    assert env.embedded_titles() == {"Alpha"}


def test_pages_indexed_before_the_stamps_are_re_embedded_once(env):
    env.run(force=False)
    for p in env.q.colls["acme_wiki"].values():
        for k in ("content_sha", "embed_model", "chunk_count"):
            p.payload.pop(k)

    env.run(force=False)
    assert env.embedded_titles() == {"Alpha", "Beta", "Gamma"}
    assert env.run(force=False) == 0


def test_deleted_page_is_still_pruned_on_a_quiet_restart(env):
    env.run(force=False)
    (env.docs / "acme/beta.md").unlink()

    assert env.run(force=False) == 0

    assert all(p.payload["path"] != "acme/beta.md" for p in env.q.colls["acme_wiki"].values())


def test_failed_stamp_scan_falls_back_to_re_embedding(env, monkeypatch):
    env.run(force=False)

    def broken_scroll(*_a, **_kw):
        raise RuntimeError("qdrant hiccup")

    monkeypatch.setattr(env.q, "scroll", broken_scroll)
    env.run(force=False)
    assert env.embedded_titles() == {"Alpha", "Beta", "Gamma"}
