"""Scrub-time window-geometry readers for the post-hoc redaction pipeline.

Bridges screenshots to app/window context via timestamp correlation, reading
the local-only ``recording.db``. This is the DB-dependent, scrub-only slice
split out of the original privacy ``context`` module; it carries the
``screencap.recording_db`` dependency and therefore lives in
``screencap.redaction`` (never imported by the capture path).

Owns:
- screenshot timestamp parsing
- nearest-event lookup (bisect-based)
- ``window_event`` / ``window_geometry`` DB loaders
- per-chunk geometry coverage-signal readers (sample timestamps, capture
  failures) used by the post-hoc video masker
- high-level screenshot → ``FrameMetadata`` association

Pure classification (``DefaultContextClassifier``, bundle/domain maps,
``domain_from_url``) lives in the shared ``screencap.privacy.classify``
module; this module imports only ``domain_from_url`` from there.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from pathlib import Path

from screencap.privacy.classify import domain_from_url
from screencap.privacy.policy import FrameMetadata
from screencap.recording_db import Connection, has_column, has_table, open_recording_db

# ---------------------------------------------------------------------------
# Screenshot timestamp parsing
# ---------------------------------------------------------------------------


def parse_screenshot_timestamp(filename: str) -> float | None:
    """Extract Unix timestamp from a screenshot filename or path.

    Handles bare filenames (``1709745600.123456.jpg``), DB image_path values
    with a directory prefix (``screenshots/1709745600.123456.jpg``), and the
    encrypted-corpus form (``1709745600.123456.jpg.enc``).
    Returns None if the name doesn't match the expected format.
    """
    # Strip directory prefix — image_path in the DB is "screenshots/{ts}.jpg"
    basename = filename.rsplit("/", 1)[-1] if "/" in filename else filename
    if basename.endswith(".enc"):
        basename = basename[: -len(".enc")]
    if basename.endswith(".jpg"):
        stem = basename[: basename.rfind(".jpg")]
    elif basename.endswith(".jpeg"):
        stem = basename[: basename.rfind(".jpeg")]
    else:
        return None
    try:
        return float(stem)
    except ValueError:
        return None


def list_screenshot_timestamps(rec_dir: Path) -> list[float]:
    """Return the parsed, sorted flat ``screenshots/*.jpg`` timestamps for a
    recording dir (the uncovered-gap / orphan-frame pass inputs).

    Shared by the SCR-178 backfill and the U3 day-timeline surface so the flat
    frame enumeration can't drift; lives here (a light module) rather than in the
    OCR-heavy backfill engine.

    Globs both the plaintext ``*.jpg`` and the encrypted-corpus ``*.jpg.enc``
    forms (mirrors ``frame_resolve``): under ``corpus_encrypted`` every still is
    ``.jpg.enc``, and a ``.jpg``-only glob would silently drop the frame-presence
    signal for the whole library. A frame present as both forms mid-migration
    counts once.
    """
    shots = rec_dir / "screenshots"
    if not shots.is_dir():
        return []
    seen: set[float] = set()
    for pattern in ("*.jpg", "*.jpg.enc"):
        for img in shots.glob(pattern):
            ts = parse_screenshot_timestamp(img.name)
            if ts is not None:
                seen.add(ts)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Nearest-event lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowContext:
    """A window_event row relevant for context association."""

    timestamp: float
    app_bundle_id: str
    title: str
    window_id: str = ""
    domain: str | None = None
    browser_url: str | None = None


def _active_at_index(timestamps: list[float], target: float) -> int | None:
    """Return index of the latest timestamp at or before target.

    Window events represent state transitions, so the active
    state at any time T is the most recent event with timestamp <= T.
    Returns None if no event is at or before target.
    """
    if not timestamps:
        return None
    pos = bisect.bisect_right(timestamps, target)
    if pos == 0:
        return None
    return pos - 1


def find_nearest_window(
    window_events: list[WindowContext],
    target_ts: float,
    max_delta: float = 5.0,
    _timestamps: list[float] | None = None,
) -> WindowContext | None:
    """Find the window_event active at target_ts within max_delta seconds.

    Uses "latest at or before" semantics since window events represent
    state transitions — the active window at time T is the most recent
    event with timestamp <= T.

    Pass _timestamps to avoid rebuilding the list on every call.
    """
    if not window_events:
        return None
    if _timestamps is None:
        _timestamps = [w.timestamp for w in window_events]
    idx = _active_at_index(_timestamps, target_ts)
    if idx is None:
        return None
    if abs(window_events[idx].timestamp - target_ts) > max_delta:
        return None
    return window_events[idx]


# ---------------------------------------------------------------------------
# DB loaders (raw sqlite3, consistent with screencap layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowGeometrySnapshot:
    """Per-screenshot window geometry with display context."""

    windows: list[dict]
    display_origin: tuple[float, float] = (0.0, 0.0)


def _load_geometry_row(
    conn: Connection, screenshot_timestamp: float,
) -> WindowGeometrySnapshot | None:
    """Read the window_geometry row nearest to ``screenshot_timestamp``.

    Skips the ``has_table`` check; the caller must have already verified
    the table exists. Used by ``mask_screenshots`` to amortise that check
    across a per-screenshot loop. Standalone callers should use
    ``load_window_geometry``.
    """
    try:
        # Tolerance-based lookup: screenshot filenames lose float precision
        # (e.g. 1773413585.910469 vs DB 1773413585.9104693). 1ms tolerance is
        # safely within a single screenshot interval.
        row = conn.execute(
            "SELECT window_list_json FROM window_geometry "
            "WHERE abs(screenshot_timestamp - ?) < 0.001 "
            "ORDER BY abs(screenshot_timestamp - ?) LIMIT 1",
            (screenshot_timestamp, screenshot_timestamp),
        ).fetchone()
    except Exception:
        return None
    if row is None or row[0] is None:
        return None

    try:
        data = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None

    # Handle both formats:
    # New: {"windows": [...], "display_bounds": [x, y, w, h]}
    # Legacy: [...] (plain list of window dicts)
    if isinstance(data, dict):
        windows = data.get("windows", [])
        bounds = data.get("display_bounds")
        origin = (float(bounds[0]), float(bounds[1])) if bounds else (0.0, 0.0)
    else:
        windows = data
        origin = (0.0, 0.0)

    return WindowGeometrySnapshot(windows=windows, display_origin=origin)


def load_window_geometry(
    db_path: Path,
    screenshot_timestamp: float,
    conn: Connection | None = None,
) -> WindowGeometrySnapshot | None:
    """Load the window geometry snapshot for a screenshot timestamp.

    Queries the ``window_geometry`` table for the nearest timestamp match.
    Returns a ``WindowGeometrySnapshot`` or ``None`` if unavailable
    (old recordings without the table, capture failure, etc.).

    Args:
        db_path: Path to the recording database.
        screenshot_timestamp: Exact timestamp to look up.
        conn: Optional open connection to reuse — avoids re-opening the
            DB when loading geometry for many screenshots from one caller.
            Each call still runs ``has_table``; loops that load geometry
            for hundreds of screenshots want ``_load_geometry_row``
            (private) with a hoisted ``has_table`` check instead.
    """
    if conn is not None:
        if not has_table(conn, "window_geometry"):
            return None
        return _load_geometry_row(conn, screenshot_timestamp)

    try:
        with open_recording_db(db_path) as own_conn:
            if not has_table(own_conn, "window_geometry"):
                return None
            return _load_geometry_row(own_conn, screenshot_timestamp)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# U4a coverage signal — read API for the post-hoc video masker (U6)
# ---------------------------------------------------------------------------
#
# U6 implements a three-way per-chunk coverage gate over a chunk's frame span
# [start_ts, end_ts]:
#   (a) geometry proves no sensitive window across the full span -> unmasked
#       copy valid;
#   (b) geometry is absent / sparse for some interval (inter-sample gap exceeds
#       a bounded max, OR a capture insert failed) -> UNPROVABLE, fail closed;
#   (c) geometry shows sensitive windows -> mask conservatively.
#
# U4a supplies the two raw inputs that gate distinguishes (b) on; U6 owns the
# gate logic (the bounded max-gap, the over-mask policy, the fail-closed
# decision). These read directly from the local-only ``recording.db``, so the
# signal never leaves the machine (R8 / U2).


def list_geometry_sample_timestamps(
    db_path: Path,
    start_ts: float,
    end_ts: float,
    conn: Connection | None = None,
) -> list[float]:
    """List ``window_geometry`` sample timestamps within ``[start_ts, end_ts]``.

    Returns the ``screenshot_timestamp`` of every geometry sample whose
    timestamp falls inside the (inclusive) span, sorted ascending. U6
    computes inter-sample gaps from these to detect sparse coverage (its
    case-b bounded-max-gap check); an empty list means no geometry at all
    was sampled across the span.

    Returns ``[]`` (without raising) for recordings that pre-date the
    ``window_geometry`` table or on any read error — U6 treats absence as
    unprovable coverage (fail closed), so a degraded read is conservatively
    safe.

    Args:
        db_path: Path to the recording database.
        start_ts: Inclusive start of the chunk frame span.
        end_ts: Inclusive end of the chunk frame span.
        conn: Optional open connection to reuse across many chunk spans.
    """

    def _query(c: Connection) -> list[float]:
        if not has_table(c, "window_geometry"):
            return []
        rows = c.execute(
            "SELECT screenshot_timestamp FROM window_geometry "
            "WHERE screenshot_timestamp IS NOT NULL "
            "AND screenshot_timestamp >= ? AND screenshot_timestamp <= ? "
            "ORDER BY screenshot_timestamp",
            (start_ts, end_ts),
        ).fetchall()
        return [float(r[0]) for r in rows]

    if conn is not None:
        return _query(conn)
    try:
        with open_recording_db(db_path) as own_conn:
            return _query(own_conn)
    except Exception:
        return []


def geometry_capture_failures_in_span(
    db_path: Path,
    start_ts: float,
    end_ts: float,
    conn: Connection | None = None,
) -> list[float]:
    """List geometry-capture-failure timestamps within ``[start_ts, end_ts]``.

    Returns the ``screenshot_timestamp`` of every durable
    ``window_geometry_capture_failure`` marker (written by
    ``recorder.write_screen_event`` when an ``insert_window_geometry`` call
    raised) whose timestamp falls inside the (inclusive) span, sorted
    ascending. A NON-EMPTY result means a geometry insert is known to have
    failed during the span, so coverage there is unprovable — U6 must treat
    the chunk as case (b) and fail closed (mark it ``FAILED``, produce NO
    masked copy), never emitting an effectively-unmasked cloud video.

    Returns ``[]`` (without raising) for recordings that pre-date the
    ``window_geometry_capture_failure`` table or on any read error. Note the
    asymmetry vs. ``list_geometry_sample_timestamps``: an empty result here
    means "no KNOWN failure", which is necessary-but-not-sufficient for case
    (a) — U6 must ALSO confirm dense sample coverage via the sample
    timestamps before treating a span as provably covered.

    Args:
        db_path: Path to the recording database.
        start_ts: Inclusive start of the chunk frame span.
        end_ts: Inclusive end of the chunk frame span.
        conn: Optional open connection to reuse across many chunk spans.
    """

    def _query(c: Connection) -> list[float]:
        if not has_table(c, "window_geometry_capture_failure"):
            return []
        rows = c.execute(
            "SELECT screenshot_timestamp FROM window_geometry_capture_failure "
            "WHERE screenshot_timestamp IS NOT NULL "
            "AND screenshot_timestamp >= ? AND screenshot_timestamp <= ? "
            "ORDER BY screenshot_timestamp",
            (start_ts, end_ts),
        ).fetchall()
        return [float(r[0]) for r in rows]

    if conn is not None:
        return _query(conn)
    try:
        with open_recording_db(db_path) as own_conn:
            return _query(own_conn)
    except Exception:
        return []


def list_muted_intervals_in_span(
    db_path: Path,
    start_ts: float,
    end_ts: float,
    conn: Connection | None = None,
) -> list[tuple[float, float | None]]:
    """List muted spans (SCR-218 U3) overlapping the chunk span ``[start_ts,
    end_ts]``, as ``(start, end)`` recording-relative tuples sorted ascending.

    An open interval (still muted at read time — ``end_ts`` NULL) is returned
    with ``end`` as ``None``; U6 treats it as muted to chunk end. A row overlaps
    the span when its start is at/before ``end_ts`` and it has no end or its end
    is at/after ``start_ts``. Returns ``[]`` (without raising) for recordings
    predating the ``muted_intervals`` table or on any read error — a recording
    with no mutes simply has no rows.

    Args:
        db_path: Path to the recording database.
        start_ts: Inclusive start of the chunk frame span.
        end_ts: Inclusive end of the chunk frame span.
        conn: Optional open connection to reuse across many chunk spans.
    """

    def _query(c: Connection) -> list[tuple[float, float | None]]:
        if not has_table(c, "muted_intervals"):
            return []
        rows = c.execute(
            "SELECT start_ts, end_ts FROM muted_intervals "
            "WHERE start_ts <= ? AND (end_ts IS NULL OR end_ts >= ?) "
            "ORDER BY start_ts",
            (end_ts, start_ts),
        ).fetchall()
        return [
            (float(r[0]), None if r[1] is None else float(r[1])) for r in rows
        ]

    if conn is not None:
        return _query(conn)
    try:
        with open_recording_db(db_path) as own_conn:
            return _query(own_conn)
    except Exception:
        return []


def load_window_events(db_path: Path) -> list[WindowContext]:
    """Load window_event rows sorted by timestamp."""
    with open_recording_db(db_path) as conn:
        if not has_table(conn, "window_event"):
            return []

        has_browser_url = has_column(conn, "window_event", "browser_url")

        if has_browser_url:
            cur = conn.execute(
                "SELECT timestamp, app_bundle_id, title, window_id, browser_url "
                "FROM window_event "
                "WHERE timestamp IS NOT NULL "
                "ORDER BY timestamp"
            )
        else:
            cur = conn.execute(
                "SELECT timestamp, app_bundle_id, title, window_id "
                "FROM window_event "
                "WHERE timestamp IS NOT NULL "
                "ORDER BY timestamp"
            )
        results = []
        for row in cur:
            domain = None
            raw_url = row[4] if has_browser_url else None
            if raw_url:
                domain = domain_from_url(raw_url)
            results.append(WindowContext(
                timestamp=float(row[0]),
                app_bundle_id=row[1] or "",
                title=row[2] or "",
                window_id=row[3] or "",
                domain=domain,
                browser_url=raw_url or None,
            ))
        return results


# ---------------------------------------------------------------------------
# High-level association: screenshot → FrameMetadata
# ---------------------------------------------------------------------------


def associate_screenshot(
    screenshot_ts: float,
    window_events: list[WindowContext],
    max_delta: float = 5.0,
    _window_timestamps: list[float] | None = None,
) -> FrameMetadata:
    """Build FrameMetadata for a screenshot by finding the active context.

    Uses "latest at or before" semantics: window events are state
    transitions, so the active state at time T is the most recent event
    with timestamp <= T.

    Args:
        screenshot_ts: Unix timestamp of the screenshot.
        window_events: Pre-loaded, sorted window events.
        max_delta: Maximum time delta (seconds) for a valid association.
        _window_timestamps: Pre-computed window timestamps (avoids rebuilding per call).

    Returns:
        FrameMetadata populated with best-available context.
    """
    window = find_nearest_window(
        window_events, screenshot_ts, max_delta, _timestamps=_window_timestamps
    )

    bundle_id = window.app_bundle_id if window else ""

    return FrameMetadata(
        bundle_id=bundle_id,
        window_title=window.title if window else "",
        domain=window.domain if window else None,
        timestamp=screenshot_ts,
        browser_url=window.browser_url if window else None,
    )
