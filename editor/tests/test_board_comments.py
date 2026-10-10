"""Comment pass-throughs: who a comment is attributed to, and who may edit it.

Author-only editing is only worth something if the author cannot be claimed by
asking. The browser sends what it was typed; the editor is where the session
becomes an identity, so the author has to be stamped there and never read out
of the request body.
"""
import inspect

import pytest

import main
from routers import boards


COMMENT_ROUTES = [
    boards.board_comment_add,
    boards.board_comment_update,
    boards.board_comment_delete,
]


@pytest.mark.parametrize("fn", COMMENT_ROUTES, ids=lambda f: f.__name__)
def test_the_caller_is_taken_from_the_session(fn):
    src = inspect.getsource(fn)
    assert "_comment_actor(claims)" in src, (
        f"{fn.__name__} does not stamp the caller from the session")


@pytest.mark.parametrize("fn", COMMENT_ROUTES, ids=lambda f: f.__name__)
def test_the_author_is_never_read_out_of_the_body(fn):
    """The one that matters. If the body could name the author, anybody could
    post as anybody and then edit their own forgery."""
    src = inspect.getsource(fn)
    for claim in ('body.get("author")', 'body.get("actor")'):
        assert claim not in src, (
            f"{fn.__name__} takes {claim} from the request — an author that can "
            f"be claimed by asking is not an author")


@pytest.mark.parametrize("fn", COMMENT_ROUTES + [boards.board_comments_list],
                         ids=lambda f: f.__name__)
def test_every_comment_route_requires_a_session(fn):
    """A comment is as private as the card it is on."""
    sig = inspect.signature(fn)
    assert "claims" in sig.parameters, f"{fn.__name__} has no authentication"
    assert sig.parameters["claims"].default.dependency is boards.require_authenticated


@pytest.mark.parametrize("fn", COMMENT_ROUTES + [boards.board_comments_list],
                         ids=lambda f: f.__name__)
def test_every_comment_route_resolves_the_tenant_from_the_session(fn):
    """Not from the query string alone — the same rule every other board route
    follows, and the one that keeps a thread inside its own org."""
    src = inspect.getsource(fn)
    assert "_effective_tenant_slug(claims, tenant_slug, session)" in src


def test_comment_authors_are_resolved_to_names():
    """A thread of raw `user:9f2c…` would be the one place the board still
    showed references at somebody."""
    src = inspect.getsource(boards._enrich_people)
    assert 'data.get("comments")' in src, \
        "comment authors are not collected for people enrichment"


def test_the_directory_tells_the_caller_who_they_are():
    """The board needs to know which comments are the caller's own to offer the
    author-only controls. That answer is the server's to give — a browser that
    decides who it is can decide to be somebody else."""
    src = inspect.getsource(boards.board_people_directory)
    assert '"me": _comment_actor(claims)' in src
