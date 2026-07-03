"""Day-scoped read surface for the Day timeline (U3).

For a local calendar day, return each recording's span intersecting the day plus
its blocked intervals split into two honesty classes (R7):

* ``blocked_proven`` — explicit ``SCRUB_BLOCK_ACTIONS`` (MASK/EXCLUDE/secure-field)
  policy intervals read from an INTACT local ``recording.db``. Provably
  nothing-capturable at those times; the UI may hatch these "blocked".
* ``unverifiable`` — deleted-row coverage gaps (a screenshot with no covering
  ``window_event``, an orphan screenshot) or a NULL policy-relevant column
  (classification ambiguity). Fail-closed for capture decisions, but NOT proof
  nothing was captured — the UI must render these as neutral gaps, never labelled
  "blocked".

Local-only and read-only. The proven/unverifiable split reuses
``backfill.skip_intervals.derive_skip_intervals`` (the same reader the SCR-178
backfill and ``frame.nearest`` use) so the classes can't drift from the
capture-time block semantics: the canonical ``SCRUB_BLOCK_ACTIONS`` intervals are
``blocked_proven`` and the fail-closed residual reasons are ``unverifiable``.
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from screencap.backfill.skip_intervals import (
    AMBIGUOUS_BROWSER_URL,
    AMBIGUOUS_SECURE_FIELD,
    AMBIGUOUS_TITLE,
    ORPHAN_SCREENSHOT,
    UNCOVERED_GAP,
    build_classifier_evaluator,
    derive_skip_intervals,
)

logger = logging.getLogger(__name__)

# The fail-closed residual reasons ``skip_intervals`` adds on top of the canonical
# ``SCRUB_BLOCK_ACTIONS`` set. An interval carrying any of these is UNVERIFIABLE
# (we can't prove nothing was captured); every other reason is a proven block.
_UNVERIFIABLE_REASONS = frozenset(
    {
        UNCOVERED_GAP,
        ORPHAN_SCREENSHOT,
        AMBIGUOUS_BROWSER_URL,
        AMBIGUOUS_TITLE,
        AMBIGUOUS_SECURE_FIELD,
    }
)

_DAY_SECONDS = 86400


class InvalidDayRequest(ValueError):
    """The date string is malformed (mapped to a typed 400, never a 500)."""


def day_bounds(date_str: str, tz_offset_seconds: int) -> tuple[float, float]:
    """``[day_start, day_end)`` as UTC epoch seconds for a local calendar day.

    ``tz_offset_seconds`` is seconds EAST of UTC (``TimeZone.secondsFromGMT()`` on
    the Swift side), so midnight local on ``date_str`` maps to the UTC epoch of
    that Y-M-D at 00:00 minus the offset.
    """
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError) as exc:
        raise InvalidDayRequest(f"invalid date {date_str!r}: {exc}") from exc
    day_start = calendar.timegm(d.timetuple()) - tz_offset_seconds
    return float(day_start), float(day_start + _DAY_SECONDS)


def _screenshot_timestamps(rec_dir: Path) -> list[float]:
    """Parsed, sorted flat ``screenshots/*.jpg`` timestamps.

    Feeds ``derive_skip_intervals``' uncovered-gap / orphan passes. Mirrors
    ``backfill.engine._screenshot_timestamps`` but stays a light local reader
    (reusing the canonical filename parser) so this read surface does not pull in
    the OCR-heavy backfill engine.
    """
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


def _clip_ms(
    start_s: float, end_s: float, win_start_s: float, win_end_s: float
) -> tuple[int, int] | None:
    """Clip ``[start_s, end_s)`` to the day window; ``(start_ms, end_ms)`` or None
    when the clipped interval is empty."""
    lo = max(start_s, win_start_s)
    hi = min(end_s, win_end_s)
    if hi <= lo:
        return None
    return int(round(lo * 1000)), int(round(hi * 1000))


def _blocked_intervals(
    rec_dir: Path, db_path: Path, win_start: float, win_end: float
) -> tuple[list[dict[str, int]], list[dict[str, int]]]:
    """Return ``(blocked_proven, unverifiable)`` interval lists for the day window.

    Strictly fail-open: any derivation error yields empty lists (a read surface
    must never 500 on one bad recording.db). The proven/unverifiable split is by
    the interval's ``reason`` (residual reasons → unverifiable).
    """
    proven: list[dict[str, int]] = []
    unverifiable: list[dict[str, int]] = []
    try:
        classifier, evaluator = build_classifier_evaluator(rec_dir)
        shots = [t for t in _screenshot_timestamps(rec_dir) if win_start <= t < win_end]
        intervals = derive_skip_intervals(
            db_path,
            classifier=classifier,
            evaluator=evaluator,
            time_range=(win_start, win_end),
            screenshot_timestamps=shots or None,
            require_canonical=False,  # fail-OPEN read surface
        )
    except Exception:
        logger.warning(
            "day_segments: skip-interval derivation failed for %s", rec_dir, exc_info=True
        )
        return proven, unverifiable

    for iv in intervals:
        clipped = _clip_ms(iv.start, iv.end, win_start, win_end)
        if clipped is None:
            continue
        entry = {"start_ms": clipped[0], "end_ms": clipped[1]}
        if iv.reason in _UNVERIFIABLE_REASONS:
            unverifiable.append(entry)
        else:
            proven.append(entry)
    return proven, unverifiable


def day_segments(
    date_str: str,
    tz_offset_seconds: int = 0,
    recordings_dir: Path | None = None,
) -> dict[str, Any]:
    """Return the day-scoped read surface (see module docstring).

    Shape::

        {"date": "YYYY-MM-DD", "recordings": [
            {"name", "recording_id", "state",
             "start_ms", "end_ms",            # span clamped to the day
             "blocked_proven": [{"start_ms","end_ms"}, ...],
             "unverifiable":   [{"start_ms","end_ms"}, ...]},
            ...
        ]}

    A recording that spans midnight appears in BOTH days, clamped to each. Raises
    :class:`InvalidDayRequest` on a malformed ``date_str`` (the handler maps that
    to a typed 400).
    """
    from screencap import catalog

    win_start, win_end = day_bounds(date_str, tz_offset_seconds)  # may raise InvalidDayRequest

    if recordings_dir is None:
        recordings_dir = catalog.get_recordings_dir()
    recordings_dir = Path(recordings_dir)

    result: dict[str, Any] = {"date": date_str, "recordings": []}
    if not recordings_dir.exists():
        return result

    # Reuse U2's derived state / stable id (one scan). Keyed by directory name.
    meta_by_name = {r.name: r for r in catalog.list_recordings(recordings_dir)}

    for d in sorted(recordings_dir.iterdir()):
        if not d.is_dir():
            continue
        db = catalog.find_db(d)
        if db is None:
            continue
        started, duration, _status = catalog._read_recording_meta(db)
        if started is None:
            continue
        end = started + (duration or 0.0)

        # Half-open overlap with the day window (a midnight-spanning recording
        # overlaps both days and is clamped into each).
        if not (started < win_end and end >= win_start):
            continue
        clamped_start = max(started, win_start)
        clamped_end = max(min(end, win_end), clamped_start)

        proven, unverifiable = _blocked_intervals(d, db, clamped_start, clamped_end)

        meta = meta_by_name.get(d.name)
        result["recordings"].append(
            {
                "name": d.name,
                "recording_id": meta.recording_id if meta else None,
                "state": meta.state if meta else "ready",
                "start_ms": int(round(clamped_start * 1000)),
                "end_ms": int(round(clamped_end * 1000)),
                "blocked_proven": proven,
                "unverifiable": unverifiable,
            }
        )
    return result
