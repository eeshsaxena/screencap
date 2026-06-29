"""Backfill engine (U4, SCR-178) — enumerate → seed → run → progress/budget/cancel.

This is the destination-agnostic core that the daemon (U5) and CLI (U6) both
invoke. It OCR-indexes a user's *existing* local recordings into the content
index, reusing the live indexing rules exactly:

  * enumerate existing recordings (sorted, deterministic) and their chunks;
  * seed a closed set of ``(recording_dir_name, chunk_index)`` units into the
    U3 ledger (frozen denominator — no survivorship bias);
  * per unit: build the U2 classifier+evaluator (mode per ``.recording_intent``),
    re-derive the U2 skip set, then run U1's :func:`screencap.index_core.index_range`
    (which holds ``content_index_write_lock()`` + the unlink-before-write barrier
    internally);
  * mark a unit ``DONE`` **only** when ``index_range`` completed the full range
    (``completed_range``) AND the store was available (``store_available``) — a
    mid-chunk stop/budget bail OR a present-but-unavailable store (SCR-192) leaves
    it ``PENDING`` so a resume (or a store repair + resume) re-OCRs the whole range
    cleanly (U1's whole-range replace makes this safe);
  * emit progress with an **opaque ordinal** unit index — never the recording
    directory name (R9/privacy: the daemon broadcasts these over the EventBus and
    a recording dir name encodes timing/context that must not leak to same-EUID
    subscribers, including the MCP ``/v0/events`` subscription).

Strictly fail-open
------------------
Per-recording exceptions degrade to a ``FAILED`` unit and the run continues
(one bad recording never aborts the run). Budget exhaustion / large-library
overflow returns ``PAUSED`` with a pending remainder (NOT a misleading
``COMPLETED`` — U8 must be able to show "N pending, resume to continue"). A
``stop_event`` set returns ``CANCELLED`` promptly. The engine **never raises**;
every exit returns a :class:`BackfillSummary` and the ledger is already persisted
per-mark.

Local-only & read-only
-----------------------
The backfill writes only ``content_index.db`` (via ``index_range``) and the
ledger; it opens ``recording.db`` read-only (delegated to U2). It does NOT import
from ``screencap.daemon`` — it is destination-agnostic, so enumeration uses a
direct sorted glob over the recordings dir (the daemon's ``_QUERY_MAX_RECORDINGS``
cap is mirrored here as a local constant).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from screencap.backfill.ledger import BackfillLedger, RunState, UnitStatus

if TYPE_CHECKING:
    import threading

logger = logging.getLogger(__name__)

# Mirror the daemon's ``_QUERY_MAX_RECORDINGS`` (daemon/app.py) WITHOUT importing
# from ``screencap.daemon`` (package boundary — the engine is destination
# agnostic). A library larger than this is processed up to the cap and the run
# returns PAUSED with the tail still PENDING, so coverage is never silently
# truncated (R8).
_MAX_RECORDINGS = 200

# The per-recording OCR frame cap and per-chunk wall-clock budget are NOT defined
# here — they are imported from ``index_core`` (``_INDEX_MAX_OCR_FRAMES`` /
# ``_INDEX_OCR_BUDGET_S``) at the point of use (deferred, matching this file's
# import style) so the backfill caps a recording exactly the way the live indexer
# caps a chunk and the two can never drift.

# Secure-field hold (mirrors privacy.policy.DEFAULT_TRANSITION_HOLD_SECONDS);
# imported lazily where used to keep this module light. Used to bias a
# synthesized whole-recording window outward so a secure-field event near the
# boundary stays fully inside the derived skip set.

# A progress callback receives ``(done, skipped, failed, total, unit_index)`` —
# the last arg is an OPAQUE ordinal (0-based position in the seeded unit order),
# never the recording directory name.
ProgressCb = Callable[[int, int, int, int, int], None]


@dataclass(frozen=True)
class BackfillSummary:
    """Terminal outcome of one :func:`run_backfill` invocation.

    ``state`` is the run-level disposition the caller surfaces:
      * ``COMPLETED`` — every seeded unit is terminal (DONE/SKIPPED/FAILED);
      * ``PAUSED`` — global budget exhausted or library exceeded the cap before
        the tail; units remain PENDING (resumable — re-run continues);
      * ``CANCELLED`` — ``stop_event`` was set; units remain PENDING (resumable).

    ``done/skipped/failed/total`` mirror the ledger's frozen-denominator
    ``progress()``; ``rows_written`` is the total rows this invocation wrote.
    """

    state: RunState
    done: int
    skipped: int
    failed: int
    total: int
    rows_written: int


def run_backfill(
    *,
    recordings_dir: Path | None = None,
    stop_event: "threading.Event | None" = None,
    progress_cb: ProgressCb | None = None,
    budget_s: float | None = None,
    max_frames_per_recording: int | None = None,
    ledger: BackfillLedger | None = None,
    ocr: object | None = None,
    store_path: Path | None = None,
) -> BackfillSummary:
    """Run (or resume) the content-index backfill over existing recordings.

    See the module docstring for the full behavior. All heavy collaborators are
    injectable (``ledger`` / ``ocr`` / ``store_path`` / ``recordings_dir``) so the
    engine is testable with fakes; defaults resolve to the production singletons
    lazily.

    Never raises — every exit path returns a :class:`BackfillSummary`.
    """
    run_start = time.monotonic()

    # Establish the cancel flag + ledger handle BEFORE any heavy init (classifier
    # build, OCR engine construction) so a cancel during setup is honored and a
    # budget/cancel exit can always flush a real ledger.
    if ledger is None:
        ledger = BackfillLedger()

    def _cancelled() -> bool:
        return stop_event is not None and stop_event.is_set()

    def _budget_exhausted() -> bool:
        return budget_s is not None and (time.monotonic() - run_start) >= budget_s

    def _emit(unit_index: int) -> None:
        if progress_cb is None:
            return
        done, skipped, failed, total = ledger.progress()
        try:
            progress_cb(done, skipped, failed, total, unit_index)
        except Exception:
            logger.debug("backfill progress_cb raised; ignoring", exc_info=True)

    def _finish(state: RunState) -> BackfillSummary:
        # Honor run_backfill's "never raises" contract even if the ledger write
        # fails (e.g. SQLite busy). A stuck-RUNNING ledger would otherwise
        # disable auto-resume, but letting set_run_state raise here is strictly
        # worse — log at warning and continue so the caller still gets a summary.
        try:
            ledger.set_run_state(state)
        except Exception:
            logger.warning(
                "backfill: failed to persist terminal run state %s", state,
                exc_info=True,
            )
        done, skipped, failed, total = ledger.progress()
        return BackfillSummary(
            state=state,
            done=done,
            skipped=skipped,
            failed=failed,
            total=total,
            rows_written=rows_written_total,
        )

    rows_written_total = 0

    # Honor a cancel requested before we even start.
    if _cancelled():
        return _finish(RunState.CANCELLED)

    if recordings_dir is None:
        from screencap import config

        recordings_dir = config.get_recordings_dir()
    if store_path is None:
        from screencap.content_index import default_index_path

        store_path = default_index_path()
    if max_frames_per_recording is None:
        from screencap.index_core import _INDEX_MAX_OCR_FRAMES

        max_frames_per_recording = _INDEX_MAX_OCR_FRAMES

    # --- Enumerate recordings + their units (deterministic, capped) --------
    rec_dirs = _enumerate_recordings(Path(recordings_dir))
    overflowed = len(rec_dirs) > _MAX_RECORDINGS
    if overflowed:
        logger.info(
            "backfill: library has %d recordings; processing first %d (cap)",
            len(rec_dirs),
            _MAX_RECORDINGS,
        )
        rec_dirs = rec_dirs[:_MAX_RECORDINGS]

    # Build the ordered closed set of (recording_dir_name, chunk_index) units and
    # remember each recording's dir + chunk window for the run loop.
    units: list[tuple[str, int]] = []
    # unit_key -> (rec_dir, start_ts, end_ts)
    unit_info: dict[tuple[str, int], tuple[Path, float, float]] = {}
    for rec_dir in rec_dirs:
        for chunk_index, start_ts, end_ts in _enumerate_chunks(rec_dir):
            key = (rec_dir.name, chunk_index)
            units.append(key)
            unit_info[key] = (rec_dir, start_ts, end_ts)

    # Seed the closed set (idempotent on resume). Re-seeding may ADDITIVELY
    # expand the set — a recording created between runs joins it on the next run,
    # which is intentional (new work should be covered). What stays frozen is the
    # per-run denominator: within a single run the total is fixed at seed time, so
    # progress can't be skewed by survivorship as units complete.
    ledger.seed(units)
    ledger.set_run_state(RunState.RUNNING)

    # --- Per-unit run loop -------------------------------------------------
    # Cache per-recording (classifier, evaluator) and the flat screenshot
    # timestamps so they are built ONCE per recording across its chunks.
    from screencap.content_index import CrossProcessLockUnavailable

    classifier_cache: dict[str, tuple[object, object]] = {}
    timestamps_cache: dict[str, list[float]] = {}

    for unit_index, key in enumerate(units):
        # Poll cancel + budget between units (the core also polls per-frame).
        if _cancelled():
            return _finish(RunState.CANCELLED)
        if _budget_exhausted():
            return _finish(RunState.PAUSED)

        status = ledger.unit_status(key[0], key[1])
        if status is not None and status != UnitStatus.PENDING:
            # Already terminal on resume — skip, but still surface progress.
            _emit(unit_index)
            continue

        rec_dir, start_ts, end_ts = unit_info[key]
        rec_name = key[0]

        # A retention-evicted / malformed recording (no screenshots or no DB) is a
        # normal SKIPPED, not a FAILED.
        if not (rec_dir / "screenshots").is_dir() or not (
            rec_dir / "recording.db"
        ).is_file():
            ledger.mark(rec_name, key[1], UnitStatus.SKIPPED)
            _emit(unit_index)
            continue

        try:
            rows = _process_unit(
                rec_dir=rec_dir,
                rec_name=rec_name,
                start_ts=start_ts,
                end_ts=end_ts,
                ledger=ledger,
                chunk_index=key[1],
                classifier_cache=classifier_cache,
                timestamps_cache=timestamps_cache,
                ocr=ocr,
                store_path=store_path,
                stop_event=stop_event,
                max_frames=max_frames_per_recording,
                run_start=run_start,
                budget_s=budget_s,
            )
            rows_written_total += rows
        except CrossProcessLockUnavailable:
            # SCR-191: the cross-process content-index flock is unavailable (a
            # no-flock filesystem / sandboxed run dir). Its support is
            # all-or-nothing per filesystem, so every remaining unit would hit
            # the same wall. Pause the whole run (this unit is left PENDING — it
            # was never marked — so the run is fully resumable) rather than churn
            # through the tail or, worse, write without cross-process protection.
            logger.warning(
                "backfill: cross-process content-index lock unavailable; pausing run"
            )
            return _finish(RunState.PAUSED)
        except Exception:
            # One bad recording never aborts the run (strictly fail-open).
            logger.warning(
                "backfill: unit failed (recording omitted from log for privacy)",
                exc_info=True,
            )
            ledger.mark(rec_name, key[1], UnitStatus.FAILED)

        _emit(unit_index)

    # --- Terminal disposition ---------------------------------------------
    if _cancelled():
        return _finish(RunState.CANCELLED)
    if ledger.is_complete():
        # Even a full pass returns PAUSED if the library overflowed the cap —
        # there is a tail we never enumerated, so coverage is incomplete (R8).
        if overflowed:
            return _finish(RunState.PAUSED)
        return _finish(RunState.COMPLETED)
    # Units remain PENDING but we exited the loop — budget/cap pause.
    return _finish(RunState.PAUSED)


# ---------------------------------------------------------------------------
# Unit processing
# ---------------------------------------------------------------------------


def _process_unit(
    *,
    rec_dir: Path,
    rec_name: str,
    start_ts: float,
    end_ts: float,
    ledger: BackfillLedger,
    chunk_index: int,
    classifier_cache: dict[str, tuple[object, object]],
    timestamps_cache: dict[str, list[float]],
    ocr: object | None,
    store_path: Path,
    stop_event: "threading.Event | None",
    max_frames: int,
    run_start: float,
    budget_s: float | None,
) -> int:
    """Process one ``(recording, chunk)`` unit; return rows written.

    Builds the classifier/evaluator + screenshot timestamps once per recording
    (cached), re-derives the skip set, runs ``index_range``, then marks the unit
    DONE only if ``index_range`` completed the full range. ``ledger.mark`` is
    called OUTSIDE any content-index lock (``index_range`` already released it).
    """
    from screencap.backfill.skip_intervals import (
        build_classifier_evaluator,
        derive_skip_intervals,
    )
    from screencap.index_core import _INDEX_OCR_BUDGET_S, index_range

    # Per-recording classifier/evaluator (mode resolved from .recording_intent).
    if rec_name not in classifier_cache:
        classifier_cache[rec_name] = build_classifier_evaluator(recording_dir=rec_dir)
    classifier, evaluator = classifier_cache[rec_name]

    # Per-recording flat screenshot timestamps (enables U2's uncovered-gap pass).
    if rec_name not in timestamps_cache:
        timestamps_cache[rec_name] = _screenshot_timestamps(rec_dir)
    screenshot_timestamps = timestamps_cache[rec_name]

    skip = derive_skip_intervals(
        rec_dir / "recording.db",
        classifier=classifier,
        evaluator=evaluator,
        time_range=(start_ts, end_ts),
        screenshot_timestamps=screenshot_timestamps,
    )

    # Per-chunk budget = min(remaining global budget, the per-chunk default) so a
    # mid-chunk bail near the global deadline sets completed_range=False and
    # leaves the unit PENDING.
    per_chunk_budget = _INDEX_OCR_BUDGET_S
    if budget_s is not None:
        remaining = budget_s - (time.monotonic() - run_start)
        # Never hand index_range a non-positive budget — clamp to a tiny positive
        # so it bails after the first frame check (completed_range=False).
        per_chunk_budget = max(0.001, min(per_chunk_budget, remaining))

    ocr_engine = ocr if ocr is not None else _default_ocr()

    result = index_range(
        rec_dir,
        start_ts,
        end_ts,
        skip,
        ocr=ocr_engine,
        store_path=store_path,
        stop_event=stop_event,
        budget_s=per_chunk_budget,
        max_frames=max_frames,
        # SCR-191: the backfill runs in the daemon process, NOT the recorder, so
        # the in-process content-index lock does not serialize it against the
        # recorder's retroactive-disable purge — only the cross-process flock
        # does. Require it: if it is unavailable index_range raises
        # CrossProcessLockUnavailable (handled in the run loop) rather than
        # writing without cross-process protection.
        require_cross_process_lock=True,
    )

    # DONE-gating: mark DONE only on a complete range AND an available store. A
    # mid-chunk stop/budget bail (completed_range=False) leaves the unit PENDING so
    # a resume re-OCRs the whole range cleanly via U1's whole-range replace.
    # SCR-192: a present-but-unavailable store (store_available=False) wrote nothing
    # despite a complete pass — also leave it PENDING so a store repair + resume
    # re-OCRs, rather than latching DONE with zero rows. Marked OUTSIDE the
    # content-index lock (index_range released it on return).
    if result.completed_range and result.store_available:
        ledger.mark(
            rec_name, chunk_index, UnitStatus.DONE, rows_written=result.rows_written
        )
    return result.rows_written


# ---------------------------------------------------------------------------
# Enumeration helpers
# ---------------------------------------------------------------------------


def _enumerate_recordings(recordings_dir: Path) -> list[Path]:
    """Return existing recording dirs, sorted by name (deterministic).

    A direct sorted glob over the recordings dir — NOT ``screencap.catalog`` (to
    avoid the heavy per-recording DB reads) and NOT ``screencap.daemon`` (package
    boundary). A "recording dir" is any direct child directory; missing
    ``screenshots/``/``recording.db`` is handled per-unit (SKIPPED), so we do not
    filter here.

    SCR-191: the recording with an ACTIVE capture is excluded. The backfill runs
    in the daemon while capture/scrub run in the recorder subprocess; processing
    the in-flight recording would read a half-written ``recording.db`` in
    skip-derivation and race the live indexer's whole-range ``write_chunk`` on the
    same ``(recording, timestamp_ms)`` keys cross-process. A later run picks it up
    once capture finishes (re-seeding is additive).
    """
    if not recordings_dir.is_dir():
        return []
    active = _active_recording_name()
    return sorted(
        (
            d
            for d in recordings_dir.iterdir()
            if d.is_dir() and d.name != active
        ),
        key=lambda d: d.name,
    )


def _active_recording_name() -> str | None:
    """Return the directory name of the recording with a live capture, or None.

    Reads the process-exclusive recording lock metadata (``screencap.pidfile`` —
    NOT ``screencap.daemon``, so the engine's destination-agnostic boundary
    holds). Returns the holder's ``recording_name`` only when the lock is actually
    held by a live process (``lock_is_active`` rules out a stale lockfile whose
    holder died). Fail-open: any error → None (process nothing-excluded rather
    than aborting the backfill).
    """
    try:
        from screencap import pidfile

        if not pidfile.lock_is_active():
            return None
        metadata = pidfile.read_lock_metadata()
        if not metadata:
            return None
        name = metadata.get("recording_name")
        return name if isinstance(name, str) and name else None
    except Exception:
        logger.debug("backfill: active-recording check failed; excluding none", exc_info=True)
        return None


def _enumerate_chunks(rec_dir: Path) -> list[tuple[int, float, float]]:
    """Return ``(chunk_index, start_ts, end_ts)`` units for one recording.

    From the chunk manifests (``sorted(d.glob("chunk_*_manifest.json"))``, reading
    the ``chunk_index``/``chunk_start``/``chunk_end`` keys). When no manifests
    exist, synthesize a single whole-recording window (chunk_index 0) from the
    min/max ``screenshots/*.jpg`` timestamps, biased OUTWARD by the secure-field
    hold-seconds so a secure-field event near the synthesized boundary stays fully
    inside the derived skip set.
    """
    import json

    manifests = sorted(rec_dir.glob("chunk_*_manifest.json"))
    out: list[tuple[int, float, float]] = []
    for mpath in manifests:
        try:
            data = json.loads(mpath.read_text(encoding="utf-8"))
            chunk_index = int(data["chunk_index"])
            start_ts = float(data["chunk_start"])
            end_ts = float(data["chunk_end"])
        except Exception:
            logger.debug("backfill: unreadable manifest %s; skipping", mpath.name)
            continue
        out.append((chunk_index, start_ts, end_ts))
    if out:
        return out

    # Fallback: synthesize a whole-recording window from screenshot timestamps.
    timestamps = _screenshot_timestamps(rec_dir)
    if not timestamps:
        return []
    from screencap.privacy.policy import DEFAULT_TRANSITION_HOLD_SECONDS

    start_ts = min(timestamps) - DEFAULT_TRANSITION_HOLD_SECONDS
    # end is half-open; widen past the last frame + the hold so a trailing
    # secure-field interval stays inside, and the last frame itself is in range.
    end_ts = max(timestamps) + DEFAULT_TRANSITION_HOLD_SECONDS + 1.0
    return [(0, start_ts, end_ts)]


def _screenshot_timestamps(rec_dir: Path) -> list[float]:
    """Return the parsed, sorted flat ``screenshots/*.jpg`` timestamps."""
    from screencap.redaction.geometry import parse_screenshot_timestamp

    shots = rec_dir / "screenshots"
    if not shots.is_dir():
        return []
    out: list[float] = []
    for img in shots.glob("*.jpg"):
        ts = parse_screenshot_timestamp(img.name)
        if ts is not None:
            out.append(ts)
    out.sort()
    return out


def _default_ocr() -> object:
    """Construct the real Apple Vision OCR, tolerating its absence (no-op).

    Deferred import (heavy, macOS-only). If Vision is unavailable the backfill
    degrades to a no-op OCR — every frame recognises empty text, so nothing is
    indexed but the run still completes cleanly. Tests inject a fake instead.
    """
    try:
        from screencap.redaction.ocr import VisionOcr

        return VisionOcr()
    except Exception:
        logger.warning(
            "backfill: Apple Vision OCR unavailable; backfill will index nothing",
            exc_info=True,
        )
        return _NoOpOcr()


class _NoOpOcr:
    """Fallback OCR used when Apple Vision is unavailable — recognises nothing."""

    def recognize(self, image_path: Path, **_kw: object) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(text_blocks=[])
