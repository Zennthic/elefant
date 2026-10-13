"""Board column request models + the _column_dict shaping helper (pure).

Also serves as the memory-api touch that gets the column-CRUD endpoints
(added in an earlier commit) actually built + deployed: CI's change-detection
keys off the pushed commit's own diff, so a service whose only change rode in
a prior fast-forwarded commit needs a fresh memory-api/ change to rebuild."""
import main


def test_column_create_request_defaults():
    r = main.ColumnCreateRequest(name="Up Next", stage="ready")
    assert r.tenant_slug == "default"
    assert r.position is None


def test_column_dict_shape():
    row = {"id": "abc", "name": "Backlog", "stage": "spark",
           "position": 1.0, "on_enter": None, "color": "#818CF8"}
    assert main._column_dict(row) == {
        "id": "abc", "name": "Backlog", "stage": "spark",
        "position": 1.0, "on_enter": None, "color": "#818CF8",
    }


def test_clean_tags_dedups_and_strips():
    assert main._clean_tags(["  a ", "A", "", "b", "b"]) == ["a", "b"]
    assert main._clean_tags(None) == []


def test_seed_stages_are_valid():
    for _, stage in main._SEED_COLUMNS:
        assert stage in main.BOARD_STAGES
