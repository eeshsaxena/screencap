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
from screencap.privacy.context import domain_from_url
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
}


def _chunks(seq: list[Any], size: int) -> list[list[Any]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


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

        Returns the number of files actually unlinked.
        """
        deleted = 0
        for rel in image_paths:
            try:
                p = (self._capture_dir / rel).resolve()
                # Defensive: never escape capture_dir.
                cap_resolved = self._capture_dir.resolve()
                if cap_resolved not in p.parents and p != cap_resolved:
                    continue
                p.unlink(missing_ok=True)
                deleted += 1
            except OSError:
                continue
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
            # search (the R7 lifecycle hole reached through the async path).
            self._purge_content_index_intervals(intervals)

            return counts
        finally:
            conn.close()

    def _purge_content_index_intervals(
        self, intervals: list[tuple[float, float]],
    ) -> None:
        """Purge content-index rows for ``intervals`` (fail-open).

        ``intervals`` are float unix SECONDS with a possible ``float('inf')``
        trailing upper bound; the content index keys on ms, so convert (and map
        ``inf`` → open-ended). Never opens/creates the store if indexing was
        never used, and never raises into the disable job.
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

            recording = self._capture_dir.name
            # SCR-134: take the shared content-index write lock around the purge
            # so it can't interleave with a concurrent inline index write that
            # already OCR'd these (now-disabled) frames — which would otherwise
            # resurrect just-purged text. The screenshot files were already
            # unlinked above (before this lock), so any inline write that runs
            # AFTER this purge re-reads an empty disk and indexes nothing.
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
        except Exception:
            logger.warning(
                "content-index interval purge failed (non-fatal)", exc_info=True
            )
