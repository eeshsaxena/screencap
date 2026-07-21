"""Sidecar scrub worker for retroactive privacy disable.

NOTE: despite the name, this is a *capture-time row-deletion sidecar* — it
deletes rows from the live ``recording.db`` when the user disables an app via
the menubar. It is NOT the post-hoc text scrubber in ``screencap.redaction``;
the class/module rename is deferred (SCR-33 U3).

When the user toggles Disable on an app or website in the menubar
mid-recording, this worker deletes every row tied to that target from
the live ``recording.db``: ``window_event``, ``action_event`` (recursive,
because ``parent_id`` has no cascade), ``screenshot``, and
``window_geometry``. It also unlinks any on-disk screenshot files
referenced by the deleted rows.

The engine writer processes batch DB commits (``crud.BATCH_SIZE``).
Without an explicit flush, a fresh ``sqlite3.connect()`` from this
thread sees only the LAST committed batch — for short or low-volume
recordings the rows we want to scrub may still be sitting in writer
buffers. The worker therefore triggers ``flush_requested`` and waits
for ``flush_ack_counter`` to stabilize (mirroring
``ChunkProcessor._trigger_flush``) before opening the SELECT
connection. The flush mechanism is shared with chunk_processor via a
``threading.Lock`` to prevent the two consumers from racing on the
shared ``flush_ack_counter``.

Disable message shape (from menubar via ``disable_q``)::

    {
        "kind": "app" | "domain",
        "bundle_id": str,
        "app_name": str | None,
        "root_domain": str | None,    # only when kind == "domain"
        "ts_unix": float,
        "source": "menubar",
    }

A ``None`` value on the queue is a poison pill for graceful shutdown.
"""

from __future__ import annotations

import logging
import math
import queue as _queue_mod
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from screencap._flush import wait_for_writer_flush
from screencap.enforcement.disable_log import DisableLogWriter
from screencap.privacy.classify import domain_from_url
from screencap.privacy.domain_loader import extract_root_domain

logger = logging.getLogger(__name__)

# SQLite has a default parameter limit of 32766 (SQLITE_MAX_VARIABLE_NUMBER).
# Stay well under that to keep the IN clauses safe even if the recording
# accumulates a lot of window events for a single target.
_IN_CLAUSE_BATCH = 5000

# How long the idle worker blocks on the queue between stop-event polls.
_QUEUE_POLL_TIMEOUT = 5.0

# Safety prelude prepended to every target interval to catch screenshots
# captured during the URL-detection lag window — Chrome's AX URL is
# typically 0.5-2 seconds behind the actual page navigation, so a
# screenshot taken just after the user clicked into the target site can
# show the target visually even though the most-recent window event is
# still pointing at the previous page. The prelude is clamped to the
# most recent same-bundle window event so we never extend further back
# than the previous page in the same browser process.
_INTERVAL_PRELUDE_SECONDS = 2.0

_EMPTY_COUNTS: dict[str, int] = {
    "window_events": 0,
    "action_events": 0,
    "screenshots": 0,
    "window_geometries": 0,
    "screenshot_files": 0,
    "matched_timestamps": 0,
    "orphan_action_events": 0,
    "ondevice_window_names": 0,
    "task_segments": 0,
    "tasks_json_entries": 0,
    "purged_intervals": 0,
}

# SCR-277: the local-only table this worker persists its retroactive-disable
# purge spans to, read back by ``backfill.skip_intervals.derive_skip_intervals``
# to block the disabled app's stale flat-file events/transcripts before a model.
# The name is a schema contract shared by-literal with that reader (enforcement
# must not import backfill — the privacy-package DAG guard pins the boundary).
_PURGED_INTERVAL_TABLE = "purged_interval"

# U8 (range delete): the additive ``origin`` column splitting a purged span into
# ``'policy'`` (this worker's retroactive-disable purge — "removed by your rules")
# vs ``'user'`` (an explicit user range delete — "removed by you"). Absent/NULL
# classifies as ``'policy'`` in BOTH day_segments and skip_intervals (every
# pre-migration purge was policy-driven). Kept a by-literal contract with those
# readers, same as ``_PURGED_INTERVAL_TABLE`` (enforcement must not import
# backfill / day_segments — the package DAG guard pins the boundary).
PURGE_ORIGIN_POLICY = "policy"
PURGE_ORIGIN_USER = "user"


def ensure_purged_interval_schema(cur: sqlite3.Cursor) -> None:
    """Create ``purged_interval`` (with the U8 ``origin`` column) + migrate an
    existing pre-U8 table by ALTER-adding ``origin`` — on the caller's cursor.

    Idempotent. A fresh table gets the full shape; an existing pre-U8 table
    (created before ``origin`` existed) is ALTER-migrated so a NULL ``origin``
    on legacy rows classifies as ``'policy'`` by the readers. Runs on the
    CALLER's connection/cursor so it can live inside an existing transaction.
    """
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {_PURGED_INTERVAL_TABLE} ("
        "  id INTEGER PRIMARY KEY,"
        "  start_ts REAL NOT NULL,"
        "  end_ts REAL,"
        "  disabled_at REAL,"
        "  origin TEXT"
        ")"
    )
    existing = {row[1] for row in cur.execute(f"PRAGMA table_info({_PURGED_INTERVAL_TABLE})")}
    if "origin" not in existing:
        try:
            cur.execute(f"ALTER TABLE {_PURGED_INTERVAL_TABLE} ADD COLUMN origin TEXT")
        except sqlite3.OperationalError as e:
            # Tolerate a concurrent opener's duplicate-column race (same TOCTOU
            # window engine.db._migrate_schema handles); any other error
            # propagates.
            if "duplicate column name" not in str(e).lower():
                raise


def _purge_ondevice_window_names(
    cur: sqlite3.Cursor,
    conn: sqlite3.Connection,
    intervals: list[tuple[float, float]],
) -> int:
    """Purge cached on-device window names overlapping ``intervals`` and bump
    the scrub generation (SCR-275, KTD-10a) — INSIDE the caller's transaction.

    A cached name is a derived artifact of window titles this scrub deletes —
    the same R7 lifecycle rule that gave the content index its purge hook. This
    runs on the scrub's OWN connection/cursor inside its existing
    ``BEGIN IMMEDIATE`` transaction (same DB), so the source-row deletes, the
    cache purge, and the generation bump commit as one atom.

    Boundaries are second-widened (floor start / ceil end — the content-index
    purge's ms floor/ceil precedent) so a name whose window span was rounded
    can't survive at a sub-second interval boundary; an ``inf`` end means
    open-ended. Both tables are existence-guarded (a pre-SCR-275
    ``recording.db`` has neither — and then no cache/pass to invalidate) and
    any failure is swallowed per the worker's fail-open discipline.
    """
    try:
        # Lazy import (like bump_scrub_generation below) so the enforcement
        # package's import surface stays light.
        from screencap.recording_db import has_table

        deleted = 0
        if has_table(conn, "ondevice_window_names"):
            before = conn.total_changes
            for start, end in intervals:
                lo = math.floor(start)
                if end == float("inf"):
                    cur.execute(
                        "DELETE FROM ondevice_window_names WHERE window_end > ?",
                        (lo,),
                    )
                else:
                    cur.execute(
                        "DELETE FROM ondevice_window_names "
                        "WHERE window_end > ? AND window_start < ?",
                        (lo, math.ceil(end)),
                    )
            deleted = conn.total_changes - before
        if has_table(conn, "scrub_generation"):
            # Signal a concurrently-running naming pass that its pre-scrub
            # snapshot is stale (KTD-10b) — bumped whenever source rows were
            # deleted, even if no cache row overlapped. Lazy import: light
            # module, and only when the SCR-275 schema is actually present.
            from screencap.pipeline_state import bump_scrub_generation

            bump_scrub_generation(conn)
        return deleted
    except Exception:
        logger.warning(
            "on-device name-cache purge failed (non-fatal)", exc_info=True
        )
        return 0


def _purge_task_segments(
    cur: sqlite3.Cursor,
    conn: sqlite3.Connection,
    intervals: list[tuple[float, float]],
    *,
    all_sources: bool = False,
) -> int:
    """Purge ``pipeline_task_segments`` rows overlapping ``intervals`` (SCR-280)
    — INSIDE the caller's transaction.

    A task ``name`` derives from the window titles this scrub deletes, so a task
    whose ``[start_ts, end_ts]`` span overlaps a scrubbed interval is a derived
    artifact under the same R7 lifecycle rule as the on-device name cache and the
    content index. By default deletes ONLY agent-owned rows (``source='agent' AND
    edited=0`` — the LOW range) so ``source='user'`` and user-edited rows are
    PRESERVED, mirroring ``PipelineLedger.replace_task_segments``'s protection
    predicate; the next segmentation pass regenerates names from the post-scrub
    rows. This is the right rule for a *policy* purge (a privacy rule doesn't own
    the user's own labels). ``all_sources=True`` drops that protection — used by
    the *user* range-delete path (U8), where explicit destroy intent over a range
    means a label derived from or describing that removed moment must go too, or
    it stays queryable via ``tasks.query``/``browse_day`` over a span the strip
    reports as "removed by you".

    U5 (KTD-3, AE5) -- on the POLICY path this ALSO strips agent-written topic
    bullets in place from the user-protected rows the whole-row DELETE kept, via
    :func:`_clear_protected_agent_bullets` (a user-renamed block keeps its name but
    loses the disabled app's bullets). The returned count covers both the deletes
    and those in-place bullet-clears, so the scrub can tell whether any block row
    changed (and the day narrative therefore went stale).

    Runs on the scrub's OWN connection/cursor inside its existing
    ``BEGIN IMMEDIATE`` transaction (same ``recording.db``), so the source-row
    deletes and this purge commit as one atom. Bounds are second-widened (floor
    start / ceil end; ``inf`` end → open-ended), matching the on-device cache
    purge, so a task whose span was rounded can't survive at a sub-second
    boundary. Existence-guarded (a pre-U4 ``recording.db`` has no table) and any
    failure is swallowed per the worker's fail-open discipline.
    """
    try:
        from screencap.recording_db import has_table

        if not has_table(conn, "pipeline_task_segments"):
            return 0
        owned = "" if all_sources else "source='agent' AND edited=0 AND "
        before = conn.total_changes
        for start, end in intervals:
            lo = math.floor(start)
            if end == float("inf"):
                cur.execute(
                    f"DELETE FROM pipeline_task_segments WHERE {owned}end_ts > ?",
                    (lo,),
                )
            else:
                cur.execute(
                    f"DELETE FROM pipeline_task_segments "
                    f"WHERE {owned}end_ts > ? AND start_ts < ?",
                    (lo, math.ceil(end)),
                )
        # U5 (KTD-3, AE5): on the POLICY path, also strip AGENT-written bullets
        # from user-protected rows the whole-row DELETE kept (e.g. a user-renamed
        # block), so the disabled app's bullet text can't live on under a kept
        # name. The range-delete path (all_sources) already dropped every
        # overlapping row, so there is nothing left to bullet-clear.
        if not all_sources:
            _clear_protected_agent_bullets(cur, conn, intervals)
        return conn.total_changes - before
    except Exception:
        logger.warning(
            "task-segment purge failed (non-fatal)", exc_info=True
        )
        return 0


def _clear_protected_agent_bullets(
    cur: sqlite3.Cursor,
    conn: sqlite3.Connection,
    intervals: list[tuple[float, float]],
) -> None:
    """Strip AGENT-written bullets from user-protected rows the whole-row DELETE
    kept (U5, KTD-3, AE5) -- INSIDE the caller's transaction.

    ``_purge_task_segments`` whole-row-deletes only UNEDITED agent rows, so every
    protected row (a ``source='user'`` block or a user-curated agent block) survives
    with its ``metadata`` bullets intact. For a user-RENAMED block that means the
    disabled app's topic bullets live on under the user's kept name -- defeating AE5.

    For each surviving row overlapping ``intervals`` whose ``bullets`` FIELD is not
    user-protected (``task_field_is_protected(row, TASK_FIELD_BULLETS)`` is False -- a
    rename-only edit leaves the agent-written bullets purge-eligible), strip the
    bullets from its ``metadata`` in place, leaving the name / ``edited`` /
    ``edited_fields`` untouched. A user-EDITED bullet set (``EDITED_FIELD_BULLETS``)
    and a ``source='user'`` row are protected on the ``bullets`` axis, so they are
    never touched. Idempotent: a row with no bullets left is not rewritten.

    Same second-widened overlap bounds as the DELETE; runs on the scrub's own cursor
    in the same ``BEGIN IMMEDIATE`` transaction, so the clears commit atomically with
    the row deletes.
    """
    from screencap.pipeline_state import (
        TASK_FIELD_BULLETS,
        TASK_SOURCE_AGENT,
        TaskSegmentRow,
        _strip_metadata_bullets,
        task_field_is_protected,
    )

    seen: set[int] = set()
    for start, end in intervals:
        lo = math.floor(start)
        if end == float("inf"):
            rows = cur.execute(
                "SELECT task_index, metadata, source, edited, edited_fields "
                "FROM pipeline_task_segments WHERE end_ts > ?",
                (lo,),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT task_index, metadata, source, edited, edited_fields "
                "FROM pipeline_task_segments "
                "WHERE end_ts > ? AND start_ts < ?",
                (lo, math.ceil(end)),
            ).fetchall()
        for task_index, metadata, source, edited, edited_fields in rows:
            idx = int(task_index)
            if idx in seen:
                continue
            probe = TaskSegmentRow(
                task_index=idx, start_ts=0.0, end_ts=0.0, name="",
                metadata=metadata,
                source=source if source is not None else TASK_SOURCE_AGENT,
                edited=bool(edited),
                edited_fields=int(edited_fields or 0),
            )
            if task_field_is_protected(probe, TASK_FIELD_BULLETS):
                continue  # user owns the bullets -> never touched
            new_metadata = _strip_metadata_bullets(metadata)
            if new_metadata == metadata:
                continue  # no agent bullets to clear -> idempotent no-op
            cur.execute(
                "UPDATE pipeline_task_segments SET metadata=?, updated_at=? "
                "WHERE task_index=?",
                (new_metadata, time.time(), idx),
            )
            seen.add(idx)


def _chunks(seq: list[Any], size: int) -> list[list[Any]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _both_still_forms(rel: str) -> tuple[str, ...]:
    """Both on-disk forms of a recorded still path (search U7 compat).

    A DB ``image_path`` may be ``screenshots/<ts>.jpg`` while the migrated file on
    disk is ``screenshots/<ts>.jpg.enc`` (or vice versa), so a purge must try both.
    """
    if rel.endswith(".jpg.enc"):
        return (rel, rel[: -len(".enc")])
    if rel.endswith(".jpg"):
        return (rel, rel + ".enc")
    return (rel,)


def _delete_in_batches(
    cur: sqlite3.Cursor, table: str, column: str, values: list[Any],
) -> int:
    """Run ``DELETE FROM <table> WHERE <column> IN (...)`` in safe batches."""
    deleted = 0
    for batch in _chunks(values, _IN_CLAUSE_BATCH):
        placeholders = ",".join("?" * len(batch))
        cur.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
            batch,
        )
        deleted += cur.rowcount or 0
    return deleted


class ScrubWorker:
    """Sidecar thread that processes disable jobs from the menubar.

    Owns one ``DisableLogWriter`` and serializes all DB writes through a
    single ``threading.Lock``. The thread lifecycle mirrors
    ``ChunkProcessor``: started by the recorder after ``Recorder.__enter__``,
    poison-pilled and joined during shutdown.
    """

    def __init__(
        self,
        disable_q: Any,
        recording_db_path: str | Path,
        capture_dir: str | Path,
        stop_event: threading.Event | None = None,
        flush_requested: Any = None,
        flush_ack_counter: Any = None,
        flush_lock: threading.Lock | None = None,
    ) -> None:
        self._q = disable_q
        self._db_path = Path(recording_db_path)
        self._capture_dir = Path(capture_dir)
        self._stop_event = stop_event or threading.Event()
        self._lock = threading.Lock()
        self._log = DisableLogWriter(self._capture_dir)
        self._thread: threading.Thread | None = None
        # Engine flush primitives — shared with the engine writer
        # processes. flush_lock serializes _trigger_flush calls between
        # this worker and chunk_processor so they don't race on the
        # shared flush_ack_counter.
        self._flush_requested = flush_requested
        self._flush_ack_counter = flush_ack_counter
        self._flush_lock = flush_lock

    @property
    def log_path(self) -> Path:
        return self._log.path

    def start(self) -> None:
        """Spawn the worker thread (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            daemon=False,
            name="scrub_worker",
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Send a poison pill and wait for the worker to drain."""
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Thread loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                msg = self._q.get(timeout=_QUEUE_POLL_TIMEOUT)
            except (_queue_mod.Empty, OSError):
                if self._stop_event.is_set():
                    return
                continue
            if msg is None:
                self._drain_remaining()
                return
            self._handle(msg)

    def _drain_remaining(self) -> None:
        while True:
            try:
                extra = self._q.get_nowait()
            except (_queue_mod.Empty, OSError):
                return
            if extra is None:
                continue
            self._handle(extra)

    # ------------------------------------------------------------------
    # Engine flush coordination
    # ------------------------------------------------------------------

    def _trigger_flush(self) -> None:
        """Force engine writer processes to commit buffered inserts.

        Shares the handshake with :meth:`ChunkProcessor._trigger_flush`
        via ``flush_lock`` so the two consumers don't race on the shared
        ``flush_ack_counter``. Released before the SQLite SELECT/DELETE
        so chunk_processor isn't blocked while we're scrubbing rows.
        """
        wait_for_writer_flush(
            flush_requested=self._flush_requested,
            flush_ack_counter=self._flush_ack_counter,
            flush_lock=self._flush_lock,
            stop_event=self._stop_event,
            logger=logger,
            caller="scrub_worker",
        )

    # ------------------------------------------------------------------
    # Job handling
    # ------------------------------------------------------------------

    def _handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._lock:
                # Force the engine writers to commit any buffered rows
                # for the target's tables BEFORE we read them. Without
                # this, BATCH_SIZE-buffered window/action events for
                # short recordings stay invisible to our SELECT.
                self._trigger_flush()
                result = self._scrub_target(msg)
            entry = self._build_entry(msg, status="completed", result=result)
            self._log.append(entry)
            logger.info(
                "scrub_worker: completed kind=%s bundle=%s domain=%s -> %s",
                msg.get("kind"), msg.get("bundle_id"),
                msg.get("root_domain"), result,
            )
            return entry
        except Exception as exc:
            logger.exception("scrub_worker: failed for %s", msg)
            entry = self._build_entry(
                msg, status="failed", result=None, error=str(exc),
            )
            try:
                self._log.append(entry)
            except Exception:
                pass
            return entry

    def _build_entry(
        self,
        msg: dict[str, Any],
        status: str,
        result: dict[str, int] | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ts_unix": float(msg.get("ts_unix") or time.time()),
            "target_kind": msg.get("kind"),
            "target": {
                "bundle_id": msg.get("bundle_id"),
                "app_name": msg.get("app_name"),
                "root_domain": msg.get("root_domain"),
            },
            "source": msg.get("source", "menubar"),
            "scrub_status": status,
            "scrub_result": result if result is not None else dict(_EMPTY_COUNTS),
            "error": error,
        }

    # ------------------------------------------------------------------
    # SQLite work
    # ------------------------------------------------------------------

    def _compute_target_set_and_intervals(
        self, conn: sqlite3.Connection, msg: dict[str, Any],
    ) -> tuple[set[float], list[tuple[float, float]]]:
        """Return ``(target_window_timestamps, intervals)``.

        ``target_window_timestamps`` is the set of ``window_event.timestamp``
        values that match the disable target — used to delete the
        action_event and window_event rows tied to them.

        ``intervals`` is a list of ``(start_ts, end_ts)`` tuples
        representing time ranges during which the target was the
        most-recently-active window (i.e. the active app at that
        moment). ``end_ts`` is ``float('inf')`` for the trailing
        interval if the recording ended while the target was active.
        Screenshots are sampled independently from window events, so
        scrubbing them by exact-timestamp match misses everything;
        we delete screenshots whose timestamp falls in any of these
        intervals instead.
        """
        cur = conn.cursor()
        kind = msg.get("kind")
        bundle_id = msg.get("bundle_id") or ""

        if kind == "app":
            app_name = msg.get("app_name") or ""

            def is_target(_bid: str | None, _name: str | None,
                          _url: str | None) -> bool:
                if _bid is not None and _bid == bundle_id:
                    return True
                if app_name and _name is not None and _name == app_name:
                    return True
                return False
        elif kind == "domain":
            target_domain = (msg.get("root_domain") or "").lower()
            if not target_domain or not bundle_id:
                return set(), []

            def is_target(_bid: str | None, _name: str | None,
                          _url: str | None) -> bool:
                if _bid != bundle_id or not _url:
                    return False
                host = domain_from_url(_url)
                if not host:
                    return False
                root = extract_root_domain(host) or host.lower()
                return root == target_domain
        else:
            return set(), []

        # Walk EVERY window event in temporal order so we can compute
        # the active intervals — we need non-target rows to know when
        # the target stopped being active.
        cur.execute(
            "SELECT timestamp, app_bundle_id, app_name, browser_url "
            "FROM window_event ORDER BY timestamp",
        )
        rows = cur.fetchall()

        target_ts: set[float] = set()
        intervals: list[tuple[float, float]] = []
        interval_start: float | None = None
        # Track the most recent same-bundle, non-target window event so
        # we can clamp the prelude — never extend back past the previous
        # page that was loaded in the same browser process.
        last_same_bundle_ts: float | None = None
        for ts_val, bid, name, url in rows:
            if ts_val is None:
                continue
            if is_target(bid, name, url):
                target_ts.add(ts_val)
                if interval_start is None:
                    # Apply the URL-detection-lag prelude. Default reach
                    # is `ts_val - _INTERVAL_PRELUDE_SECONDS`, but we
                    # never extend earlier than the most recent same-
                    # bundle predecessor (which would clobber a known
                    # different page in the same browser).
                    prelude_target = ts_val - _INTERVAL_PRELUDE_SECONDS
                    if last_same_bundle_ts is not None:
                        interval_start = max(prelude_target, last_same_bundle_ts)
                    else:
                        interval_start = prelude_target
            else:
                if interval_start is not None:
                    intervals.append((interval_start, ts_val))
                    interval_start = None
                if bid == bundle_id:
                    last_same_bundle_ts = ts_val
        if interval_start is not None:
            # Target was the LAST window in the recording — interval
            # extends to "now". Use +inf so the screenshot SELECT picks
            # up everything from the target's first event onward.
            intervals.append((interval_start, float("inf")))

        return target_ts, intervals

    def _select_screenshots_in_intervals(
        self, conn: sqlite3.Connection,
        intervals: list[tuple[float, float]],
    ) -> tuple[list[int], list[float], list[str]]:
        """Find screenshot rows whose timestamp falls in any interval.

        Returns ``(ids, timestamps, image_paths)``. ``image_paths`` is
        the subset of rows where ``image_path`` is non-null — used for
        on-disk file unlinking after the transaction commits.
        """
        ids: list[int] = []
        timestamps: list[float] = []
        image_paths: list[str] = []
        cur = conn.cursor()
        for start, end in intervals:
            if end == float("inf"):
                cur.execute(
                    "SELECT id, timestamp, image_path FROM screenshot "
                    "WHERE timestamp >= ?",
                    (start,),
                )
            else:
                cur.execute(
                    "SELECT id, timestamp, image_path FROM screenshot "
                    "WHERE timestamp >= ? AND timestamp < ?",
                    (start, end),
                )
            for row in cur.fetchall():
                ids.append(row[0])
                if row[1] is not None:
                    timestamps.append(row[1])
                if row[2]:
                    image_paths.append(row[2])
        return ids, timestamps, image_paths

    def _unlink_screenshot_files(self, image_paths: list[str]) -> int:
        """Delete on-disk screenshot files for paths gathered pre-delete.

        Tries BOTH the plaintext ``.jpg`` and the encrypted ``.jpg.enc`` form of each
        recorded ``image_path`` (search U7 compat): a pre-flip DB row still records
        ``.jpg`` after the migration converted the file to ``.jpg.enc``, so a
        single-extension unlink would leave the just-disabled frame's pixels on disk.
        Returns the number of files actually unlinked.
        """
        deleted = 0
        cap_resolved = self._capture_dir.resolve()
        for rel in image_paths:
            unlinked_any = False
            for candidate in _both_still_forms(rel):
                try:
                    p = (self._capture_dir / candidate).resolve()
                    # Defensive: never escape capture_dir.
                    if cap_resolved not in p.parents and p != cap_resolved:
                        continue
                    p.unlink(missing_ok=True)
                    unlinked_any = True
                except OSError:
                    continue
            if unlinked_any:
                deleted += 1
        return deleted

    def _delete_orphan_action_events(
        self, cur: sqlite3.Cursor, conn: sqlite3.Connection,
    ) -> int:
        """Delete action_events whose ``window_event_timestamp`` no longer
        matches any surviving window_event row.

        After a previous scrub deletes a target window_event, the engine
        keeps inserting new action_events with the deleted timestamp as
        their ``window_event_timestamp`` (the engine's in-memory
        ``prev_window_event`` is independent of the DB and never gets
        invalidated by the scrub). Those rows become orphans and survive
        the next interval-based scrub because the target window_event no
        longer exists to be matched.
        """
        before = conn.total_changes
        cur.execute(
            "DELETE FROM action_event "
            "WHERE window_event_timestamp IS NOT NULL "
            "AND window_event_timestamp NOT IN ("
            "  SELECT timestamp FROM window_event"
            ")"
        )
        return conn.total_changes - before

    def _persist_purged_intervals(
        self,
        cur: sqlite3.Cursor,
        intervals: list[tuple[float, float]],
        disabled_at: float | None,
    ) -> int:
        """Persist the purge ``intervals`` into ``purged_interval`` (SCR-277).

        Called INSIDE the caller's ``BEGIN IMMEDIATE`` transaction, alongside the
        row deletes, so the intervals are crash-consistent with them: a crash can
        never leave the disabled app's rows deleted but its blocking span
        unrecorded — which would reopen the hole where the purged span's stale
        flat-file events/transcripts (the purge does NOT rewrite them) reach the
        segmentation model, because the deletes leave the R11 re-derivation blind
        to a span a surviving benign window encloses.

        ``derive_skip_intervals`` (``backfill/skip_intervals.py``) reads this table
        and unions the spans as absolute ``EXCLUDE`` blocks. The trailing
        open-ended interval (``float('inf')`` end — target active at recording end)
        is stored as ``end_ts = NULL``; the reader maps NULL → +inf.

        The table is local-only: ``recording.db`` is never uploaded (R8), so the
        spans never leave the machine. Created on demand (``IF NOT EXISTS``) so a
        recording that never had a disable carries no table.
        """
        if not intervals:
            return 0
        # Ensure the table + the U8 ``origin`` column, then stamp these spans
        # ``'policy'`` (this is the retroactive-disable purge — "removed by your
        # rules", distinct from a user range delete's ``'user'`` origin).
        ensure_purged_interval_schema(cur)
        written = 0
        for start_ts, end_ts in intervals:
            cur.execute(
                f"INSERT INTO {_PURGED_INTERVAL_TABLE} "
                "(start_ts, end_ts, disabled_at, origin) VALUES (?, ?, ?, ?)",
                (
                    float(start_ts),
                    None if end_ts == float("inf") else float(end_ts),
                    float(disabled_at) if disabled_at is not None else None,
                    PURGE_ORIGIN_POLICY,
                ),
            )
            written += 1
        return written

    def _scrub_target(self, msg: dict[str, Any]) -> dict[str, int]:
        """Run the cascade delete for one disable target.

        Returns a dict of row counts. Empty targets still run the
        orphan cleanup pass — that's the only way to catch
        action_events that the engine inserted with a now-deleted
        window_event_timestamp after a previous scrub.

        Screenshots and window_geometry are matched by TIME INTERVAL
        (when the target was the most-recently-active window), not by
        exact timestamp. The two streams have independent sample rates
        so exact-timestamp matching never finds anything.
        """
        if not self._db_path.exists():
            return dict(_EMPTY_COUNTS)

        # NOTE: this site deliberately bypasses screencap.recording_db.open_recording_db.
        # scrub_worker is the privacy enforcement worker that runs concurrently with the
        # engine's live recorder writes. It needs:
        #   - busy_timeout=10000 (vs the helper default of 5000) to absorb engine commits;
        #   - the wait_for_writer_flush lifecycle hooks at scrub_worker.py:45;
        # neither of which the standard helper exposes.
        #
        # If a future engine schema adds a column scrub_worker should also scrub, update
        # the call sites below — these are the evolving columns/tables this worker reads:
        #   - window_event.browser_url (scrub_worker.py:317-320)
        #   - screenshot.image_path (scrub_worker.py:377/383/385)
        #   - window_geometry table DELETE (scrub_worker.py:525)
        # When that list grows, prefer extending open_recording_db with a busy_timeout
        # parameter and migrating this site over keeping a parallel implementation.
        conn = sqlite3.connect(str(self._db_path))
        # Match the engine writer's busy_timeout so a long-running engine
        # commit doesn't immediately fail this transaction.
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            target_ts, intervals = self._compute_target_set_and_intervals(
                conn, msg,
            )
            matched = len(target_ts)
            if matched == 0:
                # No surviving target window events, but we may still
                # have orphaned action_events from a previous scrub.
                # Run only the orphan cleanup and return.
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                try:
                    orphan_count = self._delete_orphan_action_events(cur, conn)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                try:
                    conn.execute("PRAGMA wal_checkpoint(RESTART)")
                except sqlite3.OperationalError:
                    pass
                empty = dict(_EMPTY_COUNTS)
                empty["orphan_action_events"] = orphan_count
                return empty

            ts_list = list(target_ts)
            counts = dict(_EMPTY_COUNTS)
            counts["matched_timestamps"] = matched

            # Collect screenshots in the target's active intervals
            # BEFORE the DELETE so we know which rows + on-disk files
            # to remove. Image paths are unlinked AFTER the commit.
            sshot_ids, sshot_timestamps, image_paths = (
                self._select_screenshots_in_intervals(conn, intervals)
            )

            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                # Recursive ActionEvent delete: walk parent → children
                # via the self-referential parent_id FK so the dataset
                # is fully removed even when only one end of the chain
                # carries window_event_timestamp. cursor.rowcount is -1
                # for compound CTE-DELETE statements, so we measure via
                # conn.total_changes deltas instead.
                action_deleted = 0
                for batch in _chunks(ts_list, _IN_CLAUSE_BATCH):
                    placeholders = ",".join("?" * len(batch))
                    before = conn.total_changes
                    cur.execute(
                        "WITH RECURSIVE descendants(id) AS ("
                        f"  SELECT id FROM action_event "
                        f"  WHERE window_event_timestamp IN ({placeholders}) "
                        "  UNION ALL "
                        "  SELECT a.id FROM action_event a "
                        "    JOIN descendants d ON a.parent_id = d.id"
                        ") "
                        "DELETE FROM action_event "
                        "WHERE id IN (SELECT id FROM descendants)",
                        batch,
                    )
                    action_deleted += conn.total_changes - before
                counts["action_events"] = action_deleted

                # WindowGeometry rows are tied to screenshot timestamps,
                # so delete by the screenshot timestamps we just collected.
                counts["window_geometries"] = _delete_in_batches(
                    cur, "window_geometry", "screenshot_timestamp",
                    sshot_timestamps,
                )

                # Screenshot rows: delete by id (collected pre-transaction).
                counts["screenshots"] = _delete_in_batches(
                    cur, "screenshot", "id", sshot_ids,
                )

                # Window event rows: still by exact timestamp.
                counts["window_events"] = _delete_in_batches(
                    cur, "window_event", "timestamp", ts_list,
                )

                # Orphan cleanup: any action_events whose
                # window_event_timestamp no longer matches a row in
                # window_event. After we delete the target window
                # events above, ALL action_events that referenced them
                # become orphans — including ones the engine inserted
                # AFTER a previous scrub of the same target.
                counts["orphan_action_events"] = (
                    self._delete_orphan_action_events(cur, conn)
                )

                # SCR-275 (KTD-10a): purge cached on-device window names whose
                # spans overlap the scrubbed intervals + bump the staleness
                # generation, in this SAME transaction (same recording.db).
                counts["ondevice_window_names"] = _purge_ondevice_window_names(
                    cur, conn, intervals,
                )

                # SCR-280: purge AGENT task-segment rows (in tasks.json's
                # structured sibling) whose spans overlap the scrubbed intervals,
                # in this SAME transaction — a name derived from a now-deleted
                # window title must not survive in the Journal. User / edited rows
                # are preserved (the ledger's protection predicate).
                counts["task_segments"] = _purge_task_segments(
                    cur, conn, intervals,
                )

                # SCR-277: persist the purge spans in this SAME transaction as the
                # row deletes, so the disabled app's stale flat-file events /
                # transcripts (which the purge does NOT rewrite) can still be
                # blocked before the segmentation model — the deletes leave the
                # re-derivation blind to a span a benign window encloses. Written
                # atomically with the deletes so a crash can't leave rows gone but
                # the interval unrecorded (which would reopen the hole).
                counts["purged_intervals"] = self._persist_purged_intervals(
                    cur, intervals, msg.get("ts_unix"),
                )

                conn.commit()
            except Exception:
                conn.rollback()
                raise

            # Force the WAL into the main DB file so the deletes can't
            # come back from the WAL on next open. RESTART blocks until
            # all readers complete; we already hold the worker lock so
            # the only competing reader/writer is the engine — its 50ms
            # batch loop will tolerate this.
            try:
                conn.execute("PRAGMA wal_checkpoint(RESTART)")
            except sqlite3.OperationalError:
                # Non-fatal: data is still committed; the WAL will be
                # checkpointed by the next normal write or DB close.
                pass

            # Now unlink the on-disk screenshot files for the deleted
            # rows. Done OUTSIDE the transaction so a slow filesystem
            # doesn't extend our write lock window.
            counts["screenshot_files"] = self._unlink_screenshot_files(image_paths)

            # SCR-118: purge the same intervals from the content index so
            # retroactively-disabled on-screen text can no longer be found via
            # search (the R7 lifecycle hole reached through the async path). U5
            # (KTD-8) extends this shared helper to also drop the diary block/bullet
            # search rows for the interval.
            self._purge_content_index_intervals(intervals)

            # U5 (R13, AE5): if the purge changed any block row (a whole-row delete
            # OR an in-place bullet clear), invalidate the day narrative — it is
            # composed from block names + bullets, so the stored prose may still
            # describe the disabled app. Cleared here so the next consolidation tick
            # recomposes it from the SURVIVING blocks (or leaves none on a degraded
            # day). Post-commit + fail-open via the U4 seam (its own connection).
            if counts["task_segments"] > 0:
                self._invalidate_day_narrative()

            # SCR-280: mirror the ledger task-segment purge in the human-readable
            # tasks.json file (post-commit, fail-open — it is a file, not part of
            # the DB transaction). The ledger row purge above is authoritative for
            # the Journal; this keeps the co-located JSON artifact consistent.
            counts["tasks_json_entries"] = self._purge_tasks_json_intervals(
                intervals,
            )

            # U10 (R17): propagate this POLICY purge to the clips store — a durable,
            # retention-exempt clip that overlaps a retroactively-purged span must
            # not keep those pixels playable (full overlap → delete; partial →
            # flag). Only the POLICY purge cascades; a user range-delete
            # (``origin='user'``, driven by ``range_delete``) never calls here (R11).
            # Post-commit, strictly fail-open — a clips-store hiccup must never break
            # the scrub worker.
            self._purge_clips_intervals(intervals)

            return counts
        finally:
            conn.close()

    def _purge_content_index_intervals(
        self, intervals: list[tuple[float, float]],
    ) -> None:
        """Purge content-index rows for this recording's ``intervals`` (fail-open).

        Thin instance wrapper over the shared module-level
        :func:`purge_content_index_intervals` so the retroactive-disable path and
        the U8 range-delete path can't drift on the R7 lifecycle rule.
        """
        purge_content_index_intervals(self._capture_dir, intervals)

    def _invalidate_day_narrative(self) -> None:
        """Clear this recording's day-narrative row after the purge touched its
        blocks (U5, R13) -- strictly fail-open.

        The day narrative (U4) is derived prose composed from block names + bullets,
        so once a block is deleted or its bullets are cleared the stored narrative
        may still describe the disabled app. This clears it through the U4 seam
        (``PipelineLedger.clear_day_narrative`` -- built as the U5 invalidation hook)
        so the next consolidation tick recomposes it from the surviving blocks.
        Post-commit with its OWN connection (the scrub transaction has already
        committed), so it can't be part of the row-delete atom; any error is
        swallowed per the worker's fail-open discipline (the block-set fingerprint
        would still force a regenerate on the next tick).
        """
        try:
            from screencap.pipeline_state import PipelineLedger

            PipelineLedger(self._db_path).clear_day_narrative()
        except Exception:
            logger.warning(
                "day-narrative invalidation failed (non-fatal)", exc_info=True
            )

    def _purge_tasks_json_intervals(
        self, intervals: list[tuple[float, float]],
    ) -> int:
        """Drop AGENT ``tasks.json`` entries overlapping this recording's ``intervals``.

        Thin instance wrapper over the shared module-level
        :func:`purge_tasks_json_intervals`.
        """
        return purge_tasks_json_intervals(self._capture_dir, intervals)

    def _purge_clips_intervals(
        self, intervals: list[tuple[float, float]],
    ) -> None:
        """Propagate this POLICY purge to overlapping clips (R17) — fail-open.

        Deletes (full overlap) or flags (partial overlap) any durable clip whose
        span intersects a retroactively-purged interval, so purged pixels can't
        survive in a retention-exempt artifact. Lazily imports ``screencap.clips``
        (like the content-index purge lazily imports ``content_index``) — the
        package-boundary guard permits ``enforcement`` → a top-level ``screencap``
        module. A user range-delete never routes here (R11). Never raises.
        """
        try:
            from screencap.clips import purge_clips_for_intervals

            purge_clips_for_intervals(
                self._capture_dir.name,
                intervals,
                recordings_dir=self._capture_dir.parent,
            )
        except Exception:
            logger.warning(
                "clips purge propagation failed (non-fatal)", exc_info=True
            )


def purge_content_index_intervals(
    capture_dir: Path, intervals: list[tuple[float, float]],
) -> None:
    """Purge content-index rows for ``capture_dir``'s ``intervals`` (fail-open).

    ``intervals`` are float unix SECONDS with a possible ``float('inf')``
    trailing upper bound; the content index keys on ms, so convert (and map
    ``inf`` → open-ended). Never opens/creates the store if indexing was
    never used, and never raises into the caller (the disable job OR the U8
    range-delete job). The SHARED body behind both purge paths so the R7
    lifecycle rule can't drift between them.

    U5 (KTD-8, AE5): the ``diary_fts`` day-diary block search rows live in the
    SAME ``content_index.db``, so this purges them for the same interval, under the
    same ``content_index_write_lock()`` and with the same ms-widened bounds, right
    beside the frame-level content purge — a disabled app's block name / bullet
    text stops being searchable too. Both purge paths (disable + range-delete) route
    through here, so the diary sink is covered by whichever fired.
    """
    if not intervals:
        return
    try:
        from screencap.content_index import default_index_path

        path = default_index_path()
        if not path.exists():
            return  # never indexed → nothing to purge, don't create the store

        from screencap.content_index import (
            ContentIndex,
            content_index_write_lock,
        )

        recording = capture_dir.name
        # SCR-134: take the shared content-index write lock around the purge
        # so it can't interleave with a concurrent inline index write that
        # already OCR'd these (now-disabled) frames — which would otherwise
        # resurrect just-purged text. The screenshot files were already
        # unlinked before this lock, so any inline write that runs AFTER this
        # purge re-reads an empty disk and indexes nothing.
        with content_index_write_lock(), ContentIndex(path) as store:
            if not store.available:
                return
            for start, end in intervals:
                # Widen the purge window (floor start, ceil end) so a frame
                # whose ms timestamp was ROUNDED at write time can't survive
                # at a sub-ms interval boundary. Writes use round(ts*1000);
                # truncating the end here would miss a rounded-up boundary
                # frame and leave disabled-app text queryable.
                start_ms = math.floor(start * 1000)
                end_ms = None if end == float("inf") else math.ceil(end * 1000)
                store.delete_recording_interval(recording, start_ms, end_ms)
                # U5 (KTD-8): drop the diary block/bullet search rows overlapping
                # the same interval (name + bullets keyed by block span), so the
                # disabled app's diary text also stops being searchable (AE5). A
                # store with no diary tables yet is a safe no-op.
                store.delete_recording_diary_interval(recording, start_ms, end_ms)
    except Exception:
        logger.warning(
            "content-index interval purge failed (non-fatal)", exc_info=True
        )


def purge_tasks_json_intervals(
    capture_dir: Path, intervals: list[tuple[float, float]],
    *,
    all_sources: bool = False,
) -> int:
    """Drop AGENT ``tasks.json`` entries overlapping ``intervals`` (SCR-280).

    The ``tasks.json`` mirror of the ledger task-segment purge: a rewrite that
    drops agent-derived entries whose ``[start_ts, end_ts]`` overlaps a
    scrubbed interval while carrying forward ``source='user'`` / edited
    entries — the same protection ``terminal_stage._persist_local_tasks``
    applies when it re-writes the file. ``tasks.json`` is local-only (excluded
    from ``upload.list_recording_files``), so this is at-rest remanence, not a
    cloud exposure.

    Intervals are second-widened (floor start / ceil end; ``inf`` →
    open-ended) to match the ledger + content-index purges. Best-effort: a
    missing / torn / legacy or non-dict file is left untouched (the ledger row
    purge is authoritative), the write is atomic (tmp + replace), and any
    failure is swallowed — it must never raise into the caller (the disable job
    OR the U8 range-delete job). Returns the number of entries removed.
    """
    if not intervals:
        return 0
    path = capture_dir / "tasks.json"
    if not path.exists():
        return 0
    try:
        import json

        # Same protection predicate the terminal-stage writer uses, imported
        # (not re-spelled) so the two can't drift on what "user-owned" means.
        from screencap.pipeline_state import TASK_SOURCE_USER

        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return 0
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            return 0

        # Pre-widen the intervals once (floor start / ceil end).
        widened = [
            (math.floor(s), None if e == float("inf") else math.ceil(e))
            for s, e in intervals
        ]

        def _overlaps_scrubbed(t: dict) -> bool:
            try:
                start = float(t.get("start_ts", 0.0))
                end = float(t.get("end_ts", 0.0))
            except (TypeError, ValueError):
                return False  # malformed span → don't treat as overlapping
            return any(
                end > lo and (hi is None or start < hi)
                for lo, hi in widened
            )

        # By default preserve user / edited entries (the ledger's predicate — a
        # policy purge doesn't own the user's own labels). ``all_sources`` drops
        # that protection for the U8 user range-delete path: explicit destroy
        # intent over a range removes every label describing that moment.
        def _protected(t: dict) -> bool:
            if all_sources:
                return False
            return t.get("source") == TASK_SOURCE_USER or bool(t.get("edited"))

        kept = [
            t for t in tasks
            if not isinstance(t, dict)
            or _protected(t)
            or not _overlaps_scrubbed(t)
        ]
        removed = len(tasks) - len(kept)
        if removed == 0:
            return 0
        data["tasks"] = kept
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2))
        tmp_path.replace(path)
        return removed
    except Exception:
        logger.warning(
            "tasks.json interval purge failed (non-fatal)", exc_info=True
        )
        return 0
