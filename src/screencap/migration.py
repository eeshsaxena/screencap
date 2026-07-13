"""Deferred-delete upgrade migration engine (SCR-258 U6, KTD-18).

Move an existing **plaintext** recordings library into the encrypted container
via a prompted, resumable, record-through background job that COPIES and VERIFIES
every recording before a single quiesced cutover, and DELETES the plaintext
originals ONLY in a post-cutover, ledger-tracked sweep.

The crux fix from doc review is that **deletion is deferred**: a migrated
recording never vanishes from the plaintext read-root mid-migration. The
plaintext store stays the single authoritative root for reads and new recordings
until the final cutover swap; only after the container takes the recordings
mountpoint does the sweep remove the plaintext copies.

Design (KTD-18)
---------------
Per-recording ledger discipline mirrors :class:`screencap.pipeline_state.PipelineLedger`
and :class:`screencap.backfill.ledger.BackfillLedger` (closed-set seeding,
``BEGIN IMMEDIATE`` + ``busy_timeout`` per transition, WAL, ``0o600``/``0o700``
perms + symlink guard, frozen denominator). Per-recording states:

    ``PENDING`` → ``COPIED`` → ``VERIFIED`` → (post-cutover) ``PLAINTEXT_DELETED``

Run-level state (:class:`MigrationState`) is the auto-resume axis (RUNNING /
PAUSED / CANCELLED / COMPLETED), orthogonal to a ``phase`` marker
(:class:`MigrationPhase`) that records how far the one-way cutover has advanced
so a crash at any window resumes forward, never re-deleting or re-copying.

Deferred-delete invariants
--------------------------
* Copy/verify only ever WRITE to the interim container mount; the plaintext root
  is untouched until cutover.
* Cutover is gated on ALL recordings ``VERIFIED`` **and** no active recording.
* Sidecar stores (content index + backfill ledger) are copied **only inside the
  quiesced cutover window** (under the caller's ``cutover_reservation`` and after
  ``pause_sidecar_writers`` has paused the writers) — never per round, so a row
  written during record-through before cutover is not silently dropped.
* ``PLAINTEXT_DELETED`` runs only after the swap makes the container the
  authoritative root; the sweep is idempotent + resumable.

Destination-agnostic seams
--------------------------
This module owns no daemon coupling (it never imports ``screencap.daemon``): the
daemon-specific pieces — the active-recording query, the ``acquire_migration``
cutover reservation, pausing the index/backfill writers, and the concrete
``hdiutil`` attach/detach — arrive as injected callables so the engine is fully
exercisable with the container mocked (copy real files between temp dirs, model
the "encrypted volume" with a symlink-backed fake mounter).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, Protocol, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "UnitState",
    "MigrationState",
    "MigrationPhase",
    "MigrationSummary",
    "MigrationLedger",
    "MigrationPaths",
    "Mounter",
    "ContainerMounter",
    "default_ledger_path",
    "run_migration",
    "DiskPreflightError",
    "SIDECAR_NAMES",
]

# Match the engine writer's busy_timeout (pipeline_state.py / backfill/ledger.py)
# so a concurrent commit doesn't immediately fail a ledger transition.
_BUSY_TIMEOUT_MS = 10000

# Single run-row key — there is one migration run per DB.
_RUN_ROW_KEY = 0

# The local-only sidecar DBs copied INSIDE the cutover window (KTD-18). Kept here
# so the engine and the daemon plan-builder agree on the set.
SIDECAR_NAMES: tuple[str, ...] = ("content_index.db", "backfill_state.db")

# Reason code carried on a PAUSED run when the disk preflight (or a mid-copy
# ENOSPC) leaves the plaintext intact and the job waiting for free space.
PAUSE_REASON_DISK = "insufficient_disk"


class DiskPreflightError(Exception):
    """The library-size transient-disk preflight failed (plaintext intact).

    Raised by a disk-preflight callback to signal the engine to PAUSE with the
    disk reason rather than proceed into a copy that would hit ENOSPC.
    """


class UnitState(str, Enum):
    """Per-recording migration state.

    ``PENDING`` / ``COPIED`` are non-terminal; ``VERIFIED`` gates cutover;
    ``PLAINTEXT_DELETED`` is the post-cutover terminal state. The states are
    DISTINCT — ``COPIED`` (bytes written to the container but not yet SHA-verified)
    is never conflated with ``VERIFIED``, and ``VERIFIED`` is never conflated with
    ``PLAINTEXT_DELETED`` (that is the whole deferred-delete guarantee).
    """

    PENDING = "pending"
    COPIED = "copied"
    VERIFIED = "verified"
    PLAINTEXT_DELETED = "plaintext_deleted"


class MigrationState(str, Enum):
    """Overall run state — the daemon auto-resume axis (mirrors ``RunState``).

    ``PAUSED`` (disk preflight / ENOSPC, plaintext intact) MAY auto-resume;
    ``CANCELLED`` (user cancelled / lock-paused mid-flight is a *pause*, not a
    cancel) must NOT auto-resume; ``COMPLETED`` has nothing left; ``RUNNING`` is
    the in-flight/interrupted state a resume continues from.
    """

    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class MigrationPhase(str, Enum):
    """How far the one-way cutover has advanced (crash-recovery axis).

    ``COPYING`` — plaintext root is authoritative; all writes go to the interim
    mount. ``CUTTING_OVER`` — the dangerous swap window is open (sidecars copied,
    interim detached, plaintext moved aside, container being attached at the real
    mountpoint); a resume completes it. ``CUTOVER_DONE`` — the container is the
    authoritative root; only the post-cutover sweep remains. ``DONE`` — swept.
    """

    COPYING = "copying"
    CUTTING_OVER = "cutting_over"
    CUTOVER_DONE = "cutover_done"
    DONE = "done"


@dataclass(frozen=True)
class MigrationSummary:
    """Privacy-safe run snapshot (R9): counts + phase only, NEVER a dir name.

    The EventBus is readable by any same-EUID subscriber (including the MCP
    ``/v0/events`` stream), so — exactly as with the backfill snapshot — a
    recording directory name (which encodes timing/context) must never cross this
    boundary. Only frozen-denominator counts, the run state, the cutover phase,
    and an optional non-identifying pause reason are carried.
    """

    state: MigrationState
    phase: MigrationPhase
    pending: int
    copied: int
    verified: int
    deleted: int
    total: int
    paused_reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "phase": self.phase.value,
            "pending": self.pending,
            "copied": self.copied,
            "verified": self.verified,
            "deleted": self.deleted,
            "total": self.total,
            "paused_reason": self.paused_reason,
        }


class Mounter(Protocol):
    """Attach/detach seam so the engine runs with ``hdiutil`` mocked.

    Production wires :class:`ContainerMounter` (real ``hdiutil`` via
    ``screencap.container``); tests inject a symlink-backed fake so copy/verify/
    cutover/sweep run against real files with the "encrypted volume" faked.
    """

    def attach(self, mountpoint: Path) -> None:
        ...

    def detach(self, mountpoint: Path) -> None:
        ...

    def is_attached(self, mountpoint: Path) -> bool:
        ...


@dataclass(frozen=True)
class MigrationPaths:
    """The concrete paths the swap moves between (all injected, all local)."""

    #: Current authoritative plaintext read-root (== the final container
    #: mountpoint). Recordings are read here throughout, until cutover.
    plaintext_root: Path
    #: Where the container is attached DURING migration (a run-dir-adjacent temp
    #: path, KTD-18 interim-mountpoint decision) so the plaintext store keeps the
    #: real mountpoint until the quiesced cutover swap.
    interim_mountpoint: Path
    #: Where the plaintext tree is moved at cutover, before the container takes
    #: the real mountpoint; swept post-cutover. MUST be on the same volume as
    #: ``plaintext_root`` so the move is an atomic rename.
    aside_root: Path
    #: Where the plaintext sidecar DBs (content_index.db / backfill_state.db)
    #: live pre-migration; copied into ``<mount>/.store`` inside the cutover
    #: window. Resolved explicitly (not via ``get_store_dir``) so the flag flip
    #: cannot make it drift mid-migration.
    sidecar_source_dir: Path
    #: Sidecar dir name inside the container.
    store_subdir_name: str = ".store"


# ---------------------------------------------------------------------------
# Null seams (defaults keep the engine importable + unit-testable in isolation)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _null_cm() -> Any:
    yield


def _no_active_recording() -> str | None:
    return None


def _no_disk_preflight(_needed_bytes: int) -> str | None:
    """Default preflight: always OK (returns no reason)."""
    return None


def _default_set_container_enabled(enabled: bool) -> None:
    """Flip ``container_enabled`` in config (the cutover makes the container the
    authoritative store). Deferred import; injected/overridable for tests."""
    from screencap.privacy_settings import _privacy_config_writer

    with _privacy_config_writer() as doc:
        doc["container_enabled"] = bool(enabled)


def default_ledger_path() -> Path:
    """Return the RUN-DIR migration ledger path ``migrate_state.db``.

    KTD-18 / Outstanding-Question resolution: the migration ledger lives in the
    RUN-DIR (``~/.screencap/migrate_state.db``), OUTSIDE the container — it must
    survive restarts AND the cutover swap, so it cannot live inside the container
    being built (a sibling of the backfill ledger's home). Local-only either way.
    """
    from screencap import config

    return config.get_base_dir() / "migrate_state.db"


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class MigrationLedger:
    """On-disk per-recording migration ledger (PipelineLedger discipline).

    One instance wraps one ``migrate_state.db``. Cheap to construct; every
    operation opens a short-lived ``sqlite3`` connection so separate processes can
    each hold their own ledger. Cross-process coordination is SQLite
    ``busy_timeout`` + ``BEGIN IMMEDIATE``.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else default_ledger_path()
        self._lock = threading.Lock()
        self._ensure_schema()

    # -- connection + schema (content_index.py-style perms/symlink guard) ----

    def _connect(self) -> sqlite3.Connection:
        # Shared connection-open discipline (mkdir/chmod parent, symlink guard,
        # WAL + busy_timeout pragmas, sidecar chmod) lives in the leaf
        # ``screencap.ledger_db`` so this and ``BackfillLedger`` can never drift.
        from screencap.ledger_db import open_ledger_connection

        return open_ledger_connection(
            self._db_path, label="migration-ledger", busy_timeout_ms=_BUSY_TIMEOUT_MS
        )

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS migration_unit_state ("
                    "  recording_dir_name TEXT PRIMARY KEY,"
                    "  status TEXT NOT NULL,"
                    "  updated_at REAL NOT NULL"
                    ")"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS migration_run_state ("
                    "  id INTEGER PRIMARY KEY,"
                    "  state TEXT NOT NULL,"
                    "  phase TEXT NOT NULL,"
                    "  paused_reason TEXT,"
                    "  aside_path TEXT,"
                    "  updated_at REAL NOT NULL"
                    ")"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # -- seeding (closed set, idempotent) + reset ---------------------------

    def seed(self, names: Iterable[str]) -> None:
        """Seed the closed set of recording dir names, idempotent for the SAME set.

        ``INSERT ... ON CONFLICT DO NOTHING`` so re-seeding never changes the
        denominator, never duplicates rows, and never resets an already-advanced
        unit back to ``PENDING``. Replacing the closed set is an explicit
        :meth:`reset` then :meth:`seed`, never a silent merge.
        """
        rows = list(names)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                ts = _now()
                conn.executemany(
                    "INSERT INTO migration_unit_state "
                    "(recording_dir_name, status, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(recording_dir_name) DO NOTHING",
                    [(name, UnitState.PENDING.value, ts) for name in rows],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def reset(self) -> None:
        """Clear all units + run state — a clean slate for a strictly new set."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM migration_unit_state")
                conn.execute("DELETE FROM migration_run_state")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # -- unit transitions + queries -----------------------------------------

    def mark(self, recording: str, status: UnitState) -> None:
        """Transition one unit to ``status`` (one ``BEGIN IMMEDIATE`` txn).

        A no-op if the unit was never seeded — the closed set is authoritative
        and :meth:`mark` never widens it.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE migration_unit_state SET status=?, updated_at=? "
                    "WHERE recording_dir_name=?",
                    (status.value, _now(), str(recording)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def unit_status(self, recording: str) -> "UnitState | None":
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status FROM migration_unit_state WHERE recording_dir_name=?",
                (str(recording),),
            ).fetchone()
            return UnitState(row[0]) if row is not None else None
        finally:
            conn.close()

    def names_in(self, *statuses: UnitState) -> list[str]:
        """Recording names currently in any of ``statuses`` (stable order)."""
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT recording_dir_name FROM migration_unit_state "
                f"WHERE status IN ({placeholders}) ORDER BY recording_dir_name",
                tuple(s.value for s in statuses),
            ).fetchall()
            return [str(r[0]) for r in rows]
        finally:
            conn.close()

    def progress(self) -> tuple[int, int, int, int, int]:
        """Return ``(pending, copied, verified, deleted, total)`` over the set."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM migration_unit_state GROUP BY status"
            ).fetchall()
            counts = {str(s): int(n) for s, n in rows}
            pending = counts.get(UnitState.PENDING.value, 0)
            copied = counts.get(UnitState.COPIED.value, 0)
            verified = counts.get(UnitState.VERIFIED.value, 0)
            deleted = counts.get(UnitState.PLAINTEXT_DELETED.value, 0)
            total = sum(counts.values())
            return (pending, copied, verified, deleted, total)
        finally:
            conn.close()

    def all_verified(self) -> bool:
        """True iff >=1 unit is seeded and EVERY unit is ``VERIFIED`` or beyond.

        The cutover gate: no unit may remain ``PENDING`` or ``COPIED``.
        """
        conn = self._connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM migration_unit_state"
            ).fetchone()[0]
            if not total:
                return False
            unready = conn.execute(
                "SELECT COUNT(*) FROM migration_unit_state WHERE status IN (?, ?)",
                (UnitState.PENDING.value, UnitState.COPIED.value),
            ).fetchone()[0]
            return unready == 0
        finally:
            conn.close()

    def is_swept(self) -> bool:
        """True iff every seeded unit is ``PLAINTEXT_DELETED`` (sweep complete)."""
        conn = self._connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM migration_unit_state"
            ).fetchone()[0]
            if not total:
                return False
            remaining = conn.execute(
                "SELECT COUNT(*) FROM migration_unit_state WHERE status != ?",
                (UnitState.PLAINTEXT_DELETED.value,),
            ).fetchone()[0]
            return remaining == 0
        finally:
            conn.close()

    # -- run-level state ----------------------------------------------------

    def set_run(
        self,
        *,
        state: MigrationState | None = None,
        phase: MigrationPhase | None = None,
        paused_reason: str | None = ...,  # type: ignore[assignment]
        aside_path: str | None = ...,  # type: ignore[assignment]
    ) -> None:
        """Upsert the single run row, updating only the provided fields.

        ``paused_reason`` / ``aside_path`` use a sentinel default so passing
        ``None`` explicitly CLEARS the column (distinct from "leave unchanged").
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT state, phase, paused_reason, aside_path "
                    "FROM migration_run_state WHERE id=?",
                    (_RUN_ROW_KEY,),
                ).fetchone()
                cur_state = existing[0] if existing else MigrationState.RUNNING.value
                cur_phase = existing[1] if existing else MigrationPhase.COPYING.value
                cur_reason = existing[2] if existing else None
                cur_aside = existing[3] if existing else None
                new_state = state.value if state is not None else cur_state
                new_phase = phase.value if phase is not None else cur_phase
                new_reason = cur_reason if paused_reason is ... else paused_reason
                new_aside = cur_aside if aside_path is ... else aside_path
                conn.execute(
                    "INSERT INTO migration_run_state "
                    "(id, state, phase, paused_reason, aside_path, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
                    "phase=excluded.phase, paused_reason=excluded.paused_reason, "
                    "aside_path=excluded.aside_path, updated_at=excluded.updated_at",
                    (_RUN_ROW_KEY, new_state, new_phase, new_reason, new_aside, _now()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def run_state(self) -> MigrationState | None:
        row = self._run_row()
        return MigrationState(row[0]) if row is not None else None

    def phase(self) -> MigrationPhase:
        row = self._run_row()
        return MigrationPhase(row[1]) if row is not None else MigrationPhase.COPYING

    def paused_reason(self) -> str | None:
        row = self._run_row()
        return row[2] if row is not None else None

    def aside_path(self) -> str | None:
        row = self._run_row()
        return row[3] if row is not None else None

    def _run_row(self) -> Any:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT state, phase, paused_reason, aside_path "
                "FROM migration_run_state WHERE id=?",
                (_RUN_ROW_KEY,),
            ).fetchone()
        finally:
            conn.close()

    def is_cancelled(self) -> bool:
        return self.run_state() == MigrationState.CANCELLED

    def is_done(self) -> bool:
        return (
            self.run_state() == MigrationState.COMPLETED
            and self.phase() == MigrationPhase.DONE
        )

    def summary(self) -> MigrationSummary:
        pending, copied, verified, deleted, total = self.progress()
        state = self.run_state() or MigrationState.RUNNING
        return MigrationSummary(
            state=state,
            phase=self.phase(),
            pending=pending,
            copied=copied,
            verified=verified,
            deleted=deleted,
            total=total,
            paused_reason=self.paused_reason(),
        )

    def close(self) -> None:  # API symmetry; connections are short-lived.
        pass


def should_auto_resume(ledger: MigrationLedger) -> bool:
    """True iff a daemon boot / unlock may auto-resume this migration.

    A ``RUNNING`` (interrupted mid-flight) or ``PAUSED`` (disk) run with work
    left auto-resumes; a ``CANCELLED`` run must NOT (honors the user's last
    intent — re-trigger is an explicit ``encrypt.start``); a ``COMPLETED`` run
    (fully swept) has nothing to do; a never-seeded ledger has no work.
    """
    state = ledger.run_state()
    if state is None:
        return False
    if state == MigrationState.CANCELLED:
        return False
    if ledger.is_done():
        return False
    # RUNNING or PAUSED with anything not yet swept → resume.
    return True


# ---------------------------------------------------------------------------
# The production mounter (real hdiutil via screencap.container)
# ---------------------------------------------------------------------------


class ContainerMounter:
    """Real ``hdiutil`` mounter binding a bundle+key to a mountpoint.

    Thin adapter over :mod:`screencap.container` so :func:`run_migration` stays
    daemon-free and container-mockable. ``attach`` hardens the mount (KTD-9);
    ``detach`` force-detaches (the migration owns the interim mount and every
    writer is ledger-disciplined, mirroring the lock verb's force posture).
    """

    def __init__(self, bundle_path: Path | str, key: bytes) -> None:
        self._bundle_path = str(bundle_path)
        self._key = key

    def attach(self, mountpoint: Path) -> None:
        from screencap import container

        mountpoint.mkdir(parents=True, exist_ok=True)
        info = container.attach(self._bundle_path, self._key, str(mountpoint))
        container.harden_mount(info.mountpoint or str(mountpoint))

    def detach(self, mountpoint: Path) -> None:
        from screencap import container

        container.detach(str(mountpoint), force=True)

    def is_attached(self, mountpoint: Path) -> bool:
        return os.path.ismount(str(mountpoint))


# ---------------------------------------------------------------------------
# SHA-256 verify
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _relative_file_digests(root: Path) -> dict[str, str]:
    """Map every regular file under ``root`` to its SHA-256, keyed by rel path.

    Symlinks are skipped (a recording tree holds no symlinks; a planted one is
    not content to verify). Deterministic — the map is order-independent.
    """
    digests: dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            fpath = Path(dirpath) / fname
            if fpath.is_symlink():
                continue
            rel = str(fpath.relative_to(root))
            digests[rel] = _sha256_file(fpath)
    return digests


def _verify_copy(src: Path, dst: Path) -> bool:
    """True iff every file under ``src`` exists byte-identical under ``dst``."""
    if not dst.is_dir():
        return False
    src_digests = _relative_file_digests(src)
    dst_digests = _relative_file_digests(dst)
    return src_digests == dst_digests


def _copy_recording(src: Path, dst: Path) -> None:
    """Copy the ``src`` recording tree to ``dst`` (idempotent overwrite).

    Removes a partial ``dst`` first so a resume after a crash mid-copy re-copies
    cleanly rather than merging a torn tree. Raises the underlying ``OSError`` on
    ENOSPC so the caller can PAUSE with the plaintext intact.
    """
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _list_plaintext_recordings(root: Path) -> list[str]:
    """Dir names of plaintext recordings under ``root`` (skips dot-dirs)."""
    try:
        return sorted(
            d.name
            for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except OSError:
        return []


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def run_migration(
    *,
    ledger: MigrationLedger,
    paths: MigrationPaths,
    mounter: Mounter,
    sidecar_names: Sequence[str] = SIDECAR_NAMES,
    stop_event: threading.Event | None = None,
    progress_cb: Callable[[MigrationSummary], None] | None = None,
    active_recording_name: Callable[[], str | None] = _no_active_recording,
    cutover_reservation: Callable[[], ContextManager[Any]] = _null_cm,
    pause_sidecar_writers: Callable[[], ContextManager[Any]] = _null_cm,
    disk_preflight: Callable[[int], str | None] = _no_disk_preflight,
    set_container_enabled: Callable[[bool], None] = _default_set_container_enabled,
) -> MigrationSummary:
    """Copy → verify → cutover → sweep one plaintext library into the container.

    ONE pass, idempotent + resumable. Returns a privacy-safe :class:`MigrationSummary`.
    Strictly deferred-delete: nothing under ``paths.plaintext_root`` is touched
    until the quiesced cutover, and plaintext copies are removed only by the
    post-cutover sweep.

    The pass:

    1. Resume-fast-paths from the ledger phase: ``DONE`` → return COMPLETED;
       ``CUTOVER_DONE`` → jump to the sweep; ``CUTTING_OVER`` → complete the
       interrupted swap then sweep.
    2. Seed the closed set (all plaintext recordings), disk-preflight, then copy
       + SHA-verify every recording — SKIPPING the actively-recording one (it is
       picked up a later round). ENOSPC / preflight failure → PAUSED, plaintext
       intact. ``stop_event`` set → CANCELLED at the recording boundary.
    3. Cutover is gated on ALL recordings ``VERIFIED`` **and** no active
       recording; otherwise the pass returns RUNNING (cutover waits). Under
       ``cutover_reservation`` (the brief ``acquire_migration`` window) and after
       ``pause_sidecar_writers``, the sidecars are copied into the container, the
       interim mount detached, the plaintext moved aside, the container attached
       at the real mountpoint, and the flag flipped.
    4. The post-cutover sweep deletes the aside plaintext per recording
       (``PLAINTEXT_DELETED``), then removes the aside root — resumable and
       idempotent.
    """

    def _emit() -> None:
        if progress_cb is not None:
            try:
                progress_cb(ledger.summary())
            except Exception:  # noqa: BLE001 — progress is best-effort telemetry
                logger.debug("migration progress_cb raised", exc_info=True)

    def _cancelled() -> bool:
        return stop_event is not None and stop_event.is_set()

    def _halt() -> MigrationSummary:
        # A ``stop_event`` halt at a recording boundary. Whether it is a user
        # CANCEL (terminal, never auto-resumed) or a lock PAUSE / teardown
        # (resumable) is a caller decision recorded on the ledger: the job's
        # ``cancel`` marks the run CANCELLED *before* setting the flag, so a
        # cancel is preserved here; a bare flag (lock pause) leaves the run
        # RUNNING (interrupted → auto-resumes). Plaintext is untouched either way
        # (deletion only runs post-cutover).
        if not ledger.is_cancelled():
            ledger.set_run(state=MigrationState.RUNNING)
        _emit()
        return ledger.summary()

    # (1) Resume fast-paths keyed off the durable phase.
    phase = ledger.phase()
    if ledger.is_done():
        return ledger.summary()
    # A halt requested before we even start (the job set the flag while the
    # worker was still queued) is honored up front, before any seeding/reset.
    if _cancelled():
        return _halt()
    if phase is MigrationPhase.CUTOVER_DONE:
        return _run_sweep(ledger, paths, _emit)
    if phase is MigrationPhase.CUTTING_OVER:
        _complete_interrupted_cutover(
            ledger, paths, mounter, sidecar_names, pause_sidecar_writers,
            set_container_enabled,
        )
        return _run_sweep(ledger, paths, _emit)

    # (2) COPYING phase. Seed the closed set from the live plaintext tree.
    ledger.set_run(state=MigrationState.RUNNING, phase=MigrationPhase.COPYING,
                   paused_reason=None)
    names = _list_plaintext_recordings(paths.plaintext_root)
    ledger.seed(names)
    _emit()

    # Disk preflight: transient need on the order of the not-yet-copied library.
    needed = _bytes_remaining_to_copy(ledger, paths)
    reason = disk_preflight(needed)
    if reason is not None:
        ledger.set_run(state=MigrationState.PAUSED, paused_reason=reason)
        _emit()
        return ledger.summary()

    # Ensure the interim mount is live so copies land in the container.
    if not mounter.is_attached(paths.interim_mountpoint):
        mounter.attach(paths.interim_mountpoint)

    skipped_active = False
    for name in ledger.names_in(UnitState.PENDING, UnitState.COPIED):
        if _cancelled():
            return _halt()
        if name == active_recording_name():
            # Record-through: the actively-recording dir is skipped this round
            # and migrated a later round; cutover waits for it.
            skipped_active = True
            continue
        src = paths.plaintext_root / name
        if not src.is_dir():
            # Vanished (e.g. retention-evicted mid-run): nothing to migrate.
            ledger.mark(name, UnitState.VERIFIED)
            continue
        dst = paths.interim_mountpoint / name
        # A COPIED-but-unverified unit (crash between COPIED and VERIFIED): try a
        # re-verify first (resume re-verifies); only re-copy if it doesn't match.
        if ledger.unit_status(name) is UnitState.COPIED and _verify_copy(src, dst):
            ledger.mark(name, UnitState.VERIFIED)
            _emit()
            continue
        try:
            _copy_recording(src, dst)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                # Leave the unit PENDING (partial dst removed by the next copy);
                # plaintext intact, distinct PAUSED state with the disk reason.
                logger.warning("migration: ENOSPC copying %s; pausing", name)
                ledger.set_run(state=MigrationState.PAUSED,
                               paused_reason=PAUSE_REASON_DISK)
                _emit()
                return ledger.summary()
            raise
        ledger.mark(name, UnitState.COPIED)
        if not _verify_copy(src, dst):
            # A verify miss leaves the unit COPIED (non-terminal) for a re-copy
            # on the next round — never advanced to VERIFIED on bad bytes.
            logger.warning("migration: verify mismatch for %s; will retry", name)
            _emit()
            continue
        ledger.mark(name, UnitState.VERIFIED)
        _emit()

    # (3) Cutover gate: every recording VERIFIED and nothing recording.
    if skipped_active or not ledger.all_verified():
        # Cutover waits — the pass stays RUNNING and a later round finishes it.
        ledger.set_run(state=MigrationState.RUNNING)
        _emit()
        return ledger.summary()
    if active_recording_name() is not None:
        ledger.set_run(state=MigrationState.RUNNING)
        _emit()
        return ledger.summary()

    _cutover(ledger, paths, mounter, sidecar_names, cutover_reservation,
             pause_sidecar_writers, set_container_enabled)
    _emit()

    # (4) Post-cutover sweep.
    return _run_sweep(ledger, paths, _emit)


def _bytes_remaining_to_copy(ledger: MigrationLedger, paths: MigrationPaths) -> int:
    """Sum on-disk sizes of recordings not yet VERIFIED (the transient need)."""
    total = 0
    for name in ledger.names_in(UnitState.PENDING, UnitState.COPIED):
        src = paths.plaintext_root / name
        for dirpath, _dirs, files in os.walk(src):
            for fname in files:
                try:
                    total += (Path(dirpath) / fname).stat().st_size
                except OSError:
                    pass
    return total


def _cutover(
    ledger: MigrationLedger,
    paths: MigrationPaths,
    mounter: Mounter,
    sidecar_names: Sequence[str],
    cutover_reservation: Callable[[], ContextManager[Any]],
    pause_sidecar_writers: Callable[[], ContextManager[Any]],
    set_container_enabled: Callable[[bool], None],
) -> None:
    """The single quiesced cutover swap (KTD-18).

    Under the caller's ``acquire_migration`` reservation and with the sidecar
    writers paused, copy the sidecars INTO the container, detach the interim
    mount, move the plaintext aside, attach the container at the real mountpoint,
    and flip the flag. The aside path is durably recorded BEFORE the destructive
    move so a crash mid-swap resumes forward.
    """
    aside = paths.aside_root
    with cutover_reservation():
        # Mark the dangerous window open + record where the plaintext will go,
        # BEFORE any destructive step, so a crash here resumes as CUTTING_OVER.
        ledger.set_run(phase=MigrationPhase.CUTTING_OVER, aside_path=str(aside))
        with pause_sidecar_writers():
            _copy_sidecars_into_container(paths, sidecar_names)
        # Detach the interim mount so the bundle can re-attach at the real
        # mountpoint (same backing store, new mountpoint).
        if mounter.is_attached(paths.interim_mountpoint):
            mounter.detach(paths.interim_mountpoint)
        _swap_plaintext_for_container(paths, mounter, set_container_enabled)
        ledger.set_run(phase=MigrationPhase.CUTOVER_DONE)


def _complete_interrupted_cutover(
    ledger: MigrationLedger,
    paths: MigrationPaths,
    mounter: Mounter,
    sidecar_names: Sequence[str],
    pause_sidecar_writers: Callable[[], ContextManager[Any]],
    set_container_enabled: Callable[[bool], None],
) -> None:
    """Finish a cutover that crashed inside the ``CUTTING_OVER`` window.

    Idempotent recovery: re-copy the sidecars (a partial copy is overwritten),
    ensure the interim is detached, complete the plaintext→container swap, then
    mark ``CUTOVER_DONE``. Safe to run whether the crash landed before or after
    the plaintext move — :func:`_swap_plaintext_for_container` is itself
    idempotent.
    """
    with pause_sidecar_writers():
        with contextlib.suppress(FileNotFoundError):
            _copy_sidecars_into_container(paths, sidecar_names)
    if mounter.is_attached(paths.interim_mountpoint):
        with contextlib.suppress(Exception):
            mounter.detach(paths.interim_mountpoint)
    _swap_plaintext_for_container(paths, mounter, set_container_enabled)
    ledger.set_run(phase=MigrationPhase.CUTOVER_DONE)


def _copy_sidecars_into_container(
    paths: MigrationPaths, sidecar_names: Sequence[str]
) -> None:
    """Copy the plaintext sidecar DBs into ``<interim>/.store`` (cutover window).

    Copied only here — after the writers paused — so a row written during
    record-through before cutover is captured, never dropped by a per-round copy.
    Copies each DB plus its ``-wal``/``-shm`` sidecars when present.
    """
    store_dir = paths.interim_mountpoint / paths.store_subdir_name
    store_dir.mkdir(parents=True, exist_ok=True)
    for name in sidecar_names:
        for suffix in ("", "-wal", "-shm"):
            src = paths.sidecar_source_dir / f"{name}{suffix}"
            if src.exists() and not src.is_symlink():
                shutil.copy2(src, store_dir / f"{name}{suffix}")


def _swap_plaintext_for_container(
    paths: MigrationPaths,
    mounter: Mounter,
    set_container_enabled: Callable[[bool], None],
) -> None:
    """Move the plaintext aside and attach the container at the real mountpoint.

    Idempotent across the crash windows inside CUTTING_OVER:

    * plaintext still at the mountpoint → move it aside, then attach + flip.
    * plaintext already moved (mountpoint empty/missing) → attach + flip.
    * container already attached (attach + flip already ran) → no-op.
    """
    final = paths.plaintext_root
    aside = paths.aside_root
    if mounter.is_attached(final):
        # The container is already the authoritative root — nothing left to swap.
        set_container_enabled(True)
        return
    # Move the plaintext aside if it is still occupying the real mountpoint and
    # has not already been relocated.
    if final.exists() and not aside.exists():
        os.rename(str(final), str(aside))
    # Re-create an empty mountpoint and attach the container there.
    final.mkdir(parents=True, exist_ok=True)
    mounter.attach(final)
    set_container_enabled(True)


def _run_sweep(
    ledger: MigrationLedger,
    paths: MigrationPaths,
    emit: Callable[[], None],
) -> MigrationSummary:
    """Delete the aside plaintext per recording (deferred delete), idempotently.

    Runs ONLY once the container is the authoritative root (phase
    ``CUTOVER_DONE``). Each recording's plaintext copy is removed and the unit
    marked ``PLAINTEXT_DELETED``; a missing copy is a no-op (no double-delete).
    When every unit is deleted the aside root is removed and the run COMPLETED.
    Resumable: a crash mid-sweep re-enters here and finishes the remainder.
    """
    aside_str = ledger.aside_path()
    aside = Path(aside_str) if aside_str else paths.aside_root
    for name in ledger.names_in(
        UnitState.PENDING, UnitState.COPIED, UnitState.VERIFIED
    ):
        target = aside / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        ledger.mark(name, UnitState.PLAINTEXT_DELETED)
        emit()
    # Remove the now-empty aside root (best-effort — leftover non-recording
    # cruft must not wedge completion).
    if aside.exists():
        with contextlib.suppress(OSError):
            shutil.rmtree(aside, ignore_errors=True)
    ledger.set_run(state=MigrationState.COMPLETED, phase=MigrationPhase.DONE)
    emit()
    return ledger.summary()
