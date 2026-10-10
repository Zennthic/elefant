"""The local embedder loads its model cache-first, pinned to one revision
(card #422).

Every start used to call `SentenceTransformer(name)` with no revision, so
huggingface_hub asked huggingface.co for a newer copy each time: a start
needed the internet, logged an "unauthenticated requests" warning, and a new
upload by the publisher would have changed the weights behind the vectors
without anyone being told.

sentence-transformers is not installed in CI, so these tests hand
`_load_local_model` a stand-in class that behaves like the real one: with
`local_files_only=True` it reads only the cache, otherwise it goes to the Hub,
which here raises.
"""
import json

import pytest

import main


class _HubCalled(AssertionError):
    pass


def _fake_st(cache: set, hub_calls: list, hub_reachable: bool):
    """A SentenceTransformer stand-in over a fake cache of (repo, revision)."""

    class _ST:
        def __init__(self, name, revision=None, device=None, local_files_only=False):
            self.name, self.revision, self.device = name, revision, device
            if (name, revision) in cache:
                return
            if local_files_only:
                # What huggingface_hub raises for a miss in offline mode.
                raise OSError(f"{name}@{revision} is not in the local cache")
            hub_calls.append((name, revision))
            if not hub_reachable:
                raise _HubCalled("request made to the Hugging Face Hub")
            cache.add((name, revision))

    return _ST


PINNED = (main.LOCKED_LOCAL_MODEL_REPO, main.LOCKED_LOCAL_MODEL_REVISION)


def test_revision_is_a_full_commit_hash():
    rev = main.LOCKED_LOCAL_MODEL_REVISION
    assert len(rev) == 40 and int(rev, 16) >= 0
    assert main.LOCKED_LOCAL_MODEL_REPO.endswith("/" + main.LOCKED_LOCAL_MODEL)


def test_vector_compat_check_uses_the_same_pin():
    import vector_compat as vc
    assert vc.MODEL == main.LOCKED_LOCAL_MODEL
    assert vc.REVISION == main.LOCKED_LOCAL_MODEL_REVISION


def test_populated_cache_loads_without_any_hub_request():
    hub_calls: list = []
    st = _fake_st({PINNED}, hub_calls, hub_reachable=False)

    model = main._load_local_model(st)

    assert hub_calls == []
    assert (model.name, model.revision, model.device) == (*PINNED, "cpu")


def test_build_backend_with_cache_and_no_network(monkeypatch):
    """_build_backend's local branch goes through the cache-first loader."""
    import sys
    import types

    hub_calls: list = []
    st = _fake_st({PINNED}, hub_calls, hub_reachable=False)
    st.get_embedding_dimension = lambda self: 768
    st.encode = lambda self, text, normalize_embeddings=True: types.SimpleNamespace(
        tolist=lambda: [0.0] * 768
    )
    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        types.SimpleNamespace(SentenceTransformer=st))

    fn, vsize = main._build_backend({"provider": "local", "model": main.LOCKED_LOCAL_MODEL})

    assert vsize == 768 and len(fn("hello")) == 768
    assert hub_calls == []


def test_empty_cache_downloads_once_pinned(caplog):
    hub_calls: list = []
    cache: set = set()
    st = _fake_st(cache, hub_calls, hub_reachable=True)

    with caplog.at_level("INFO", logger=main.log.name):
        model = main._load_local_model(st)

    assert hub_calls == [PINNED]
    assert model.revision == main.LOCKED_LOCAL_MODEL_REVISION
    assert any("downloading it once" in r.getMessage() for r in caplog.records)

    # The next start finds it in the cache and stays offline.
    hub_calls.clear()
    main._load_local_model(_fake_st(cache, hub_calls, hub_reachable=False))
    assert hub_calls == []


def test_a_real_fault_is_not_retried_online():
    class _Broken:
        calls = 0

        def __init__(self, *a, **k):
            type(self).calls += 1
            raise RuntimeError("corrupt weights")

    with pytest.raises(RuntimeError):
        main._load_local_model(_Broken)
    assert _Broken.calls == 1


# ── Revision in the skip stamp and the sidecar ────────────────────────────

@pytest.fixture
def local_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_embed_cfg", {"provider": "local", "model": main.LOCKED_LOCAL_MODEL})
    monkeypatch.setattr(main, "_vector_size", 768)
    monkeypatch.setattr(main, "EMBEDDING_STATE_FILE", tmp_path / "embedding_state.json")
    return monkeypatch


def test_stamp_carries_the_revision(local_backend):
    rev7 = main.LOCKED_LOCAL_MODEL_REVISION[:7]
    assert main._embed_model_tag() == f"local/{main.LOCKED_LOCAL_MODEL}@{rev7}/768"


def test_external_provider_stamp_is_unchanged(monkeypatch):
    monkeypatch.setattr(main, "_embed_cfg", {"provider": "custom", "model": "nomic"})
    monkeypatch.setattr(main, "_vector_size", 768)
    assert main._embed_model_tag() == "custom/nomic/768"
    assert main._legacy_embed_model_tag() is None


def test_revision_change_changes_the_stamp_so_the_page_is_re_embedded(local_backend):
    sha = "a" * 64
    old_tag = main._embed_model_tag()
    entry = {"stamps": {(sha, old_tag, 2)}, "points": 2}
    assert main._page_is_current(entry, sha, old_tag, main._legacy_embed_model_tag())

    local_backend.setattr(main, "LOCKED_LOCAL_MODEL_REVISION", "f" * 40)
    new_tag = main._embed_model_tag()

    assert new_tag != old_tag
    assert not main._page_is_current(entry, sha, new_tag, main._legacy_embed_model_tag())


def test_unpinned_stamp_counts_as_the_pinned_revision_for_this_upgrade(local_backend):
    """Chunks embedded before #422 carry `local/<model>/768`. They came from
    the revision now pinned, so the upgrade must not re-embed the wiki."""
    sha = "b" * 64
    legacy = f"local/{main.LOCKED_LOCAL_MODEL}/768"
    entry = {"stamps": {(sha, legacy, 1)}, "points": 1}

    assert main._legacy_embed_model_tag() == legacy
    assert main._page_is_current(entry, sha, main._embed_model_tag(), main._legacy_embed_model_tag())

    # A later revision bump ends the alias, so those chunks re-embed then.
    local_backend.setattr(main, "LOCKED_LOCAL_MODEL_REVISION", "f" * 40)
    assert main._legacy_embed_model_tag() is None
    assert not main._page_is_current(entry, sha, main._embed_model_tag(), None)


def test_sidecar_records_revision_apart_from_model(local_backend):
    main._record_collection_state("knowledge_base")
    rec = json.loads(main.EMBEDDING_STATE_FILE.read_text())["collections"]["knowledge_base"]
    # `model` stays the bare name: the admin UI compares it to the config.
    assert rec["model"] == main.LOCKED_LOCAL_MODEL
    assert rec["revision"] == main.LOCKED_LOCAL_MODEL_REVISION
    assert main._sidecar_backend_differs("knowledge_base") is False

    local_backend.setattr(main, "LOCKED_LOCAL_MODEL_REVISION", "f" * 40)
    assert main._sidecar_backend_differs("knowledge_base") is True


def test_pre_422_sidecar_entry_does_not_force_a_full_re_embed(local_backend):
    main.EMBEDDING_STATE_FILE.write_text(json.dumps({
        "schema_version": 1,
        "collections": {"knowledge_base": {
            "provider": "local", "model": main.LOCKED_LOCAL_MODEL,
            "vector_size": 768, "applied_at": None,
        }},
    }))
    assert main._sidecar_backend_differs("knowledge_base") is False
