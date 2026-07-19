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
   was focused; the final span runs to the window end, clamped to the
   recording's actual coverage end (its last captured evidence) so a recording
   that ended before the window contributes nothing.
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

# Longest-focused window titles surfaced per app (narration color, not an
# exhaustive list).
_TOP_TITLES = 3


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
    """Per-app figures: wall-clock focus time (within recorded coverage),
    corroborated active time, event count, and backing source pointers.

    ``focus_ms`` is the app's focused wall-clock inside the recording's actual
    coverage — the number a person means by "time in Brave". ``covered_active_ms``
    is the stricter corroborated-active union (samples ± the activity window);
    reading/watching spans count toward focus but not active. ``display_name`` is
    the human app name from ``window_event`` (falls back to ``app``);
    ``top_titles`` are the longest-focused window titles (blocked spans already
    stripped), for narration.
    """

    app: str
    covered_active_ms: int
    event_count: int
    sources: list[SourcePointer] = field(default_factory=list)
    focus_ms: int = 0
    display_name: str = ""
    top_titles: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WindowAggregate:
    """The coverage-qualified aggregate for one window.

    ``covered_active_ms`` is the union of every app's covered active time;
    ``uncovered_ms`` is the cross-recording union of the RECORDED coverage with
    no corroborated activity (idle / away while capture ran — never time when no
    recording existed). ``recorded_ms`` is the union of the recordings' actual
    coverage inside the window MINUS blocked (masked) intervals — even an
    unattributed total must not carry a masked session's timing — so a caller
    can narrate "about X was recorded in this period". Neither active figure is
    presented as exact wall-clock — ``coverage_note`` carries the honest framing
    for narration.
    """

    window_ms: int
    apps: list[AppActivity]
    covered_active_ms: int
    uncovered_ms: int
    uncovered_spans: list[UncoveredSpan]
    coverage_note: str
    recorded_ms: int = 0


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
) -> list[tuple[str, str, str, float, float]]:
    """Partition ``[win_start, win_end)`` into
    ``(app, display_name, title, span_start, span_end)`` focus spans from
    time-ordered ``window_event`` rows.

    ``windows`` includes the last row before ``win_start`` (focus at entry) so a
    span that begins before the window is clipped in, not dropped. ``win_end``
    must already be clamped to the recording's actual coverage end — a focus
    span never extends past the last captured evidence.
    """
    spans: list[tuple[str, str, str, float, float]] = []
    for i, w in enumerate(windows):
        app = w["app_bundle_id"] or w["app_name"] or ""
        name = w["app_name"] or ""
        title = (w["title"] if "title" in w.keys() else None) or ""
        span_start = max(w["timestamp"], win_start)
        next_ts = windows[i + 1]["timestamp"] if i + 1 < len(windows) else win_end
        span_end = min(next_ts, win_end)
        if span_end > span_start:
            spans.append((app, name, title, span_start, span_end))
    return spans


def _aggregate_recording(
    rec_dir: Path,
    win_start: float,
    win_end: float,
    app_token: str | None,
) -> tuple[dict[str, dict], list[tuple[float, float]], list[tuple[float, float]]]:
    """Aggregate one recording over ``[win_start, win_end)``.

    Returns ``(per_app, uncovered, recorded_spans)`` where ``per_app[bundle]`` is
    ``{"covered": [(s,e)...], "count": int, "focus": [(s,e)...], "name": str,
    "titles": {title: focus_s}}``, ``uncovered`` is the list of ``(s, e)`` spans
    in this recording with no corroborated activity, and ``recorded_spans`` is the
    window slice this recording actually covers MINUS its blocked intervals
    (empty when it contributes nothing). Blocked time is excluded from the
    recorded figure for the same reason a masked span is never surfaced as
    uncovered: even an unattributed total must not carry masked sessions' timing.

    The recording's contribution is clamped to its own coverage: focus spans and
    uncovered time never extend past the last captured evidence (window / action /
    screenshot timestamp). Without the clamp, a recording that ENDED before the
    window still carries its last focus row into a phantom span across the whole
    window — every dead recording in the library would add ``window_ms`` of fake
    "uncovered" time (and mis-attribute focus to a long-closed app).

    Fail-open: an unreadable/older ``recording.db`` yields empty results (a read
    surface must never explode on one bad recording).
    """
    db_path = rec_dir / "recording.db"
    per_app: dict[str, dict] = {}
    uncovered: list[tuple[float, float]] = []
    if not db_path.is_file():
        return per_app, uncovered, []

    try:
        with open_recording_db(db_path, read_only=True, row_factory=Row) as conn:
            if not has_table(conn, "window_event") or not has_column(
                conn, "window_event", "timestamp"
            ):
                return per_app, uncovered, []

            title_expr = (
                "title" if has_column(conn, "window_event", "title") else "NULL"
            )
            windows = conn.execute(
                f"""SELECT timestamp, app_bundle_id, app_name, {title_expr} AS title
                   FROM window_event
                   WHERE timestamp IS NOT NULL AND timestamp < ?
                   ORDER BY timestamp""",
                (win_end,),
            ).fetchall()
            # Keep the window rows inside the window plus the last one before it.
            in_win = [w for w in windows if w["timestamp"] >= win_start]
            prev = [w for w in windows if w["timestamp"] < win_start]
            relevant = ([prev[-1]] if prev else []) + in_win

            # Corroborating action-event timestamps (exclude 'move' noise), plus
            # the recording's last action overall (a coverage-end signal).
            action_ts: list[float] = []
            last_action: float | None = None
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
                # Deliberately UNBOUNDED: an action at or after win_end proves
                # the recording ran through the window (coverage_end is then
                # clamped to win_end below); bounding at win_end would truncate
                # a still-running recording's coverage to its last in-window event.
                row = conn.execute(
                    "SELECT MAX(timestamp) FROM action_event"
                ).fetchone()
                last_action = row[0] if row else None
            # Also unbounded (the `windows` list stops at win_end): a focus
            # change after the window proves coverage through it.
            row = conn.execute("SELECT MAX(timestamp) FROM window_event").fetchone()
            last_window = row[0] if row else None
    except Exception:
        # rec_dir.name only in the log — never row content.
        logger.debug("aggregate skipped %s", rec_dir.name, exc_info=True)
        return per_app, uncovered, []

    if not relevant:
        return per_app, uncovered, []

    # On-disk screenshots are a coverage signal too (a captured frame vouches
    # for activity even absent an action event). Unbounded above like the DB
    # coverage-end signals: a still past win_end proves coverage through it.
    all_shot_ts = _screenshot_timestamps(rec_dir, 0.0, float("inf"))

    # Clamp to the recording's actual coverage end: the last evidence this
    # recording captured. A recording whose coverage ended before the window
    # contributes nothing (its last focus must not become a window-wide span).
    end_candidates = [t for t in (last_window, last_action) if t is not None]
    end_candidates += all_shot_ts[-1:]
    coverage_end = max(end_candidates) if end_candidates else win_start
    win_end = min(win_end, coverage_end)
    if win_end <= win_start:
        return per_app, uncovered, []
    # Filtered AFTER the clamp so no sample can seed coverage past the
    # recording's real coverage end.
    screenshot_ts = [t for t in all_shot_ts if win_start <= t < win_end]
    # Coverage start: the window start when the recording was already running at
    # entry (a pre-window focus row was carried in), else its first in-window event.
    recorded_start = win_start if prev else relevant[0]["timestamp"]

    # Corroborating samples: action events + screenshots.
    samples = sorted(action_ts + screenshot_ts)

    spans = _focus_spans(relevant, win_start, win_end)

    # FIX A — the fail-closed strip. Re-derive this recording's blocked intervals
    # (the same SCRUB_BLOCK_ACTIONS set the point flow's _strip_blocked uses, over
    # the intact local recording.db, require_canonical=True) and SUBTRACT them from
    # every focus span BEFORE crediting per-app name / duration. A masked
    # (MASK_WINDOW/EXCLUDE / secure-field) window's whole span lands in the blocked
    # set, so subtraction leaves nothing — its NAME and time never reach the
    # figures. A small residual (an uncovered-gap sliver at a span edge) clips only
    # the overlapped part rather than erasing the whole span. The blocked part is
    # NOT surfaced as uncovered either — an uncovered span still carries the
    # recording + timing, which for a masked window is exactly the metadata we
    # must not egress. Fail-closed: if the intervals can't be derived, this
    # recording contributes nothing (mirrors build_is_blocked's _always_blocked
    # sentinel and the point flow's whole-recording drop).
    blocked_intervals = _derive_blocked_intervals(
        rec_dir, win_start, win_end,
        screenshot_ts=screenshot_ts, action_ts=action_ts,
    )
    if blocked_intervals is None:
        return {}, [], []

    # Recorded coverage MINUS blocked time: even an unattributed "screen time
    # recorded" total must not carry a masked session's duration.
    recorded_spans = _subtract((recorded_start, win_end), blocked_intervals)

    for app, name, title, span_start, span_end in spans:
        for s_start, s_end in _subtract((span_start, span_end), blocked_intervals):
            # Active windows around each sample inside this focus sub-span.
            active = [
                (max(t - _ACTIVITY_WINDOW_S, s_start), min(t + _ACTIVITY_WINDOW_S, s_end))
                for t in samples
                if s_start <= t < s_end
            ]
            covered = _merge([(a, b) for a, b in active if b > a])
            # Count meaningful action events landing in this span (screenshots are
            # a coverage signal, not "events" the user performed).
            count = sum(1 for t in action_ts if s_start <= t < s_end)

            bucket = per_app.setdefault(
                app, {"covered": [], "count": 0, "focus": [], "name": "", "titles": {}}
            )
            bucket["covered"].extend(covered)
            bucket["count"] += count
            # Wall-clock focus (within recorded coverage) — the "time in this app"
            # a person means; the corroborated-active figure stays separate.
            bucket["focus"].append((s_start, s_end))
            if name and not bucket["name"]:
                bucket["name"] = name
            # A title that is just the app name carries no narration value.
            if title and title != name:
                bucket["titles"][title] = bucket["titles"].get(title, 0.0) + (
                    s_end - s_start
                )

            # The uncorroborated remainder of this focus span is uncovered.
            uncovered.extend(_subtract((s_start, s_end), covered))

    for bucket in per_app.values():
        bucket["covered"] = _merge(bucket["covered"])
        bucket["focus"] = _merge(bucket["focus"])

    if app_token is not None:
        per_app = {
            app: b for app, b in per_app.items() if app_token in app.lower()
        }

    return per_app, uncovered, recorded_spans


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


def _derive_blocked_intervals(
    rec_dir: Path,
    win_start: float,
    win_end: float,
    *,
    screenshot_ts: list[float],
    action_ts: list[float],
) -> list[tuple[float, float]] | None:
    """Return this recording's blocked ``[(start, end), …]`` for the window
    (FIX A), or ``None`` when they cannot be derived (fail-closed: the caller
    drops the recording's ENTIRE contribution).

    Re-derives the recording's ``SCRUB_BLOCK_ACTIONS`` skip set over
    ``[win_start, win_end)`` — the SAME machinery ``frame_blocked.build_is_blocked``
    and the point flow's ``_strip_blocked`` use (``build_classifier_evaluator`` +
    ``derive_skip_intervals(require_canonical=True)`` over the intact local
    ``recording.db``, with the ``PrivacyMode`` frozen at capture). The caller
    SUBTRACTS these from every focus span: a masked (MASK_WINDOW/EXCLUDE /
    secure-field) window's whole span is inside the set, so nothing of it
    survives; a residual sliver (e.g. an uncovered-gap at a span edge) clips
    only its overlap instead of erasing minutes of legitimate activity.

    The two timestamp families are routed separately: ``screenshot_ts`` (flat
    frame timestamps) get the full residual set including the SCR-191
    orphan-screenshot cross-check; ``action_ts`` (action-event timestamps) ride
    ``coverage_timestamps`` — uncovered-gap protection only — because an action
    timestamp never coincides with a ``screenshot`` row and routing it through
    ``screenshot_timestamps`` would falsely orphan-flag every action event and
    drop every active focus span from the figures.

    Fail-closed: ANY failure to derive the canonical block set (a partial/locked/
    missing ``recording.db`` under ``require_canonical`` → ``CanonicalDerivationError``,
    or any other error) returns ``None``. Heavy imports (scrubber / backfill /
    privacy) are deferred to the call so the aggregate module stays light.
    """
    try:
        from screencap.backfill.skip_intervals import (
            build_classifier_evaluator,
            derive_skip_intervals,
        )

        classifier, evaluator = build_classifier_evaluator(rec_dir)
        intervals = derive_skip_intervals(
            rec_dir / "recording.db",
            classifier=classifier,
            evaluator=evaluator,
            time_range=(win_start, win_end),
            screenshot_timestamps=list(screenshot_ts),
            coverage_timestamps=list(action_ts),
            require_canonical=True,
        )
    except Exception:
        # MUST stay broad enough to catch CanonicalDerivationError (the fail-closed
        # signal a partial canonical read raises under require_canonical=True) —
        # mapping it to None is what fails closed. Matched via Exception (not by
        # name) so the heavy skip_intervals stack isn't imported at module top.
        logger.warning(
            "aggregate: blocked-interval derivation failed for %s; failing closed "
            "(dropping the whole recording's contribution)",
            rec_dir.name, exc_info=True,
        )
        return None

    return _merge([(iv.start, iv.end) for iv in intervals])


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
    app_focus_intervals: dict[str, list[tuple[float, float]]] = {}
    app_counts: dict[str, int] = {}
    app_names: dict[str, str] = {}
    app_titles: dict[str, dict[str, float]] = {}
    uncovered_spans: list[UncoveredSpan] = []
    recorded_intervals: list[tuple[float, float]] = []
    any_recording = False

    for rec_dir in _iter_recording_dirs(recordings_dir):
        per_app, uncovered, recorded_spans = _aggregate_recording(
            rec_dir, win_start, win_end, app_token
        )
        if per_app or uncovered or recorded_spans:
            any_recording = True
        recorded_intervals.extend(recorded_spans)
        for app_id, bucket in per_app.items():
            intervals = bucket["covered"]
            app_covered_intervals.setdefault(app_id, []).extend(intervals)
            app_focus_intervals.setdefault(app_id, []).extend(bucket["focus"])
            app_covered.setdefault(app_id, []).extend(
                SourcePointer(rec_dir.name, _ms(s), _ms(e)) for s, e in intervals
            )
            app_counts[app_id] = app_counts.get(app_id, 0) + bucket["count"]
            if bucket["name"] and app_id not in app_names:
                app_names[app_id] = bucket["name"]
            titles = app_titles.setdefault(app_id, {})
            for title, dur in bucket["titles"].items():
                titles[title] = titles.get(title, 0.0) + dur
        uncovered_spans.extend(
            UncoveredSpan(rec_dir.name, _ms(s), _ms(e)) for s, e in uncovered
        )

    apps: list[AppActivity] = []
    for app_id, intervals in app_covered_intervals.items():
        merged = _merge(intervals)
        covered_ms = sum(_ms(e) - _ms(s) for s, e in merged)
        focus_merged = _merge(app_focus_intervals.get(app_id, []))
        focus_ms = sum(_ms(e) - _ms(s) for s, e in focus_merged)
        top_titles = [
            t
            for t, _dur in sorted(
                app_titles.get(app_id, {}).items(), key=lambda kv: -kv[1]
            )[:_TOP_TITLES]
        ]
        apps.append(
            AppActivity(
                app=app_id,
                covered_active_ms=covered_ms,
                event_count=app_counts.get(app_id, 0),
                sources=app_covered.get(app_id, []),
                focus_ms=focus_ms,
                display_name=app_names.get(app_id, ""),
                top_titles=top_titles,
            )
        )
    # Focus time is the headline figure for narration; sort by it (active time
    # breaks ties).
    apps.sort(key=lambda a: (a.focus_ms, a.covered_active_ms), reverse=True)

    # Total covered = union across apps (a span covered in two apps — e.g. two
    # recordings overlapping the window — is counted once).
    all_covered = _merge(
        [(s, e) for iv in app_covered_intervals.values() for s, e in iv]
    )
    covered_active_ms = sum(_ms(e) - _ms(s) for s, e in all_covered)
    # Union across recordings (like recorded_ms/covered_active_ms) — overlapping
    # recordings (e.g. a scrubbed sibling dir) must not double-count idle time,
    # or the narration reads "more uncovered than recorded".
    uncovered_ms = sum(
        _ms(e) - _ms(s)
        for s, e in _merge(
            [(u.start_ms / 1000.0, u.end_ms / 1000.0) for u in uncovered_spans]
        )
    )

    recorded_ms = sum(_ms(e) - _ms(s) for s, e in _merge(recorded_intervals))

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
        recorded_ms=recorded_ms,
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
