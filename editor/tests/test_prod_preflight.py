"""scripts/prod-preflight.sh catches the 2026-09-29 prod incident (card #300).

That promotion pulled the non-root images onto a host whose compose file still
published nginx's old port 80 and whose volumes were root-owned, and the
editor could not read its own crypto.key. `docker compose ps` said healthy the
whole time. These tests run the real script against a stub `docker` that
answers the way that host would have, and assert the preflight fails on each of
the three problems — then that the same host, fixed, passes.

They also hold the healthchecks the incident showed were missing.
"""
import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "prod-preflight.sh"
COMPOSE = REPO / "docker-compose.yml"
ENV_EXAMPLE = REPO / ".env.example"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists() or shutil.which("bash") is None or shutil.which("git") is None,
    reason="the repo root, bash or git is not available beside the editor",
)

IMG = "ghcr.io/zennthic/elefant"
REQUIRED_ENV = {
    "ELEFANT_SESSION_SECRET": "s", "ELEFANT_PUBLIC_DOMAIN": "wiki.example.com",
    "ELEFANT_INTERNAL_SERVICE_TOKEN": "t", "POSTGRES_PASSWORD": "p",
    "ELEFANT_S3_ACCESS_KEY": "a", "ELEFANT_S3_SECRET_KEY": "k",
}

# A stand-in for `docker`, answering from a scenario file. It understands only
# the calls the script makes; anything else fails loudly so a new call in the
# script shows up here rather than passing by accident.
STUB = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    sc = json.load(open(os.environ["STUB_SCENARIO"]))
    a = sys.argv[1:]
    def out(s): sys.stdout.write(s); sys.exit(0)
    if a[0] == "compose":
        rest, i = [], 1
        while i < len(a):
            if a[i] in ("--project-directory", "-f"): i += 2; continue
            rest.append(a[i]); i += 1
        sub = rest[0]
        if sub == "pull": sys.exit(0)
        if sub == "config": out(json.dumps(sc["config"]))
        if sub == "ps" and "-q" in rest:
            svc = rest[-1]; out(("cid-" + svc + "\n") if svc in sc["running"] else "")
        if sub == "ps": out("\n".join(json.dumps(r) for r in sc.get("ps", [])))
        if sub == "logs": out(sc.get("logs", ""))
    if a[:2] == ["image", "inspect"]:
        img = a[-1]
        if img in sc["images"]: out(json.dumps(sc["images"][img]))
        sys.exit(1)
    if a[0] == "inspect":
        out(json.dumps(sc["running"][a[-1][len("cid-"):]]))
    if a[:2] == ["volume", "inspect"]:
        sys.exit(0 if a[2] in sc["volumes"] else 1)
    if a[0] == "run":
        vol = next(x for x in a if x.endswith(":/v:ro")).split(":")[0]
        out(sc["volumes"][vol])
    sys.stderr.write("stub docker: unexpected call %r\n" % (a,)); sys.exit(99)
''').lstrip()


def _config(nginx_port, with_volume_perms):
    vol = lambda s, t: {"type": "volume", "source": s, "target": t}  # noqa: E731
    services = {
        "nginx": {"image": f"{IMG}/nginx:main-abc1234",
                  "ports": [{"target": nginx_port, "published": "8321", "protocol": "tcp"}]},
        "editor": {"image": f"{IMG}/editor:main-abc1234",
                   "volumes": [vol("docs", "/docs"), vol("editor_data", "/data")]},
        "postgres": {"image": "postgres:16-alpine",
                     "volumes": [vol("postgres_data", "/var/lib/postgresql/data")]},
    }
    if with_volume_perms:
        services["volume-perms"] = {
            "image": f"{IMG}/editor:main-abc1234", "user": "0:0",
            "volumes": [vol("docs", "/docs"), vol("editor_data", "/data")]}
    return {"services": services,
            "volumes": {n: {"name": f"elefant_{n}"} for n in ("docs", "editor_data", "postgres_data")}}


IMAGES = {
    f"{IMG}/nginx:main-abc1234": {"User": "101:101", "ExposedPorts": {"8080/tcp": {}}},
    f"{IMG}/editor:main-abc1234": {"User": "10001:10001", "ExposedPorts": {"8080/tcp": {}}},
}


def _release_repo(tmp_path):
    """A git repo whose one commit is the release: this checkout's compose
    file and .env.example."""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(COMPOSE, src / "docker-compose.yml")
    shutil.copy(ENV_EXAMPLE, src / ".env.example")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "release"]):
        subprocess.run(["git", "-C", str(src), *cmd], check=True, env=env)
    sha = subprocess.run(["git", "-C", str(src), "rev-parse", "--short=7", "HEAD"],
                         check=True, capture_output=True, text=True, env=env).stdout.strip()
    return src, f"main-{sha}"


def _host(tmp_path, compose_text, env, scenario):
    host = tmp_path / "host"
    host.mkdir()
    (host / "docker-compose.yml").write_text(compose_text)
    (host / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "scenario.json").write_text(json.dumps(scenario))
    return host, bin_dir


def _run(tmp_path, host, bin_dir, *args):
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "STUB_SCENARIO": str(tmp_path / "scenario.json")}
    for k in ("ELEFANT_VERIFY_TOKEN", "ELEFANT_TAG", *REQUIRED_ENV):
        env.pop(k, None)
    p = subprocess.run(["bash", str(SCRIPT), "--dir", str(host), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    assert "unexpected call" not in p.stderr, p.stderr
    return p


def _fails(out):
    return [l.strip() for l in out.splitlines() if l.strip().startswith("FAIL")]


def test_preflight_catches_all_three_problems_of_the_incident(tmp_path):
    src, tag = _release_repo(tmp_path)
    # Pre-incident prod: nginx still published on :80, no volume-perms step.
    old = COMPOSE.read_text().replace('"${PORT:-8321}:8080"', '"${PORT:-8321}:80"')
    host, bin_dir = _host(tmp_path, old, {**REQUIRED_ENV, "ELEFANT_TAG": tag}, {
        "config": _config(nginx_port=80, with_volume_perms=False),
        "images": IMAGES,
        # The containers being replaced ran as root.
        "running": {"nginx": "", "editor": ""},
        "volumes": {
            "elefant_docs": "0:0\n40\n/v\n/v/default\n/v/default/index.md\n",
            "elefant_editor_data": "0:0\n3\n/v\n/v/crypto.key\n/v/config.db\n",
        },
    })

    p = _run(tmp_path, host, bin_dir, "--source", str(src))
    fails = _fails(p.stdout)
    assert p.returncode == 1, p.stdout

    # 1. the nginx port mismatch — both as compose drift and against the image
    assert any("compose-sync" in f and "docker-compose.yml differs" in f for f in fails), p.stdout
    assert '-      - "${PORT:-8321}:8080"' in p.stdout and '+      - "${PORT:-8321}:80"' in p.stdout
    assert any("ports" in f and "nginx" in f and "8080/tcp" in f for f in fails), p.stdout
    # 2. root-owned volumes, with the exact chown to fix them
    assert any("elefant_docs" in f for f in fails), p.stdout
    assert (f"docker run --rm --network none --user 0:0 --entrypoint chown "
            f"-v elefant_editor_data:/v {IMG}/editor:main-abc1234 -R 10001:10001 /v") in p.stdout
    # 3. the crypto.key the editor could not read is named
    assert "elefant_editor_data:/crypto.key" in p.stdout
    # ...and the user change that caused 2 and 3
    assert any("user" in f and "editor" in f and "10001:10001" in f for f in fails), p.stdout
    # third-party volumes are not the script's business
    assert "postgres_data" not in p.stdout


def test_preflight_passes_the_same_host_once_it_is_fixed(tmp_path):
    src, tag = _release_repo(tmp_path)
    host, bin_dir = _host(tmp_path, COMPOSE.read_text(), {**REQUIRED_ENV, "ELEFANT_TAG": tag}, {
        "config": _config(nginx_port=8080, with_volume_perms=True),
        "images": IMAGES,
        "running": {"nginx": "101:101", "editor": "10001:10001"},
        "volumes": {"elefant_docs": "10001:10001\n0\n", "elefant_editor_data": "10001:10001\n0\n"},
    })
    p = _run(tmp_path, host, bin_dir, "--source", str(src))
    assert p.returncode == 0, p.stdout
    assert _fails(p.stdout) == []
    assert "docker-compose.yml matches release" in p.stdout


def test_root_owned_volumes_only_warn_when_volume_perms_will_chown_them(tmp_path):
    src, tag = _release_repo(tmp_path)
    host, bin_dir = _host(tmp_path, COMPOSE.read_text(), {**REQUIRED_ENV, "ELEFANT_TAG": tag}, {
        "config": _config(nginx_port=8080, with_volume_perms=True),
        "images": IMAGES,
        "running": {"nginx": "101:101", "editor": "10001:10001"},
        "volumes": {"elefant_docs": "0:0\n1\n/v\n", "elefant_editor_data": "10001:10001\n0\n"},
    })
    p = _run(tmp_path, host, bin_dir, "--source", str(src))
    assert p.returncode == 0, p.stdout
    warn = [l for l in p.stdout.splitlines() if l.strip().startswith("WARN")]
    assert any("elefant_docs" in w and "volume-perms" in w for w in warn), p.stdout


def test_env_check_names_missing_keys_and_never_prints_values(tmp_path):
    src, tag = _release_repo(tmp_path)
    env = {**REQUIRED_ENV, "ELEFANT_TAG": tag, "POSTGRES_PASSWORD": "",
           "ELEFANT_SESSION_SECRET": "hunter2-do-not-print"}
    del env["ELEFANT_S3_SECRET_KEY"]
    host, bin_dir = _host(tmp_path, COMPOSE.read_text(), env, {
        "config": _config(nginx_port=8080, with_volume_perms=True), "images": IMAGES,
        "running": {}, "volumes": {},
    })
    p = _run(tmp_path, host, bin_dir, "--source", str(src))
    assert p.returncode == 1
    assert "POSTGRES_PASSWORD" in p.stdout and "ELEFANT_S3_SECRET_KEY" in p.stdout
    assert "hunter2" not in p.stdout + p.stderr


def test_an_unpinned_release_fails(tmp_path):
    host, bin_dir = _host(tmp_path, COMPOSE.read_text(), {**REQUIRED_ENV, "ELEFANT_TAG": "latest"}, {
        "config": _config(nginx_port=8080, with_volume_perms=True), "images": IMAGES,
        "running": {}, "volumes": {},
    })
    p = _run(tmp_path, host, bin_dir, "--no-pull")
    assert p.returncode == 1
    assert any("release" in f and "no pinned release" in f for f in _fails(p.stdout)), p.stdout


def test_verify_fails_on_unhealthy_services_and_permission_errors(tmp_path):
    host, bin_dir = _host(tmp_path, COMPOSE.read_text(), {**REQUIRED_ENV, "PORT": "1"}, {
        "config": {}, "images": {}, "running": {}, "volumes": {},
        "ps": [{"Service": "nginx", "State": "running", "Health": "unhealthy"},
               {"Service": "editor", "State": "running", "Health": "healthy"},
               {"Service": "volume-perms", "State": "exited", "ExitCode": 0}],
        "logs": "editor-1  | PermissionError: [Errno 13] Permission denied: '/data/crypto.key'\n"
                "editor-1  | Failed to initialize encryption\n",
    })
    p = _run(tmp_path, host, bin_dir, "--verify", "--host", "127.0.0.1", "--wait", "0")
    assert p.returncode == 1
    fails = _fails(p.stdout)
    assert any("health" in f for f in fails) and "nginx: unhealthy" in p.stdout, p.stdout
    assert any("logs" in f and "2 permission/encryption" in f for f in fails), p.stdout
    # No token: the token checks are skipped, and said to be.
    assert "SKIP  auth-check" in p.stdout and "SKIP  mcp-search" in p.stdout


# ── The healthchecks the incident showed were missing ────────────────────────

def _services():
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_nginx_healthcheck_goes_through_the_proxy_to_the_editor():
    test = _services()["nginx"]["healthcheck"]["test"]
    assert test[:2] == ["CMD", "wget"]
    # /login is proxied to the editor, so a misrouted nginx fails this; its
    # own listen port is 8080, and 127.0.0.1 avoids the IPv6 localhost stall.
    assert test[-1] == "http://127.0.0.1:8080/login"


@pytest.mark.parametrize("service,port", [("mcp", 8003), ("memory-api", 8002)])
def test_python_services_probe_health_with_urllib_on_ipv4(service, port):
    test = _services()[service]["healthcheck"]["test"]
    assert test[:3] == ["CMD", "python", "-c"]
    assert f"'http://127.0.0.1:{port}/health'" in test[3]
    assert "localhost" not in test[3]
