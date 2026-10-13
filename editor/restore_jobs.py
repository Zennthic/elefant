"""Org restore as a supervised job: preview, progress, pause, cancel.

A full org restore re-embeds every memory and rewrites every wiki file, which
on a real org is minutes of work. Before this module it ran as a bare thread
behind an in-process dict, and that shape cost the operator four things at
once: no preview of what a bundle would do before it did it, no sign of life
while it ran, no way to stop a restore that was clearly wrong, and — because
the only handle on the job was the id handed to the tab that started it — a
browser refresh lost the job while the work carried on regardless.

Two halves live here:

  * `RestoreControl` is the worker's side. Phases carry weights, because the
    five stages of a restore differ by an order of magnitude in cost and a
    percentage that ignored that would be a lie; every item boundary is a
    checkpoint where a pause can block and a cancel can raise.
  * `RestoreJobRegistry` is the operator's side. Live state stays in memory —
    a restore touches thousands of items and a DB write per item is not free
    — and is mirrored to `org_restore_jobs` often enough that a browser
    reattaching later reads something true.

Cancellation is honest rather than transactional. The editor-DB half of a
restore commits as a single transaction, so a cancel before that point leaves
nothing behind; a cancel after it leaves an org partly restored. The job
records the phases it completed so the operator is told which of those two
they actually got, instead of a bare "cancelled" that implies the first.
"""
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("editor.restore")


class RestoreCancelled(Exception):
    """Raised inside the worker at the first checkpoint after a cancel."""


# The weights are the reason a single percentage means anything here. They
# only have to be right in proportion: re-embedding memories dominates a real
# restore, unpacking the bundle barely registers, and wiki writes sit between.
PHASES = (
    ("preparing", "Decrypting and unpacking the bundle",       5),
    ("database",  "Restoring users, teams and configuration", 10),
    ("memories",  "Re-embedding memories",                    60),
    ("boards",    "Restoring boards and the hot memory tier",  5),
    ("wiki",      "Restoring wiki files",                     20),
)
PHASE_WEIGHT = {name: weight for name, _label, weight in PHASES}
PHASE_LABEL  = {name: label for name, label, _weight in PHASES}
PHASE_ORDER  = [name for name, _label, _weight in PHASES]


# ── What a restore may put back, and how ───────────────────────────────────
# A backup is one bundle, but the things inside it fail independently: a wiki
# tree gets mangled without the board being wrong, a board gets churned
# without the users changing. Restoring all of it to fix one of them is a
# bigger blast radius than the problem, so the areas are chosen separately.
AREAS = ("identity", "wiki", "boards", "memories")

# A job that has reached one of these is over, and the row is the only record
# of HOW it ended — finished_at, and the error text that says whether the org
# was left partially restored. Nothing may move a job back out of one.
TERMINAL_STATUSES = frozenset({"cancelled", "done", "error"})

AREA_LABEL = {
    "identity": "Users, teams, tokens and org settings",
    "wiki":     "Wiki pages, history and recycle bin",
    "boards":   "Board, cards and the hot memory tier",
    "memories": "Durable memories",
}

# Which areas offer a choice of mode, and what the absence of a choice means.
#
#   merge   — the backup is written over the top; anything not in the backup
#             stays. Recovers what was lost without discarding what came after.
#   replace — the area BECOMES the backup; anything not in the backup is
#             deleted. The point-in-time restore, and the destructive one.
#
# `identity` is merge-only on purpose: replacing it would delete every account
# created since the backup, up to and including the operator running the
# restore. `boards` is replace-only because that is what memory-api's import
# does and a correct merge there is unbuilt — see its org_import docstring.
AREA_MODES = {
    "identity": ("merge",),
    "wiki":     ("merge", "replace"),
    "boards":   ("replace",),
    "memories": ("merge", "replace"),
}

# Phases only exist for the areas that were chosen, so the percentage is a
# percentage of the work actually asked for.
AREA_PHASES = {
    "identity": ("database",),
    "wiki":     ("database", "wiki"),
    "boards":   ("boards",),
    "memories": ("memories",),
}

# Deferring the embedding is the whole reason to defer it: writing a memory
# without a vector is a fraction of the cost of embedding one, so the phase
# stops dominating and the bar stops lying about where the time goes.
DEFERRED_MEMORY_WEIGHT = 8


class RestorePlan:
    """Which areas of a bundle to restore, how, and whether to embed now."""

    def __init__(self, areas=None, modes=None, embed_memories: bool = True):
        self.areas = tuple(a for a in AREAS if a in set(areas or AREAS))
        self.modes = {a: (modes or {}).get(a, AREA_MODES[a][0]) for a in AREAS}
        self.embed_memories = bool(embed_memories)

    @classmethod
    def parse(cls, raw: Optional[dict]) -> "RestorePlan":
        """Build a plan from request JSON, refusing anything it cannot honour.

        A mode this area does not support is refused rather than quietly
        downgraded: an operator who asked for a point-in-time wiki restore and
        silently got a merge would go away believing the tree was clean.
        """
        raw = raw or {}
        areas = raw.get("areas")
        if areas is None:
            areas = list(AREAS)
        if not isinstance(areas, (list, tuple)) or not areas:
            raise ValueError("Select at least one area to restore.")
        unknown = [a for a in areas if a not in AREAS]
        if unknown:
            raise ValueError(f"Unknown restore area(s): {', '.join(map(str, unknown))}. "
                             f"Valid areas: {', '.join(AREAS)}.")
        modes = dict(raw.get("modes") or {})
        for area, mode in modes.items():
            if area not in AREAS:
                raise ValueError(f"Unknown restore area: {area}")
            if mode not in AREA_MODES[area]:
                allowed = " or ".join(f"'{m}'" for m in AREA_MODES[area])
                raise ValueError(
                    f"'{area}' does not support mode '{mode}' — it is {allowed} only.")
        return cls(areas=areas, modes=modes,
                   embed_memories=raw.get("embed_memories", True))

    def includes(self, area: str) -> bool:
        return area in self.areas

    def mode(self, area: str) -> str:
        return self.modes.get(area, AREA_MODES[area][0])

    def replaces(self, area: str) -> bool:
        return self.includes(area) and self.mode(area) == "replace"

    @property
    def is_full_restore(self) -> bool:
        return set(self.areas) == set(AREAS)

    @property
    def defers_embedding(self) -> bool:
        return self.includes("memories") and not self.embed_memories

    def phases(self) -> tuple:
        """The phases this plan will actually run, in order."""
        wanted = {"preparing"}
        for area in self.areas:
            wanted.update(AREA_PHASES[area])
        return tuple(p for p in PHASE_ORDER if p in wanted)

    def weights(self) -> dict:
        """Phase weights normalised over the phases this plan runs, so the bar
        reaches 100% on a two-area restore as surely as on a full one."""
        active = self.phases()
        raw = {}
        for phase in active:
            weight = PHASE_WEIGHT[phase]
            if phase == "memories" and not self.embed_memories:
                weight = DEFERRED_MEMORY_WEIGHT
            raw[phase] = weight
        total = sum(raw.values()) or 1
        return {phase: (weight * 100.0) / total for phase, weight in raw.items()}

    def effects(self) -> list:
        """What this plan will do, in the operator's terms, before it does it."""
        out = [AREA_EFFECT[f"{area}:{self.mode(area)}"] for area in self.areas]
        skipped = [AREA_LABEL[a] for a in AREAS if a not in self.areas]
        if skipped:
            out.append("Left completely untouched: " + "; ".join(skipped) + ".")
        if self.includes("memories"):
            out.append(EMBED_EFFECT["now" if self.embed_memories else "later"])
        return out

    def to_dict(self) -> dict:
        return {"areas": list(self.areas),
                "modes": {a: self.mode(a) for a in self.areas},
                "embed_memories": self.embed_memories}


# One wording for each area-and-mode, served to the UI so the review screen
# and the job record cannot describe the same restore differently.
AREA_EFFECT = {
    "identity:merge":
        "Users, teams, groups, API tokens, SSO providers and org settings are "
        "overwritten from the backup. Accounts that exist here but not in the "
        "backup are left alone — this area is a merge, never a wipe, so a "
        "restore cannot delete the account running it.",
    "wiki:merge":
        "Every wiki page in the backup overwrites the page at the same path. "
        "Pages created since the backup are left alone. Page history and the "
        "recycle bin are replaced with the backup's either way.",
    "wiki:replace":
        "POINT-IN-TIME: the wiki becomes exactly what the backup holds. Pages "
        "created or renamed since the backup are DELETED, not merged. This is "
        "the one that fixes a mangled tree — and the one that loses this "
        "week's pages if the backup is older than you think.",
    "boards:replace":
        "POINT-IN-TIME: the board becomes exactly what the backup holds. "
        "Cards, columns, comments and hot-tier captures created since the "
        "backup are DELETED. The board has no merge mode.",
    "memories:merge":
        "Memories from the backup are written back under their original ids. "
        "Memories created since the backup are left alone.",
    "memories:replace":
        "POINT-IN-TIME: every durable memory for this org is DELETED first, "
        "then the backup's are written back. Anything remembered since the "
        "backup is gone.",
}

EMBED_EFFECT = {
    "now":
        "Memories are re-embedded as they are restored. This is the slow part "
        "— on the bundled CPU model, most of the restore's wall-clock time.",
    "later":
        "Memories are restored WITHOUT embeddings, which is far faster. They "
        "are readable immediately but will NOT appear in semantic search "
        "until you run the embedding pass — which you can start from here, or "
        "leave until after pointing the deployment at a faster embedding "
        "backend in Settings.",
}


class RestoreControl:
    """The worker's handle on one restore: report progress, obey the operator.

    Deliberately free of any I/O. It is created with no callback by default,
    which makes a control that is never paused and never cancelled — so the
    restore code has exactly one path whether or not anybody is watching.
    """

    def __init__(self, on_update: Optional[Callable[[dict], None]] = None,
                 emit_interval: float = 1.0, plan: Optional["RestorePlan"] = None):
        self._lock         = threading.RLock()
        self._resume       = threading.Event()
        self._resume.set()
        self._cancelled    = False
        self._on_update    = on_update
        self._emit_every   = emit_interval
        self._last_emit    = 0.0
        self._started      = time.monotonic()
        self._paused_at    = None
        self._paused_total = 0.0
        self._committed    = False
        # A partial restore runs fewer phases, so the weights are the plan's
        # — normalised over what it will actually do. Without a plan this is
        # the full-restore table, which is what a bare control means.
        self.plan          = plan
        self._weights      = plan.weights() if plan is not None else dict(PHASE_WEIGHT)
        self.phase: Optional[str] = None
        self.detail        = ""
        self.processed     = 0
        self.total         = 0
        self.phases_done: list = []

    # ── worker side ────────────────────────────────────────────────────────

    def begin(self, phase: str, total: int = 0, detail: str = "") -> None:
        """Enter a phase. Closes out the previous one so its weight counts.

        Re-entering the phase already running only revises its total — a
        caller that can't count its work until it has opened the archive
        should not be forced to guess, and must not be charged the phase's
        weight twice for asking again.
        """
        with self._lock:
            if phase == self.phase:
                self.total = max(0, int(total or 0))
                if detail:
                    self.detail = detail
            else:
                if self.phase and self.phase not in self.phases_done:
                    self.phases_done.append(self.phase)
                self.phase     = phase
                self.detail    = detail or PHASE_LABEL.get(phase, "")
                self.processed = 0
                self.total     = max(0, int(total or 0))
        self.checkpoint()
        self._emit(force=True)

    def advance(self, n: int = 1, detail: str = "") -> None:
        """One item done. Also the checkpoint a pause or cancel acts on, which
        is why it is called per item and not per batch."""
        self.checkpoint()
        with self._lock:
            self.processed += n
            if detail:
                self.detail = detail
        self._emit()

    def mark_committed(self) -> None:
        """The point of no return: the restore has committed its database
        transaction, so a cancel from here on leaves a partly-restored org
        rather than an untouched one. Called by the restore itself instead of
        being inferred from the phase, because the boundary is a commit and
        inferring it from phase order would quietly rot the first time the
        phases are reordered."""
        self._committed = True
        self._emit(force=True)

    def finish(self) -> None:
        """Close out the last phase so a completed job reads as 100%."""
        with self._lock:
            if self.phase and self.phase not in self.phases_done:
                self.phases_done.append(self.phase)
            self.phase  = None
            self.detail = ""
        self._emit(force=True)

    def checkpoint(self) -> None:
        """Block while paused; raise if cancelled. Safe to call anywhere."""
        if self._cancelled:
            raise RestoreCancelled("Restore cancelled by the operator")
        while not self._resume.is_set():
            if self._cancelled:
                raise RestoreCancelled("Restore cancelled by the operator")
            # Short wait rather than a blocking one so a cancel arriving
            # during a pause is acted on promptly instead of at resume.
            self._resume.wait(0.25)
        if self._cancelled:
            raise RestoreCancelled("Restore cancelled by the operator")

    # ── operator side ──────────────────────────────────────────────────────

    def pause(self) -> None:
        with self._lock:
            if self._paused_at is None:
                self._paused_at = time.monotonic()
        self._resume.clear()
        self._emit(force=True)

    def resume(self) -> None:
        with self._lock:
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None
        self._resume.set()
        self._emit(force=True)

    def cancel(self) -> None:
        self._cancelled = True
        self._resume.set()          # wake a paused worker so it can raise
        self._emit(force=True)

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def touched_org(self) -> bool:
        """True once a cancel would leave a partial restore rather than none."""
        return self._committed

    # ── reporting ──────────────────────────────────────────────────────────

    @property
    def percent(self) -> float:
        with self._lock:
            done = sum(self._weights.get(p, 0) for p in self.phases_done)
            cur  = self._weights.get(self.phase, 0)
            frac = min(1.0, self.processed / self.total) if self.total else 0.0
        return round(min(100.0, done + cur * frac), 1)

    @property
    def working_seconds(self) -> float:
        """Wall-clock time minus time spent paused. An operator who parks a
        restore until the evening should come back to the estimate they left,
        not one inflated by the hours it spent deliberately doing nothing."""
        now = time.monotonic()
        with self._lock:
            paused = self._paused_total
            if self._paused_at is not None:
                paused += now - self._paused_at
        return max(0.0, now - self._started - paused)

    @property
    def eta_seconds(self) -> Optional[int]:
        """Extrapolated from working time against overall percent — which is
        only defensible because the phase weights are cost-weighted. Withheld
        early on, when the estimate would be noise rather than information."""
        pct = self.percent
        elapsed = self.working_seconds
        if pct < 5.0 or elapsed < 5.0 or pct >= 100.0:
            return None
        return int(elapsed * (100.0 - pct) / pct)

    def snapshot(self) -> dict:
        with self._lock:
            phase, detail = self.phase, self.detail
            processed, total = self.processed, self.total
            phases_done = list(self.phases_done)
        return {
            "phase":        phase,
            "phase_label":  PHASE_LABEL.get(phase, detail),
            "phase_detail": detail,
            "processed":    processed,
            "total":        total,
            "percent":      self.percent,
            "eta_seconds":  self.eta_seconds,
            "phases_done":  phases_done,
            "paused":       self.paused,
            "cancelled":    self.cancelled,
            "touched_org":  self.touched_org,
        }

    def _emit(self, force: bool = False) -> None:
        if self._on_update is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) < self._emit_every:
            return
        self._last_emit = now
        try:
            self._on_update(self.snapshot())
        except Exception:            # a progress report must never fail a restore
            log.exception("restore progress callback failed")


class RestoreJobRegistry:
    """Durable job records plus the live controls that drive them.

    The split matters: the DB row is what a reattaching browser (or a later
    audit) reads, while the control is what the running thread updates many
    times a second. Reads overlay the live control on the stored row so the
    UI sees current state without the restore paying for a write per item.
    """

    def __init__(self, session_factory=None, flush_interval: float = 2.0):
        self._lock       = threading.RLock()
        self._controls:  dict = {}
        self._passcodes: dict = {}     # job_id -> passcode; memory only, never stored
        self._last_flush: dict = {}
        self._factory    = session_factory
        self._flush_every = flush_interval

    # ── plumbing ───────────────────────────────────────────────────────────

    def _session(self):
        if self._factory is None:
            from config_db import SessionLocal
            self._factory = SessionLocal
        return self._factory()

    def _row(self, session, job_id):
        from config_db import OrgRestoreJob
        return session.query(OrgRestoreJob).filter_by(id=job_id).first()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def create(self, job_id: str, org_id: str, *, org_slug: str = "",
               source_label: str = "", staged_path: Optional[Path] = None,
               preview: Optional[dict] = None, passcode: Optional[str] = None,
               status: str = "staged") -> dict:
        from config_db import OrgRestoreJob
        with self._session() as s:
            s.add(OrgRestoreJob(
                id=job_id, org_id=org_id, org_slug=org_slug or None,
                source_label=source_label or None,
                staged_path=str(staged_path) if staged_path else None,
                status=status,
                preview_json=json.dumps(preview) if preview else None,
            ))
            s.commit()
            row = self._row(s, job_id)
            out = row.to_dict()
        if passcode:
            with self._lock:
                self._passcodes[job_id] = passcode
        return out

    def passcode(self, job_id: str) -> Optional[str]:
        with self._lock:
            return self._passcodes.get(job_id)

    def attach(self, job_id: str, control: RestoreControl) -> None:
        with self._lock:
            self._controls[job_id] = control

    def detach(self, job_id: str) -> None:
        with self._lock:
            self._controls.pop(job_id, None)
            self._passcodes.pop(job_id, None)
            self._last_flush.pop(job_id, None)

    def control(self, job_id: str) -> Optional[RestoreControl]:
        with self._lock:
            return self._controls.get(job_id)

    def _mark_status(self, job_id: str, status: str) -> bool:
        """Move a job to a WORKING status, unless it has already finished.

        The worker thread and whoever is steering it write to the same row
        from different threads, and the worker's terminal write has to be the
        last word: it carries finished_at and the error text saying whether
        the org was left partially restored. An unguarded write from the
        steering side can land on top of it and leave a finished job claiming
        to be still going — and with the control detached by then, nothing
        would ever move it off again.

        Returns False when the job was already finished (or is gone), which is
        not a failure to cancel: the thing the caller wanted has happened.
        """
        with self._session() as s:
            row = self._row(s, job_id)
            if row is None or row.status in TERMINAL_STATUSES:
                return False
            row.status = status
            row.updated_at = datetime.utcnow()
            s.commit()
            return True

    def update(self, job_id: str, **fields) -> None:
        """Write named fields straight through to the stored row."""
        with self._session() as s:
            row = self._row(s, job_id)
            if row is None:
                return
            for k, v in fields.items():
                if k in ("result", "preview", "plan"):
                    setattr(row, k + "_json", json.dumps(v) if v is not None else None)
                elif k == "phases_done":
                    row.phases_done = json.dumps(v or [])
                elif hasattr(row, k):
                    setattr(row, k, v)
            row.updated_at = datetime.utcnow()
            s.commit()

    def progress(self, job_id: str, snap: dict, force: bool = False) -> None:
        """Mirror a control snapshot into the row, throttled — the row exists
        for whoever comes back later, not for the poll that is already being
        answered from the live control."""
        now = time.monotonic()
        with self._lock:
            last = self._last_flush.get(job_id, 0.0)
            if not force and (now - last) < self._flush_every:
                return
            self._last_flush[job_id] = now
        self.update(
            job_id,
            phase=snap.get("phase"),
            phase_detail=snap.get("phase_detail"),
            processed=snap.get("processed") or 0,
            total=snap.get("total") or 0,
            percent=snap.get("percent") or 0.0,
            eta_seconds=snap.get("eta_seconds"),
            phases_done=snap.get("phases_done") or [],
        )

    # ── reads ──────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Optional[dict]:
        with self._session() as s:
            row = self._row(s, job_id)
            if row is None:
                return None
            out = row.to_dict()
        ctl = self.control(job_id)
        if ctl is not None and out["status"] in ("running", "paused", "pausing", "cancelling"):
            snap = ctl.snapshot()
            out.update({k: snap[k] for k in
                        ("phase", "phase_detail", "processed", "total",
                         "percent", "eta_seconds", "phases_done")})
            out["phase_label"] = snap["phase_label"]
            out["touched_org"] = snap["touched_org"]
            # The control is the truth about paused/cancelling; the stored
            # status can only ever be as fresh as the last flush.
            if snap["cancelled"]:
                out["status"] = "cancelling"
            elif snap["paused"]:
                out["status"] = "paused"
            else:
                out["status"] = "running"
        return out

    def list(self, org_id: Optional[str] = None, limit: int = 20,
             active_only: bool = False) -> list:
        from config_db import OrgRestoreJob
        with self._session() as s:
            q = s.query(OrgRestoreJob)
            if org_id:
                q = q.filter_by(org_id=org_id)
            if active_only:
                q = q.filter(OrgRestoreJob.status.in_(OrgRestoreJob.ACTIVE))
            rows = q.order_by(OrgRestoreJob.created_at.desc()).limit(limit).all()
            ids = [r.id for r in rows]
        # Overlay live state so an active job in the list is not stale.
        return [self.get(i) for i in ids]

    # ── operator actions ───────────────────────────────────────────────────

    def pause(self, job_id: str) -> bool:
        ctl = self.control(job_id)
        if ctl is None:
            return False
        ctl.pause()
        self._mark_status(job_id, "paused")
        return True

    def resume(self, job_id: str) -> bool:
        ctl = self.control(job_id)
        if ctl is None:
            return False
        ctl.resume()
        self._mark_status(job_id, "running")
        return True

    def cancel(self, job_id: str) -> bool:
        """Cancel a running job, or discard one still awaiting confirmation."""
        ctl = self.control(job_id)
        if ctl is not None:
            # Stamp the intent BEFORE waking the worker. ctl.cancel() can carry
            # the worker all the way to its terminal write in the time it takes
            # to get to the next line, and this must not land on top of that.
            # The guard in _mark_status makes it safe whichever wins; going
            # first is what puts "cancelling" in front of the operator at once.
            self._mark_status(job_id, "cancelling")
            ctl.cancel()
            return True
        job = self.get(job_id)
        if job and job["status"] in ("staged", "queued"):
            self.discard_staged(job_id)
            self.update(job_id, status="cancelled",
                        finished_at=datetime.utcnow(),
                        error="Discarded before it started — nothing was changed.")
            return True
        return False

    # ── staging ────────────────────────────────────────────────────────────

    def staged_path(self, job_id: str) -> Optional[Path]:
        with self._session() as s:
            row = self._row(s, job_id)
            if row is None or not row.staged_path:
                return None
            return Path(row.staged_path)

    def discard_staged(self, job_id: str) -> None:
        """Remove the staged upload, if this job owns one. A stored backup is
        never staged, so this can only ever delete a temp copy."""
        path = self.staged_path(job_id)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Could not remove staged bundle %s: %s", path, exc)
        self.update(job_id, staged_path=None)
        self.detach(job_id)

    # ── boot ───────────────────────────────────────────────────────────────

    def reap_orphans(self) -> int:
        """A restore thread does not survive a restart, so any job the DB
        still calls active is dead. Say so plainly rather than leaving the UI
        attached to a job that will never move again."""
        from config_db import OrgRestoreJob
        reaped = 0
        with self._session() as s:
            rows = s.query(OrgRestoreJob).filter(
                OrgRestoreJob.status.in_(OrgRestoreJob.ACTIVE)).all()
            for row in rows:
                staged = row.staged_path
                if row.status in ("staged", "queued"):
                    row.status = "cancelled"
                    row.error = ("Discarded when the service restarted — "
                                 "nothing was changed.")
                else:
                    row.status = "error"
                    row.error = (
                        "Interrupted by a service restart. Phases completed "
                        "before the restart were applied; re-run the restore "
                        "to finish it."
                    )
                row.finished_at = datetime.utcnow()
                row.staged_path = None
                reaped += 1
                if staged:
                    try:
                        Path(staged).unlink(missing_ok=True)
                    except OSError:
                        pass
            if reaped:
                s.commit()
        if reaped:
            log.warning("Marked %d interrupted org restore job(s) on startup", reaped)
        return reaped


def sweep_staging(root: Path, max_age_hours: float = 24.0) -> int:
    """Delete staged bundles nobody confirmed. An operator who previews a
    restore and then closes the tab otherwise leaves a full org backup — the
    most sensitive file the platform handles — sitting on disk indefinitely."""
    if not root.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for path in root.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("Could not sweep staged bundle %s: %s", path, exc)
    return removed


# The process-wide registry the API talks to.
registry = RestoreJobRegistry()
