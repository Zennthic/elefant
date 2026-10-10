"""docs/configuration.md, held complete against the code.

A self-hoster tunes Elefant through environment variables, and the only list of
them is docs/configuration.md. Nothing fails when somebody adds an
`os.getenv("ELEFANT_NEW_THING")` and forgets the page, so this test does.

It parses every Python file in the repo, collects the name of every environment
variable the code reads, and asserts each one is on the page in backticks, or is
on ALLOWLIST below with a reason. The scan reads the AST rather than grepping, so
a call split over two lines is still found, and it also follows the small
`_env_int(name, default)` style helpers that wrap a read.
"""
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
REFERENCE = REPO / "docs" / "configuration.md"

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}

# Calls whose first argument is the name of the variable read.
READ_CALLS = {
    "os.environ.get", "os.getenv", "os.environ.setdefault", "os.environ.pop",
    "environ.get", "getenv",
}

# Read by tests and CI, never by a running service. Each needs a reason.
ALLOWLIST = {
    "BASE_URL": "tests/smoke.py: the deployment the post-deploy smoke test hits",
    "SMOKE_READY_WAIT": "tests/smoke.py: how long the smoke test waits for readiness",
    "SMOKE_SESSION_COOKIE": "tests/smoke.py: a session cookie CI hands the smoke test",
    "SMOKE_REQUIRE_AUTH": "tests/smoke.py: opt-in switch that fails smoke on a missing cookie",
    "ELEFANT_TEST_PG_DSN": "memory-api integration tests: the throwaway Postgres they run against",
    "CI": "memory-api tests: set by the CI runner to tell the suite where it is",
    "PATH": "editor tests: put a stub binary first on the PATH of a subprocess",
}


def _python_files():
    for p in REPO.rglob("*.py"):
        if SKIP_DIRS.intersection(p.relative_to(REPO).parts):
            continue
        yield p


def _literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _helpers(tree) -> set[str]:
    """Functions that pass their first parameter straight to an env read, like
    editor/login_throttle.py's `_env_int(name, default)`."""
    found = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.args.args:
            continue
        param = fn.args.args[0].arg
        for call in ast.walk(fn):
            if (isinstance(call, ast.Call) and ast.unparse(call.func) in READ_CALLS
                    and call.args and isinstance(call.args[0], ast.Name)
                    and call.args[0].id == param):
                found.add(fn.name)
    return found


def env_reads(source: str) -> set[str]:
    """Every environment variable name `source` reads by a literal name."""
    tree = ast.parse(source)
    calls = READ_CALLS | _helpers(tree)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) in calls and node.args:
            name = _literal(node.args[0])
        elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
              and ast.unparse(node.value) == "os.environ"):
            name = _literal(node.slice)
        else:
            continue
        if name:
            names.add(name)
    return names


def _all_reads() -> dict[str, set[str]]:
    reads: dict[str, set[str]] = {}
    for p in _python_files():
        for name in env_reads(p.read_text(encoding="utf-8")):
            reads.setdefault(name, set()).add(str(p.relative_to(REPO)))
    return reads


def _documented(name: str, text: str) -> bool:
    return f"`{name}`" in text


def test_the_scan_finds_the_services_reads():
    """Guards the scanner itself: if it ever stops finding reads, every other
    assertion here passes vacuously."""
    reads = _all_reads()
    for expected in ("SESSION_SECRET", "INTERNAL_SERVICE_TOKEN", "POSTGRES_PASSWORD",
                     "COMPACTOR_ENABLED", "LOGIN_THROTTLE_WINDOW",
                     "ELEFANT_DOMAIN_VERIFY_HOST"):
        assert expected in reads, f"the scan no longer finds {expected}"


def test_every_variable_the_code_reads_is_documented():
    text = REFERENCE.read_text(encoding="utf-8")
    missing = sorted(
        f"{name} (read in {', '.join(sorted(files))})"
        for name, files in _all_reads().items()
        if name not in ALLOWLIST and not _documented(name, text)
    )
    assert not missing, (
        "These environment variables are read by the code but are not in "
        "docs/configuration.md. Add a row for each (default, required or "
        "optional, what it does, what a change needs), or, if only tests or CI "
        "read it, add it to ALLOWLIST in this file with a reason:\n  "
        + "\n  ".join(missing))


def test_the_allowlist_is_not_stale():
    reads = _all_reads()
    stale = sorted(n for n in ALLOWLIST if n not in reads)
    assert not stale, f"Nothing reads these any more; take them off ALLOWLIST: {stale}"


def test_the_allowlist_holds_nothing_the_reference_documents():
    text = REFERENCE.read_text(encoding="utf-8")
    both = sorted(n for n in ALLOWLIST if _documented(n, text))
    assert not both, f"Documented and allowlisted at once; pick one: {both}"


def test_an_undocumented_read_is_caught():
    """The mutation check, kept as a test: each way of reading a variable is
    found, and a name that is not on the page is reported as missing."""
    source = '''
import os

def _env_float(key, default):
    return float(os.environ.get(key, default))

A = os.getenv("ELEFANT_NEW_THING")
B = os.environ.get(
    "ELEFANT_SPLIT_THING", "x")
C = os.environ["ELEFANT_INDEXED_THING"]
D = _env_float("ELEFANT_HELPER_THING", 1.0)
os.environ["ELEFANT_WRITTEN_THING"] = "a write, not a read"
'''
    reads = env_reads(source)
    assert reads == {"ELEFANT_NEW_THING", "ELEFANT_SPLIT_THING",
                     "ELEFANT_INDEXED_THING", "ELEFANT_HELPER_THING"}
    text = REFERENCE.read_text(encoding="utf-8")
    assert not any(_documented(n, text) for n in reads)


def test_the_reference_is_linked_from_the_entry_points():
    for rel in ("README.md", ".env.example", "docs/index.md",
                "docs/install/docker-compose.md", "docs/install/kubernetes.md"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert re.search(r"configuration\.md", text), f"{rel} does not link docs/configuration.md"
