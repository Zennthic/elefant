"""Throttle password and TOTP guessing on /login and /mfa (SEC-08).

nginx has a `limit_req` zone for these paths too, but that only helps a
deployment that runs the bundled nginx. This one lives in the app, so it holds
behind any ingress.

THREE COUNTERS, EACH A TIME-BOXED LOCK

Every failed attempt is counted against three keys:

  ip            every failure from one client address, whatever account
  account+ip    one account guessed from one address
  account       one account guessed from anywhere (a distributed guess)

When a key reaches its limit inside the window it is locked for LOCKOUT
seconds, and an attempt that hits a locked key is refused before the password
or code is even checked, so a lock is not an oracle. Refused attempts are not
counted, so a lock cannot be kept alive by hammering it: it always ends when
its time is up.

WHY THE ACCOUNT-WIDE LIMIT IS HIGHER

An attacker who knows an admin's username can trip the account-wide lock from
their own addresses. Keeping that limit well above the account+ip one means a
single guesser is stopped by the account+ip lock long before it reaches the
account-wide one, so the real admin, from their own address, is not locked out
by it. The account-wide lock is for a guess spread across many addresses, and
even then it lasts LOCKOUT seconds and no longer.

The per-address limit assumes the editor can see real client addresses. If a
proxy in front hides them, every client shares one address and 30 failures
anywhere lock everyone out for LOCKOUT seconds. The bundled nginx resolves
X-Forwarded-For from private peers so that does not happen behind the usual
ingress; a deployment that still sees one address can raise
LOGIN_THROTTLE_IP_LIMIT.

STATE IS IN PROCESS

The editor runs as one replica with one worker (deploy/k8s/base/editor.yaml),
so a dict is the whole truth. A restart forgets every counter, which is the
safe direction for a limiter to fail in. If the editor ever scales out, each
replica will count on its own and the effective limits multiply by the replica
count; the nginx zone still bounds the total rate.
"""
from __future__ import annotations

import ipaddress
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


WINDOW = _env_int("LOGIN_THROTTLE_WINDOW", 900)        # seconds failures are counted over
LOCKOUT = _env_int("LOGIN_THROTTLE_LOCKOUT", 900)      # seconds a tripped key stays locked
LIMIT_IP = _env_int("LOGIN_THROTTLE_IP_LIMIT", 30)
LIMIT_ACCOUNT_IP = _env_int("LOGIN_THROTTLE_ACCOUNT_IP_LIMIT", 5)
LIMIT_ACCOUNT = _env_int("LOGIN_THROTTLE_ACCOUNT_LIMIT", 50)

# Bounds memory under a flood of distinct keys. Past this, expired entries are
# swept, and if that is not enough the oldest entries go.
_MAX_KEYS = 50_000


@dataclass
class _Entry:
    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class LoginThrottle:
    def __init__(
        self,
        *,
        window: int = WINDOW,
        lockout: int = LOCKOUT,
        limit_ip: int = LIMIT_IP,
        limit_account_ip: int = LIMIT_ACCOUNT_IP,
        limit_account: int = LIMIT_ACCOUNT,
        clock=time.monotonic,
    ) -> None:
        self.window = window
        self.lockout = lockout
        self.limits = {"ip": limit_ip, "acct_ip": limit_account_ip, "acct": limit_account}
        self._clock = clock
        self._entries: dict[tuple, _Entry] = {}
        self._lock = threading.Lock()

    # ── keys ────────────────────────────────────────────────────────────────
    @staticmethod
    def _keys(scope: str, account: str, ip: str) -> list[tuple[str, tuple]]:
        return [
            ("ip", ("ip", ip)),
            ("acct_ip", (scope, "acct_ip", account, ip)),
            ("acct", (scope, "acct", account)),
        ]

    # ── public API ──────────────────────────────────────────────────────────
    def retry_after(self, scope: str, account: str, ip: str) -> int:
        """Seconds until an attempt is allowed; 0 when it is allowed now."""
        now = self._clock()
        wait = 0.0
        with self._lock:
            for _kind, key in self._keys(scope, account, ip):
                e = self._entries.get(key)
                if e is not None and e.locked_until > now:
                    wait = max(wait, e.locked_until - now)
        return math.ceil(wait)

    def record_failure(self, scope: str, account: str, ip: str) -> None:
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            if len(self._entries) >= _MAX_KEYS:
                self._sweep(now)
            for kind, key in self._keys(scope, account, ip):
                e = self._entries.setdefault(key, _Entry())
                e.failures = [t for t in e.failures if t > cutoff]
                e.failures.append(now)
                if len(e.failures) >= self.limits[kind]:
                    e.locked_until = now + self.lockout
                    e.failures = []

    def record_success(self, scope: str, account: str, ip: str) -> None:
        """Forget this account's failures from this address.

        Only the account+ip key is cleared. Clearing the account-wide or
        per-address counts on a success would let a guesser who also holds one
        working login reset their own budget with it.
        """
        with self._lock:
            self._entries.pop((scope, "acct_ip", account, ip), None)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    # ── housekeeping ────────────────────────────────────────────────────────
    def _sweep(self, now: float) -> None:
        cutoff = now - self.window
        dead = [k for k, e in self._entries.items()
                if e.locked_until <= now and not any(t > cutoff for t in e.failures)]
        for k in dead:
            del self._entries[k]
        overflow = len(self._entries) - _MAX_KEYS + 1
        if overflow > 0:
            for k in list(self._entries)[:overflow]:
                del self._entries[k]


class RateLimiter:
    """At most `limit` accepted calls per key inside a sliding `window`.

    Unlike LoginThrottle this counts every call, not failures: it is for an
    endpoint anyone may call but nobody needs to call often, such as OAuth
    Dynamic Client Registration. Same in-process caveat as above.
    """

    def __init__(self, *, limit: int, window: int, clock=time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> int:
        """Count a call. Returns 0 when it is allowed, else seconds to wait.

        A refused call is not counted, so the wait always runs out.
        """
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            if len(self._hits) >= _MAX_KEYS:
                for k in [k for k, ts in self._hits.items() if not ts or ts[-1] <= cutoff]:
                    del self._hits[k]
                overflow = len(self._hits) - _MAX_KEYS + 1
                for k in list(self._hits)[:max(0, overflow)]:
                    del self._hits[k]
            hits = [t for t in self._hits.get(key, ()) if t > cutoff]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return max(1, math.ceil(hits[0] + self.window - now))
            hits.append(now)
            self._hits[key] = hits
            return 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

# ── client address ──────────────────────────────────────────────────────────

# The same ranges nginx.conf trusts with set_real_ip_from. Listed rather than
# `is_private`, which also covers documentation and other reserved ranges.
_PROXY_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7", "::1/128",
)]


def _is_proxy(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _PROXY_NETS)


def client_ip(peer: Optional[str], headers: Iterable[tuple[str, str]] | dict) -> str:
    """The address to count a failure against.

    Behind the bundled nginx the peer is nginx itself, on a private address,
    and nginx passes the client's address in X-Real-IP. That header is only
    believed when the peer is in a private or loopback range: a client reaching the
    editor directly over a public address cannot choose its own key.
    """
    peer = peer or ""
    get = headers.get if hasattr(headers, "get") else dict(headers).get
    if _is_proxy(peer):
        real = (get("x-real-ip") or "").strip()
        if real:
            return real
    return peer or "unknown"


throttle = LoginThrottle()

# POST /oauth/register is open by design (RFC 7591), so it is bounded per
# client address instead (SEC-10). A connector registers once per connection;
# 20 an hour from one address is far above any real client.
oauth_register_limiter = RateLimiter(
    limit=_env_int("OAUTH_REGISTER_LIMIT", 20),
    window=_env_int("OAUTH_REGISTER_WINDOW", 3600),
)
