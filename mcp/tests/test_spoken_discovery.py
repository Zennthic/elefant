"""Tool descriptions have to be findable from the words a person SAYS.

A client does not load 50-odd tool schemas up front - it sees a
stub per tool and runs a keyword search over the DESCRIPTIONS to decide which
to load. So a description written in our vocabulary is a tool the caller
cannot find: `memory_capture` opened "Cheap streaming capture into the memory
hot tier", and a search for "memory capture" returned `memory_introspect`,
`memory_provenance` and `memory_update` instead of it. Nobody says "cheap
streaming capture".

WHAT THIS TEST IS, AND WHAT IT IS NOT. The real matcher lives in the client
and we do not have it. `_rank` below is a deliberately plain stand-in -
BM25 over name + description - and a phrase ranking first here is not proof
it ranks first there. What it IS is a regression
guard with a sharp edge: the phrases are spoken English, none of them is
lifted from the text they are matched against, and a description rewritten
back into house jargon stops matching them. Run against the descriptions as
they stood before this card, the same 56 phrases put the right tool first
22 times and in the top three 33 times; after the rewrite that is 48 and 56.
The threshold below is set from what the rewrite actually achieves, with a
little headroom, not from what would be nice.

The lint-shaped tests underneath (a spoken verb in every opening line, the
server name where a search for it will find it, the routing block on the four
tools whose spoken intents overlap) are the part that does not depend on the
scorer at all.
"""
import math
import re

import pytest


@pytest.fixture(scope="module")
def tools():
    """(name, description) for every registered tool, as the client sees it."""
    import main
    return [(t.name, t.description or "") for t in main.mcp._tool_manager.list_tools()]


@pytest.fixture(scope="module")
def instructions():
    import main
    return main.mcp.instructions or ""


# ── a stand-in for the client's keyword matcher ───────────────────────────

def _tok(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, crudely singularised.

    Plurals are stripped symmetrically on both sides so "my tasks" can match
    "task" - a spoken phrase and a written description rarely agree on number.
    """
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def _rank(phrase: str, tools: list[tuple[str, str]]) -> list[str]:
    """Tool names, best match first, by BM25 over name + description.

    BM25 rather than plain token overlap because of the length term: our
    descriptions differ by an order of magnitude in size, and without
    normalisation the longest one wins every loose query simply by
    containing more words. Any real matcher normalises; a stand-in that
    does not would have us writing descriptions to beat an artefact of the
    stand-in.
    """
    k1, b = 1.2, 0.75
    docs = {name: _tok(name + " " + desc) for name, desc in tools}
    n = len(docs)
    avg_len = sum(len(d) for d in docs.values()) / n
    df: dict[str, int] = {}
    for toks in docs.values():
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    # A token every tool carries (like "elefant") separates nothing, so it
    # scores ~zero rather than tilting the ranking.
    idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    q = set(_tok(phrase))
    scored = []
    for name, toks in docs.items():
        dl = len(toks)
        score = 0.0
        for t in q:
            tf = toks.count(t)
            if not tf:
                continue
            score += idf[t] * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_len))
        scored.append((score, name))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [name for _, name in scored]


# ── the spoken phrase set ─────────────────────────────────────────────────
# Said out loud, the way they were said in the sessions that produced the
# card. No phrase is copied from the description it is expected to find.

PHRASES = [
    ("remember that for later", "memory_capture"),
    ("save this so I don't forget it", "memory_capture"),
    ("make a note of that", "memory_capture"),
    ("remember this exactly, word for word", "memory_add"),
    ("what do you remember about the printer", "memory_search"),
    ("what do I know about the deploy process", "search"),
    ("did I write anything about postgres", "search"),
    ("that's wrong, it's actually the other way round", "memory_update"),
    ("forget that, delete it", "memory_delete"),
    ("where did that come from", "memory_provenance"),
    ("how is my memory doing, give me the stats", "memory_introspect"),
    ("what did you save in that session", "memory_session"),
    ("show me the earlier versions of that memory", "memory_revisions"),
    ("list what you have saved", "memory_list"),
    ("show me the audit trail", "memory_events"),
    ("pull up the runbook page", "get_wiki_page"),
    ("what pages do I have", "wiki_tree"),
    ("write it up as a new page", "wiki_write"),
    ("tweak the wording on that page", "wiki_edit"),
    ("put this screenshot in the page", "wiki_upload_image"),
    ("embed that mockup image in the design page", "wiki_upload_image"),
    ("add a section to that page", "wiki_edit"),
    ("rename that page and file it under projects", "wiki_move"),
    ("get rid of that page", "wiki_delete"),
    ("who edited this page", "wiki_history"),
    ("what did it say before", "wiki_revision"),
    ("put it back the way it was", "wiki_revert"),
    ("compare those two versions", "wiki_diff"),
    ("rebuild the search index", "wiki_reindex"),
    ("is the reindex finished yet", "reindex_status"),
    ("what's on the board", "card_list"),
    ("what am I working on", "card_list"),
    ("put a task on the board", "card_add"),
    ("file a ticket for that", "card_add"),
    ("note to self, order more filament", "note_to_self"),
    ("what should I work on next", "card_next"),
    ("pull up that card", "card_get"),
    ("move that card to in progress", "card_move"),
    ("reword the title of that card", "card_update"),
    ("mark that card done", "card_close"),
    ("leave a comment on that card", "card_comment"),
    ("assign that card to me", "card_assignee"),
    ("that card has a deadline of next friday", "card_due"),
    ("who raised that card", "card_reporter"),
    ("loop her in on that card", "card_participant"),
    ("tag that card as a bug", "card_label"),
    ("which codebase does that card belong to", "card_repo"),
    ("what columns does the board have", "board_columns"),
    ("connect those two cards", "card_link"),
    ("attach that write-up to the card", "card_link_wiki"),
    ("claim that card while it is evaluated", "card_eval_lock"),
    ("release the lock on that card", "card_eval_unlock"),
    ("where is that function in the code", "git_search"),
    ("open that file and show me the lines", "git_read"),
    ("which repos do you have", "list_sources"),
    ("browse the files in that repo", "source_tree"),
    ("ignore that repo for this session", "set_source_state"),
    ("how am I configured", "get_user_settings"),
    ("change one of my settings", "set_user_settings"),
    ("sketch a flowchart of the signup process", "diagram_create"),
    ("rename that box in the diagram", "diagram_patch"),
    ("let me see that chart drawn out", "diagram_render"),
    ("which diagrams have we got", "diagram_list"),
    ("give me the mermaid code behind that diagram", "diagram_get"),
    # Card #416: an agent unsure which tool it needs.
    ("which tool should I use for this", "tool_index"),
    ("help, what can you do", "tool_index"),
    ("find anything about the dispatch routine", "search"),
]

# What the rewrite achieves on the stand-in scorer (0.86), less headroom for
# a description edit that costs a phrase or two. Before the rewrite: 0.39.
TOP1_FLOOR = 0.80


@pytest.mark.parametrize("phrase,expected", PHRASES, ids=[p for p, _ in PHRASES])
def test_a_spoken_phrase_finds_its_tool(phrase, expected, tools):
    """Every spoken phrase puts its tool in the top three candidates."""
    ranked = _rank(phrase, tools)
    assert expected in ranked[:3], (
        f"{phrase!r} → {ranked[:3]} (wanted {expected})"
    )


def test_most_spoken_phrases_land_on_the_tool_outright(tools):
    """And most of them rank it FIRST, which is the one a caller loads."""
    misses = [
        (p, _rank(p, tools)[0]) for p, want in PHRASES if _rank(p, tools)[0] != want
    ]
    rate = 1 - len(misses) / len(PHRASES)
    assert rate >= TOP1_FLOOR, f"top-1 {rate:.2f} < {TOP1_FLOOR}; missed: {misses}"


def test_the_repro_on_the_card_is_fixed(tools):
    """The exact search from the card's live repro, which returned three
    other memory tools and not this one."""
    assert _rank("memory capture", tools)[0] == "memory_capture"


# ── the parts that do not depend on the scorer ────────────────────────────

# Verbs and question openers a person says out loud. One of these has to
# open every tool, or the tool is described in our words rather than theirs.
SPOKEN_OPENERS = {
    "add", "assign", "archive", "attach", "browse", "change", "check", "claim",
    "close", "comment", "compare", "connect", "correct", "create", "delete",
    "diff", "draft", "edit", "enable", "file", "find", "fix", "forget", "get",
    "give", "how", "label", "link", "list", "load", "lock", "look", "mark",
    "move", "note", "open", "pull", "put", "read", "rebuild", "recall",
    "reindex", "release", "remember", "remove", "rename", "reorganise",
    "reword", "roll", "save", "search", "set", "show", "shift", "start",
    "tag", "trace", "turn", "undo", "unlock", "update", "what", "when",
    "where", "which", "who", "why", "write",
}


def _opening(desc: str) -> str:
    for line in desc.splitlines():
        if line.strip():
            return line.strip()
    return ""


def test_every_tool_opens_with_a_word_a_person_would_say(tools):
    bad = []
    for name, desc in tools:
        first = _opening(desc)
        word = re.split(r"[^a-zA-Z]+", first.lower(), maxsplit=1)[0]
        if word not in SPOKEN_OPENERS:
            bad.append((name, first[:60]))
    assert not bad, f"opening line is not spoken language: {bad}"


def test_every_tool_names_the_server_it_belongs_to(tools):
    """A search for "Elefant" has to return Elefant's tools. Before this
    card the name appeared in three descriptions out of fifty-two."""
    missing = [n for n, d in tools if "elefant" not in d.lower()]
    assert not missing, f"description never names Elefant: {missing}"


def test_the_front_door_tools_carry_the_misheard_spelling(tools):
    """Speech-to-text writes "elephant". The tools a person reaches for by
    naming the server say so, so the mis-transcription still finds them."""
    front_door = {
        "search", "memory_search", "memory_capture", "get_wiki_page",
        "wiki_write", "card_add", "card_list", "note_to_self", "board_columns",
    }
    by_name = dict(tools)
    missing = [n for n in sorted(front_door) if "elephant" not in by_name[n].lower()]
    assert not missing, f"front-door tool missing the spoken spelling: {missing}"


def test_the_overlapping_four_say_which_one_to_use(tools):
    """memory_capture / memory_add / note_to_self / card_add all answer
    "save this somewhere", and a caller loads ONE of them. Each has to be
    able to redirect to the other three on its own."""
    by_name = dict(tools)
    for name in ("memory_capture", "memory_add", "note_to_self", "card_add"):
        desc = by_name[name]
        assert "WHICH ONE OF THESE FOUR" in desc, f"{name} has no routing block"
        for other in ("memory_capture", "memory_add", "note_to_self", "card_add"):
            assert other in desc, f"{name} does not route to {other}"


def test_the_server_instructions_carry_the_phrasebook(instructions):
    assert "elephant" in instructions.lower()
    assert "SPOKEN PHRASEBOOK" in instructions
    assert "LOAD BEFORE YOU CALL" in instructions
    # The phrasebook is only worth anything if it names real tools.
    for name in ("memory_capture", "note_to_self", "card_list", "wiki_edit"):
        assert name in instructions
