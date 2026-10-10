"""The card `repo` field: canonicalization, storage, filtering, and backfill.

`repo` is a card's GitHub repository promoted from a line of body prose
(`repo: github.com/acme/widgets`) to a structured, filterable attribute, so
several projects can share one board and still be sliced apart.

It is scalar rather than an array — one card, one repo, matching one dispatch
routine per repo — and it is canonicalized to `owner/name` on the way in. That
canonical form is the point: the value exists to be COMPARED, and two spellings
of one repository would split a filter in two.

Pure logic plus SQL-string assertions, in the style of the other board tests.
The backfill is exercised against a fake connection because it is the one piece
that runs exactly once, unattended, against data nobody is watching.
"""
import inspect

import pytest
from fastapi import HTTPException

import main
from conftest import card_insert_lists


# ── canonicalization ───────────────────────────────────────────────────────

@pytest.mark.parametrize("given", [
    "zennthic/elefant",
    "  zennthic/elefant  ",
    "github.com/zennthic/elefant",
    "GitHub.com/zennthic/elefant",
    "www.github.com/zennthic/elefant",
    "http://github.com/zennthic/elefant",
    "https://github.com/zennthic/elefant",
    "https://github.com/zennthic/elefant/",
    "https://www.github.com/zennthic/elefant.git",
    "git@github.com:zennthic/elefant.git",
    "git@github.com:zennthic/elefant",
])
def test_every_form_people_paste_normalizes_to_owner_name(given):
    """The browser URL, the clone URL, the SSH remote and the bare pair are all
    the same repository, so they must all store as one value — otherwise the
    board filter splits one project across several spellings."""
    assert main._clean_repo(given) == "zennthic/elefant"


def test_case_of_owner_and_name_is_preserved():
    """GitHub resolves owner and name case-insensitively but DISPLAYS the case
    it was given. Storing the spelling the user typed keeps the card reading
    the way they wrote it; matching is done case-insensitively in SQL."""
    assert main._clean_repo("Zennthic/Elefant") == "Zennthic/Elefant"


def test_empty_means_no_repo_rather_than_an_error():
    assert main._clean_repo("") == ""
    assert main._clean_repo("   ") == ""
    assert main._clean_repo(None) == ""


def test_dots_dashes_and_underscores_are_legal_in_both_segments():
    assert main._clean_repo("my-org/some_repo.js") == "my-org/some_repo.js"


@pytest.mark.parametrize("given,expected", [
    ("acme/widgets.", "acme/widgets"),
    ("github.com/acme/widgets.", "acme/widgets"),
    ("acme/widgets...", "acme/widgets"),
    ("github.com/acme/widgets.git.", "acme/widgets"),
])
def test_a_trailing_dot_is_punctuation_not_part_of_the_name(given, expected):
    """A dot is legal INSIDE a repo name, but GitHub rejects a name that ends
    in one. This is what makes the backfill safe: the body convention is
    written mid-sentence ("repo: github.com/acme/widgets. See wiki."), and
    storing `widgets.` would produce a card that matches no filter, ever."""
    assert main._clean_repo(given) == expected


def test_trimming_the_trailing_dot_does_not_touch_a_dot_inside_the_name():
    assert main._clean_repo("my-org/some_repo.js") == "my-org/some_repo.js"
    assert main._clean_repo("my-org/repo.js.git") == "my-org/repo.js"


@pytest.mark.parametrize("given", [
    "elefant",                              # no owner
    "zennthic/elefant/extra",               # not a repo, a path inside one
    "https://github.com/zennthic",          # owner page, not a repo
    "https://gitlab.com/zennthic/elefant",  # a real repo, but not GitHub
    "git@gitlab.com:zennthic/elefant.git",
    "see the readme",
    "owner/",
    "/name",
    "own er/name",
])
def test_anything_that_is_not_a_github_repo_is_refused(given):
    """Storing junk would be the quiet failure: the card would claim a repo,
    the filter would never match it, and nobody would know which is wrong.
    Non-GitHub hosts are refused rather than silently filed as GitHub."""
    with pytest.raises(HTTPException) as exc:
        main._clean_repo(given)
    assert exc.value.status_code == 400
    assert "owner/name" in exc.value.detail


def test_the_refusal_quotes_the_value_it_refused():
    with pytest.raises(HTTPException) as exc:
        main._clean_repo("not a repo")
    assert "not a repo" in exc.value.detail


# ── filter values are lenient where stored values are strict ───────────────

def test_filter_value_gets_the_same_canonicalization():
    assert main._match_repo("https://github.com/zennthic/elefant") == "zennthic/elefant"


def test_a_malformed_filter_matches_nothing_instead_of_raising():
    """A stale filter in a board URL should show an empty board, not a 500.
    Writing junk is an error; asking for junk is just a query with no hits."""
    assert main._match_repo("not a repo") == "not a repo"
    assert main._match_repo("") == ""


# ── request shape ──────────────────────────────────────────────────────────

def test_card_add_request_defaults_repo_to_empty():
    assert main.CardAddRequest(title="Test").repo == ""


def test_card_update_distinguishes_unchanged_from_cleared():
    """A scalar needs this distinction spelled out where the list fields get it
    for free: None leaves the repo alone, "" is a deliberate clear. Collapse
    them and either every title edit drops the repo, or a repo set by mistake
    can never be removed."""
    assert main.CardUpdateRequest().repo is None
    assert main.CardUpdateRequest(repo="").repo == ""
    assert main.CardUpdateRequest(repo="zennthic/elefant").repo == "zennthic/elefant"


# ── storage ────────────────────────────────────────────────────────────────

def test_cards_table_declares_repo_column():
    src = inspect.getsource(main._ensure_schema)
    assert "repo         TEXT NOT NULL DEFAULT ''" in src
    # Boards created before repo existed gain the column at startup — this repo
    # has no migration runner, the ADD COLUMN IF NOT EXISTS is it.
    assert "ADD COLUMN IF NOT EXISTS repo TEXT NOT NULL DEFAULT ''" in src


def test_backfill_runs_only_when_the_column_is_new():
    """ADD COLUMN IF NOT EXISTS cannot report whether it did anything, so the
    schema asks information_schema first. Without that gate the backfill would
    run on every startup and keep re-deriving a repo a human deliberately
    cleared."""
    src = inspect.getsource(main._ensure_schema)
    assert "information_schema.columns" in src
    assert "if not repo_existed:" in src
    assert "_backfill_card_repos(conn)" in src


def test_card_add_inserts_repo():
    src = inspect.getsource(main.card_add)
    assert "repo = _clean_repo(req.repo)" in src
    # The INSERT names its columns explicitly, so repo has to be in both lists.
    # Checked per column rather than as one adjacent run of names: the next
    # field added to a card lands BETWEEN two of these, and a test that fails
    # for that is reporting the column order, not a bug.
    cols, values = card_insert_lists(src)
    assert "repo" in cols
    assert ":repo" in values


def test_card_add_response_carries_repo():
    """card_add hand-builds its response dict rather than going through
    _card_dict, so it is the one place repo can go missing on create."""
    assert '"repo": repo' in inspect.getsource(main.card_add)


def test_card_dict_serialises_repo():
    src = inspect.getsource(main._card_dict)
    assert '"repo":         row["repo"] or ""' in src


def test_card_update_sets_repo_only_when_given():
    src = inspect.getsource(main.card_update)
    assert "if req.repo is not None:" in src
    assert 'sets.append("repo = :repo")' in src
    assert 'params["repo"] = _clean_repo(req.repo)' in src


# ── filtering ──────────────────────────────────────────────────────────────

def test_card_list_filters_by_repo_case_insensitively():
    src = inspect.getsource(main.card_list)
    assert "lower(c.repo) = lower(:repo)" in src


def test_card_list_repo_filter_is_canonicalized_not_taken_raw():
    """Filtering by a pasted browser URL has to find cards stored as
    owner/name, or the chip a user clicks on one card finds nothing."""
    src = inspect.getsource(main.card_list)
    # The canonicalized value is what reaches the query — never the raw input.
    assert "_match_repo(repo)" in src
    assert 'params["repo"] = repo' not in src


def test_card_list_repo_filter_accepts_a_bare_name():
    """`repo: elefant` is what people type in the board's search bar, and
    nobody writing it means a repo with no owner — a card's repo always has
    one. Without this the filter canonicalizes to `elefant`, compares equal to
    nothing, and the search silently returns an empty board."""
    src = inspect.getsource(main.card_list)
    assert "split_part(c.repo, '/', 2)" in src


def test_card_list_bare_repo_match_uses_no_like_wildcards():
    """An underscore is legal in a repo name and is a single-character wildcard
    in LIKE, so matching the name segment with LIKE would make `my_repo` also
    find `myXrepo`. Equality on the segment keeps the name a name."""
    src = inspect.getsource(main.card_list)
    repo_branch = src[src.index("if repo:"):src.index("if reporter:")]
    # The SQL only — a comment explaining why LIKE is avoided is not a LIKE.
    sql = "\n".join(l for l in repo_branch.splitlines()
                    if "q +=" in l or l.strip().startswith('"'))
    assert "LIKE" not in sql.upper()


def test_card_list_owner_qualified_repo_still_matches_exactly():
    """A value that canonicalizes to owner/name must take the exact branch it
    always took — the bare-name path is additive, not a loosening of it."""
    src = inspect.getsource(main.card_list)
    repo_branch = src[src.index("if repo:"):src.index("if reporter:")]
    assert 'if "/" in canon:' in repo_branch
    assert "lower(c.repo) = lower(:repo)" in repo_branch


def test_card_list_search_covers_repo():
    """A repo the user can see on a card but cannot find by typing it would be
    a surprise — free-text search already reaches tags and labels."""
    assert "c.repo ILIKE :q" in inspect.getsource(main.card_list)


# ── disaster recovery ──────────────────────────────────────────────────────

def test_bcdr_restore_carries_repo():
    """The restore INSERT enumerates every card column by hand, so a new one
    that isn't added here is silently dropped on restore — the kind of loss
    nobody notices until they need the backup."""
    src = inspect.getsource(main._bcdr_insert_cards)
    assert "aliases, labels, repo," in src
    assert '"repo": r.get("repo") or ""' in src


# ── backfill from the body convention ──────────────────────────────────────

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeConn:
    """Enough of a SQLAlchemy connection for the backfill: it answers the one
    SELECT with canned rows and records every UPDATE it is asked to make."""

    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def execute(self, statement, params=None):
        sql = str(statement)
        if sql.strip().upper().startswith("SELECT"):
            return _FakeResult(self._rows)
        self.updates.append(params)
        return _FakeResult([])


def _backfill(*bodies):
    conn = _FakeConn([{"id": f"id-{i}", "body": b} for i, b in enumerate(bodies)])
    filled = main._backfill_card_repos(conn)
    return filled, {u["id"]: u["repo"] for u in conn.updates}


def test_backfill_reads_the_body_convention_it_replaces():
    filled, updates = _backfill("repo: github.com/acme/widgets. Do the thing.")
    assert filled == 1
    assert updates["id-0"] == "acme/widgets"


def test_backfill_accepts_the_url_forms_the_convention_was_written_in():
    filled, updates = _backfill(
        "repo: https://github.com/zennthic/elefant",
        "repo:github.com/acme/widgets",
        "Repo: https://www.github.com/example/project. See wiki.",
    )
    assert filled == 3
    assert updates["id-0"] == "zennthic/elefant"
    assert updates["id-1"] == "acme/widgets"
    assert updates["id-2"] == "example/project"


def test_backfill_ignores_a_bare_owner_name_after_repo():
    """Deliberately narrow. Prose is full of slashed pairs, and a WRONG repo on
    a card is worse than a blank one: a blank field is visibly missing, a wrong
    one points a routine at the wrong repository."""
    filled, updates = _backfill("repo: the one we discussed / maybe elsewhere")
    assert filled == 0
    assert updates == {}


def test_backfill_leaves_bodies_with_no_convention_alone():
    filled, updates = _backfill("Just a card about repository hygiene.")
    assert filled == 0
    assert updates == {}


def test_backfill_only_considers_cards_with_no_repo_yet():
    """A repo already on the card — set by a human, or by an earlier run — is
    the truth, and prose in the body must never overwrite it."""
    src = inspect.getsource(main._backfill_card_repos)
    assert "WHERE repo = ''" in src


def test_backfill_survives_a_body_whose_convention_does_not_parse():
    """One unparseable card must not abort the migration for every other card
    on the board."""
    filled, updates = _backfill(
        "repo: github.com/zennthic/elefant",
        "repo: github.com/not-a-repo",
        "repo: github.com/acme/widgets",
    )
    assert filled == 2
    assert updates == {"id-0": "zennthic/elefant", "id-2": "acme/widgets"}
