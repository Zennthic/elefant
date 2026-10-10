"""The security pages, checked against the code they describe.

`security/` is where a reader goes to decide whether to trust Elefant with
their data, and it names files and functions as evidence for each control. A
page that cites a file which was moved, or a function which was renamed, is
evidence that points nowhere, and nothing else fails when it happens. These
tests resolve every cited path and function, and keep the pages from claiming
a certification the project does not hold.

Relative Markdown links are covered by test_markdown_links.py; this covers the
paths written as code (`editor/config_db.py`), which that test skips.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
PAGES = sorted((REPO / "security").glob("*.md"))

CODE = re.compile(r"`([^`\n]+)`")
FUNC = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)$")
PATH = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.{}-]+)*/?$")
TOP_LEVEL = {p.name for p in REPO.iterdir() if not p.name.startswith(".")}


def _repo_path(token):
    """The token as a repository path, or None when it is not one.

    A path is anything whose first segment is a top-level entry of the repo,
    so `editor/main.py` and `deploy/k8s/base/` count while `/data/config.db`
    (a container path), `cards/{card-slug}/` and `{slug}_wiki` do not.
    """
    if not PATH.match(token) or "{" in token:
        return None
    first = token.split("/", 1)[0]
    if first not in TOP_LEVEL:
        return None
    if "/" not in token and "." not in token:
        return None
    return token


def _cited(page):
    """(line number, functions, paths) for each line that cites code."""
    for number, line in enumerate(page.read_text().splitlines(), 1):
        tokens = CODE.findall(line)
        funcs = [m.group(1) for t in tokens if (m := FUNC.match(t))]
        paths = [p for t in tokens if (p := _repo_path(t))]
        if funcs or paths:
            yield number, funcs, paths


def _py_sources():
    return [p for p in REPO.rglob("*.py")
            if "node_modules" not in p.parts and "/tests/" not in p.as_posix()]


@pytest.fixture(scope="module")
def all_defs():
    names = set()
    for p in _py_sources():
        names.update(re.findall(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(",
                                p.read_text(errors="replace"), re.M))
    return names


def test_there_are_security_pages():
    assert PAGES, "security/*.md not found"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_cited_path_exists(page):
    missing = [f"{page.name}:{n}: {p}"
               for n, _, paths in _cited(page) for p in paths
               if not (REPO / p.rstrip("/")).exists()]
    assert not missing, "cited paths that do not exist:\n" + "\n".join(missing)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_cited_function_exists(page, all_defs):
    """A function named on a line that also names files must be defined in
    one of those files; with no file on the line, somewhere in the repo."""
    missing = []
    for number, funcs, paths in _cited(page):
        files = [REPO / p for p in paths if (REPO / p).is_file()]
        for fn in funcs:
            if files:
                pat = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(fn)}\s*\(", re.M)
                if not any(pat.search(f.read_text(errors="replace")) for f in files):
                    missing.append(f"{page.name}:{number}: {fn}() not in {', '.join(paths)}")
            elif fn not in all_defs:
                missing.append(f"{page.name}:{number}: {fn}() not defined anywhere")
    assert not missing, "cited functions that do not exist:\n" + "\n".join(missing)


CERTIFICATION_CLAIMS = re.compile(
    r"Framework Mapping|Type II|Trust Service|\bcompliant\b|\bcertified by\b|"
    r"FedRAMP|HIPAA|TLS 1\.[0-3]\+?\s+enforced",
    re.I,
)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_certification_claims(page):
    """Elefant holds no certification. A page may help an operator map
    controls to their own programme, but must not read as an attestation."""
    text = page.read_text()
    hits = [m.group(0) for m in CERTIFICATION_CLAIMS.finditer(text)]
    assert not hits, f"{page.name} reads as a certification claim: {hits}"
    assert "not certified" in text, f"{page.name} must say Elefant is not certified"


def test_tls_and_config_db_statements_match_the_code():
    """The two facts the pages once contradicted the README on."""
    compose = (REPO / "docker-compose.yml").read_text()
    assert re.search(r'POSTGRES_EDITOR_DSN:\s*"\$\{POSTGRES_EDITOR_DSN:-\}"', compose)
    assert "POSTGRES_EDITOR_DSN" not in "".join(
        p.read_text() for p in (REPO / "deploy" / "k8s" / "base").glob("*.yaml"))
    assert 'os.environ.get("DB_PATH", "/data/config.db")' in \
        (REPO / "editor" / "config_db.py").read_text()

    readme = (REPO / "README.md").read_text()
    data = (REPO / "security" / "data-management.md").read_text()
    for name, text in (("README.md", readme), ("data-management.md", data)):
        assert "Nothing in the stack terminates TLS" in text, name
        assert "SQLite at `/data/config.db`" in text, name
    assert "Config DB" not in readme
