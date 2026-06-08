"""Durable per-chunk state ledger over the local-only ``recording.db`` (U1).

This is the on-disk replacement for ``ChunkProcessor._chunk_results`` — the
in-memory ``dict[int, ChunkStatus]`` that the four prior production incidents
in
``docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md``
showed could not be trusted across a crash. The ledger re-establishes the five
data-loss prevention rules ON DISK so a terminal stage (U7) can reconstruct
correct state after an interruption (R3/R9). Every later unit (U2/U5/U7/U8/U9)
reads and advances this ledger.

The five prevention rules, ported to ledger invariants
------------------------------------------------------
1. **Track the full universe of expected work.** One row per *expected*
   chunk index is seeded ``PENDING`` at chunk rotation — a CLOSED SET,
   never appended on completion. ``all_uploaded()`` over a closed set
   cannot exhibit survivorship bias: a chunk that rotated but never
   processed reads as ``PENDING`` (blocking the gate), not as a missing
   entry.
2. **Distinguish "disabled" from "succeeded".** Per-concern tri-state
   columns (``upload_state``: ``UPLOADED | SKIPPED | FAILED | PENDING``).
   ``SKIPPED`` ("uploads intentionally off") is never conflated with
   ``UPLOADED`` or ``FAILED``. Only ``UPLOADED`` permits eviction.
3. **Never delete local files without confirming remote existence NOW.**
   ``begin_eviction`` takes a ``remote_exists`` callback and re-confirms
   remote presence at eviction time — a historical ``UPLOADED`` is not
   trusted (we do NOT port ``_delete_old_chunks``' delete-on-EMITTED-alone
   shortcut). ``begin_local_eviction`` is the sibling for ``local``-only
   chunks (``LOCAL_DONE``, never uploaded): a local size/time cap evicts the
   rich copy with no remote precondition (R11) — but it accepts ONLY
   ``LOCAL_DONE``, so it can never be used to bypass the cloud floor for an
   un-uploaded cloud chunk.
4. **Test degraded paths.** Crash-mid-transition, refused-eviction, and
   resume-from-``EVICT_PENDING`` are all first-class API states with tests.
5. **The completeness signal is derived from completeness evidence.**
   ``chunks_expected`` is frozen ONCE and never shrunk on eviction;
   ``finalize_gate_satisfied`` gates on the frozen count, not a live glob.

Pinned ChunkStatus -> ledger upload-state mapping
-------------------------------------------------
The legacy ``chunk_processor.ChunkStatus`` enum maps onto ``UploadState``
as follows (see :func:`from_chunk_status`):

  - ``EMITTED``            -> ``UPLOADED``  (the only evictable state)
  - ``NETWORK_SKIPPED``    -> ``SKIPPED``   ("disabled != success")
  - ``NETWORK_INCOMPLETE`` -> ``FAILED``    (terminal; never evicted /
                                             never counted complete)
  - ``FAILED``             -> ``FAILED``
  - ``PENDING``            -> ``PENDING``

``NETWORK_INCOMPLETE -> FAILED`` (not ``PENDING``) was confirmed by reading
how ``NETWORK_INCOMPLETE`` is used in ``chunk_processor.py`` and
``engine/db/models.py``: it is a *terminal* non-EMITTED state set when a
chunk's window overlaps a ``proxy_crashed`` / ``network_writer_failed``
``NetworkHealth`` row. ``reconcile_against_gcs`` explicitly excludes it from
re-promotion ("terminal-non-EMITTED"), ``all_chunks_uploaded()`` blocks the
gate on it, and ``_delete_old_chunks`` refuses to evict it — exactly the
ledger ``FAILED`` contract (reachable, never evicted, never counted
complete). It is NOT ``PENDING``/retryable because the proxy-crash incident
is permanent for that window.

Cross-process write ownership
-----------------------------
The ledger lives in ``recording.db`` and is written by more than one
process:

  - The **engine writer** (recording side) seeds ``PENDING`` rows at chunk
    rotation, concurrently with its screenshot/event writes, and freezes
    ``chunks_expected`` at the final rotation.
  - A separate **terminal-stage** process (U7) advances states
    (STAGED / SCRUBBED / UPLOADED / SKIPPED / FAILED) and runs eviction.
  - ``list_recording_files`` / ``checkpoint_and_upload_db`` run
    ``wal_checkpoint(TRUNCATE)`` on the same DB.

To survive that concurrency the writer here mirrors
``privacy/scrub_worker.py``: a dedicated ``sqlite3.connect`` with
``PRAGMA busy_timeout=10000`` and ONE ``BEGIN IMMEDIATE`` transaction per
state change, so a concurrent WAL checkpoint can never observe a torn
transition. This deliberately bypasses ``recording_db.open_recording_db``
(which is read-mostly with ``busy_timeout=5000`` and no live-writer
coexistence story) for the same reason ``scrub_worker`` does.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from screencap.chunk_processor import ChunkStatus

__all__ = [
    "Lifecycle",
    "StageState",
    "ScrubState",
    "UploadState",
    "EvictState",
    "ChunkRow",
    "PipelineLedger",
    "LedgerError",
    "EvictionRefused",
    "ensure_pipeline_state_schema",
    "from_chunk_status",
    "reconcile_ledger_from_disk",
]

# Match the engine writer's busy_timeout (privacy/scrub_worker.py:471) so a
# long-running engine commit doesn't immediately fail a ledger transition.
_BUSY_TIMEOUT_MS = 10000


class Lifecycle(str, Enum):
    """Coarse per-chunk lifecycle state (the U1 state diagram).

    PENDING -> STAGED -> (SCRUBBED -> UPLOADED | LOCAL_DONE | SKIPPED);
    UPLOADED -> EVICTED (via the evict_state sub-machine). FAILED is
    reachable from STAGED/SCRUBBED and is terminal — never evicted, never
    counted complete.
    """

    PENDING = "pending"
    STAGED = "staged"
    SCRUBBED = "scrubbed"
    UPLOADED = "uploaded"
    LOCAL_DONE = "local_done"
    SKIPPED = "skipped"
    FAILED = "failed"
    EVICTED = "evicted"


class StageState(str, Enum):
    """Destination-agnostic stages (transcribe / export / manifest)."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class ScrubState(str, Enum):
    """Cloud-bound privacy transform state."""

    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class UploadState(str, Enum):
    """Per-chunk upload result — only ``UPLOADED`` permits eviction.

    ``SKIPPED`` is "uploads intentionally off" and is a DISTINCT terminal
    state, never conflated with ``UPLOADED`` (eviction-eligible) or
    ``FAILED`` (retry/leave-on-disk). Per
    ``chunk-upload-sentinel-gating-and-data-loss.md`` fix #4.
    """

    PENDING = "pending"
    UPLOADED = "uploaded"
    SKIPPED = "skipped"
    FAILED = "failed"


class EvictState(str, Enum):
    """Local-file eviction sub-machine.

    NONE -> EVICT_PENDING -> EVICTED. The explicit ``EVICT_PENDING`` is the
    crash-safety pivot: it is committed BEFORE the unlink, so a crash
    before commit leaves the file present + ``UPLOADED`` (safe, no loss),
    and a crash after commit resumes the unlink from ``EVICT_PENDING``.
    """

    NONE = "none"
    EVICT_PENDING = "evict_pending"
    EVICTED = "evicted"


class LedgerError(RuntimeError):
    """A ledger invariant was violated (e.g. re-freezing chunks_expected)."""


class EvictionRefused(RuntimeError):
    """Eviction was refused because a precondition did not hold.

    Raised when a chunk is not ``UPLOADED``, or when the fresh remote
    re-confirmation came back False. The local file is left in place — a
    refused eviction is the safe outcome (no data loss).
    """


@dataclass(frozen=True)
class ChunkRow:
    """An immutable snapshot of one ``pipeline_chunk_state`` row."""

    chunk_index: int
    lifecycle: Lifecycle
    stages_state: StageState
    scrub_state: ScrubState
    upload_state: UploadState
    evict_state: EvictState
    detail: str | None = None


def from_chunk_status(status: "ChunkStatus") -> UploadState:
    """Map a legacy ``chunk_processor.ChunkStatus`` onto an ``UploadState``.

    See the module docstring for the pinned mapping and the
    ``NETWORK_INCOMPLETE -> FAILED`` confirmation. Used by U9 migration to
    project an in-flight recording's in-memory results onto the ledger.
    """
    from screencap.chunk_processor import ChunkStatus

    return {
        ChunkStatus.EMITTED: UploadState.UPLOADED,
        ChunkStatus.NETWORK_SKIPPED: UploadState.SKIPPED,
        ChunkStatus.NETWORK_INCOMPLETE: UploadState.FAILED,
        ChunkStatus.FAILED: UploadState.FAILED,
        ChunkStatus.PENDING: UploadState.PENDING,
    }[status]


def ensure_pipeline_state_schema(db_path: Path | str) -> None:
    """Create the ledger table + ``chunks_expected`` column on an EXISTING DB.

    Idempotent. Delegates to ``engine.db._migrate_schema`` (which creates
    the ``pipeline_chunk_state`` table via ``checkfirst=True`` and
    ALTER-adds ``recording.chunks_expected``) so there is a single
    schema-evolution code path. Safe to call on a fresh ``create_db`` DB
    (no-op) or an old pre-U1 ``recording.db`` (creates the missing table).
    """
    from screencap.engine.db import _migrate_schema

    _migrate_schema(str(db_path))


def _now() -> float:
    return time.time()


class PipelineLedger:
    """Read/write API over the ``pipeline_chunk_state`` ledger.

    One instance wraps one ``recording.db``. Cheap to construct — it does
    not hold a connection open; every operation opens a short-lived
    ``sqlite3`` connection (mirroring ``scrub_worker``) so the engine
    writer and terminal-stage process can each hold their own
    ``PipelineLedger`` without sharing a handle.

    The ``recording_id`` is resolved once at construction (the recording
    table has a single row per ``recording.db``). All writes are scoped to
    it.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        # Serialize this instance's own writes; cross-process coordination
        # is handled by SQLite's busy_timeout + BEGIN IMMEDIATE.
        self._lock = threading.Lock()
        self._recording_id = self._lookup_recording_id()

    # ------------------------------------------------------------------
    # Connection helpers (scrub_worker-style: dedicated short-lived conn).
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # NOTE: deliberately bypasses recording_db.open_recording_db. This
        # is a LIVE WRITER that coexists with the engine recorder, so it
        # needs busy_timeout=10000 (vs the helper's 5000) and BEGIN
        # IMMEDIATE transactions — same rationale as privacy/scrub_worker.py.
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return conn

    def _lookup_recording_id(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT id FROM recording LIMIT 1").fetchone()
            if row is None:
                raise LedgerError(
                    f"recording.db has no recording row: {self._db_path}"
                )
            return int(row[0])
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Seeding (closed set) + chunks_expected freezing.
    # ------------------------------------------------------------------

    def seed_chunk(self, chunk_index: int) -> None:
        """Seed one ``PENDING`` row for ``chunk_index`` (closed-set insert).

        Idempotent per index: an ``INSERT OR IGNORE`` so re-seeding (e.g. a
        retried rotation) never resets an already-advanced row. Called by
        the engine writer at chunk rotation, BEFORE any processing — this
        is the survivorship-bias fix.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO pipeline_chunk_state "
                    "(recording_id, chunk_index, lifecycle, stages_state, "
                    " scrub_state, upload_state, evict_state, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._recording_id, chunk_index,
                        Lifecycle.PENDING.value, StageState.PENDING.value,
                        ScrubState.PENDING.value, UploadState.PENDING.value,
                        EvictState.NONE.value, _now(),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def freeze_chunks_expected(self, count: int) -> None:
        """Freeze the expected-chunk count ONCE; reject a conflicting refreeze.

        Called by the engine writer at the final rotation. The terminal
        finalize gate keys off this frozen count, never a live file glob,
        so eviction (which removes on-disk files) cannot shrink the
        completeness target. Re-freezing the SAME value is a no-op;
        re-freezing a DIFFERENT value raises ``LedgerError``.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT chunks_expected FROM recording WHERE id=?",
                    (self._recording_id,),
                ).fetchone()
                existing = row[0] if row is not None else None
                if existing is not None and int(existing) != count:
                    conn.rollback()
                    raise LedgerError(
                        f"chunks_expected already frozen at {existing}, "
                        f"refusing to refreeze to {count}"
                    )
                conn.execute(
                    "UPDATE recording SET chunks_expected=? WHERE id=?",
                    (count, self._recording_id),
                )
                conn.commit()
            except LedgerError:
                raise
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def chunks_expected(self) -> int | None:
        """Return the frozen expected-chunk count, or ``None`` if not frozen."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT chunks_expected FROM recording WHERE id=?",
                (self._recording_id,),
            ).fetchone()
            if row is None or row[0] is None:
                return None
            return int(row[0])
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Forward state transitions (one BEGIN IMMEDIATE transaction each).
    # ------------------------------------------------------------------

    def _transition(
        self,
        chunk_index: int,
        *,
        lifecycle: Lifecycle | None = None,
        stages_state: StageState | None = None,
        scrub_state: ScrubState | None = None,
        upload_state: UploadState | None = None,
        evict_state: EvictState | None = None,
        detail: str | None = None,
    ) -> None:
        """Apply a partial state update in a single atomic transaction.

        Only the provided columns are written; the rest are untouched. The
        whole update is one ``BEGIN IMMEDIATE`` + ``UPDATE`` + ``commit`` so
        a concurrent WAL checkpoint can never observe a torn transition.
        Raises ``LedgerError`` if the row does not exist (seed first).
        """
        sets: list[str] = []
        params: list[object] = []
        if lifecycle is not None:
            sets.append("lifecycle=?")
            params.append(lifecycle.value)
        if stages_state is not None:
            sets.append("stages_state=?")
            params.append(stages_state.value)
        if scrub_state is not None:
            sets.append("scrub_state=?")
            params.append(scrub_state.value)
        if upload_state is not None:
            sets.append("upload_state=?")
            params.append(upload_state.value)
        if evict_state is not None:
            sets.append("evict_state=?")
            params.append(evict_state.value)
        if detail is not None:
            sets.append("detail=?")
            params.append(detail)
        sets.append("updated_at=?")
        params.append(_now())
        params.extend([self._recording_id, chunk_index])

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "UPDATE pipeline_chunk_state SET " + ", ".join(sets)
                    + " WHERE recording_id=? AND chunk_index=?",
                    params,
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    raise LedgerError(
                        f"no ledger row for chunk {chunk_index} — seed first"
                    )
                conn.commit()
            except LedgerError:
                raise
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def mark_staged(self, chunk_index: int) -> None:
        """Destination-agnostic stages done: PENDING -> STAGED."""
        self._transition(
            chunk_index,
            lifecycle=Lifecycle.STAGED,
            stages_state=StageState.DONE,
        )

    def mark_scrubbed(self, chunk_index: int) -> None:
        """Cloud-bound privacy transform done: STAGED -> SCRUBBED."""
        self._transition(
            chunk_index,
            lifecycle=Lifecycle.SCRUBBED,
            scrub_state=ScrubState.DONE,
        )

    def mark_uploaded(
        self,
        chunk_index: int,
        *,
        on_confirm: Callable[[], None] | None = None,
    ) -> None:
        """Record a confirmed upload: -> UPLOADED.

        Crash-safe irreversible-transition ordering: ``on_confirm`` (the
        GCS-confirmation callback supplied by the upload caller, U7) runs
        FIRST and must succeed before the ``UPLOADED`` row is written. If
        it raises, the row is left at its prior committed state — never a
        half-written ``UPLOADED``. On re-entry the caller always re-stats
        rather than trusting a bare ``UPLOADED`` (see ``begin_eviction``).
        """
        if on_confirm is not None:
            on_confirm()
        self._transition(
            chunk_index,
            lifecycle=Lifecycle.UPLOADED,
            upload_state=UploadState.UPLOADED,
        )

    def mark_skipped(self, chunk_index: int) -> None:
        """Uploads intentionally disabled: -> SKIPPED (NOT uploaded).

        ``SKIPPED`` blocks the all-uploaded gate and refuses eviction —
        "disabled != success" (fix #4). Use this when uploads are turned
        off by policy/config, NEVER when an upload failed (use
        ``mark_failed``).
        """
        self._transition(
            chunk_index,
            lifecycle=Lifecycle.SKIPPED,
            upload_state=UploadState.SKIPPED,
        )

    def mark_local_done(self, chunk_index: int) -> None:
        """Local-only recording: stages done, no scrub/upload -> LOCAL_DONE.

        Counts toward ``all_complete()`` (the recording is finished) but
        NOT toward ``all_uploaded()`` and is never eviction-eligible — a
        local-only chunk's rich artifacts stay on disk (R7).
        """
        self._transition(
            chunk_index,
            lifecycle=Lifecycle.LOCAL_DONE,
        )

    def mark_failed(self, chunk_index: int, detail: str | None = None) -> None:
        """Mark a chunk FAILED: terminal, never evicted, never counted complete."""
        self._transition(
            chunk_index,
            lifecycle=Lifecycle.FAILED,
            upload_state=UploadState.FAILED,
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Eviction: re-confirm remote NOW, commit EVICT_PENDING, unlink, EVICTED.
    # ------------------------------------------------------------------

    def begin_eviction(
        self,
        chunk_index: int,
        *,
        remote_exists: Callable[[], bool],
    ) -> None:
        """Step 1+2 of eviction: re-confirm remote, then commit EVICT_PENDING.

        Refuses (``EvictionRefused``) unless the chunk is ``UPLOADED`` AND a
        FRESH ``remote_exists()`` returns True. A historical ``UPLOADED`` is
        NOT trusted — we re-stat now. We deliberately do NOT port
        ``_delete_old_chunks``' delete-on-EMITTED-alone shortcut (it
        deletes without a fresh re-stat).

        On success ``evict_state`` is committed ``EVICT_PENDING`` BEFORE any
        unlink, so a crash here leaves the file present + ``UPLOADED``
        (safe). The unlink itself happens in :meth:`commit_eviction`.
        """
        row = self.get_chunk(chunk_index)
        if row is None:
            raise EvictionRefused(f"no ledger row for chunk {chunk_index}")
        if row.upload_state != UploadState.UPLOADED:
            raise EvictionRefused(
                f"chunk {chunk_index} is {row.upload_state.value}, not uploaded "
                "— only UPLOADED chunks are evictable"
            )
        # Re-confirm remote existence NOW — do not trust a historical
        # UPLOADED (prevention rule #3).
        if not remote_exists():
            raise EvictionRefused(
                f"chunk {chunk_index} remote re-confirmation failed — "
                "refusing to delete local copy"
            )
        self._transition(chunk_index, evict_state=EvictState.EVICT_PENDING)

    def begin_local_eviction(self, chunk_index: int) -> None:
        """Step 1+2 of eviction for a LOCAL-only chunk (no remote precondition).

        The sibling of :meth:`begin_eviction` for ``local`` recordings whose
        chunks are ``LOCAL_DONE`` (never uploaded, so there is no remote copy
        to re-confirm — and demanding one would make a local-only size/time cap
        un-evictable, defeating R11). Refuses (``EvictionRefused``) unless the
        chunk is ``LOCAL_DONE``; in particular a ``FAILED`` / in-flight / cloud
        (``UPLOADED``) chunk is NOT evictable via this path.

        Like the cloud path it commits ``EVICT_PENDING`` BEFORE any unlink, so
        the SAME :meth:`commit_eviction` finishes it and the SAME
        ``EVICT_PENDING -> EVICTED`` crash-resumability holds: a crash before
        commit leaves the rich local file present + ``LOCAL_DONE`` (safe), a
        crash after commit resumes the unlink from ``EVICT_PENDING``.

        The cloud :meth:`begin_eviction` (``UPLOADED`` + fresh remote re-stat)
        is deliberately left UNCHANGED — this is an additive, distinct entry so
        the never-delete-un-uploaded floor for cloud chunks cannot be bypassed
        through the local path (only ``LOCAL_DONE`` is accepted here, never
        ``PENDING``/``STAGED``/``SCRUBBED``/``UPLOADED``/``FAILED``).
        """
        row = self.get_chunk(chunk_index)
        if row is None:
            raise EvictionRefused(f"no ledger row for chunk {chunk_index}")
        if row.lifecycle != Lifecycle.LOCAL_DONE:
            raise EvictionRefused(
                f"chunk {chunk_index} is {row.lifecycle.value}, not local_done "
                "— begin_local_eviction only evicts LOCAL_DONE chunks"
            )
        self._transition(chunk_index, evict_state=EvictState.EVICT_PENDING)

    def commit_eviction(
        self,
        chunk_index: int,
        *,
        unlink: Callable[[], None],
    ) -> None:
        """Step 3+4 of eviction: unlink local files, then commit EVICTED.

        Precondition: the row must already be ``EVICT_PENDING`` (set by
        :meth:`begin_eviction`, possibly in a prior process before a crash).
        ``unlink`` runs while the row is ``EVICT_PENDING`` — so a crash
        mid-unlink resumes here on restart (the row is still
        ``EVICT_PENDING``). Only after ``unlink`` returns do we commit
        ``EVICTED``.
        """
        row = self.get_chunk(chunk_index)
        if row is None:
            raise EvictionRefused(f"no ledger row for chunk {chunk_index}")
        if row.evict_state != EvictState.EVICT_PENDING:
            raise EvictionRefused(
                f"chunk {chunk_index} evict_state is {row.evict_state.value}, "
                "expected evict_pending — call begin_eviction first"
            )
        # Unlink while still EVICT_PENDING so a crash resumes from here.
        unlink()
        self._transition(
            chunk_index,
            lifecycle=Lifecycle.EVICTED,
            evict_state=EvictState.EVICTED,
        )

    # ------------------------------------------------------------------
    # Queries.
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> ChunkRow:
        return ChunkRow(
            chunk_index=int(row["chunk_index"]),
            lifecycle=Lifecycle(row["lifecycle"]),
            stages_state=StageState(row["stages_state"]),
            scrub_state=ScrubState(row["scrub_state"]),
            upload_state=UploadState(row["upload_state"]),
            evict_state=EvictState(row["evict_state"]),
            detail=row["detail"],
        )

    def get_chunk(self, chunk_index: int) -> ChunkRow | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM pipeline_chunk_state "
                "WHERE recording_id=? AND chunk_index=?",
                (self._recording_id, chunk_index),
            ).fetchone()
            return self._row_to_chunk(row) if row is not None else None
        finally:
            conn.close()

    def all_chunks(self) -> list[ChunkRow]:
        """Return every seeded chunk row, ordered by ``chunk_index``."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM pipeline_chunk_state WHERE recording_id=? "
                "ORDER BY chunk_index",
                (self._recording_id,),
            ).fetchall()
            return [self._row_to_chunk(r) for r in rows]
        finally:
            conn.close()

    def updated_at_map(self, indices: list[int]) -> dict[int, float]:
        """Return ``{chunk_index: updated_at}`` for the given chunk indices.

        Used by retention's age-based eviction (``delete_after_days``) to read
        each candidate's last-modified epoch through the ledger's own
        connection/locking discipline, rather than callers reaching into private
        attributes. Rows with a NULL ``updated_at`` are omitted.
        """
        if not indices:
            return {}
        placeholders = ",".join("?" for _ in indices)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT chunk_index, updated_at FROM pipeline_chunk_state "
                f"WHERE recording_id=? AND chunk_index IN ({placeholders})",
                [self._recording_id, *indices],
            ).fetchall()
            return {int(r[0]): float(r[1]) for r in rows if r[1] is not None}
        finally:
            conn.close()

    def chunks_in_state(
        self,
        *,
        lifecycle: Lifecycle | None = None,
        upload: UploadState | None = None,
        evict: EvictState | None = None,
    ) -> list[ChunkRow]:
        """Return chunks matching the given (ANDed) state filters."""
        clauses = ["recording_id=?"]
        params: list[object] = [self._recording_id]
        if lifecycle is not None:
            clauses.append("lifecycle=?")
            params.append(lifecycle.value)
        if upload is not None:
            clauses.append("upload_state=?")
            params.append(upload.value)
        if evict is not None:
            clauses.append("evict_state=?")
            params.append(evict.value)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM pipeline_chunk_state WHERE "
                + " AND ".join(clauses) + " ORDER BY chunk_index",
                params,
            ).fetchall()
            return [self._row_to_chunk(r) for r in rows]
        finally:
            conn.close()

    def chunks_needing_upload(self) -> list[ChunkRow]:
        """Chunks not yet in a terminal upload state (PENDING upload_state).

        The reconstruct-from-disk seam (R3/R9): after a crash the terminal
        stage asks this to find exactly which chunks still need work, so it
        re-runs only the unfinished ones (no duplicate uploads).
        """
        return self.chunks_in_state(upload=UploadState.PENDING)

    def all_uploaded(self) -> bool:
        """True iff there is >=1 seeded chunk and EVERY chunk is UPLOADED.

        Closed-set, EMITTED/UPLOADED-only — mirrors
        ``ChunkProcessor.all_chunks_uploaded()``. Returns False on an empty
        ledger (no rotations -> writer likely failed) and False if ANY
        chunk is PENDING / SKIPPED / FAILED. Because the set is closed, a
        forgotten chunk surfaces as PENDING and blocks the gate (Bug 2).
        Note an ``EVICTED`` chunk was ``UPLOADED`` first; its
        ``upload_state`` stays ``UPLOADED`` so it still satisfies the gate.
        """
        rows = self.all_chunks()
        if not rows:
            return False
        return all(r.upload_state == UploadState.UPLOADED for r in rows)

    def all_complete(self) -> bool:
        """True iff every seeded chunk reached a terminal "done" lifecycle.

        "Done" = UPLOADED | LOCAL_DONE | SKIPPED | EVICTED. FAILED and any
        in-flight (PENDING/STAGED/SCRUBBED) chunk block completeness.
        Distinct from ``all_uploaded`` so a local-only recording (all
        LOCAL_DONE) reads complete without being "uploaded".
        """
        rows = self.all_chunks()
        if not rows:
            return False
        terminal = {
            Lifecycle.UPLOADED,
            Lifecycle.LOCAL_DONE,
            Lifecycle.SKIPPED,
            Lifecycle.EVICTED,
        }
        return all(r.lifecycle in terminal for r in rows)

    def finalize_gate_satisfied(self) -> bool:
        """True iff the frozen ``chunks_expected`` closed set is all uploaded.

        The completeness signal derived from completeness evidence
        (prevention rule #5): gates on the FROZEN count, never a live glob.
        Returns False if ``chunks_expected`` is not yet frozen, if fewer
        rows are seeded than expected (the closed set is incomplete), or if
        ANY seeded chunk is not in a ``UPLOADED``/``EVICTED`` lifecycle with a
        confirmed ``UPLOADED`` upload state. Eviction does not shrink
        ``chunks_expected`` (and an ``EVICTED`` row keeps ``upload_state ==
        UPLOADED``), so evicting local files never relaxes this gate.

        Gates on the actual seeded closed set rather than assuming chunk
        indices are contiguous ``0..expected-1``: the U9 migration reconciler
        freezes ``chunks_expected = len(closed_set)`` over a possibly-SPARSE
        on-disk index set, so a ``range(expected)`` superset check would be
        permanently unsatisfiable (e.g. on-disk indices ``{2,3,4}`` →
        ``expected=3`` → ``range(3)={0,1,2}`` never covered) even when every
        real chunk is uploaded. The closed set IS the seeded rows.
        """
        expected = self.chunks_expected()
        if expected is None:
            return False
        rows = self.all_chunks()
        # The closed set must be fully seeded: at least ``expected`` rows.
        if len(rows) < expected:
            return False
        # Every seeded chunk must have a confirmed upload (UPLOADED, or
        # EVICTED which kept its UPLOADED upload_state). A single PENDING /
        # FAILED / SKIPPED row blocks the gate (closed-set, no survivorship).
        return all(
            r.lifecycle in (Lifecycle.UPLOADED, Lifecycle.EVICTED)
            and r.upload_state == UploadState.UPLOADED
            for r in rows
        )


# ---------------------------------------------------------------------------
# U9 migration reconciler — seed the ledger from on-disk evidence,
# CONSERVATIVELY. Legacy status is a HINT, a fresh GCS re-stat is the only
# proof, GCS-unreachable -> PENDING (never UPLOADED), stale sentinel ignored.
# ---------------------------------------------------------------------------


def _legacy_uploaded_indices(recording_dir: Path) -> set[int]:
    """Parse the legacy ``.chunk_*_status.json`` markers into chunk indices.

    The OLD live path (``chunk_processor.upload_chunk_files``) wrote one marker
    per chunk whose CORE files uploaded, named
    ``.chunk_<basename-without-ext>_status.json`` — i.e.
    ``.chunk_chunk_0000_status.json`` for ``chunk_0000.mp4``. These prove the
    core files were uploaded UNDER THE OLD PATH; they say NOTHING about
    new-model scrub completeness or current remote presence, so this is used
    only as a HINT (which chunks to *re-stat first* / prioritise), NEVER as
    proof of an ``UPLOADED`` state. The fresh GCS re-confirm is the only proof.

    Best-effort: an unparseable / oddly-named marker is skipped, never fatal.
    """
    out: set[int] = set()
    for marker in recording_dir.glob(".chunk_*_status.json"):
        # ".chunk_chunk_0000_status.json" -> stem "chunk_chunk_0000_status".
        stem = marker.name
        # Strip the ".chunk_" prefix and "_status.json" suffix, leaving the
        # inner basename (e.g. "chunk_0000").
        if not stem.startswith(".chunk_") or not stem.endswith("_status.json"):
            continue
        inner = stem[len(".chunk_"):-len("_status.json")]
        parts = inner.split("_")
        # inner is like "chunk_0000"; take the trailing numeric component.
        for tok in reversed(parts):
            if tok.isdigit():
                out.add(int(tok))
                break
    return out


def _on_disk_chunk_indices(recording_dir: Path) -> set[int]:
    """Chunk indices for which a ``chunk_NNNN.mp4`` exists on disk.

    The closed set is seeded from these (the chunks the recording PRODUCED).
    A legacy status marker for a chunk whose media was already evicted is still
    counted so the closed set does not develop survivorship holes.
    """
    out: set[int] = set()
    for vf in recording_dir.glob("chunk_*.mp4"):
        try:
            out.add(int(vf.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return out


def reconcile_ledger_from_disk(
    recording_dir: Path | str,
    *,
    remote_exists: Callable[[int], bool] | None = None,
) -> "PipelineLedger":
    """Seed/reconcile the U1 ledger from on-disk evidence, CONSERVATIVELY (U9).

    Run on the first pass of the new code over an OLD-PATH recording (legacy
    ``.chunk_*_status.json`` markers, a possibly-stale ``recording_complete.json``,
    a partially-uploaded chunk set), and idempotently on every later pass. It
    re-establishes the closed-set, no-survivorship invariant on the ledger and
    NEVER over-optimistically marks a chunk done:

    1. **Closed set from disk.** One ``PENDING`` row per chunk found on disk
       (``chunk_NNNN.mp4``) OR named by a legacy status marker — so an evicted
       chunk that still has its marker is not silently dropped. Seeding is
       ``INSERT OR IGNORE`` (``seed_chunk``), so a row the engine writer or a
       prior reconcile already advanced is NEVER reset to PENDING.
    2. **Legacy status is a HINT, not proof.** ``_legacy_uploaded_indices``
       only tells us which chunks the OLD path *claimed* to upload. We do NOT
       trust it: we re-stat.
    3. **Fresh GCS re-confirm is the only proof of UPLOADED.** For every chunk
       not already ``UPLOADED``, we call ``remote_exists(idx)`` (the U7
       ``_chunk_confirmed_remote`` seam by default) and ``mark_uploaded`` ONLY
       on a fresh True. A False, or a raise (GCS unreachable: offline, expired
       token, signing function down), leaves the chunk ``PENDING`` —
       needs-verification, never ``UPLOADED``. This mirrors
       ``chunk_processor.reconcile_against_gcs``'s fail-conservative posture
       (on stat failure, stay needs-work). A migration seed therefore never
       authorises a later eviction (U8 re-stats at delete time regardless).
    4. **Stale sentinel ignored (Bug 3).** Any legacy ``recording_complete.json``
       is NEVER read for ``chunks_expected``; the frozen count is regenerated
       from the reconciled closed set (the chunks actually on disk / claimed).
    5. **Frozen count respected.** If the engine writer already froze
       ``chunks_expected`` (final rotation), we do NOT refreeze a conflicting
       value — ``freeze_chunks_expected`` no-ops on the same count and we never
       pass a different one.

    Legacy single-file recordings (no ``chunk_*.mp4``, no markers) seed zero
    chunk rows and a frozen count of 0 — they are NOT forced into the per-chunk
    model (R14); the caller routes them through the whole-dir scrub branch.

    Args:
        recording_dir: the recording's source dir (``recording.db`` + chunks).
        remote_exists: ``(idx) -> bool`` fresh remote re-confirmation. When
            ``None``, the real U7 ``terminal_stage._chunk_confirmed_remote``
            seam is used (probes ``request_signed_urls``; any error -> False).
            A callback that RAISES is treated as GCS-unreachable -> the chunk
            stays ``PENDING`` (conservative).

    Returns:
        The reconciled :class:`PipelineLedger`.
    """
    recording_dir = Path(recording_dir)
    db_path = recording_dir / "recording.db"
    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)

    # 1. Closed set: chunks on disk ∪ chunks named by a surviving legacy marker.
    on_disk = _on_disk_chunk_indices(recording_dir)
    legacy = _legacy_uploaded_indices(recording_dir)
    closed_set = sorted(on_disk | legacy)
    for idx in closed_set:
        ledger.seed_chunk(idx)  # INSERT OR IGNORE — never resets an advanced row.

    # 4 + 5. Freeze chunks_expected from the RECONCILED count (NOT the stale
    # sentinel — Bug 3). Respect an existing frozen count: only freeze when the
    # ledger has not frozen a conflicting value yet.
    existing = ledger.chunks_expected()
    if existing is None:
        ledger.freeze_chunks_expected(len(closed_set))
    # If a count is already frozen we leave it — the engine writer's frozen
    # count is authoritative and `freeze_chunks_expected` would raise on a
    # conflict. (A same-value refreeze would be a no-op but is unnecessary.)

    # 2 + 3. Re-stat every not-yet-UPLOADED chunk; mark UPLOADED ONLY on a
    # fresh remote confirm. Legacy markers are not consulted as proof here —
    # the confirm is the gate.
    confirm = _make_reconcile_confirm(recording_dir, remote_exists)
    for row in ledger.all_chunks():
        if row.upload_state == UploadState.UPLOADED:
            continue  # idempotent: never downgrade a confirmed chunk.
        if confirm(row.chunk_index):
            try:
                ledger.mark_uploaded(row.chunk_index)
            except LedgerError:
                # Row vanished between read and write (concurrent process) —
                # conservative: leave it for the next pass.
                pass

    return ledger


def _make_reconcile_confirm(
    recording_dir: Path,
    remote_exists: Callable[[int], bool] | None,
) -> Callable[[int], bool]:
    """Build the fresh-remote-confirm callback for the reconciler (fail-closed).

    When ``remote_exists`` is injected (tests / a caller with its own re-stat),
    use it but SWALLOW any exception as a conservative ``False`` (GCS
    unreachable must never flip a chunk to UPLOADED — it stays PENDING). When
    ``None``, reuse U7's :func:`terminal_stage._chunk_confirmed_remote`, the
    SAME seam the terminal stage and eviction use (probes ``request_signed_urls``
    over the chunk's core files; any error -> False).
    """
    if remote_exists is not None:
        def _confirm_injected(idx: int) -> bool:
            try:
                return bool(remote_exists(idx))
            except Exception:  # noqa: BLE001 — GCS unreachable -> conservative False
                return False
        return _confirm_injected

    def _confirm_real(idx: int) -> bool:
        from screencap.terminal_stage import _chunk_confirmed_remote

        return _chunk_confirmed_remote(recording_dir, idx, remote_exists=None)

    return _confirm_real
