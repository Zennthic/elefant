"""Card labels: request shape, hygiene, and the SQL that stores and filters them.

Labels are a card's ONE free-form classification field (`bug`, `enhancement`, a
repo, a sprint, and the board's own `note-to-self`). There were two arrays once
— `tags` for markers the board set itself, `labels` for everything a person or
an agent chose — and the separation was what stopped an agent replacing a
card's labels from stripping the marker that recorded how the card was
captured.

They are one field now, so that protection has to live somewhere else, and it
lives at the write path: `card_update` re-attaches RESERVED_CARD_LABELS unless
the caller says it means to remove one. The tests below cover both halves —
that the merge happened and nothing was dropped on the way, and that the
guarantee the merge could have cost is still enforced.

Pure logic plus SQL-string assertions, in the style of the other board tests —
the endpoints themselves are exercised against a real Postgres in integration.
"""
import inspect

import main
from conftest import card_insert_lists


# ── request shape ──────────────────────────────────────────────────────────

def test_card_add_request_defaults_labels_to_empty():
    req = main.CardAddRequest(title="Test")
    assert req.labels == []


def test_card_add_request_accepts_labels():
    req = main.CardAddRequest(title="Test", labels=["bug", "elefant"])
    assert req.labels == ["bug", "elefant"]


def test_card_update_request_labels_defaults_to_none():
    """None and [] must stay distinguishable: None means "leave the card's
    labels alone", [] means "clear them". Collapsing the two would make every
    title edit wipe the card's labels."""
    req = main.CardUpdateRequest()
    assert req.labels is None
    assert main.CardUpdateRequest(labels=[]).labels == []


# ── hygiene ────────────────────────────────────────────────────────────────

def test_labels_share_tag_hygiene():
    """Labels get the same cleaning as tags: strip, drop empties, de-dup
    case-insensitively with the first spelling winning."""
    assert main._clean_tags(["  bug ", "bug", "BUG", "", "  ", "elefant"]) == [
        "bug", "elefant"]
    assert main._clean_tags(None) == []
    assert main._clean_tags(["Bug", "bug"]) == ["Bug"]


# ── storage ────────────────────────────────────────────────────────────────

def test_cards_table_declares_labels_column():
    src = inspect.getsource(main._ensure_schema)
    assert "labels       TEXT[] NOT NULL DEFAULT '{}'" in src
    # Boards created before labels existed must gain the column on startup —
    # this repo has no migration runner, the ADD COLUMN IF NOT EXISTS is it.
    assert "ADD COLUMN IF NOT EXISTS labels TEXT[] NOT NULL DEFAULT '{}'" in src


def test_cards_table_no_longer_declares_a_tags_column():
    """A board created today starts with one classification field. Leaving the
    CREATE TABLE alone would have meant new installs growing a column the
    migration then immediately dropped."""
    src = inspect.getsource(main._ensure_schema)
    assert "tags         TEXT[] NOT NULL DEFAULT '{}'" not in src
    assert "ADD COLUMN IF NOT EXISTS tags" not in src


def test_card_add_inserts_labels():
    src = inspect.getsource(main.card_add)
    assert "labels" in src
    # The INSERT names its columns explicitly, so labels has to be in both
    # lists — asserted per column, so adding a field between them fails only
    # if it actually drops one.
    cols, values = card_insert_lists(src)
    assert "labels" in cols
    assert ":labels" in values


def test_card_add_response_carries_labels():
    """card_add hand-builds its response dict rather than going through
    _card_dict, so it is the one place labels can go missing on create."""
    src = inspect.getsource(main.card_add)
    assert '"labels": labels' in src


def test_card_dict_serialises_labels():
    src = inspect.getsource(main._card_dict)
    assert '"labels":       list(row["labels"] or [])' in src


def test_card_update_sets_labels_only_when_given():
    """None still has to mean "leave them alone" now that the branch also has
    to answer for `tags` — a title-only edit must not touch labels."""
    src = inspect.getsource(main.card_update)
    assert "if req.labels is not None or req.tags is not None:" in src
    assert 'sets.append("labels = :labels")' in src


# ── filtering ──────────────────────────────────────────────────────────────

def test_card_list_filters_by_label():
    src = inspect.getsource(main.card_list)
    assert "unnest(c.labels) l WHERE lower(l) = lower(:label)" in src


def test_card_list_search_covers_labels():
    """Free-text search already reached tags; a label the user can see on a
    card but cannot find by typing it would be a surprise."""
    src = inspect.getsource(main.card_list)
    assert "unnest(c.labels) l WHERE l ILIKE :q" in src


# ── disaster recovery ──────────────────────────────────────────────────────

def test_bcdr_restore_carries_labels():
    """The restore INSERT enumerates every card column by hand, so a new one
    that isn't added here is silently dropped on restore — the kind of loss
    nobody notices until they need the backup."""
    src = inspect.getsource(main._bcdr_insert_cards)
    assert "aliases, labels, repo," in src
    assert "CAST(:labels AS text[])" in src


def test_bcdr_restore_merges_a_pre_merge_snapshot():
    """A backup taken before the merge carries BOTH arrays. Restoring only
    `labels` would silently drop every tag in it — and a restore is the one
    operation with no second chance, because the thing you would recover from
    is the backup you just restored badly."""
    src = inspect.getsource(main._bcdr_insert_cards)
    assert '_clean_tags(list(r.get("labels") or []) + list(r.get("tags") or []))' in src


# ── the merge: tags folded into labels ─────────────────────────────────────

def test_migration_merges_tags_into_labels_then_drops_the_column():
    """Both halves in one function, and the order matters: merge first, drop
    second. A drop that ran before the merge would take the values with it."""
    src = inspect.getsource(main._merge_card_tags_into_labels)
    merge_at = src.index("UPDATE cards SET labels")
    drop_at = src.index("ALTER TABLE cards DROP COLUMN tags")
    assert merge_at < drop_at
    # labels first in the concatenation, so a value spelled both ways keeps the
    # casing the label already had.
    assert "unnest(labels || tags)" in src


def test_migration_is_guarded_on_the_column_still_existing():
    """Runs exactly once. `DROP COLUMN` is not idempotent in the way the
    `ADD COLUMN IF NOT EXISTS` upgrades around it are, and an unguarded merge
    would also re-run over rows it had already merged."""
    src = inspect.getsource(main._merge_card_tags_into_labels)
    assert "information_schema.columns" in src
    assert "column_name = 'tags'" in src
    assert "if not exists:" in src
    assert "return" in src


def test_migration_dedupes_case_insensitively_keeping_original_order():
    """DISTINCT ON has to sort by its own key to choose a winner, which would
    otherwise reorder every card's labels alphabetically as a side effect of
    migrating. The outer ORDER BY puts them back."""
    src = inspect.getsource(main._merge_card_tags_into_labels)
    assert "DISTINCT ON (lower(btrim(u.v)))" in src
    assert "ORDER BY lower(btrim(u.v)), u.ord" in src
    assert "ORDER BY d.ord" in src


def test_migration_runs_from_ensure_schema():
    """An unreferenced migration is a no-op with a test suite."""
    assert "_merge_card_tags_into_labels(conn)" in inspect.getsource(main._ensure_schema)


def test_card_add_folds_tags_into_labels():
    """The published skill still sends `tags`; a session running it must not
    lose the values or get a 422."""
    src = inspect.getsource(main.card_add)
    assert "_clean_tags(list(req.labels) + list(req.tags))" in src
    # ...and the INSERT no longer names a column that isn't there.
    cols, values = card_insert_lists(src)
    assert "tags" not in cols
    assert ":tags" not in values
    assert "labels" in cols


def test_card_dict_no_longer_serialises_tags():
    src = inspect.getsource(main._card_dict)
    assert '"tags"' not in src
    assert '"labels":       list(row["labels"] or [])' in src


def test_card_list_tag_filter_answers_off_labels():
    """`card_list(tag="note-to-self")` is documented in the published skill and
    is running in sessions today. It has to keep finding the same cards."""
    src = inspect.getsource(main.card_list)
    assert "unnest(c.labels) l WHERE lower(l) = lower(:tag)" in src
    assert "unnest(c.tags)" not in src


def test_card_list_search_no_longer_reaches_a_dropped_column():
    """Free-text search unnested `tags`; that column is gone, so the clause
    would now be a SQL error on every search rather than a missing result."""
    src = inspect.getsource(main.card_list)
    assert "unnest(c.tags) t WHERE t ILIKE :q" not in src
    assert "unnest(c.labels) l WHERE l ILIKE :q" in src


# ── the guarantee the merge could have cost ────────────────────────────────

def test_note_to_self_is_reserved():
    assert "note-to-self" in main.RESERVED_CARD_LABELS


def test_keep_reserved_re_attaches_a_dropped_marker():
    """The routine case: an agent rewrites a card's labels for some unrelated
    reason and never mentions the marker."""
    assert main._keep_reserved(["note-to-self", "bug"], ["enhancement"]) == [
        "enhancement", "note-to-self"]


def test_keep_reserved_leaves_ordinary_labels_behind():
    """It re-attaches MARKERS, not everything. A replace is still a replace."""
    assert main._keep_reserved(["bug", "infra"], ["enhancement"]) == ["enhancement"]


def test_keep_reserved_does_not_duplicate_a_marker_that_survived():
    assert main._keep_reserved(["note-to-self"], ["note-to-self", "bug"]) == [
        "note-to-self", "bug"]
    # Case-insensitively, matching the hygiene every other label comparison uses.
    assert main._keep_reserved(["note-to-self"], ["Note-To-Self"]) == ["Note-To-Self"]


def test_keep_reserved_handles_a_card_with_no_labels():
    assert main._keep_reserved(None, ["bug"]) == ["bug"]
    assert main._keep_reserved([], []) == []


def test_card_update_preserves_reserved_labels_by_default():
    """Protection is what you get by NOT thinking about it — which is the state
    every routine label rewrite is in."""
    assert main.CardUpdateRequest().allow_reserved is False
    src = inspect.getsource(main.card_update)
    assert "if not req.allow_reserved:" in src
    assert '_keep_reserved(list(card["labels"] or []), incoming)' in src


def test_card_update_can_be_told_to_drop_a_reserved_label():
    """Removing one stays possible, or the marker would be permanent and the
    board would have swapped one wrong behaviour for another."""
    assert main.CardUpdateRequest(allow_reserved=True).allow_reserved is True


def test_card_update_merges_both_spellings_when_given_both():
    src = inspect.getsource(main.card_update)
    assert "_clean_tags(list(req.labels or []) + list(req.tags or []))" in src


# ── the in-use vocabulary (GET /board/labels) ──────────────────────────────

def test_board_labels_only_counts_live_cards():
    """IN USE is the whole definition of the list, and archive is how this
    board retires things. A label surviving only on archived cards is
    vocabulary the user has already put down — offering it back as a
    suggestion would quietly resurrect it."""
    src = inspect.getsource(main.board_labels)
    assert "c.archived_at IS NULL" in src


def test_board_labels_dedupes_case_insensitively():
    """`Bug` and `bug` are one label everywhere else — the write path collapses
    them, the filter matches either. A suggestion list that offered both would
    be the only place on the board they looked like two."""
    src = inspect.getsource(main.board_labels)
    assert "DISTINCT ON (lower(btrim(l)))" in src


def test_board_labels_sorts_case_insensitively():
    """Read by a human scanning for a word. ASCII order files every
    capitalised label ahead of every lowercase one, which for a free-form
    vocabulary means the list is sorted by something nobody typed on purpose."""
    src = inspect.getsource(main.board_labels)
    assert "ORDER BY lower(btrim(l))" in src


def test_board_labels_serves_the_reserved_set():
    """The client marks reserved pills and decides whether a removal was
    deliberate. It reads the set from here rather than holding its own copy,
    which would be wrong — silently — the day the set changes."""
    src = inspect.getsource(main.board_labels)
    assert '"reserved": sorted(RESERVED_CARD_LABELS)' in src


def test_board_labels_is_tenant_scoped():
    """Every other board read sets the tenant before it queries; a label list
    that skipped it would leak one tenant's vocabulary into another's
    autocomplete."""
    src = inspect.getsource(main.board_labels)
    assert "_set_tenant(conn, slug)" in src
    assert "c.tenant_id = :t" in src
