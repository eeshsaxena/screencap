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

Cross-window paging (SCR-193)
-----------------------------
A library larger than one seed-window (``_MAX_RECORDINGS``) is **paged through
within a single run**: the engine seeds + processes one window of not-yet-covered
recordings, then advances to the next window until the whole library is covered
(or budget/cancel intervenes). The seed denominator grows per window
(cumulative); ``is_recording_covered`` (ledger) is the resume frontier so a
re-run skips already-finished recordings and starts at the tail rather than
re-scanning the same first window forever. ``COMPLETED`` therefore means the
*entire* library was indexed — overflow is no longer a permanent ``PAUSED``.

Strictly fail-open
------------------
Per-recording exceptions degrade to a ``FAILED`` unit and the run continues
(one bad recording never aborts the run). Budget exhaustion returns ``PAUSED``
with a real pending remainder (NOT a misleading ``COMPLETED`` — U8 must be able
to show "N pending, resume to continue"). A ``stop_event`` set returns
``CANCELLED`` promptly. The engine **never raises**; every exit returns a
:class:`BackfillSummary` and the ledger is already persisted per-mark.

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

# Per-window seed-batch size. Mirrors the daemon's ``_QUERY_MAX_RECORDINGS``
# (daemon/app.py) WITHOUT importing from ``screencap.daemon`` (package boundary —
# the engine is destination agnostic). A library larger than this is NOT
# truncated: the run pages through successive windows of this size until the whole
# library is covered (SCR-193). The cap only bounds per-window seed/memory work
# and makes progress checkpoints granular for huge libraries.
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

    # --- SCR-193: content-index-deletion guard -----------------------------
    # The ledger (backfill_state.db) and the content index (content_index.db) are
    # separate files. If the index was deleted after a (partial or full) backfill,
    # DONE units would be skipped on resume and the index would stay empty for
    # them. Detect that exact case — the store file is genuinely GONE yet the
    # ledger holds a DONE unit that wrote rows — and reset the ledger so the
    # deleted index is rebuilt from scratch. The rows>0 guard (in
    # has_done_with_rows) avoids a needless reset when the store legitimately never
    # existed (an all-blocked / empty library never creates it, per index_range's
    # empty-store guard). The ``not is_symlink()`` guard scopes this to a true
    # deletion: a present-but-unopenable store (dangling/symlinked/corrupt) is left
    # to index_range's per-unit ``store_available`` gate (SCR-192), which leaves
    # units PENDING without discarding ledger progress — so a transient store
    # problem can never wipe a completed run's bookkeeping.
    store_p = Path(store_path)
    if (
        not store_p.exists()
        and not store_p.is_symlink()
        and ledger.has_done_with_rows()
    ):
        logger.info(
            "backfill: content index missing but ledger has indexed units; "
            "resetting ledger to rebuild from scratch"
        )
        ledger.reset()

    # --- Enumerate recordings (deterministic) ------------------------------
    all_rec_dirs = _enumerate_recordings(Path(recordings_dir))

    # Cache per-recording (classifier, evaluator) + flat screenshot timestamps so
    # they are built ONCE per recording across its chunks (and across windows).
    from screencap.content_index import CrossProcessLockUnavailable

    classifier_cache: dict[str, tuple[object, object]] = {}
    timestamps_cache: dict[str, list[float]] = {}

    # --- Cross-window paging loop (SCR-193 Gap 1) --------------------------
    # Page through the library window-by-window. Each window is the next
    # _MAX_RECORDINGS recordings NOT already covered in the ledger; seed + process
    # it, then advance until no uncovered recording remains (full coverage →
    # COMPLETED) or budget/cancel intervenes. ``is_recording_covered`` is the
    # resume frontier, so a re-run skips finished recordings and reaches the tail
    # — fixing the prior dead-end where a >cap library re-scanned the same first
    # window forever.
    ledger.set_run_state(RunState.RUNNING)
    unit_ordinal = 0  # opaque, monotonic across windows (R9: never a dir name)
    cursor = 0
    n = len(all_rec_dirs)
    while cursor < n:
        # Build the next window of not-yet-covered recordings + their units.
        window_units: list[tuple[str, int]] = []
        window_info: dict[tuple[str, int], tuple[Path, float, float]] = {}
        window_recordings = 0
        while cursor < n and window_recordings < _MAX_RECORDINGS:
            rec_dir = all_rec_dirs[cursor]
            cursor += 1
            # Skip recordings already fully terminal (a prior window/run) so the
            # run advances to the tail rather than re-scanning the same first
            # window. A partial recording (any PENDING chunk) is NOT covered, so
            # it is re-selected and its remaining chunks finish first.
            if ledger.is_recording_covered(rec_dir.name):
                continue
            chunks = _enumerate_chunks(rec_dir, max_frames=max_frames_per_recording)
            if not chunks:
                # No indexable content (no manifests + no screenshots) — nothing
                # to cover; do not consume a window slot.
                continue
            window_recordings += 1
            for chunk_index, start_ts, end_ts in chunks:
                key = (rec_dir.name, chunk_index)
                window_units.append(key)
                window_info[key] = (rec_dir, start_ts, end_ts)

        if not window_units:
            # This slice was all already-covered / empty recordings — keep paging.
            continue

        # Seed this window's closed set (idempotent on resume). The denominator
        # grows per window — cumulative coverage; within a window it is frozen, so
        # progress can't be skewed by survivorship as units complete.
        ledger.seed(window_units)

        for key in window_units:
            # Poll cancel + budget between units (the core also polls per-frame).
            if _cancelled():
                return _finish(RunState.CANCELLED)
            if _budget_exhausted():
                return _finish(RunState.PAUSED)

            status = ledger.unit_status(key[0], key[1])
            if status is not None and status != UnitStatus.PENDING:
                # Already terminal on resume — skip, but still surface progress.
                _emit(unit_ordinal)
                unit_ordinal += 1
                continue

            rec_dir, start_ts, end_ts = window_info[key]
            rec_name = key[0]

            # A retention-evicted / malformed recording (no screenshots or no DB)
            # is a normal SKIPPED, not a FAILED.
            if not (rec_dir / "screenshots").is_dir() or not (
                rec_dir / "recording.db"
            ).is_file():
                ledger.mark(rec_name, key[1], UnitStatus.SKIPPED)
                _emit(unit_ordinal)
                unit_ordinal += 1
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
                # was never marked — so the run is fully resumable) rather than
                # churn through the tail or write without cross-process protection.
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

            _emit(unit_ordinal)
            unit_ordinal += 1

    # --- Terminal disposition ---------------------------------------------
    if _cancelled():
        return _finish(RunState.CANCELLED)
    # No PENDING unit remains → COMPLETED: every seeded unit across every window is
    # terminal AND the paging loop drained the library (or there was nothing to
    # index at all). Keyed on next_pending() rather than is_complete() (which is
    # False on an EMPTY ledger) so an empty library — including one left empty by
    # the content-index-deletion reset above when its recordings are also gone —
    # completes cleanly instead of a churning PAUSED that should_auto_resume would
    # re-run every boot. A unit left PENDING without a bail (e.g. SCR-192 store
    # unavailable) still falls through to PAUSED below.
    if ledger.next_pending() is None:
        return _finish(RunState.COMPLETED)
    # Units remain PENDING but we exited the loop — budget pause.
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
    from screencap.privacy.policy import DEFAULT_TRANSITION_HOLD_SECONDS

    # Per-recording classifier/evaluator (mode resolved from .recording_intent).
    if rec_name not in classifier_cache:
        classifier_cache[rec_name] = build_classifier_evaluator(recording_dir=rec_dir)
    classifier, evaluator = classifier_cache[rec_name]

    # Per-recording flat screenshot timestamps (enables U2's uncovered-gap pass).
    if rec_name not in timestamps_cache:
        timestamps_cache[rec_name] = _screenshot_timestamps(rec_dir)
    screenshot_timestamps = timestamps_cache[rec_name]

    # Derive the skip set over a range biased OUTWARD by the secure-field hold,
    # while index_range below scopes FRAMES to the exact [start_ts, end_ts). This
    # keeps a secure-field event up to ``hold`` before this unit's start — whose
    # mask extends into this unit's first frames — inside the skip set even when
    # the unit is a bounded sub-chunk of a longer recording (SCR-193 Gap 2: the
    # no-manifest fallback is split into ≤max_frames sub-chunks, creating internal
    # boundaries a hold can straddle). Widening only ADDS skip intervals (a
    # superset — never a subset), matching the module's fail-closed contract; for
    # the recording's outer edges it subsumes the old whole-window outward bias.
    skip = derive_skip_intervals(
        rec_dir / "recording.db",
        classifier=classifier,
        evaluator=evaluator,
        time_range=(
            start_ts - DEFAULT_TRANSITION_HOLD_SECONDS,
            end_ts + DEFAULT_TRANSITION_HOLD_SECONDS,
        ),
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


def _enumerate_chunks(
    rec_dir: Path, *, max_frames: int
) -> list[tuple[int, float, float]]:
    """Return ``(chunk_index, start_ts, end_ts)`` units for one recording.

    From the chunk manifests (``sorted(d.glob("chunk_*_manifest.json"))``, reading
    the ``chunk_index``/``chunk_start``/``chunk_end`` keys). When no manifests
    exist, synthesize whole-recording sub-windows from the ``screenshots/*.jpg``
    timestamps, **split into consecutive groups of at most ``max_frames`` frames**
    (SCR-193 Gap 2). Splitting matters because ``index_range`` caps at
    ``max_frames`` distinct frames per range and reports the capped range as a
    *complete* pass — so a single whole-recording window over a long legacy
    recording would be marked DONE with everything past the first ~``max_frames``
    frames permanently un-indexed. Bounding each synthesized sub-window to
    ``max_frames`` frames guarantees no sub-window ever hits the cap, so the whole
    recording is indexed across multiple independently-resumable units.

    The sub-window ranges are contiguous and non-overlapping (each group's
    half-open ``[group[0], next_group[0])``; the final group runs past its last
    frame so that frame is in range), so every frame falls in exactly one unit.
    The secure-field outward bias is applied at skip-derivation time in
    ``_process_unit`` (which widens the derive range by the hold on both ends), so
    a secure-field event straddling a sub-window boundary still skips the right
    frames — it is not handled here.

    REPLACE-overlap ordering invariant: ``index_range`` clears its range with a
    whole-range REPLACE whose ms bounds are ``floor(start*1000)`` /
    ``ceil(end*1000)``, so two adjacent sub-windows sharing a boundary frame ``F``
    can overlap by up to 1ms — group N's REPLACE may delete F (the FIRST frame of
    group N+1) when F's fractional ms rounds down. This is safe because sub-windows
    of one recording are always processed in ASCENDING order and a DONE sub-window
    is never reprocessed before a later one (resume skips DONE units; the Gap-3
    reset reprocesses the whole recording from group 0 up): group N+1 always
    (re)writes F after any earlier overlap-delete. Were that ordering ever broken,
    F could be lost — so keep sub-window processing strictly ascending.
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

    # Fallback: synthesize bounded whole-recording sub-windows from screenshot
    # timestamps, ≤ max_frames frames each (defensive: never a non-positive step).
    timestamps = _screenshot_timestamps(rec_dir)
    if not timestamps:
        return []
    step = max(1, max_frames)
    n = len(timestamps)
    chunks: list[tuple[int, float, float]] = []
    for chunk_index, lo in enumerate(range(0, n, step)):
        hi = min(lo + step, n)
        start_ts = timestamps[lo]
        if hi < n:
            # Internal boundary: half-open up to the next group's first frame, so
            # the two sub-windows are contiguous and never overlap.
            end_ts = timestamps[hi]
        else:
            # Final group: widen past the last frame so it falls in the half-open
            # range (index_range's own end-bias is the +1.0 here).
            end_ts = timestamps[hi - 1] + 1.0
        chunks.append((chunk_index, start_ts, end_ts))
    return chunks


def _screenshot_timestamps(rec_dir: Path) -> list[float]:
    """Return the parsed, sorted flat ``screenshots/*.jpg`` timestamps."""
    from screencap.redaction.geometry import list_screenshot_timestamps

    return list_screenshot_timestamps(rec_dir)


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
