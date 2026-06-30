"""Blocked-frame predicate for ``frame.nearest`` (SCR-186 U6).

Builds the ALLOW-only filter the daemon adapter feeds into
:func:`screencap.frame_resolve.resolve_nearest` so the resolution primitive never
points an agent at a frame the privacy pipeline masked, excluded, or
secure-field-redacted (SCR-186 R8). It preserves the content index's documented
ALLOW-only invariant (``SECURITY.md`` SCR-118) at the resolution layer.

The blocked geometry is **re-derived per request** from the recording's intact
local ``recording.db`` by reusing the backfill skip-interval logic
(:func:`screencap.backfill.skip_intervals.derive_skip_intervals`) rather than
forking it — the same ``SCRUB_BLOCK_ACTIONS`` set, the same fail-closed
uncovered-gap / ambiguity / orphan residual, and the same capture-time
``PrivacyMode``. The per-frame membership test reuses the scrubber's
``find_blocked_interval`` (the exact check ``index_core`` uses), so the
resolve-time skip can never drift from the index-time skip.

**Fail-closed.** When blocked geometry cannot be determined — a missing /
unreadable ``recording.db`` (every frame becomes an uncovered gap) or any failure
building the privacy machinery — the predicate flags *every* frame, so
``frame.nearest`` returns a miss rather than risk surfacing a sensitive frame.
This is the one place the system is deliberately fail-*closed* (the rest of the
content-index path is fail-open): a resolution that could point at a masked frame
is worse than a null.

This module reads ``recording.db`` and imports the privacy/scrubber stack, so it
is **not** importable from the pure :mod:`screencap.frame_resolve` core; the
daemon adapter wires the two together. It does not import ``screencap.daemon``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Sequence

logger = logging.getLogger(__name__)


def _always_blocked(_ts: float) -> bool:
    """Fail-closed sentinel predicate: every frame is treated as blocked."""
    return True


def build_is_blocked(
    recording_dir: Path, frame_tss: Sequence[float]
) -> Callable[[float], bool]:
    """Return ``is_blocked(ts)`` for a recording's frames, fail-closed on error.

    ``frame_tss`` is the epoch-second timestamps of the recording's flat
    ``screenshots/*.jpg`` frames (already parsed by the adapter), needed so the
    re-derivation can add its uncovered-gap and orphan-screenshot residual for
    exactly those frames. The returned predicate answers, for a frame's ``ts``,
    whether it falls in any ``SCRUB_BLOCK_ACTIONS`` (or fail-closed residual)
    interval.

    Heavy imports (scrubber / backfill / privacy) are deferred to the call so
    importing this module stays light.
    """
    recording_dir = Path(recording_dir)
    if not frame_tss:
        # No frames means the predicate is never consulted; an allow-all keeps
        # the contract total without forcing a miss on an empty recording.
        return lambda _ts: False

    try:
        from screencap.backfill.skip_intervals import (
            build_classifier_evaluator,
            derive_skip_intervals,
        )
        from screencap.scrubber import find_blocked_interval

        db_path = recording_dir / "recording.db"
        classifier, evaluator = build_classifier_evaluator(recording_dir)
        start = min(frame_tss)
        # Half-open [start, end): pad the end so the last frame is in range.
        end = max(frame_tss) + 1.0
        intervals = derive_skip_intervals(
            db_path,
            classifier=classifier,
            evaluator=evaluator,
            time_range=(start, end),
            screenshot_timestamps=list(frame_tss),
        )
        # derive_skip_intervals returns a merged, start-sorted list; pre-build the
        # starts list for the bisect-backed membership test (mirrors index_core).
        starts = [iv.start for iv in intervals]

        def is_blocked(ts: float) -> bool:
            return find_blocked_interval(ts, intervals, starts) is not None

        return is_blocked
    except Exception:
        logger.warning(
            "frame.nearest: blocked-interval derivation failed for %s; failing "
            "closed (all frames treated as blocked)",
            recording_dir.name,
            exc_info=True,
        )
        return _always_blocked
