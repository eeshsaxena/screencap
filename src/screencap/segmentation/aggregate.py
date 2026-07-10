"""Deterministic, coverage-qualified aggregation over a time window (U2).

Answers "how much time in app X over ``[start, end)``" for the recall chat's
period-summary flow (R5) — but HONESTLY. It computes per-app ACTIVE time and
event counts from authoritative local event data (``window_event`` /
``action_event`` / on-disk ``screenshots/*.jpg``), never estimated by a model,
and it surfaces the spans it CANNOT vouch for rather than over-counting them.

Why this exists instead of ``task_manifest._compute_dominant_app``
------------------------------------------------------------------
``window_event`` rows are written only when the focused window *changes*, so a
static-focus span (reading, watching, idle, lunch) produces NO rows.
``_compute_dominant_app`` credits the entire inter-event gap
(``next_ts - ts``) to the last-focused app — so an hour at lunch with
Salesforce in front is counted as an hour "in Salesforce". That over-count is
exactly the trust failure the chat must avoid.

Coverage model (the load-bearing decision)
-------------------------------------------
For each app, over the window:

1. Read ``window_event`` rows intersecting the window, plus the last window
   *before* the window start (the focus at window entry — the same carry-the-
   prior-window trick ``_compute_dominant_app`` uses). These partition the
   window into **focus spans**: ``[ts_i, ts_{i+1})`` is the span where app *i*
   was focused; the final span runs to the window end.
2. A focus span is credited as ACTIVE only where **corroborated** by activity:
   an ``action_event`` (excluding ``move`` noise) timestamp, or a
   ``screenshots/*.jpg`` present in that span. Around each corroborating sample
   we credit a bounded active window (``_ACTIVITY_WINDOW_S`` on each side,
   clamped to the focus span), then merge overlaps → the covered active union
   for that app.
3. The **uncorroborated remainder** of every focus span is emitted as an
   uncovered descriptor (idle / away), NEVER credited to the app.

Event counts per app are ``action_event`` rows (``name != 'move'``) falling
inside that app's focus spans.

The layer never claims exact wall-clock. It returns covered active time PLUS
the uncovered remainder, mirroring how ``day_segments`` surfaces its
unverifiable intervals — so the caller narrates "≈X over covered spans" and
shows the gap, rather than a fabricated exact figure.

Local-only and read-only. Reads the intact local ``recording.db`` and the flat
``screenshots/`` dir; touches no OCR, no content index, and nothing uploaded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from screencap.recording_db import Row, has_column, has_table, open_recording_db

logger = logging.getLogger(__name__)

# Seconds of active time credited on EACH side of a corroborating sample
# (an action event or a screenshot). A click or a captured frame vouches for a
# bounded neighbourhood of activity, not the whole static-focus span. Kept
# modest so a lone stray click can't launder a long idle gap into "active" time.
_ACTIVITY_WINDOW_S = 15.0

# Cap on recordings scanned for one window (DoS bound, mirrors the daemon
# query verbs' ``_QUERY_MAX_RECORDINGS``).
_MAX_RECORDINGS = 200


@dataclass(frozen=True)
class SourcePointer:
    """A backing pointer for a credited active interval: which recording and the
    covered ``[start_ms, end_ms)`` sub-span within it. Pointer-only — no bytes,
    no content — so a caller can deep-link to the moment (frame.nearest et al.)."""

    recording: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class UncoveredSpan:
    """A span the layer could not vouch for as active in any app (idle / away /
    no captured evidence). Surfaced so the caller can show the gap instead of
    summing it as active time (mirrors ``day_segments`` unverifiable intervals)."""

    recording: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class AppActivity:
    """Per-app covered active time + event count + backing source pointers."""

    app: str
    covered_active_ms: int
    event_count: int
    sources: list[SourcePointer] = field(default_factory=list)


@dataclass(frozen=True)
class WindowAggregate:
    """The coverage-qualified aggregate for one window.

    ``covered_active_ms`` is the union of every app's covered active time;
    ``uncovered_ms`` is the remainder of the window with no corroborated
    activity. Neither is presented as exact wall-clock — ``coverage_note``
    carries the honest framing for narration.
    """

    window_ms: int
    apps: list[AppActivity]
    covered_active_ms: int
    uncovered_ms: int
    uncovered_spans: list[UncoveredSpan]
    coverage_note: str


# ---------------------------------------------------------------------------
# Interval helpers
# ---------------------------------------------------------------------------


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/adjacent ``[start, end)`` intervals; drop empties."""
    cleaned = [(s, e) for s, e in intervals if e > s]
    if not cleaned:
        return []
    cleaned.sort()
    out = [cleaned[0]]
    for s, e in cleaned[1:]:
        ls, le = out[-1]
        if s <= le:
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def _subtract(
    span: tuple[float, float], covered: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Return the sub-intervals of ``span`` not overlapped by ``covered`` (the
    uncovered remainder). ``covered`` need not be pre-merged."""
    s, e = span
    gaps: list[tuple[float, float]] = []
    cursor = s
    for cs, ce in _merge(covered):
        if ce <= s or cs >= e:
            continue
        cs, ce = max(cs, s), min(ce, e)
        if cs > cursor:
            gaps.append((cursor, cs))
        cursor = max(cursor, ce)
    if cursor < e:
        gaps.append((cursor, e))
    return gaps


def _ms(ts: float) -> int:
    return int(round(ts * 1000))


# ---------------------------------------------------------------------------
# Per-recording pass
# ---------------------------------------------------------------------------


def _focus_spans(
    windows: list[Row], win_start: float, win_end: float
) -> list[tuple[str, float, float]]:
    """Partition ``[win_start, win_end)`` into ``(app, span_start, span_end)``
    focus spans from time-ordered ``window_event`` rows.

    ``windows`` includes the last row before ``win_start`` (focus at entry) so a
    span that begins before the window is clipped in, not dropped.
    """
    spans: list[tuple[str, float, float]] = []
    for i, w in enumerate(windows):
        app = w["app_bundle_id"] or w["app_name"] or ""
        span_start = max(w["timestamp"], win_start)
        next_ts = windows[i + 1]["timestamp"] if i + 1 < len(windows) else win_end
        span_end = min(next_ts, win_end)
        if span_end > span_start:
            spans.append((app, span_start, span_end))
    return spans


def _aggregate_recording(
    rec_dir: Path,
    win_start: float,
    win_end: float,
    app_token: str | None,
) -> tuple[dict[str, dict], list[tuple[float, float]]]:
    """Aggregate one recording over ``[win_start, win_end)``.

    Returns ``(per_app, uncovered)`` where ``per_app[bundle]`` is
    ``{"covered": [(s,e)...], "count": int}`` and ``uncovered`` is the list of
    ``(s, e)`` spans in this recording with no corroborated activity.

    Fail-open: an unreadable/older ``recording.db`` yields empty results (a read
    surface must never explode on one bad recording).
    """
    db_path = rec_dir / "recording.db"
    per_app: dict[str, dict] = {}
    uncovered: list[tuple[float, float]] = []
    if not db_path.is_file():
        return per_app, uncovered

    try:
        with open_recording_db(db_path, read_only=True, row_factory=Row) as conn:
            if not has_table(conn, "window_event") or not has_column(
                conn, "window_event", "timestamp"
            ):
                return per_app, uncovered

            windows = conn.execute(
                """SELECT timestamp, app_bundle_id, app_name FROM window_event
                   WHERE timestamp IS NOT NULL AND timestamp < ?
                   ORDER BY timestamp""",
                (win_end,),
            ).fetchall()
            # Keep the window rows inside the window plus the last one before it.
            in_win = [w for w in windows if w["timestamp"] >= win_start]
            prev = [w for w in windows if w["timestamp"] < win_start]
            relevant = ([prev[-1]] if prev else []) + in_win

            # Corroborating action-event timestamps (exclude 'move' noise).
            action_ts: list[float] = []
            if has_table(conn, "action_event") and has_column(
                conn, "action_event", "timestamp"
            ):
                action_ts = [
                    r["timestamp"]
                    for r in conn.execute(
                        """SELECT timestamp FROM action_event
                           WHERE timestamp >= ? AND timestamp < ?
                             AND (name IS NULL OR name != 'move')
                           ORDER BY timestamp""",
                        (win_start, win_end),
                    ).fetchall()
                ]
    except Exception:
        # rec_dir.name only in the log — never row content.
        logger.debug("aggregate skipped %s", rec_dir.name, exc_info=True)
        return per_app, uncovered

    if not relevant:
        return per_app, uncovered

    # On-disk screenshots are a coverage signal too (a captured frame vouches
    # for activity even absent an action event).
    screenshot_ts = _screenshot_timestamps(rec_dir, win_start, win_end)

    # Corroborating samples: action events + screenshots.
    samples = sorted(action_ts + screenshot_ts)

    spans = _focus_spans(relevant, win_start, win_end)
    for app, s_start, s_end in spans:
        # Active windows around each sample inside this focus span.
        active = [
            (max(t - _ACTIVITY_WINDOW_S, s_start), min(t + _ACTIVITY_WINDOW_S, s_end))
            for t in samples
            if s_start <= t < s_end
        ]
        covered = _merge([(a, b) for a, b in active if b > a])
        # Count meaningful action events landing in this span (screenshots are a
        # coverage signal, not "events" the user performed).
        count = sum(1 for t in action_ts if s_start <= t < s_end)

        bucket = per_app.setdefault(app, {"covered": [], "count": 0})
        bucket["covered"].extend(covered)
        bucket["count"] += count

        # The uncorroborated remainder of this focus span is uncovered.
        uncovered.extend(_subtract((s_start, s_end), covered))

    for bucket in per_app.values():
        bucket["covered"] = _merge(bucket["covered"])

    if app_token is not None:
        per_app = {
            app: b for app, b in per_app.items() if app_token in app.lower()
        }

    return per_app, uncovered


def _screenshot_timestamps(
    rec_dir: Path, win_start: float, win_end: float
) -> list[float]:
    """Flat ``screenshots/*.jpg`` timestamps intersecting the window.

    Reuses ``redaction.geometry`` (the same flat-frame enumeration the backfill
    and day-timeline use) so the frame-presence signal can't drift.
    """
    try:
        from screencap.redaction.geometry import list_screenshot_timestamps

        return [
            t for t in list_screenshot_timestamps(rec_dir) if win_start <= t < win_end
        ]
    except Exception:
        logger.debug("aggregate: screenshot enum failed %s", rec_dir.name, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def aggregate_window(
    start_ms: int,
    end_ms: int,
    *,
    app: str | None = None,
    recordings_dir: Path | None = None,
) -> WindowAggregate:
    """Compute the coverage-qualified per-app aggregate for ``[start_ms, end_ms)``.

    Args:
        start_ms / end_ms: half-open window in Unix epoch milliseconds.
        app: optional case-insensitive substring filter over the app bundle id /
            name; ``None`` returns every app seen in the window.
        recordings_dir: recordings root (defaults to the configured dir). Passed
            explicitly by tests over a ``tmp_path`` library.

    Returns a :class:`WindowAggregate`. Never raises for ordinary bad data —
    an empty or unreadable library yields a zeroed result with a coverage note,
    never a fabricated figure (R5).
    """
    win_start = start_ms / 1000.0
    win_end = end_ms / 1000.0
    window_ms = max(0, end_ms - start_ms)
    app_token = app.lower() if app else None

    if win_end <= win_start:
        return WindowAggregate(
            window_ms=0,
            apps=[],
            covered_active_ms=0,
            uncovered_ms=0,
            uncovered_spans=[],
            coverage_note="empty window (no time span)",
        )

    if recordings_dir is None:
        from screencap.config import get_recordings_dir

        recordings_dir = get_recordings_dir()
    recordings_dir = Path(recordings_dir)

    # Per-app covered intervals + counts across every recording intersecting the
    # window, plus the per-recording uncovered spans.
    app_covered: dict[str, list[SourcePointer]] = {}
    app_covered_intervals: dict[str, list[tuple[float, float]]] = {}
    app_counts: dict[str, int] = {}
    uncovered_spans: list[UncoveredSpan] = []
    any_recording = False

    for rec_dir in _iter_recording_dirs(recordings_dir):
        per_app, uncovered = _aggregate_recording(
            rec_dir, win_start, win_end, app_token
        )
        if per_app or uncovered:
            any_recording = True
        for app_id, bucket in per_app.items():
            intervals = bucket["covered"]
            app_covered_intervals.setdefault(app_id, []).extend(intervals)
            app_covered.setdefault(app_id, []).extend(
                SourcePointer(rec_dir.name, _ms(s), _ms(e)) for s, e in intervals
            )
            app_counts[app_id] = app_counts.get(app_id, 0) + bucket["count"]
        uncovered_spans.extend(
            UncoveredSpan(rec_dir.name, _ms(s), _ms(e)) for s, e in uncovered
        )

    apps: list[AppActivity] = []
    for app_id, intervals in app_covered_intervals.items():
        merged = _merge(intervals)
        covered_ms = sum(_ms(e) - _ms(s) for s, e in merged)
        apps.append(
            AppActivity(
                app=app_id,
                covered_active_ms=covered_ms,
                event_count=app_counts.get(app_id, 0),
                sources=app_covered.get(app_id, []),
            )
        )
    apps.sort(key=lambda a: a.covered_active_ms, reverse=True)

    # Total covered = union across apps (a span covered in two apps — e.g. two
    # recordings overlapping the window — is counted once).
    all_covered = _merge(
        [(s, e) for iv in app_covered_intervals.values() for s, e in iv]
    )
    covered_active_ms = sum(_ms(e) - _ms(s) for s, e in all_covered)
    uncovered_ms = sum(u.end_ms - u.start_ms for u in uncovered_spans)

    coverage_note = _coverage_note(
        any_recording, apps, covered_active_ms, uncovered_ms, app_token
    )

    return WindowAggregate(
        window_ms=window_ms,
        apps=apps,
        covered_active_ms=covered_active_ms,
        uncovered_ms=uncovered_ms,
        uncovered_spans=uncovered_spans,
        coverage_note=coverage_note,
    )


def _coverage_note(
    any_recording: bool,
    apps: list[AppActivity],
    covered_active_ms: int,
    uncovered_ms: int,
    app_token: str | None,
) -> str:
    """A short, honest framing of the figure for narration (never exact
    wall-clock)."""
    if not any_recording:
        if app_token:
            return f"no recorded activity for '{app_token}' in this window"
        return "no recorded activity in this window"
    if not apps or covered_active_ms == 0:
        return (
            "no corroborated active time in this window; the focused span had no "
            "captured activity (idle / away)"
        )
    parts = [
        "figures are active time over CORROBORATED spans (action events / "
        "captured frames), not exact wall-clock"
    ]
    if uncovered_ms > 0:
        parts.append(f"{uncovered_ms} ms is uncovered (idle / away / not captured)")
    return "; ".join(parts)


def _iter_recording_dirs(recordings_dir: Path) -> list[Path]:
    """Recording dirs under ``recordings_dir``, capped (fail-open on a missing
    root)."""
    if not recordings_dir.is_dir():
        return []
    dirs = [
        d
        for d in sorted(recordings_dir.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]
    return dirs[:_MAX_RECORDINGS]
