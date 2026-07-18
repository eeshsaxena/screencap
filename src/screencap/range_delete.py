"""Irreversible, chunk-rounded, LOCAL-ONLY range delete (U8, KTD-3).

Range delete removes a time range's footage from THIS Mac — playback,
screenshots, search, and every agent retrieval path — by composing the existing
purge / retention machinery rather than inventing a parallel delete path:

* **Chunk mapping** via :func:`retention.chunk_capture_bounds` (per-chunk
  CAPTURE-time bounds from the manifest). The requested ms range is rounded
  OUTWARD to whole chunk windows (R10 v1 granularity). The live in-flight chunk
  — no manifest yet, so an unreadable capture window — is EXCLUDED and reported;
  it is never deleted (fail-closed: a chunk we cannot place is never destroyed).

* **No TOCTOU** (Goal Capsule stop condition). :func:`resolve_range` (the
  ``dry_run`` preview) returns the resolved per-recording chunk set; the confirm
  call passes it back and :func:`execute_delete` RE-RESOLVES under the flock and
  deletes exactly the confirmed set, aborting a recording with a
  ``reconfirm_required`` outcome if its set changed (e.g. the excluded live chunk
  flushed between preview and confirm) — so the job never deletes more than the
  confirmed extent.

* **Crash-consistent transaction** (mirrors ``scrub_worker._scrub_target``): per
  recording, under the per-recording terminal flock, ONE ``BEGIN IMMEDIATE``
  deletes the in-range event rows, writes the ``origin='user'`` ``purged_interval``
  row, and transitions the covered ``pipeline_chunk_state`` rows to the terminal
  ``USER_DELETED`` state — so a crash can never leave rows deleted but the span /
  ledger state unrecorded. The unlink of on-disk artifacts happens AFTER the
  commit; a crash between commit and unlink is completed by
  :func:`reconcile_user_deletes` on the next daemon / job start (the ledger's
  ``USER_DELETED`` rows are the durable record of what to unlink).

* **Full artifact set.** Beyond ``retention._unlink_chunk`` (mp4/audio/events/
  manifest) the delete also unlinks the covered chunks' screenshots, per-chunk
  ``transcript_<idx>.txt`` / ``.json``, REGENERATES the bare whole-recording
  ``transcript.txt`` from surviving chunks (deletes it when none survive — the
  bare transcript spans every chunk and is grepped directly by
  ``/v0/transcript.search``), purges the content index + ``tasks.json`` intervals
  (the shared ``scrub_worker`` helpers), and purges the SAME range from the
  ``<name>-scrubbed`` sibling (a persistent reuse cache ``_iter_recording_dirs``
  walks — leaving it would let deleted speech survive ``transcript.search``).

* **recording.db is NEVER deleted** (tombstone rule): a fully-deleted recording
  leaves a tombstone dir preserving provenance (the ``purged_interval`` /
  ``USER_DELETED`` records). Everything here is LOCAL-ONLY (R20 v1): cloud
  propagation is deferred follow-up work.

The ``origin='user'`` spans read back through ``day_segments`` as "removed by
you" (R8) and through ``backfill.skip_intervals`` as their own fail-closed
category so ``frame.nearest`` / backfill degrade honestly (the footage is gone).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "RecordingPlan",
    "DeletePreview",
    "RecordingDeleteOutcome",
    "DeleteReport",
    "resolve_range",
    "execute_delete",
    "reconcile_user_deletes",
]

_ORIGIN_USER = "user"


# ---------------------------------------------------------------------------
# Preview / plan value types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordingPlan:
    """One recording's resolved slice of a range delete (the confirm unit).

    ``chunk_indices`` are the covered chunks that WILL be deleted (chunk-rounded);
    ``rounded_start_ms`` / ``rounded_end_ms`` the actual removed extent the confirm
    sheet shows (R20). ``excluded_live_chunks`` are chunks that overlap the range
    but were EXCLUDED (the live in-flight chunk — no readable manifest — never
    deleted). ``kept_clips`` are overlapping clips-to-keep (U10 clips store);
    surviving clips are disclosed so "removed from this Mac" is never silently
    false. Recording identifiers are fine here — this is a direct same-EUID reply,
    not an EventBus payload (the name-free rule is a progress-event rule).
    """

    recording: str
    recording_id: str | None
    chunk_indices: list[int]
    rounded_start_ms: int | None
    rounded_end_ms: int | None
    excluded_live_chunks: list[int] = field(default_factory=list)
    kept_clips: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DeletePreview:
    """The ``dry_run`` preview: the resolved per-recording plans, deleting NOTHING."""

    start_ms: int
    end_ms: int
    recordings: list[RecordingPlan]

    def resolved_map(self) -> dict[str, list[int]]:
        """The confirm token: ``{recording: [chunk_indices]}`` for the confirm call.

        Only recordings with at least one covered chunk are included (an
        excluded-only recording has nothing to confirm).
        """
        return {
            p.recording: list(p.chunk_indices)
            for p in self.recordings
            if p.chunk_indices
        }

    @property
    def total_chunks(self) -> int:
        return sum(len(p.chunk_indices) for p in self.recordings)


@dataclass
class RecordingDeleteOutcome:
    """What was actually deleted for one recording (exact, for review)."""

    recording: str
    deleted_chunks: list[int] = field(default_factory=list)
    screenshots_unlinked: int = 0
    tasks_json_removed: int = 0
    bytes_freed: int = 0


@dataclass
class DeleteReport:
    """The outcome of one :func:`execute_delete` / :func:`reconcile_user_deletes`.

    ``reconfirm_required`` is True (with the recording names in
    ``reconfirm_recordings``) when a recording's resolved set changed between
    preview and confirm — that recording is aborted and DELETES NOTHING; the
    client must re-preview and re-confirm.
    """

    recordings: list[RecordingDeleteOutcome] = field(default_factory=list)
    reconfirm_required: bool = False
    reconfirm_recordings: list[str] = field(default_factory=list)

    @property
    def deleted_chunk_count(self) -> int:
        return sum(len(r.deleted_chunks) for r in self.recordings)


# ---------------------------------------------------------------------------
# Recordings-dir enumeration (skips dot dirs + the ``-scrubbed`` reuse siblings).
# ---------------------------------------------------------------------------


def _recordings_dir(recordings_dir: Path | None) -> Path:
    if recordings_dir is not None:
        return Path(recordings_dir)
    from screencap import catalog

    return Path(catalog.get_recordings_dir())


def _candidate_recording_dirs(recordings_dir: Path) -> list[Path]:
    """Source recording dirs under ``recordings_dir`` (skip dot dirs + siblings).

    A ``<name>-scrubbed`` dir is a reuse cache, never a source recording, so it is
    skipped as an enumeration ROOT (its range is purged as part of its source
    recording's delete). Dot-prefixed dirs (``.clips`` / ``.store``) are skipped
    by the recordings-tree convention.
    """
    if not recordings_dir.is_dir():
        return []
    out: list[Path] = []
    for d in sorted(recordings_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name.endswith("-scrubbed"):
            continue
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Resolution (dry-run preview) — DELETES NOTHING.
# ---------------------------------------------------------------------------


def _resolve_recording(rec_dir: Path, start_s: float, end_s: float) -> RecordingPlan | None:
    """Resolve one recording's covered + excluded chunks for ``[start_s, end_s)``.

    Returns ``None`` when the recording has no ledger (legacy single-file — no
    per-chunk model to round to) or contributes nothing to the range.
    """
    from screencap.pipeline_state import Lifecycle, open_ledger_or_none, spans_overlap
    from screencap.retention import chunk_capture_bounds

    ledger = open_ledger_or_none(rec_dir)
    if ledger is None:
        return None
    try:
        rows = ledger.all_chunks()
    except Exception:  # noqa: BLE001 — a read hiccup must never delete anything
        logger.debug("range_delete: ledger read failed for %s; skipping", rec_dir.name)
        return None

    covered: list[int] = []
    excluded_live: list[int] = []
    covered_bounds: list[tuple[float, float]] = []
    for r in rows:
        # Already gone — never re-report / re-delete.
        if r.lifecycle in (Lifecycle.EVICTED, Lifecycle.USER_DELETED):
            continue
        bounds = chunk_capture_bounds(rec_dir, r.chunk_index)
        if bounds is None:
            # No readable manifest → the live in-flight chunk (or an unplaceable
            # one). Fail-closed: never delete a chunk we cannot place. Report it.
            excluded_live.append(r.chunk_index)
            continue
        cs, ce = bounds
        if spans_overlap(cs, ce, start_s, end_s):
            covered.append(r.chunk_index)
            covered_bounds.append((cs, ce))

    if not covered and not excluded_live:
        return None

    covered.sort()
    if covered_bounds:
        rounded_start = int(round(min(b[0] for b in covered_bounds) * 1000))
        rounded_end = int(round(max(b[1] for b in covered_bounds) * 1000))
    else:
        rounded_start = rounded_end = None

    from screencap.catalog import read_recording_id

    return RecordingPlan(
        recording=rec_dir.name,
        recording_id=read_recording_id(rec_dir),
        chunk_indices=covered,
        rounded_start_ms=rounded_start,
        rounded_end_ms=rounded_end,
        excluded_live_chunks=sorted(excluded_live),
        kept_clips=_overlapping_clips(rec_dir, start_s, end_s),
    )


def _overlapping_clips(rec_dir: Path, start_s: float, end_s: float) -> list[dict[str, Any]]:
    """Clips (U10 store) overlapping ``[start_s, end_s)`` for this recording.

    User range-deletes NEVER cascade to clips (R11) — surviving clips are
    disclosed at confirm time so "removed from this Mac" is never silently false.
    The U10 ``.clips`` store may not exist yet; a missing catalog yields ``[]``.
    """
    from screencap.pipeline_state import spans_overlap

    catalog_path = rec_dir.parent / ".clips" / "catalog.json"
    if not catalog_path.is_file():
        return []
    try:
        import json

        data = json.loads(catalog_path.read_text())
    except Exception:  # noqa: BLE001 — a clips-store hiccup must never block delete
        return []
    entries = data.get("clips") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict) or e.get("source_recording") != rec_dir.name:
            continue
        try:
            cs = float(e["start_ms"]) / 1000.0
            ce = float(e["end_ms"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        if spans_overlap(cs, ce, start_s, end_s):
            out.append(e)
    return out


def resolve_range(
    start_ms: int, end_ms: int, *, recordings_dir: Path | None = None,
) -> DeletePreview:
    """Resolve the requested ms range into per-recording chunk sets (DELETES NOTHING).

    Rounds to whole chunk windows, excludes the live in-flight chunk, and returns
    the resolved set + rounded extents + any overlapping clips-to-keep. This is
    the ``dry_run`` preview the confirm sheet consumes (R20) and the confirm token
    (``resolved_map``) source.
    """
    recordings_dir = _recordings_dir(recordings_dir)
    start_s, end_s = start_ms / 1000.0, end_ms / 1000.0
    plans: list[RecordingPlan] = []
    for rec_dir in _candidate_recording_dirs(recordings_dir):
        plan = _resolve_recording(rec_dir, start_s, end_s)
        if plan is not None:
            plans.append(plan)
    return DeletePreview(start_ms=start_ms, end_ms=end_ms, recordings=plans)


# ---------------------------------------------------------------------------
# The crash-consistent transaction (mirrors scrub_worker._scrub_target).
# ---------------------------------------------------------------------------


def _covered_union_seconds(rec_dir: Path, chunk_indices: list[int]) -> tuple[float, float] | None:
    """Union capture-time interval ``(min start, max end)`` over the covered chunks.

    Read from the (still-present) chunk manifests at execute time. ``None`` when
    no covered chunk has a readable manifest.
    """
    from screencap.retention import chunk_capture_bounds

    starts: list[float] = []
    ends: list[float] = []
    for idx in chunk_indices:
        b = chunk_capture_bounds(rec_dir, idx)
        if b is not None:
            starts.append(b[0])
            ends.append(b[1])
    if not starts:
        return None
    return min(starts), max(ends)


def _commit_delete_transaction(
    rec_dir: Path, chunk_indices: list[int], del_start_s: float, del_end_s: float,
) -> list[str]:
    """Delete the in-range rows + write the ``origin='user'`` span + mark chunks
    ``USER_DELETED`` in ONE ``BEGIN IMMEDIATE`` (crash-consistent). Returns the
    ``image_path`` values of the deleted screenshot rows (for the post-commit
    file unlink).

    recording.db is NEVER deleted here (tombstone rule) — only ROWS inside it are.
    """
    from screencap.enforcement.scrub_worker import (
        PURGE_ORIGIN_USER,
        _purge_ondevice_window_names,
        _purge_task_segments,
        ensure_purged_interval_schema,
    )
    from screencap.recording_db import has_column, has_table

    db_path = rec_dir / "recording.db"
    if not db_path.exists():
        return []

    # Match the engine writer's busy_timeout + BEGIN IMMEDIATE (scrub_worker
    # rationale) so a concurrent WAL checkpoint can never observe a torn delete.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        # Collect the screenshot image_paths in range BEFORE the delete so the
        # on-disk files can be unlinked after commit.
        image_paths: list[str] = []
        if has_table(conn, "screenshot"):
            for row in conn.execute(
                "SELECT image_path FROM screenshot "
                "WHERE timestamp >= ? AND timestamp < ? AND image_path IS NOT NULL",
                (del_start_s, del_end_s),
            ):
                if row[0]:
                    image_paths.append(row[0])

        rec_row = conn.execute("SELECT id FROM recording LIMIT 1").fetchone()
        recording_id = int(rec_row[0]) if rec_row is not None else None

        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            if has_table(conn, "action_event"):
                cur.execute(
                    "DELETE FROM action_event WHERE timestamp >= ? AND timestamp < ?",
                    (del_start_s, del_end_s),
                )
            if has_table(conn, "window_geometry") and has_column(
                conn, "window_geometry", "screenshot_timestamp"
            ):
                cur.execute(
                    "DELETE FROM window_geometry "
                    "WHERE screenshot_timestamp >= ? AND screenshot_timestamp < ?",
                    (del_start_s, del_end_s),
                )
            if has_table(conn, "screenshot"):
                cur.execute(
                    "DELETE FROM screenshot WHERE timestamp >= ? AND timestamp < ?",
                    (del_start_s, del_end_s),
                )
            if has_table(conn, "window_event"):
                cur.execute(
                    "DELETE FROM window_event WHERE timestamp >= ? AND timestamp < ?",
                    (del_start_s, del_end_s),
                )

            # Derived artifacts whose content derives from the now-deleted rows —
            # the SAME R7 lifecycle purge the retroactive-disable path applies,
            # in this SAME transaction. Agent task segments / cached names whose
            # spans overlap the range go; user / edited rows are preserved by the
            # helpers' own predicate.
            span = [(del_start_s, del_end_s)]
            _purge_ondevice_window_names(cur, conn, span)
            _purge_task_segments(cur, conn, span)

            # The origin='user' purge span — crash-consistent with the deletes so
            # a crash can never leave rows gone but the span (the ground truth the
            # readers use) unrecorded. disabled_at is NULL (no disable target).
            ensure_purged_interval_schema(cur)
            cur.execute(
                "INSERT INTO purged_interval (start_ts, end_ts, disabled_at, origin) "
                "VALUES (?, ?, ?, ?)",
                (float(del_start_s), float(del_end_s), None, PURGE_ORIGIN_USER),
            )

            # Transition the covered chunks to the terminal USER_DELETED state in
            # the SAME transaction (satisfied-by-deletion for the sentinel gate).
            now = time.time()
            for idx in chunk_indices:
                if recording_id is not None:
                    cur.execute(
                        "UPDATE pipeline_chunk_state SET lifecycle=?, updated_at=? "
                        "WHERE recording_id=? AND chunk_index=?",
                        ("user_deleted", now, recording_id, idx),
                    )
                else:
                    cur.execute(
                        "UPDATE pipeline_chunk_state SET lifecycle=?, updated_at=? "
                        "WHERE chunk_index=?",
                        ("user_deleted", now, idx),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Force the WAL into the main DB so the deletes can't return from the WAL
        # on next open (scrub_worker precedent). Non-fatal if it can't checkpoint.
        try:
            conn.execute("PRAGMA wal_checkpoint(RESTART)")
        except sqlite3.OperationalError:
            pass
        return image_paths
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The on-disk unlink (shared by execute + reconcile — idempotent, missing_ok).
# ---------------------------------------------------------------------------


def _unlink(path: Path) -> int:
    """Unlink ``path`` if present; return bytes freed. Never raises."""
    try:
        if path.exists():
            size = path.stat().st_size
            path.unlink()
            return size
    except OSError as exc:
        logger.warning("range_delete: failed to unlink %s: %s", path.name, exc)
    return 0


def _unlink_screenshots_in_spans(
    base_dir: Path,
    spans: list[tuple[float, float]],
    *,
    image_paths: list[str] | None = None,
) -> int:
    """Unlink screenshots for ``image_paths`` (DB-referenced) + every flat
    ``screenshots/*.jpg[.enc]`` whose parsed timestamp falls in any span.

    The flat-glob-by-timestamp pass is the crash-robust catch-all: after the
    transaction commits the screenshot ROWS are gone (so reconciliation has no
    ``image_paths``), but the files may survive a crash — the timestamp glob
    finds them regardless of the deleted rows.
    """
    from screencap.enforcement.scrub_worker import _both_still_forms
    from screencap.retention import _still_timestamp

    count = 0
    base_resolved = base_dir.resolve()
    # 1. Exact DB-referenced paths (both plaintext + encrypted forms).
    for rel in image_paths or []:
        removed_any = False
        for candidate in _both_still_forms(rel):
            p = (base_dir / candidate)
            try:
                if base_resolved not in p.resolve().parents and p.resolve() != base_resolved:
                    continue
            except OSError:
                continue
            if _unlink(p) or not p.exists():
                removed_any = removed_any or True
        if removed_any:
            count += 1
    # 2. Flat glob by timestamp (catch-all, crash-robust).
    shots_dir = base_dir / "screenshots"
    if shots_dir.is_dir():
        for p in list(shots_dir.iterdir()):
            if not (p.name.endswith(".jpg") or p.name.endswith(".jpg.enc")):
                continue
            ts = _still_timestamp(p)
            if ts is None:
                continue
            if any(s <= ts < e for s, e in spans):
                if _unlink(p):
                    count += 1
    return count


def _regenerate_bare_transcript(base_dir: Path) -> None:
    """Rebuild ``base_dir/transcript.txt`` from surviving ``transcript_<idx>.txt``.

    The bare whole-recording transcript spans every chunk and is grepped directly
    by ``/v0/transcript.search``, so after deleting a chunk's per-chunk transcript
    it must be regenerated from the survivors (or deleted when none survive) — else
    deleted speech survives the search. A no-op when there is no bare file to keep
    honest (never CREATES one that didn't exist).
    """
    bare = base_dir / "transcript.txt"
    if not bare.exists():
        return
    surviving = sorted(
        p for p in base_dir.glob("transcript_*.txt")
        if not p.name.endswith(".scrub_failed")
    )
    if not surviving:
        _unlink(bare)
        return
    parts: list[str] = []
    for p in surviving:
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace").rstrip("\n"))
        except OSError:
            continue
    text = "\n".join(parts)
    try:
        bare.write_text(text + "\n" if text else "", encoding="utf-8")
    except OSError as exc:
        logger.warning("range_delete: transcript.txt regen failed: %s", exc)


def _purge_scrubbed_sibling(
    rec_dir: Path, chunk_indices: list[int], spans: list[tuple[float, float]],
) -> None:
    """Purge the deleted range from the ``<name>-scrubbed`` reuse sibling.

    Without this, ``transcript.search`` (which ``_iter_recording_dirs`` walks over
    the scrubbed sibling too) would still find the deleted speech in
    ``<name>-scrubbed/transcript_*.txt``. Best-effort per artifact.
    """
    scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
    if not scrubbed.is_dir():
        return
    for idx in chunk_indices:
        _unlink(scrubbed / f"chunk_{idx:04d}.mp4")
        _unlink(scrubbed / f"audio_{idx:04d}.flac")
        _unlink(scrubbed / f"events_{idx:04d}.jsonl")
        _unlink(scrubbed / f"chunk_{idx:04d}_manifest.json")
        _unlink(scrubbed / f"transcript_{idx:04d}.txt")
        _unlink(scrubbed / f"transcript_{idx:04d}.json")
        _unlink(scrubbed / "masked_video" / f"chunk_{idx:04d}.mp4")
    _unlink_screenshots_in_spans(scrubbed, spans)
    _regenerate_bare_transcript(scrubbed)


def _unlink_covered_artifacts(
    rec_dir: Path,
    chunk_indices: list[int],
    spans: list[tuple[float, float]],
    *,
    image_paths: list[str] | None = None,
) -> RecordingDeleteOutcome:
    """Unlink the FULL artifact set for the covered chunks + spans (idempotent).

    Shared by :func:`execute_delete` (post-commit) and
    :func:`reconcile_user_deletes` (crash recovery) so the two can never drift on
    what a user delete removes. Everything is ``missing_ok``, so re-running over an
    already-completed delete is a clean no-op.
    """
    from screencap.enforcement.scrub_worker import (
        purge_content_index_intervals,
        purge_tasks_json_intervals,
    )
    from screencap.retention import _unlink_chunk

    outcome = RecordingDeleteOutcome(recording=rec_dir.name)
    for idx in chunk_indices:
        outcome.bytes_freed += _unlink_chunk(rec_dir, idx)  # mp4/audio/events/manifest
        outcome.bytes_freed += _unlink(rec_dir / f"transcript_{idx:04d}.txt")
        outcome.bytes_freed += _unlink(rec_dir / f"transcript_{idx:04d}.json")
        outcome.deleted_chunks.append(idx)

    outcome.screenshots_unlinked += _unlink_screenshots_in_spans(
        rec_dir, spans, image_paths=image_paths
    )
    _regenerate_bare_transcript(rec_dir)

    # Content index + tasks.json intervals (the shared scrub_worker helpers).
    purge_content_index_intervals(rec_dir, spans)
    outcome.tasks_json_removed += purge_tasks_json_intervals(rec_dir, spans)

    # The scrubbed reuse sibling.
    _purge_scrubbed_sibling(rec_dir, chunk_indices, spans)
    return outcome


# ---------------------------------------------------------------------------
# Execute — per recording, under the per-recording flock, re-resolve then delete.
# ---------------------------------------------------------------------------


def execute_delete(
    start_ms: int,
    end_ms: int,
    resolved: dict[str, list[int]],
    *,
    recordings_dir: Path | None = None,
    stop_event: Any = None,
    progress_cb: Callable[[int, int, int], None] | None = None,
) -> DeleteReport:
    """Delete exactly the confirmed ``{recording: [chunk_indices]}`` set.

    Reconciles outstanding user-deletes on entry (mirrors the terminal stage's
    reconcile-on-every-entry), then per recording, UNDER the per-recording flock:
    re-resolves the range and ABORTS that recording (``reconfirm_required``,
    deleting nothing) if its covered set changed since preview; otherwise runs the
    crash-consistent transaction then unlinks the full artifact set.

    ``stop_event`` (a ``threading.Event``) halts the job cleanly BETWEEN
    recordings (each recording is atomic under its flock). ``progress_cb(done,
    total, current_index)`` fires per recording — the caller keeps it recording-
    name-free (R9).
    """
    recordings_dir = _recordings_dir(recordings_dir)
    names = sorted(resolved.keys())

    # Reconcile-on-entry: complete any outstanding user-delete unlink a prior
    # crash left, for exactly the recordings this run touches (mirrors
    # terminal_stage's reconcile-on-every-entry). BEFORE we take any flock below
    # (reconcile takes + releases the per-recording flock itself), so no nesting.
    reconcile_user_deletes(recordings_dir=recordings_dir, only=set(names))

    start_s, end_s = start_ms / 1000.0, end_ms / 1000.0
    report = DeleteReport()
    total = len(names)
    for i, name in enumerate(names):
        if stop_event is not None and stop_event.is_set():
            break  # cancel mid-job — cleanly, at a recording boundary
        confirmed = sorted({int(x) for x in resolved.get(name, [])})
        rec_dir = recordings_dir / name
        _delete_one_recording(rec_dir, start_s, end_s, confirmed, report)
        if progress_cb is not None:
            try:
                progress_cb(i + 1, total, i)
            except Exception:  # noqa: BLE001 — progress is best-effort telemetry
                logger.debug("range_delete: progress_cb raised; ignored")
    return report


def _delete_one_recording(
    rec_dir: Path,
    start_s: float,
    end_s: float,
    confirmed: list[int],
    report: DeleteReport,
) -> None:
    """Delete one recording's confirmed chunk set, under its flock, TOCTOU-safe."""
    from screencap.terminal_stage import TerminalStageBusy, terminal_lock

    name = rec_dir.name
    try:
        with terminal_lock(name):
            # Re-resolve NOW (under the flock) and compare to the confirmed set.
            # A live chunk that flushed between preview and confirm now overlaps →
            # the set grew → abort this recording (never delete more than confirmed).
            plan = _resolve_recording(rec_dir, start_s, end_s)
            current = plan.chunk_indices if plan is not None else []
            if sorted(current) != confirmed:
                report.reconfirm_required = True
                report.reconfirm_recordings.append(name)
                logger.info(
                    "range_delete: %s resolved set changed since preview "
                    "(confirmed=%s, now=%s) — re-confirm required, nothing deleted",
                    name, confirmed, sorted(current),
                )
                return
            if not confirmed:
                return

            union = _covered_union_seconds(rec_dir, confirmed)
            if union is None:
                # No readable manifest for any confirmed chunk (should not happen
                # since resolution required readable bounds) — fail closed.
                report.reconfirm_required = True
                report.reconfirm_recordings.append(name)
                return
            del_start_s, del_end_s = union

            # 1. Crash-consistent transaction (rows + span + USER_DELETED).
            image_paths = _commit_delete_transaction(
                rec_dir, confirmed, del_start_s, del_end_s
            )
            # 2. Unlink the full artifact set (post-commit; a crash here is
            #    completed by reconcile on the next start).
            outcome = _unlink_covered_artifacts(
                rec_dir, confirmed, [(del_start_s, del_end_s)],
                image_paths=image_paths,
            )
            report.recordings.append(outcome)
    except TerminalStageBusy:
        # Another terminal-stage run holds the flock past the timeout — abort this
        # recording safely (nothing deleted), surface as re-confirm required.
        report.reconfirm_required = True
        report.reconfirm_recordings.append(name)
        logger.info("range_delete: %s flock contended; nothing deleted", name)


# ---------------------------------------------------------------------------
# Crash reconciliation — complete outstanding user-delete unlinks under the flock.
# ---------------------------------------------------------------------------


def _read_user_purge_spans(db_path: Path) -> list[tuple[float, float]]:
    """The ``origin='user'`` purge spans (seconds; NULL end → +inf) for a recording."""
    from screencap.recording_db import has_column, has_table, open_recording_db

    try:
        with open_recording_db(db_path, busy_timeout_ms=10000) as conn:
            if not has_table(conn, "purged_interval"):
                return []
            if not has_column(conn, "purged_interval", "origin"):
                return []  # pre-U8: no user deletes ever recorded
            rows = conn.execute(
                "SELECT start_ts, end_ts FROM purged_interval "
                "WHERE origin=? AND start_ts IS NOT NULL",
                (_ORIGIN_USER,),
            ).fetchall()
    except Exception:  # noqa: BLE001 — reconciliation must never raise into startup
        logger.warning("range_delete: user-span read failed for %s", db_path, exc_info=True)
        return []
    return [
        (float(s), float(e) if e is not None else float("inf"))
        for s, e in rows
    ]


def reconcile_user_deletes(
    *, recordings_dir: Path | None = None, only: set[str] | None = None,
) -> DeleteReport:
    """Complete any outstanding user-delete unlink a crash left (idempotent).

    Runs on daemon start AND at every delete-job start (mirrors the terminal
    stage's reconcile-on-entry). Per recording, under the per-recording flock,
    re-scans the ledger's ``USER_DELETED`` chunk rows + the ``origin='user'``
    ``purged_interval`` spans against on-disk artifacts and finishes the unlink —
    so a crash between the transaction and the unlink cannot leave "removed by you"
    bytes on disk. A fully-completed delete re-runs as a clean no-op.

    ``only`` scopes the pass to a set of recording names (the delete job passes the
    recordings it is about to touch); ``None`` scans every recording (daemon start).
    """
    from screencap.pipeline_state import Lifecycle, open_ledger_or_none
    from screencap.terminal_stage import TerminalStageBusy, terminal_lock

    recordings_dir = _recordings_dir(recordings_dir)
    report = DeleteReport()
    for rec_dir in _candidate_recording_dirs(recordings_dir):
        if only is not None and rec_dir.name not in only:
            continue
        db_path = rec_dir / "recording.db"
        if not db_path.exists():
            continue
        ledger = open_ledger_or_none(rec_dir)
        if ledger is None:
            continue
        try:
            deleted_rows = ledger.chunks_in_state(lifecycle=Lifecycle.USER_DELETED)
        except Exception:  # noqa: BLE001
            continue
        spans = _read_user_purge_spans(db_path)
        if not deleted_rows and not spans:
            continue
        chunk_indices = sorted(r.chunk_index for r in deleted_rows)
        try:
            with terminal_lock(rec_dir.name, non_blocking=True):
                outcome = _unlink_covered_artifacts(
                    rec_dir, chunk_indices, spans or [],
                )
                report.recordings.append(outcome)
        except TerminalStageBusy:
            # A live terminal run holds the flock — skip; the next start retries.
            logger.debug(
                "range_delete: reconcile skipped %s (flock contended)", rec_dir.name
            )
    return report
