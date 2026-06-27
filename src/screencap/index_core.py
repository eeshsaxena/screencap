"""Shared content-index core: frame-selection → dedup → OCR → write_chunk.

SCR-178 U1. This is the single implementation of the on-screen-text indexing
rules that BOTH the live ``chunk_processor`` pass and the (future) backfill
engine call, so the skip/index policy can never drift between the two paths
(structural R2 parity).

:func:`index_range` is a pure, dependency-injected helper: the OCR engine, the
content-index store *path*, the stop event, and the budgets are all passed in, so
it can be exercised with a fake OCR + a temp store path without any real Apple
Vision or recording pipeline. It owns:

- glob + ``parse_screenshot_timestamp`` time-scoping to ``[start_ts, end_ts)``;
- per-frame ``find_blocked_interval`` skip against the caller's ``skip_intervals``
  (the live caller passes ``scrub_result.blocked_intervals``; the backfill passes
  its re-derived set);
- ``dhash``/``hamming_distance`` consecutive-frame dedup;
- ``VisionOcr.recognize`` → ``IndexFrame`` build;
- the ``content_index_write_lock()`` + store lifecycle + unlink-before-write
  barrier + ``write_chunk``.

:func:`index_range` now owns the store lifecycle, including the **empty-store
guard**: it opens (and thus creates) the store lazily, INSIDE
``content_index_write_lock()`` and only when there is something to write — so an
empty / fully-blocked range whose store does not yet exist never creates an empty
PII store. This restores the pre-refactor behavior (the inline body guarded
``if not frames and not store_path.exists(): return``) and matters for the U4
backfill, which reuses ``index_range`` over fully-blocked old recordings.

The caller decides *whether* to index (the live path keeps its
``has_masking_context`` fail-closed guard and its ``content_index_enabled`` gate;
the backfill keeps its own gating) and *where* ``skip_intervals`` come from. This
module assumes that decision has already been made — it only runs the loop.

R9 (resurrection barrier): the OCR reads happen OUTSIDE the content-index write
lock, so a retroactive "disable this app" purge (``scrub_worker``) can unlink a
screenshot while OCR is in flight. After acquiring ``content_index_write_lock()``
and immediately BEFORE ``write_chunk``, each surviving frame's backing screenshot
is re-``stat``'d and dropped if it was unlinked, so an interleaved purge can never
be resurrected. The lock is held across (re-confirm + write) so the purge can't
unlink mid-write.
"""

from __future__ import annotations

import bisect
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import threading

    from screencap.scrubber import BlockedInterval

logger = logging.getLogger(__name__)

# SCR-118 content-index OCR pass tuning (moved here from chunk_processor so the
# live path and the backfill share ONE source of truth; chunk_processor
# re-imports these names).
#
# dHash threshold matching mask_screenshots' OCR-dedup (a false cache hit only
# costs reused recognised text, so the tight value is intentional).
_INDEX_DHASH_THRESHOLD = 5
# Hard cap on OCR calls per range so a pathological action-burst can never stall
# the capture-adjacent thread / upload. Excess frames are dropped with a log line
# (no silent truncation).
_INDEX_MAX_OCR_FRAMES = 240
# Wall-clock budget for the per-range OCR pass — a second backstop (alongside the
# frame cap and the stop-event check) so a slow/wedged OCR engine can never sit in
# front of this chunk's upload indefinitely.
_INDEX_OCR_BUDGET_S = 30.0


class _OcrEngine(Protocol):
    """Minimal OCR surface used here — ``VisionOcr`` and the test fake both fit."""

    def recognize(self, image_path: Path, **kwargs: object) -> object: ...


@dataclass(frozen=True)
class IndexRangeResult:
    """Outcome of one :func:`index_range` pass.

    ``rows_written`` is the count :meth:`ContentIndex.write_chunk` reported (0 when
    nothing survived skip/dedup/OCR, or when the write was short-circuited because
    there was nothing to index and no store yet).

    ``completed_range`` is ``True`` only when the frame loop ran to completion over
    the entire ``[start_ts, end_ts)`` candidate set; it is ``False`` when the loop
    bailed early on the stop event or the wall-clock budget. Callers (the backfill
    ledger, U4) gate a chunk's DONE transition on ``completed_range`` so a
    mid-range bail re-runs cleanly via the whole-range replace.
    """

    rows_written: int
    completed_range: bool


def index_range(
    capture_dir: Path,
    start_ts: float,
    end_ts: float,
    skip_intervals: "list[BlockedInterval]",
    *,
    ocr: _OcrEngine,
    store_path: Path,
    stop_event: "threading.Event | None" = None,
    budget_s: float = _INDEX_OCR_BUDGET_S,
    max_frames: int = _INDEX_MAX_OCR_FRAMES,
    dhash_threshold: int = _INDEX_DHASH_THRESHOLD,
) -> IndexRangeResult:
    """OCR the screenshots in ``[start_ts, end_ts)`` into the store at ``store_path``.

    Time-scopes ``capture_dir/screenshots/*.jpg`` to the half-open window, drops
    every frame inside any ``skip_intervals`` entry, dedups near-identical
    consecutive frames, OCRs the survivors, and replaces the whole chunk
    time-range in the store (so a re-process drops stale rows).

    Owns the store lifecycle and the **empty-store guard**: the store at
    ``store_path`` is opened lazily INSIDE ``content_index_write_lock()`` and only
    when there is something to write — if no frames survived AND the store file
    does not already exist, the write is skipped entirely so an empty / fully
    blocked range never creates an empty PII store (matching the pre-refactor
    inline body's ``if not frames and not store_path.exists(): return`` guard; the
    U4 backfill relies on this over fully-blocked old recordings).

    Returns an :class:`IndexRangeResult`. The caller owns the policy decisions:
    whether to index at all and where ``skip_intervals`` come from.
    """
    from PIL import Image

    from screencap.content_index import (
        ContentIndex,
        IndexFrame,
        content_index_write_lock,
    )
    from screencap.engine.dedup import dhash, hamming_distance
    from screencap.redaction.geometry import parse_screenshot_timestamp
    from screencap.scrubber import find_blocked_interval

    capture_dir = Path(capture_dir)
    screenshots_dir = capture_dir / "screenshots"
    if not screenshots_dir.is_dir():
        return IndexRangeResult(rows_written=0, completed_range=True)

    # ``skip_intervals`` is sorted by start (built via build_blocked_intervals /
    # merge_intervals), so reuse the scrubber's bisect-backed lookup with a
    # pre-built starts list instead of an O(n) per-frame linear scan. It also
    # handles overlapping intervals (a short secure-field nested in a long
    # MASK_WINDOW) correctly.
    skip_starts = [iv.start for iv in skip_intervals]

    def _skipped(ts: float) -> bool:
        return find_blocked_interval(ts, skip_intervals, skip_starts) is not None

    # Time-scope to [start_ts, end_ts); drop policy-flagged frames. Parse each
    # filename's timestamp and sort by it (not by lexical name, which is only
    # monotonic when the integer part has a fixed width), then bisect to the
    # window: take only [start_ts, end_ts) and stop at the upper bound rather than
    # scanning every later screenshot in the recording.
    parsed: list[tuple[float, Path]] = []
    for img_path in screenshots_dir.glob("*.jpg"):
        ts = parse_screenshot_timestamp(img_path.name)
        if ts is not None:
            parsed.append((ts, img_path))
    parsed.sort(key=lambda p: p[0])

    ts_keys = [ts for ts, _ in parsed]
    lo = bisect.bisect_left(ts_keys, start_ts)
    candidates: list[tuple[float, Path]] = []
    for ts, img_path in parsed[lo:]:
        if ts >= end_ts:
            break
        if _skipped(ts):
            continue
        candidates.append((ts, img_path))

    # Nothing in range → nothing to index AND nothing of ours to clear in this
    # range: return without touching the store (mirrors the live path's
    # ``if not candidates: return``, which kept an empty range from creating an
    # empty PII store).
    if not candidates:
        return IndexRangeResult(rows_written=0, completed_range=True)

    # The ``max_frames`` cap is deterministic (a re-run truncates identically), so
    # a capped range is still a "complete" pass — the dropped tail is never going
    # to be indexed and re-running would not recover it. Only a stop/budget bail
    # (non-deterministic, recoverable on resume) clears ``completed_range``.
    if len(candidates) > max_frames:
        logger.info(
            f"content-index OCR capping at {max_frames} of {len(candidates)} frames"
        )
        candidates = candidates[:max_frames]

    # SCR-134: hold the shared content-index write lock across the OCR READS and
    # the write. The lock pairs with scrub_worker's retroactive-disable purge so
    # the two can't interleave: if a disable lands mid-pass, either this write
    # completes first and the purge then deletes it, or the purge (which unlinks
    # the screenshots before taking the lock) runs first and the OCR reads below
    # then find the files already gone — so just-disabled text can never be
    # re-indexed after a purge. The loop's own stop / budget bails bound how long
    # this is held.
    with content_index_write_lock():
        started = time.perf_counter()
        prev_hash: int | None = None
        frames: list[IndexFrame] = []
        bailed = False
        for ts, img_path in candidates:
            # This pass may run synchronously before a chunk's upload, so a
            # force-stop or a slow/wedged OCR engine must not pin the
            # capture-adjacent thread: bail on stop, and on a wall-clock budget.
            if stop_event is not None and stop_event.is_set():
                bailed = True
                break
            if time.perf_counter() - started > budget_s:
                logger.info("content-index OCR budget hit; stopping early")
                bailed = True
                break
            # Dedup near-identical consecutive frames (same primitive + tight
            # threshold mask_screenshots uses); keep prev_hash on a dup so we
            # compare against the last distinct frame.
            cur_hash: int | None = None
            try:
                with Image.open(img_path) as im:
                    cur_hash = dhash(im)
            except Exception:
                cur_hash = None
            if (
                cur_hash is not None
                and prev_hash is not None
                and hamming_distance(cur_hash, prev_hash) <= dhash_threshold
            ):
                continue
            if cur_hash is not None:
                prev_hash = cur_hash
            try:
                result = ocr.recognize(img_path)
            except Exception:
                continue
            text = " ".join(b.text for b in result.text_blocks if b.text).strip()
            if text:
                frames.append(
                    IndexFrame(timestamp_ms=int(round(ts * 1000)), text=text)
                )

        # A bail (stop/budget) means the loop did NOT cover the entire range — the
        # caller must not mark this range DONE (a resume re-OCRs it via the
        # whole-range replace). A clean pass (incl. a deterministic max-frames cap)
        # is complete.
        completed_range = not bailed

        # R9 unlink-before-write barrier: the OCR reads above happened while the
        # purge could have unlinked a now-disabled app's screenshots. Re-confirm
        # each surviving frame's backing file still exists (we hold the write lock,
        # so the purge can't unlink mid-write) and drop any that were unlinked, so
        # an interleaved purge is never resurrected. ``frames`` and ``candidates``
        # share order (frames is a filtered subsequence), so walk candidates and
        # keep only frames whose file survives.
        if frames:
            path_by_ms: dict[int, Path] = {}
            for ts, img_path in candidates:
                path_by_ms.setdefault(int(round(ts * 1000)), img_path)
            surviving: list[IndexFrame] = []
            for fr in frames:
                backing = path_by_ms.get(fr.timestamp_ms)
                if backing is not None and not backing.exists():
                    logger.info(
                        "content-index: dropping frame whose screenshot was "
                        "unlinked during OCR (purge race)"
                    )
                    continue
                surviving.append(fr)
            frames = surviving

        # Replace the WHOLE chunk time-range (not just the surviving frames) so a
        # re-process where a frame is now skipped (policy/dedup) drops its stale,
        # less-redacted row instead of leaving it behind. Range is [start, end) in
        # ms, widened (floor/ceil) to cover any rounded frame timestamp.
        #
        # Empty-store guard (pre-refactor behavior): if nothing survived AND the
        # store file does not already exist, skip the write entirely — never open
        # (and thus create) an empty PII store for an empty / fully-blocked range.
        # Otherwise open the store HERE (inside the write lock, matching SCR-134's
        # original lock-scope) and write; ``store.available`` is False when it could
        # not be opened (missing/corrupt/symlinked), in which case there is nothing
        # to write to.
        start_ms = math.floor(start_ts * 1000)
        end_ms = math.ceil(end_ts * 1000)
        rows_written = 0
        if frames or store_path.exists():
            with ContentIndex(store_path) as store:
                if store.available:
                    rows_written = store.write_chunk(
                        capture_dir.name, start_ms, end_ms, frames
                    )
        logger.info(
            f"content-indexed {len(frames)} frames in "
            f"{time.perf_counter() - started:.2f}s"
        )
    return IndexRangeResult(rows_written=rows_written, completed_range=completed_range)
