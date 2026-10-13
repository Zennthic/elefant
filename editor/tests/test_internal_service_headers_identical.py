"""Every Python service carries its own copy of `_internal_service_headers()`.

It would live in one shared module, but each image is built from its own
directory (`build: ./editor`, `context: ./${{ matrix.service }}` in CI, the
same in the release and install workflows), so a shared file would mean
widening four build contexts and every Dockerfile's COPY lines for a
six-line function. The copies stay; this test is what keeps them one
function. A service that starts sending a different header, or none, fails
closed at its peer, and that shows up as a 401 somewhere far from the edit.

Docstrings are allowed to differ — each says which peer that service calls.

The editor's copy lives in editor/internal_service.py rather than main.py
since CQ-04 phase 2, so the wiki router can use it without importing main.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SERVICES = ("editor", "embedder", "mcp", "memory-api")
NAME = "_internal_service_headers"
# Where each service keeps its copy, relative to the service's directory.
SOURCE = {"editor": "internal_service.py"}


def _body_without_docstring(service: str) -> str:
    rel = f"{service}/{SOURCE.get(service, 'main.py')}"
    path = REPO / rel
    if not path.exists():
        pytest.skip(f"{rel} is not checked out beside the editor")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == NAME]
    assert len(found) == 1, f"{rel}: expected one top-level {NAME}(), found {len(found)}"
    fn = found[0]
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    parts = [fn.args, fn.returns, *fn.decorator_list, *body]
    return "\n".join(ast.dump(p) if p is not None else "None" for p in parts)


def test_every_service_has_the_same_internal_service_headers():
    bodies = {s: _body_without_docstring(s) for s in SERVICES}
    reference = bodies["editor"]
    drifted = [s for s, b in bodies.items() if b != reference]
    assert not drifted, (
        f"{NAME}() differs from editor/{SOURCE['editor']} in: {', '.join(drifted)}. "
        "Change all four copies together."
    )
