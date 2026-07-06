"""Closed-set, resumable, cross-process backfill progress ledger (U3, SCR-178).

The backfill (SCR-178) OCR-indexes a user's *existing* recordings into the
content index. A run may span minutes-to-hours, survive a daemon restart, be
cancelled, or be paused on budget exhaustion — so its progress must live on
disk, not in memory. This ledger is that durable state.

It mirrors :class:`screencap.pipeline_state.PipelineLedger`'s disciplines:

  - A SQLite table written cross-process with ``PRAGMA busy_timeout=10000`` +
    ONE ``BEGIN IMMEDIATE`` transaction per transition, so a concurrent writer
    (a second daemon, a resumed run) can never observe a torn transition.
  - ``PRAGMA journal_mode=WAL`` for reader/writer concurrency.
  - ``0o600`` DB file / ``0o700`` parent perms, with a symlink guard and a
    post-WAL chmod of the ``-wal``/``-shm`` sidecars (mirrors
    ``content_index.py``'s ``_open``).
  - A **closed set** seeded up front so "% done" is computed against a FROZEN
    denominator (no survivorship bias — see
    ``docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md``).

Closed-set / no-survivorship-bias contract
-------------------------------------------
:meth:`seed` is idempotent for the SAME set: re-seeding never changes the
denominator and never duplicates rows (``INSERT ... ON CONFLICT DO NOTHING``).
It must NOT silently expand the denominator either — seeding a strictly
*different* set is an explicit :meth:`reset` then :meth:`seed`, never a silent
merge. ``PENDING`` / ``DONE`` / ``SKIPPED`` / ``FAILED`` are DISTINCT states;
``SKIPPED`` (a frame-skipped-for-privacy outcome) is never collapsed into
``DONE``.

Sibling-DB location (NOT inside content_index.db)
-------------------------------------------------
The ledger defaults to a **sibling** ``~/.screencap/backfill_state.db`` — NOT a
table inside ``content_index.db`` — so ledger writes never share the
``fcntl.flock``/WAL that ``content_index_write_lock()`` guards. The backfill
engine (U4) marks ledger transitions while it may *also* hold the content-index
lock around ``index_range``; colocating the ledger there would risk
re-entrancy/contention. Keeping it separate sidesteps that entirely. The path
is injectable for tests. Local-only either way — nothing here is uploaded.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Iterable

__all__ = [
    "UnitStatus",
    "RunState",
    "BackfillLedger",
    "default_ledger_path",
]

# Match the engine writer's busy_timeout (pipeline_state.py / scrub_worker.py)
# so a concurrent commit doesn't immediately fail a ledger transition.
_BUSY_TIMEOUT_MS = 10000

# A single overall-run row keyed by this sentinel — there is one run per DB.
_RUN_ROW_KEY = 0


class UnitStatus(str, Enum):
    """Per-``(recording, chunk)`` unit state.

    ``PENDING`` is the only non-terminal state; ``DONE`` / ``SKIPPED`` /
    ``FAILED`` are terminal and DISTINCT — ``SKIPPED`` (a unit whose frames
    were all privacy-skipped, or whose recording was retention-evicted) is
    NEVER conflated with ``DONE`` (a unit that indexed its full range to
    completion).
    """

    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class RunState(str, Enum):
    """Overall run state, used by the daemon auto-resume rule (U5).

    ``CANCELLED`` (user cancelled) must NOT auto-resume on daemon restart;
    ``PAUSED`` (budget exhausted / large library) MAY auto-resume. Kept
    distinct from per-unit terminality so ``is_complete()`` (all units
    terminal) is orthogonal to the run-level intent.
    """

    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


def default_ledger_path() -> Path:
    """Return the default sibling ledger path for ``backfill_state.db``.

    Deliberately a SIBLING of ``content_index.db``, not a table inside it — see
    the module docstring. Container-aware (SCR-236 KTD-2): when the
    encrypted-at-rest container flag is on the ledger lives inside the volume
    under the reserved ``.store/`` dir (``<data_root>/.store/backfill_state.db``,
    still a sibling of the index); when the flag is off it stays at the
    pre-SCR-236 location ``~/.screencap/backfill_state.db`` — byte-identical.

    Resolved via :mod:`screencap.config` (deferred import to keep this module
    light).
    """
    from screencap import config

    if config.container_enabled():
        return config.get_store_dir() / "backfill_state.db"
    return config.get_base_dir() / "backfill_state.db"


def _now() -> float:
    return time.time()


class BackfillLedger:
    """Read/write API over the ``backfill_unit_state`` ledger.

    One instance wraps one ``backfill_state.db``. Cheap to construct — it does
    not hold a connection open; every operation opens a short-lived
    ``sqlite3`` connection (mirroring ``PipelineLedger``/``scrub_worker``) so
    separate processes can each hold their own ``BackfillLedger`` without
    sharing a handle. Cross-process coordination is via SQLite's
    ``busy_timeout`` + ``BEGIN IMMEDIATE``.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else default_ledger_path()
        # Serialize this instance's own writes; cross-process coordination is
        # handled by SQLite's busy_timeout + BEGIN IMMEDIATE.
        self._lock = threading.Lock()
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Connection + schema (content_index.py-style perms/symlink guard).
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        path = self._db_path
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass

        # Symlink guard: sqlite cannot open with O_NOFOLLOW, so refuse a
        # symlinked DB path or parent before connect (mirrors content_index._open).
        if os.path.realpath(str(parent)) != os.path.abspath(str(parent)):
            raise OSError(f"backfill-ledger parent dir resolves through a symlink: {parent}")
        if path.is_symlink():
            raise OSError(f"backfill-ledger db path is a symlink: {path}")

        existed = path.exists()
        conn = sqlite3.connect(str(path))
        # Close the create-to-chmod window before any second reader can open it.
        if not existed:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        # WAL sidecars are created by SQLite's C internals at umask mode — lock
        # them down immediately, before a concurrent reader could open them.
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.exists():
                try:
                    os.chmod(side, 0o600)
                except OSError:
                    pass
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS backfill_unit_state ("
                    "  recording_dir_name TEXT NOT NULL,"
                    "  chunk_index INTEGER NOT NULL,"
                    "  status TEXT NOT NULL,"
                    "  rows_written INTEGER NOT NULL DEFAULT 0,"
                    "  updated_at REAL NOT NULL,"
                    "  PRIMARY KEY (recording_dir_name, chunk_index)"
                    ")"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS backfill_run_state ("
                    "  id INTEGER PRIMARY KEY,"
                    "  state TEXT NOT NULL,"
                    "  updated_at REAL NOT NULL"
                    ")"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Seeding (closed set, idempotent) + reset.
    # ------------------------------------------------------------------

    def seed(self, units: Iterable[tuple[str, int]]) -> None:
        """Seed the closed set of ``(recording_dir_name, chunk_index)`` units.

        Idempotent for the SAME set: ``INSERT ... ON CONFLICT DO NOTHING`` so
        re-seeding never changes the denominator and never duplicates rows, and
        never resets an already-advanced unit back to ``PENDING``. It does NOT
        silently expand the denominator on re-seed of a different set — to
        replace the closed set, call :meth:`reset` first. One atomic
        ``BEGIN IMMEDIATE`` transaction for the whole batch.
        """
        rows = list(units)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                ts = _now()
                conn.executemany(
                    "INSERT INTO backfill_unit_state "
                    "(recording_dir_name, chunk_index, status, rows_written, updated_at) "
                    "VALUES (?, ?, ?, 0, ?) "
                    "ON CONFLICT(recording_dir_name, chunk_index) DO NOTHING",
                    [(rec, int(chunk), UnitStatus.PENDING.value, ts) for rec, chunk in rows],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def reset(self) -> None:
        """Clear all units and the run state — a clean slate.

        Used when seeding a strictly different closed set (an explicit
        replacement, never a silent merge into the old denominator).
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM backfill_unit_state")
                conn.execute("DELETE FROM backfill_run_state")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Unit transitions + queries.
    # ------------------------------------------------------------------

    def mark(
        self,
        recording: str,
        chunk: int,
        status: UnitStatus,
        rows_written: int = 0,
    ) -> None:
        """Transition one unit to a terminal (or PENDING) ``status``.

        One ``BEGIN IMMEDIATE`` transaction so a concurrent writer can never
        observe a torn transition. A no-op if the unit was never seeded (the
        closed set is authoritative — :meth:`mark` never widens it).
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE backfill_unit_state "
                    "SET status=?, rows_written=?, updated_at=? "
                    "WHERE recording_dir_name=? AND chunk_index=?",
                    (status.value, int(rows_written), _now(), recording, int(chunk)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def next_pending(self) -> tuple[str, int] | None:
        """Return the next ``PENDING`` unit (stable order), or ``None``.

        Drives resume: a unit left ``PENDING`` after an aborted/cancelled run
        is returned on the next open, so the run continues from exactly the
        un-terminal units.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT recording_dir_name, chunk_index FROM backfill_unit_state "
                "WHERE status=? ORDER BY recording_dir_name, chunk_index LIMIT 1",
                (UnitStatus.PENDING.value,),
            ).fetchone()
            if row is None:
                return None
            return (str(row[0]), int(row[1]))
        finally:
            conn.close()

    def unit_status(self, recording: str, chunk: int) -> "UnitStatus | None":
        """Return the status of one seeded unit, or ``None`` if not seeded.

        Lets a resuming run (U4) skip already-terminal units without
        re-deriving them, without reaching into the ledger's connection.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status FROM backfill_unit_state "
                "WHERE recording_dir_name=? AND chunk_index=?",
                (str(recording), int(chunk)),
            ).fetchone()
            if row is None:
                return None
            return UnitStatus(row[0])
        finally:
            conn.close()

    def progress(self) -> tuple[int, int, int, int]:
        """Return ``(done, skipped, failed, total)`` over the frozen closed set.

        ``total`` is the denominator — the size of the seeded closed set, which
        never drifts on re-seed. ``done + skipped + failed`` may be < ``total``
        while units remain ``PENDING``.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM backfill_unit_state GROUP BY status"
            ).fetchall()
            counts = {str(s): int(n) for s, n in rows}
            done = counts.get(UnitStatus.DONE.value, 0)
            skipped = counts.get(UnitStatus.SKIPPED.value, 0)
            failed = counts.get(UnitStatus.FAILED.value, 0)
            total = sum(counts.values())
            return (done, skipped, failed, total)
        finally:
            conn.close()

    def is_complete(self) -> bool:
        """True iff there is >=1 seeded unit and EVERY unit is terminal.

        Terminal = ``DONE`` | ``SKIPPED`` | ``FAILED``. Returns False on an
        empty ledger (nothing seeded) and False while ANY unit is ``PENDING``.
        Independent of the run-level :meth:`run_state`.
        """
        conn = self._connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM backfill_unit_state"
            ).fetchone()[0]
            if not total:
                return False
            pending = conn.execute(
                "SELECT COUNT(*) FROM backfill_unit_state WHERE status=?",
                (UnitStatus.PENDING.value,),
            ).fetchone()[0]
            return pending == 0
        finally:
            conn.close()

    def is_recording_covered(self, recording: str) -> bool:
        """True iff this recording has >=1 seeded unit and ALL of them are terminal.

        Drives the SCR-193 cross-window paging: a library larger than one
        seed-window is processed window-by-window, and a recording whose every
        chunk is already terminal (``DONE`` / ``SKIPPED`` / ``FAILED``) must be
        skipped when building the next window so the run advances to the tail
        instead of re-scanning the same first window forever. Returns False for
        a recording with no seeded units (never started → still work to do) and
        False while ANY of its chunks is ``PENDING`` (a partial recording is
        re-selected so its remaining chunks finish first).
        """
        conn = self._connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM backfill_unit_state WHERE recording_dir_name=?",
                (str(recording),),
            ).fetchone()[0]
            if not total:
                return False
            pending = conn.execute(
                "SELECT COUNT(*) FROM backfill_unit_state "
                "WHERE recording_dir_name=? AND status=?",
                (str(recording), UnitStatus.PENDING.value),
            ).fetchone()[0]
            return pending == 0
        finally:
            conn.close()

    def has_done_with_rows(self) -> bool:
        """True iff any unit is ``DONE`` with ``rows_written > 0``.

        Drives the SCR-193 content-index-deletion guard: the ledger
        (``backfill_state.db``) and the index (``content_index.db``) are separate
        files, so deleting the index after a backfill would otherwise leave DONE
        units that are skipped forever on resume — the index stays empty for
        them. A DONE unit with ``rows_written > 0`` is positive proof that rows
        were written to the index; if the index file is then missing, the engine
        resets the ledger and re-OCRs. The ``> 0`` guard is deliberate: a fully
        privacy-blocked / empty recording is legitimately DONE with zero rows and
        never created the store (``index_range``'s empty-store guard), so its
        absence is expected and must NOT trigger a reset.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM backfill_unit_state "
                "WHERE status=? AND rows_written>0 LIMIT 1",
                (UnitStatus.DONE.value,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Run-level state (for the U5 auto-resume rule).
    # ------------------------------------------------------------------

    def set_run_state(self, state: RunState) -> None:
        """Set the overall run state (upsert the single run row)."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO backfill_run_state (id, state, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
                    "updated_at=excluded.updated_at",
                    (_RUN_ROW_KEY, state.value, _now()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def run_state(self) -> RunState | None:
        """Return the overall run state, or ``None`` if never set."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state FROM backfill_run_state WHERE id=?",
                (_RUN_ROW_KEY,),
            ).fetchone()
            return RunState(row[0]) if row is not None else None
        finally:
            conn.close()

    def is_cancelled(self) -> bool:
        """True iff the run was explicitly ``CANCELLED`` (not budget-``PAUSED``).

        The daemon (U5) uses this to honor the user's last intent: a cancelled
        run does NOT auto-resume on restart; a paused run may.
        """
        return self.run_state() == RunState.CANCELLED

    # ------------------------------------------------------------------
    # Lifecycle.
    # ------------------------------------------------------------------

    def close(self) -> None:
        """No-op for API symmetry — connections are short-lived per operation."""

    def __enter__(self) -> "BackfillLedger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
