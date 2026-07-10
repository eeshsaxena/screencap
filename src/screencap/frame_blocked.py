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
unreadable ``recording.db`` (every frame becomes an uncovered gap), any failure
building the privacy machinery, or a *partial* ``recording.db`` read that
under-builds the canonical block set (SCR-198: ``derive_skip_intervals`` is
called with ``require_canonical=True`` so it raises rather than silently
returning the under-blocked set) — the predicate flags *every* frame, so
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
    recording_dir: Path,
    frame_tss: Sequence[float],
    *,
    screenshot_residuals: bool = True,
) -> Callable[[float], bool]:
    """Return ``is_blocked(ts)`` for a recording's frames, fail-closed on error.

    ``frame_tss`` is the epoch-second timestamps the caller wants to test. The
    returned predicate answers, for a ``ts``, whether it falls in any
    ``SCRUB_BLOCK_ACTIONS`` (or fail-closed residual) interval. Heavy imports
    (scrubber / backfill / privacy) are deferred to the call so importing this
    module stays light.

    ``screenshot_residuals`` selects which interval set the predicate tests:

    * ``True`` (default, the ``frame.nearest`` contract) — ``frame_tss`` are the
      recording's flat ``screenshots/*.jpg`` timestamps, and the derivation adds
      its uncovered-gap and orphan-screenshot residual for exactly those files,
      validating on-disk screenshots.
    * ``False`` (the recall evidence-strip) — the caller's timestamps are NOT
      on-disk screenshot files (timeline evidence carries ``window_event`` times,
      transcript evidence carries chunk-start times), so the screenshot-file
      residuals are inapplicable and would false-positive every item. Test
      membership against the **genuine** privacy set only (canonical
      ``SCRUB_BLOCK_ACTIONS`` + ambiguity + secure-field); ``frame_tss`` still
      bounds the derivation time range. ``require_canonical`` is preserved, so a
      missing/partial ``recording.db`` still fails closed (all blocked).
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
            # Screenshot-file residuals (uncovered-gap + orphan-screenshot) are
            # added ONLY when the caller's timestamps are actual on-disk frames.
            # For non-frame evidence timestamps (screenshot_residuals=False), pass
            # None so only the genuine canonical/ambiguity/secure-field intervals
            # apply — the file residuals would otherwise flag every item.
            screenshot_timestamps=list(frame_tss) if screenshot_residuals else None,
            # Fail closed on a partial canonical read: build_scrub_context empties
            # the canonical set gracefully (no raise) on a partial recording.db
            # read, which is indistinguishable from a genuine all-ALLOW recording.
            # require_canonical makes derive_skip_intervals raise instead, so the
            # except below maps it to the all-blocked sentinel rather than
            # surfacing an under-blocked frame (SCR-198). Preserved on BOTH
            # residual modes — the fail-closed guarantee is independent of them.
            require_canonical=True,
        )
        # derive_skip_intervals returns a merged, start-sorted list; pre-build the
        # starts list for the bisect-backed membership test (mirrors index_core).
        starts = [iv.start for iv in intervals]

        def is_blocked(ts: float) -> bool:
            return find_blocked_interval(ts, intervals, starts) is not None

        return is_blocked
    except Exception:
        # MUST stay broad enough to catch CanonicalDerivationError — that is the
        # signal a partial canonical read raises (require_canonical=True above),
        # and mapping it to _always_blocked is what fails closed (SCR-198). Do not
        # narrow this to a specific type set without keeping that error in it.
        # (CanonicalDerivationError is not imported at module top on purpose: that
        # would pull the heavy skip_intervals/scrubber stack into this otherwise-
        # light module — so it is matched via Exception, not by name.)
        logger.warning(
            "frame.nearest: blocked-interval derivation failed for %s; failing "
            "closed (all frames treated as blocked)",
            recording_dir.name,
            exc_info=True,
        )
        return _always_blocked
