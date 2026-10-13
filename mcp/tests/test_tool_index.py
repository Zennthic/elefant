"""Card #416: a "start here" tool an agent reaches for by reflex.

A caller that does not know a tool's name searches the descriptions, and a
vague query matches literal words: "card add routine wiki page" found card_add
and note_to_self, not the search the caller wanted. tool_index is the single,
obviously-named entry point that lists every tool by purpose instead.

It is only worth anything while it is complete and true, so these tests hold
it to the registered tool list in both directions.
"""
import asyncio

import pytest


@pytest.fixture(scope="module")
def main_mod():
    import main
    return main


@pytest.fixture(scope="module")
def registered(main_mod):
    return {t.name for t in main_mod.mcp._tool_manager.list_tools()}


def _indexed(main_mod):
    return [name for _, _, tools in main_mod._TOOL_INDEX for name, _ in tools]


def test_every_registered_tool_is_in_the_index(main_mod, registered):
    missing = registered - set(_indexed(main_mod))
    assert not missing, f"add these to _TOOL_INDEX in mcp/main.py: {sorted(missing)}"


def test_the_index_names_only_real_tools_once_each(main_mod, registered):
    names = _indexed(main_mod)
    assert len(names) == len(set(names)), "a tool is listed in two groups"
    ghosts = set(names) - registered
    assert not ghosts, f"the index lists tools the server does not serve: {sorted(ghosts)}"


def test_the_full_index_lists_every_tool_and_points_at_search(main_mod, registered):
    out = asyncio.run(main_mod.tool_index())
    for name in registered:
        assert f"`{name}`" in out, name
    assert "Call `search`" in out


def test_a_topic_narrows_the_list(main_mod):
    out = asyncio.run(main_mod.tool_index(topic="Search"))
    assert "`wiki_search_only`" in out and "`memory_search`" in out
    assert "`card_add`" not in out


def test_an_unknown_topic_names_the_real_ones(main_mod):
    out = asyncio.run(main_mod.tool_index(topic="nonsense"))
    assert "No tool group named `nonsense`" in out
    assert "board" in out and "wiki" in out


def test_the_search_tools_say_to_start_with_search(main_mod):
    descs = {t.name: t.description for t in main_mod.mcp._tool_manager.list_tools()}
    assert "CALL THIS FIRST WHEN UNSURE" in descs["search"]
    assert "find anything" in descs["search"]
    for name in ("search", "wiki_search_only", "memory_search"):
        assert "tool_index" in descs[name], name


def test_the_server_instructions_open_with_start_here(main_mod):
    instr = main_mod.mcp.instructions
    assert "START HERE" in instr
    assert instr.index("tool_index") < instr.index("OPERATING MODE")
