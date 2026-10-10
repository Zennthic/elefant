#!/usr/bin/env bash
#
# Elefant promotion preflight and post-deploy verify (card #300).
#
# Run it ON THE TARGET HOST, from (or pointed at with --dir) the directory that
# holds the stack's docker-compose.yml and .env. It changes nothing that is
# running: preflight pulls the release's images and reads; verify only reads.
#
#   scripts/prod-preflight.sh [--dir ~/elefant] [--tag main-abc1234]
#       Before `up -d`. Fails loudly when:
#         - the local compose file(s) differ from the release's;
#         - .env is missing a key the release requires (names only, never
#           values);
#         - an image runs as a different user than the container it replaces;
#         - a published port or Traefik port is one the image does not listen
#           on;
#         - a non-root image mounts an app volume it does not own (prints the
#           exact chown).
#
#   scripts/prod-preflight.sh --verify [--dir ~/elefant]
#       After `up -d`. Waits for every service to be running and healthy,
#       fetches /login on the published port at the host's LAN IP, checks
#       the auth gate, asks the editor's /api/auth/check to accept a token,
#       runs one semantic search through /mcp, and greps the last two minutes
#       of editor logs for permission and encryption failures. The token
#       checks need ELEFANT_VERIFY_TOKEN in the environment: an API token with
#       board_read and wiki/memory read. Never pass it as an argument.
#
# The procedure it belongs to is in README.md, "Promoting a release".
#
# The release's compose file and .env.example are fetched for the tag's commit
# (main-<sha7> → <sha7>) from a git checkout (--source DIR) or from the GitHub
# API. The repo is private for now, so the API needs GITHUB_TOKEN set.
#
# Needs bash, python3 (3.8+) and the docker compose plugin. Exit status: 0
# clean, 1 a check failed, 2 bad usage.
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  echo "prod-preflight: python3 is required on this host" >&2
  exit 2
fi

exec python3 - "$@" <<'PY'
import argparse
import difflib
from http.client import HTTPConnection
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP_IMAGE_PREFIX = "ghcr.io/zennthic/elefant/"
REPO = os.environ.get("ELEFANT_REPO", "zennthic/elefant")
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.traefik.yml")
LOG_PROBLEMS = re.compile(r"Errno 13|Failed to initialize encryption")
ROOT_USERS = ("", "0", "0:0", "root", "root:root")


# ── Reporting ────────────────────────────────────────────────────────────────

class Report:
    def __init__(self):
        self.counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}

    def _line(self, level, check, msg, details=()):
        self.counts[level] += 1
        print(f"  {level:<4}  {check:<16} {msg}", flush=True)
        for d in details:
            print(f"        {d}", flush=True)

    def ok(self, check, msg, details=()):
        self._line("PASS", check, msg, details)

    def warn(self, check, msg, details=()):
        self._line("WARN", check, msg, details)

    def fail(self, check, msg, details=()):
        self._line("FAIL", check, msg, details)

    def skip(self, check, msg, details=()):
        self._line("SKIP", check, msg, details)


R = Report()


# ── Small helpers ────────────────────────────────────────────────────────────

def run(cmd, input=None, env=None):
    return subprocess.run(cmd, input=input, capture_output=True, text=True,
                          env=env, stdin=None if input is not None else subprocess.DEVNULL)


def tail(text, n=5):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-n:]


def read_env(path):
    """KEY=value pairs, the way compose reads .env: comments and blank lines
    ignored, `export ` allowed, quotes stripped, ` #` ends an unquoted value."""
    env = {}
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            value = value.strip()
            if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
                value = value[1:-1]
            else:
                value = re.split(r"\s#", value, 1)[0].strip()
            env[key] = value
    return env


def parse_user(user):
    """(uid, gid) for a numeric `uid[:gid]`, None for root, "?" if named."""
    if user in ROOT_USERS:
        return None
    uid, _, gid = user.partition(":")
    if not uid.isdigit() or (gid and not gid.isdigit()):
        return "?"
    return int(uid), int(gid or uid)


def same_user(a, b):
    return (a in ROOT_USERS and b in ROOT_USERS) or a == b


def describe_user(u):
    return "root" if u in ROOT_USERS else u


# ── The release's files ──────────────────────────────────────────────────────

def release_ref(tag):
    """main-<sha7> is a commit on main; anything else (v1.2.3) is a git ref."""
    if tag.startswith("main-") and re.fullmatch(r"[0-9a-f]{7,40}", tag[5:]):
        return tag[5:]
    return tag


class FetchError(Exception):
    pass


def fetch_release_file(args, ref, path):
    """Text of `path` at `ref`, None if the release has no such file."""
    if args.source:
        p = run(["git", "-C", args.source, "cat-file", "-e", f"{ref}^{{commit}}"])
        if p.returncode != 0:
            raise FetchError(f"{ref} is not a commit in {args.source} (git fetch first?)")
        p = run(["git", "-C", args.source, "show", f"{ref}:{path}"])
        return p.stdout if p.returncode == 0 else None
    url = (f"https://api.github.com/repos/{REPO}/contents/{urllib.parse.quote(path)}"
           f"?ref={urllib.parse.quote(ref)}")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.raw",
        "User-Agent": "elefant-prod-preflight",
    })
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise FetchError(f"GitHub answered {e.code} for {path}@{ref}")
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"could not reach GitHub: {e}")


# ── docker compose ───────────────────────────────────────────────────────────

def compose_cmd(args, *rest):
    cmd = ["docker", "compose", "--project-directory", args.dir]
    for f in args.files:
        cmd += ["-f", f]
    return cmd + list(rest)


def compose_config(args):
    p = run(compose_cmd(args, "config", "--format", "json"))
    if p.returncode != 0:
        raise RuntimeError("\n".join(tail(p.stderr)) or "docker compose config failed")
    return json.loads(p.stdout)


def compose_ps(args):
    p = run(compose_cmd(args, "ps", "-a", "--format", "json"))
    if p.returncode != 0:
        raise RuntimeError("\n".join(tail(p.stderr)) or "docker compose ps failed")
    out = p.stdout.strip()
    if not out:
        return []
    # Older compose prints one JSON array, newer prints one object per line.
    if out.startswith("["):
        return json.loads(out)
    return [json.loads(l) for l in out.splitlines() if l.strip()]


def image_config(image):
    p = run(["docker", "image", "inspect", "--format", "{{json .Config}}", image])
    if p.returncode != 0:
        return None
    return json.loads(p.stdout) or {}


def container_user(args, service):
    p = run(compose_cmd(args, "ps", "-a", "-q", service))
    cids = p.stdout.split() if p.returncode == 0 else []
    if not cids:
        return None
    p = run(["docker", "inspect", "--format", "{{json .Config.User}}", cids[0]])
    if p.returncode != 0:
        return None
    return json.loads(p.stdout) or ""


# ── Preflight ────────────────────────────────────────────────────────────────

def check_compose_sync(args, ref):
    """Returns the release's compose text (for the .env check), or None."""
    release_text = None
    for name in COMPOSE_NAMES:
        local_path = os.path.join(args.dir, name)
        local = open(local_path).read() if os.path.exists(local_path) else None
        try:
            release = fetch_release_file(args, ref, name)
        except FetchError as e:
            R.fail("compose-sync", f"cannot fetch the release's {name}: {e}",
                   ["Set GITHUB_TOKEN (the repo is private) or pass --source <checkout>."])
            continue
        if name == "docker-compose.yml":
            release_text = release
        if local is None and release is None:
            continue
        if release is None:
            R.fail("compose-sync", f"{name} is here but not in release {ref}")
        elif local is None:
            if name == "docker-compose.yml":
                R.fail("compose-sync", f"{name} is missing from {args.dir}")
            else:
                R.ok("compose-sync", f"{name} not used on this host")
        elif local == release:
            R.ok("compose-sync", f"{name} matches release {ref}")
        else:
            diff = list(difflib.unified_diff(
                release.splitlines(), local.splitlines(),
                f"release {ref}/{name}", f"local {local_path}", lineterm="", n=2))
            shown = diff[:80] + ([f"... {len(diff) - 80} more diff lines"] if len(diff) > 80 else [])
            R.fail("compose-sync", f"{name} differs from release {ref}; copy the release's file here",
                   shown)
    return release_text


def check_env(args, ref, release_compose):
    env_path = os.path.join(args.dir, ".env")
    if not os.path.exists(env_path):
        R.fail("env", f"no .env in {args.dir}")
        return
    have = read_env(env_path)

    example = None
    if ref:
        try:
            example = fetch_release_file(args, ref, ".env.example")
        except FetchError:
            example = None
    if example is None and os.path.exists(os.path.join(args.dir, ".env.example")):
        example = open(os.path.join(args.dir, ".env.example")).read()

    required = set()
    if example is not None:
        for line in example.splitlines():
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
            if m:
                required.add(m.group(1))
    # Anything compose itself refuses to start without (${VAR:?...}).
    compose_text = release_compose
    if compose_text is None and os.path.exists(os.path.join(args.dir, "docker-compose.yml")):
        compose_text = open(os.path.join(args.dir, "docker-compose.yml")).read()
    if compose_text:
        required |= set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?", compose_text))

    if not required:
        R.warn("env", "found no .env.example or compose file to take required keys from")
        return
    # The shell environment counts too: compose interpolates from it first.
    missing = sorted(k for k in required if not (have.get(k) or os.environ.get(k)))
    if missing:
        R.fail("env", f".env is missing or has empty values for {len(missing)} required key(s)",
               missing)
    else:
        R.ok("env", f".env sets all {len(required)} required keys")


def check_images(args, config):
    services = config.get("services", {})
    volumes = config.get("volumes", {})
    perms_sources = {v.get("source") for v in services.get("volume-perms", {}).get("volumes", [])
                     if v.get("type") == "volume"}
    checked = set()

    for name in sorted(services):
        svc = services[name]
        image = svc.get("image", "")
        if not image.startswith(APP_IMAGE_PREFIX):
            continue
        cfg = image_config(image)
        if cfg is None:
            R.fail("image", f"{name}: {image} is not on this host; pull it first")
            continue

        # ── user ────────────────────────────────────────────────────────────
        new_user = svc.get("user") or cfg.get("User") or ""
        old_user = container_user(args, name)
        if old_user is None:
            R.ok("user", f"{name}: no existing container; release runs as {describe_user(new_user)}")
        elif same_user(old_user, new_user):
            R.ok("user", f"{name}: runs as {describe_user(new_user)}, unchanged")
        else:
            msg = (f"{name}: the running container is {describe_user(old_user)}, "
                   f"the release runs as {describe_user(new_user)}")
            if args.allow_user_change:
                R.warn("user", msg + " (allowed by --allow-user-change)")
            else:
                R.fail("user", msg, ["Check the volume ownership lines below, then re-run "
                                     "with --allow-user-change."])

        # ── ports ───────────────────────────────────────────────────────────
        exposed = set((cfg.get("ExposedPorts") or {}).keys())
        wanted = []
        for p in svc.get("ports", []) or []:
            wanted.append((f"publishes {p.get('published', '?')}->{p['target']}",
                           f"{p['target']}/{p.get('protocol') or 'tcp'}"))
        labels = svc.get("labels") or {}
        if isinstance(labels, list):
            labels = dict(l.split("=", 1) for l in labels if "=" in l)
        for key, value in labels.items():
            if re.fullmatch(r"traefik\.http\.services\.[^.]+\.loadbalancer\.server\.port", key):
                wanted.append((f"sends Traefik to {value}", f"{value}/tcp"))
        for what, port in wanted:
            if not exposed:
                R.warn("ports", f"{name}: {what}, but the image declares no ports to check against")
            elif port in exposed:
                R.ok("ports", f"{name}: {what}, and the image listens on {port}")
            else:
                R.fail("ports", f"{name}: {what}, but the image listens on "
                                f"{', '.join(sorted(exposed))}")

        # ── volume ownership ────────────────────────────────────────────────
        ids = parse_user(new_user)
        mounts = [v for v in svc.get("volumes", []) or [] if v.get("type") == "volume"]
        if not mounts or ids is None:
            continue
        if ids == "?":
            R.warn("volumes", f"{name}: runs as named user {new_user!r}; ownership not checked")
            continue
        uid, gid = ids
        for m in mounts:
            source = m.get("source")
            vname = (volumes.get(source) or {}).get("name") or source
            if (vname, uid, gid) in checked:
                continue
            checked.add((vname, uid, gid))
            if run(["docker", "volume", "inspect", vname]).returncode != 0:
                R.ok("volumes", f"{vname}: does not exist yet; it will be created empty")
                continue
            probe = (f"stat -c %u:%g /v; "
                     f"find /v \\( ! -user {uid} -o ! -group {gid} \\) -print | wc -l; "
                     f"find /v \\( ! -user {uid} -o ! -group {gid} \\) -print | head -n 3")
            p = run(["docker", "run", "--rm", "--network", "none", "--user", "0:0",
                     "--entrypoint", "sh", "-v", f"{vname}:/v:ro", image, "-c", probe])
            lines = p.stdout.split("\n")
            if p.returncode != 0 or len(lines) < 2 or not lines[1].strip().isdigit():
                R.fail("volumes", f"{vname}: could not read its ownership",
                       tail(p.stderr) or tail(p.stdout))
                continue
            owner, stray = lines[0].strip(), int(lines[1].strip())
            samples = [l for l in lines[2:] if l.strip()]
            if stray == 0:
                R.ok("volumes", f"{vname}: owned by {uid}:{gid}, as {name} runs")
                continue
            chown = (f"docker run --rm --network none --user 0:0 --entrypoint chown "
                     f"-v {vname}:/v {image} -R {uid}:{gid} /v")
            details = [f"volume root is {owner}; {stray} path(s) not {uid}:{gid}, e.g.:"]
            details += [f"  {s.replace('/v', vname + ':', 1)}" for s in samples]
            details += ["fix with:", f"  {chown}"]
            if source in perms_sources:
                R.warn("volumes", f"{vname}: {name} runs as {uid}:{gid} but the volume is not "
                                  f"owned by it; the release's volume-perms step will chown it "
                                  f"on `up`", details)
            else:
                R.fail("volumes", f"{vname}: {name} runs as {uid}:{gid} and cannot write here; "
                                  f"nothing in this compose file will fix it", details)


def preflight(args):
    env_path = os.path.join(args.dir, ".env")
    env = read_env(env_path) if os.path.exists(env_path) else {}
    # Compose lets the shell environment override .env, so that is what
    # `up -d` will run (CI sets ELEFANT_TAG this way).
    env_tag = os.environ.get("ELEFANT_TAG") or env.get("ELEFANT_TAG", "")
    tag = args.tag or env_tag
    print(f"Elefant preflight in {args.dir}, release {tag or '(none)'}")

    ref = None
    if not tag or tag == "latest":
        R.fail("release", "no pinned release: set ELEFANT_TAG=main-<sha7> (or vX.Y.Z) in .env",
               ["`latest` is whatever main was when the image was last pulled; a promotion "
                "has to name the build it promotes."])
    else:
        if args.tag and env_tag != args.tag:
            R.fail("release", f"--tag {args.tag}, but ELEFANT_TAG is {env_tag or 'unset'}; "
                              f"`up -d` would run that one")
        else:
            R.ok("release", f"ELEFANT_TAG pins {tag}")
        ref = release_ref(tag)

    release_compose = check_compose_sync(args, ref) if ref else None
    check_env(args, ref, release_compose)

    if args.no_pull:
        R.skip("pull", "--no-pull: checking whatever images are already on this host")
    else:
        p = run(compose_cmd(args, "pull", "--quiet"))
        if p.returncode == 0:
            R.ok("pull", "pulled the release's images (running containers untouched)")
        else:
            R.fail("pull", "docker compose pull failed", tail(p.stderr))

    try:
        config = compose_config(args)
    except RuntimeError as e:
        R.fail("config", "docker compose config failed", str(e).splitlines())
        return
    check_images(args, config)


# ── Verify ───────────────────────────────────────────────────────────────────

def lan_ip():
    p = run(["hostname", "-I"])
    ips = p.stdout.split() if p.returncode == 0 else []
    return next((ip for ip in ips if ":" not in ip), "127.0.0.1")


def http(method, url, headers=None, body=None, timeout=30):
    """One request, no redirects followed and no proxy: this is about the
    host's own published port."""
    u = urllib.parse.urlsplit(url)
    conn = HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()



def check_health(args):
    deadline = time.time() + args.wait
    while True:
        try:
            rows = compose_ps(args)
        except RuntimeError as e:
            R.fail("health", "docker compose ps failed", str(e).splitlines())
            return
        bad, waiting = [], []
        for r in rows:
            svc, state, health = r.get("Service"), r.get("State"), r.get("Health") or ""
            if state == "exited" and r.get("ExitCode") == 0 and svc == "volume-perms":
                continue
            if state != "running":
                bad.append(f"{svc}: {state}")
            elif health == "starting":
                waiting.append(svc)
            elif health not in ("", "healthy"):
                bad.append(f"{svc}: {health}")
        if not rows:
            R.fail("health", "no containers in this project")
            return
        if not waiting and not bad:
            R.ok("health", f"{len(rows)} containers running, none unhealthy")
            return
        if time.time() >= deadline:
            R.fail("health", f"not healthy after {args.wait}s",
                   bad + [f"{s}: still starting" for s in waiting])
            return
        time.sleep(5)


MCP_ACCEPT = "application/json, text/event-stream"


def mcp_messages(headers, body):
    text = body.decode(errors="replace")
    if headers.get("content-type", "").startswith("text/event-stream"):
        out = []
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    out.append(json.loads(line[5:].strip()))
                except ValueError:
                    pass
        return out
    try:
        data = json.loads(text) if text.strip() else []
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def check_mcp_search(base, token, query):
    url = f"{base}/mcp"
    hdrs = {"Authorization": f"Bearer {token}", "Accept": MCP_ACCEPT,
            "Content-Type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "elefant-prod-preflight", "version": "1"}}}
    status, h, body = http("POST", url, hdrs, json.dumps(init))
    if status != 200:
        R.fail("mcp-search", f"initialize through /mcp answered {status}")
        return
    session = h.get("mcp-session-id")
    msgs = mcp_messages(h, body)
    version = next((m["result"].get("protocolVersion") for m in msgs if "result" in m), None)
    if session:
        hdrs["Mcp-Session-Id"] = session
    if version:
        hdrs["MCP-Protocol-Version"] = version
    try:
        http("POST", url, hdrs, json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": query, "limit": 3}}}
        status, h, body = http("POST", url, hdrs, json.dumps(call), timeout=60)
        msgs = [m for m in mcp_messages(h, body) if m.get("id") == 2]
        if status != 200 or not msgs:
            R.fail("mcp-search", f"tools/call search answered {status} with no result")
            return
        m = msgs[0]
        if "error" in m or m.get("result", {}).get("isError"):
            err = m.get("error", {}).get("message") or "the tool reported an error"
            R.fail("mcp-search", f"search failed: {err[:200]}")
            return
        text = "".join(c.get("text", "") for c in m["result"].get("content", []))
        R.ok("mcp-search", f"semantic search through /mcp answered ({len(text)} chars)")
    finally:
        if session:
            try:
                http("DELETE", url, hdrs)
            except OSError:
                pass


def check_auth_check(args, token):
    # Through the editor itself, because nginx serves /api/auth/check only to
    # its own subrequests. A bearer is accepted there for the board stream
    # path, which is what X-Original-URI says. The token travels on stdin, so
    # it never appears in a process list.
    script = (
        "import sys, urllib.request, urllib.error\n"
        "req = urllib.request.Request('http://127.0.0.1:8080/api/auth/check', headers={\n"
        f"    'Authorization': 'Bearer ' + {token!r},\n"
        "    'X-Original-URI': '/api/board/stream'})\n"
        "try:\n"
        "    print(urllib.request.urlopen(req, timeout=10).status)\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.code)\n"
    )
    p = run(compose_cmd(args, "exec", "-T", "editor", "python", "-"), input=script)
    got = p.stdout.strip()
    if p.returncode == 0 and got == "200":
        R.ok("auth-check", "editor /api/auth/check accepts the verify token (200)")
    else:
        R.fail("auth-check", f"editor /api/auth/check answered {got or 'nothing'}",
               tail(p.stderr) + ["The token needs board_read."])


def check_logs(args):
    p = run(compose_cmd(args, "logs", "--no-color", "--since", args.since, "editor"))
    if p.returncode != 0:
        R.fail("logs", "could not read editor logs", tail(p.stderr))
        return
    hits = [l for l in (p.stdout + p.stderr).splitlines() if LOG_PROBLEMS.search(l)]
    if hits:
        R.fail("logs", f"editor logged {len(hits)} permission/encryption failure(s) in the last {args.since}",
               [h[:240] for h in hits[:5]])
    else:
        R.ok("logs", f"no Errno 13 or encryption failures in the last {args.since} of editor logs")


def verify(args):
    env_path = os.path.join(args.dir, ".env")
    env = read_env(env_path) if os.path.exists(env_path) else {}
    host = args.host or lan_ip()
    port = args.port or env.get("PORT") or "8321"
    base = f"http://{host}:{port}"
    token = os.environ.get("ELEFANT_VERIFY_TOKEN", "")
    tag = os.environ.get("ELEFANT_TAG") or env.get("ELEFANT_TAG") or "(none)"
    print(f"Elefant verify in {args.dir}, front door {base}, release {tag}")

    check_health(args)

    try:
        status, _, _ = http("GET", f"{base}/login")
        if status == 302:  # first run: /login sends you to /setup
            status, _, _ = http("GET", f"{base}/setup")
        if status == 200:
            R.ok("front-door", f"{base}/login answered 200 through nginx")
        else:
            R.fail("front-door", f"{base}/login answered {status}")
        status, h, _ = http("GET", f"{base}/editor/")
        if status in (302, 401) and (status == 401 or "/login" in h.get("location", "")):
            R.ok("auth-gate", f"/editor/ without a session answered {status} "
                              f"(nginx asked the editor's auth check)")
        else:
            R.fail("auth-gate", f"/editor/ without a session answered {status}; "
                                f"expected a redirect to /login")
    except OSError as e:
        R.fail("front-door", f"cannot reach {base}: {e}",
               ["Is the published port right? PORT in .env, and the nginx `ports:` target 8080."])

    if token:
        check_auth_check(args, token)
        try:
            check_mcp_search(base, token, args.query)
        except OSError as e:
            R.fail("mcp-search", f"cannot reach {base}/mcp: {e}")
    else:
        R.skip("auth-check", "ELEFANT_VERIFY_TOKEN is not set")
        R.skip("mcp-search", "ELEFANT_VERIFY_TOKEN is not set")

    check_logs(args)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog="prod-preflight.sh",
                                 description="Elefant promotion preflight and post-deploy verify.")
    ap.add_argument("--dir", default=os.getcwd(),
                    help="directory with docker-compose.yml and .env (default: here)")
    ap.add_argument("-f", "--file", dest="files", action="append", default=None,
                    help="compose file to use; repeatable (default: docker-compose.yml, plus "
                         "docker-compose.traefik.yml when it is there)")
    ap.add_argument("--verify", action="store_true", help="post-deploy verify instead of preflight")
    ap.add_argument("--tag", help="release tag being promoted (default: ELEFANT_TAG in .env)")
    ap.add_argument("--source", help="git checkout of the repo to read the release's files from")
    ap.add_argument("--no-pull", action="store_true", help="do not pull; check local images")
    ap.add_argument("--allow-user-change", action="store_true",
                    help="report an image user change as a warning, not a failure")
    ap.add_argument("--host", help="host address for verify (default: first IPv4 of hostname -I)")
    ap.add_argument("--port", help="published nginx port for verify (default: PORT in .env or 8321)")
    ap.add_argument("--query", default="Elefant", help="semantic search query for verify")
    ap.add_argument("--since", default="2m", help="how far back verify reads editor logs")
    ap.add_argument("--wait", type=int, default=180,
                    help="seconds verify waits for services to become healthy")
    ap.add_argument("--allow-skips", action="store_true",
                    help="exit 0 even if checks were skipped (e.g. no verify token)")
    args = ap.parse_args()

    args.dir = os.path.abspath(os.path.expanduser(args.dir))
    if not os.path.isdir(args.dir):
        print(f"prod-preflight: {args.dir} is not a directory", file=sys.stderr)
        return 2
    if args.files is None:
        args.files = [os.path.join(args.dir, n) for n in COMPOSE_NAMES
                      if os.path.exists(os.path.join(args.dir, n))]
    else:
        args.files = [os.path.join(args.dir, f) for f in args.files]

    (verify if args.verify else preflight)(args)

    c = R.counts
    print(f"\n{'verify' if args.verify else 'preflight'}: {c['PASS']} passed, {c['WARN']} warnings, "
          f"{c['FAIL']} failed, {c['SKIP']} skipped")
    if c["FAIL"]:
        print("NOT READY: fix the FAIL lines above.")
        return 1
    if c["SKIP"] and not args.allow_skips:
        print("INCOMPLETE: checks were skipped (pass --allow-skips to accept that).")
        return 1
    print("OK")
    return 0


sys.exit(main())
PY
