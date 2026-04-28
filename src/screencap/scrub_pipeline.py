"""Shared scrub pipeline for recording privacy enforcement.

Composable functions called by both the full ``screencap scrub`` command
(``scrubber.py``) and the live chunk processor (``chunk_processor.py``).
This module is the single source of truth for scrubbing logic — callers
orchestrate the order, but never duplicate the implementation.

Three-layer architecture:
    privacy/              → Pure decision-making (policy, detection, masking)
    scrub_pipeline.py     → File-level orchestration (this module)
    scrubber.py           → CLI entry for ``screencap scrub`` (copy dir, call pipeline)
    chunk_processor.py    → Live upload orchestration (call pipeline in-place)
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import re
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from screencap.privacy.actions import (
    BLOCK_ACTIONS,
    KEYSTROKE_CONTENT_FIELDS,
    MOUSE_COORDINATE_FIELDS,
    SCRUB_BLOCK_ACTIONS,
    PrivacyAction,
)
from screencap.privacy.policy import DEFAULT_TRANSITION_HOLD_SECONDS
from screencap.privacy.reasons import AuditEntry, ReasonCode
from screencap.recording_db import Connection, has_column, has_table, open_recording_db

logger = logging.getLogger(__name__)

# Actions that trigger masking for background windows.
_BG_MASK_ACTIONS = frozenset({
    PrivacyAction.EXCLUDE,
    PrivacyAction.MASK_WINDOW,
    PrivacyAction.MASK_REGION,
    PrivacyAction.TEXT_REDACT,
    PrivacyAction.OCR_FALLBACK,
})


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------


@dataclass
class ScrubResult:
    """Summary of a scrub operation."""

    output_dir: Path = field(default_factory=Path)
    entity_counts: Counter = field(default_factory=Counter)
    deleted_files: list[str] = field(default_factory=list)
    audit_entries: list[AuditEntry] = field(default_factory=list)


@dataclass(frozen=True)
class BlockedInterval:
    """Time interval where a blocked app was frontmost."""

    start: float
    end: float
    action: PrivacyAction
    reason: str


@dataclass(frozen=True)
class ElementStateDetection:
    """A PII detection from an element_state AXValue field.

    Used to cross-reference against keystrokes whose timestamps
    fall within the same action_event rows.
    """

    original_text: str
    entity_type: str
    timestamps: frozenset[float]


@dataclass
class ScrubContext:
    """Bundled context for scrubbing — built once, passed to all surface scrubbers.

    Pure input data. Output/audit state is tracked separately via ScrubResult
    passed to each pipeline function.
    """

    blocked_intervals: list[BlockedInterval] = field(default_factory=list)
    xref_detections: list[ElementStateDetection] = field(default_factory=list)
    window_events: list = field(default_factory=list)  # list[WindowContext]
    evaluator: object | None = None  # DefaultPolicyEvaluator
    classifier: object | None = None  # DefaultContextClassifier
    pixel_ratio: float = 2.0


# ---------------------------------------------------------------------------
# Shared scrub_text()
# ---------------------------------------------------------------------------


def scrub_text(
    text: str,
    pipeline,
    anonymizer,
    *,
    result: ScrubResult | None = None,
):
    """Run text through the detection pipeline and anonymize.

    Returns (scrubbed_text, detection_result). detection_result is None when
    text was empty, whitespace-only, or all detectors failed.

    On AllDetectorsFailedError or other pipeline errors, returns
    ('<SCRUB_FAILED>', None). No console output — callers log/print
    in their own style.
    """
    if not text or not text.strip():
        return text, None

    from screencap.privacy import AllDetectorsFailedError

    try:
        detection_result = pipeline.detect(text)
    except AllDetectorsFailedError:
        return "<SCRUB_FAILED>", None
    except Exception:
        return "<SCRUB_FAILED>", None

    scrubbed = anonymizer.anonymize(
        detection_result.normalized_text,
        detection_result.detections,
    )

    if result is not None:
        for det in detection_result.detections:
            result.entity_counts[det.entity_type] += 1

    return scrubbed, detection_result


# ---------------------------------------------------------------------------
# Blocked-app interval building
# ---------------------------------------------------------------------------


def build_blocked_intervals(
    window_events,
    evaluator,
    classifier,
    *,
    actions: frozenset[PrivacyAction] = BLOCK_ACTIONS,
) -> list[BlockedInterval]:
    """Build intervals where the frontmost app triggers a blocking action.

    Each window event defines a period from its timestamp to the next
    window event's timestamp (or infinity for the last event).

    Args:
        window_events: Ordered list of WindowContext.
        evaluator: Policy evaluator that returns ActionDecision per frame.
        classifier: Context classifier mapping frames to ContextClass.
        actions: Set of PrivacyActions that mark an interval as blocked.
            Defaults to BLOCK_ACTIONS (screenshot capture-time semantics, just
            EXCLUDE). Pass SCRUB_BLOCK_ACTIONS for scrub-time pointer
            suppression (EXCLUDE/MASK_WINDOW/TEXT_REDACT/OCR_FALLBACK).
    """
    from screencap.privacy.policy import FrameMetadata

    if not window_events:
        return []

    intervals: list[BlockedInterval] = []

    for i, we in enumerate(window_events):
        end_ts = (
            window_events[i + 1].timestamp
            if i + 1 < len(window_events)
            else float("inf")
        )

        meta = FrameMetadata(
            bundle_id=we.app_bundle_id,
            window_title=we.title,
            domain=we.domain,
            timestamp=we.timestamp,
            browser_url=we.browser_url,
        )
        ctx = classifier.classify(meta)
        decision = evaluator.evaluate(ctx, meta)

        if decision.action in actions:
            intervals.append(
                BlockedInterval(
                    start=we.timestamp,
                    end=end_ts,
                    action=decision.action,
                    reason=decision.reason,
                )
            )

    return intervals


def build_secure_field_intervals(
    db_path: Path | None,
    hold_seconds: float = DEFAULT_TRANSITION_HOLD_SECONDS,
    *,
    time_range: tuple[float, float] | None = None,
    conn: Connection | None = None,
) -> list[BlockedInterval]:
    """Build blocked intervals from action events with AXSecureTextField.

    Scans element_state JSON for AXRole or AXSubrole == "AXSecureTextField".
    Each detection creates a blocked interval starting at the event timestamp
    and lasting hold_seconds. Adjacent/overlapping intervals are merged.

    Args:
        db_path: Path to the recording database.
        hold_seconds: Duration of each secure-field block interval.
        time_range: Optional (start, end) to scope DB queries for chunks.
        conn: Optional existing connection (for chunk processor reuse).
    """
    if db_path is None and conn is None:
        return []

    if conn is not None:
        return _build_secure_field_intervals_with_conn(
            conn, hold_seconds, time_range=time_range,
        )
    with open_recording_db(db_path) as own_conn:
        return _build_secure_field_intervals_with_conn(
            own_conn, hold_seconds, time_range=time_range,
        )


def _build_secure_field_intervals_with_conn(
    conn: Connection,
    hold_seconds: float,
    *,
    time_range: tuple[float, float] | None,
) -> list[BlockedInterval]:
    if not has_table(conn, "action_event"):
        return []
    if not has_column(conn, "action_event", "element_state"):
        return []

    if time_range is not None:
        rows = conn.execute(
            "SELECT timestamp, element_state FROM action_event "
            "WHERE element_state IS NOT NULL AND timestamp IS NOT NULL "
            "AND timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp",
            (time_range[0], time_range[1]),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT timestamp, element_state FROM action_event "
            "WHERE element_state IS NOT NULL AND timestamp IS NOT NULL "
            "ORDER BY timestamp"
        ).fetchall()

    raw_intervals: list[tuple[float, float]] = []
    for ts, es_raw in rows:
        if not es_raw:
            continue
        try:
            es = json.loads(es_raw) if isinstance(es_raw, str) else es_raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(es, dict):
            continue
        if (
            es.get("AXRole") == "AXSecureTextField"
            or es.get("AXSubrole") == "AXSecureTextField"
        ):
            raw_intervals.append((float(ts), float(ts) + hold_seconds))

    if not raw_intervals:
        return []

    # Merge overlapping/adjacent intervals
    merged: list[BlockedInterval] = []
    cur_start, cur_end = raw_intervals[0]
    for start, end in raw_intervals[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            merged.append(BlockedInterval(
                start=cur_start,
                end=cur_end,
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.SECURE_FIELD_DETECTED,
            ))
            cur_start, cur_end = start, end
    merged.append(BlockedInterval(
        start=cur_start,
        end=cur_end,
        action=PrivacyAction.EXCLUDE,
        reason=ReasonCode.SECURE_FIELD_DETECTED,
    ))
    return merged


def merge_intervals(
    *interval_lists: list[BlockedInterval],
) -> list[BlockedInterval]:
    """Concatenate and sort interval lists by start time.

    Intervals may overlap (e.g., a long app-window MASK_WINDOW interval
    with nested short secure-field EXCLUDE intervals from
    ``build_scrub_context``). ``find_blocked_interval`` handles overlaps
    by walking backwards through all prior intervals whose ``start <=
    timestamp``; ``_interval_intersects`` relies on that fix in its
    first branch.
    """
    all_intervals: list[BlockedInterval] = []
    for ivs in interval_lists:
        all_intervals.extend(ivs)
    all_intervals.sort(key=lambda iv: iv.start)
    return all_intervals


def find_blocked_interval(
    timestamp: float,
    intervals: list[BlockedInterval],
    _starts: list[float] | None = None,
) -> BlockedInterval | None:
    """Return any blocked interval containing timestamp, or None.

    Pass _starts (pre-computed [iv.start for iv in intervals]) to avoid
    rebuilding the list on every call.

    Handles overlapping intervals correctly: walks backwards through all
    intervals whose ``start <= timestamp``, returning the first one whose
    ``end > timestamp``. ``O(k)`` where ``k`` = overlap depth at this
    timestamp; typically 1 for non-overlapping intervals, small for the
    overlapping case (long app-window interval + nested short
    secure-field interval).

    Pre-fix, this used a single point-lookup at
    ``bisect_right(_starts, timestamp) - 1`` and missed the case where
    the closest-by-start interval ended before ``timestamp`` while a
    longer earlier interval still contained it (e.g. a 1-second
    secure-field EXCLUDE nested inside a 90-second MASK_WINDOW would
    leak pointer events at timestamps after the EXCLUDE ended but
    inside the MASK_WINDOW).
    """
    if not intervals:
        return None
    if _starts is None:
        _starts = [iv.start for iv in intervals]
    # bisect_right returns the first index whose start > timestamp.
    # idx-1 is the latest interval with start <= timestamp; walk
    # backwards through earlier intervals (which all also have
    # start <= timestamp by sorted order) until we find one with
    # end > timestamp.
    idx = bisect.bisect_right(_starts, timestamp) - 1
    while idx >= 0:
        iv = intervals[idx]
        if iv.start <= timestamp < iv.end:
            return iv
        idx -= 1
    return None


def _interval_intersects(
    start_ts: float,
    end_ts: float,
    intervals: list[BlockedInterval],
    _starts: list[float] | None = None,
) -> BlockedInterval | None:
    """Return any blocked interval that intersects ``[start_ts, end_ts]``.

    Used by the scrub layer to drop ``mouse.move`` events whose merged span
    crosses into a blocked interval. ``merge_consecutive_mouse_move_events``
    collapses runs of raw moves into a single event whose ``timestamp`` is
    the start and ``last_timestamp`` the end — a point-lookup at the start
    timestamp would miss merges that begin before a blocked interval and end
    inside it (the leak this helper closes).

    Half-open overlap semantics matching ``find_blocked_interval``: an
    interval ``[i.start, i.end)`` intersects the move span if
    ``i.start <= end_ts AND i.end > start_ts``. A move whose ``end_ts``
    lands exactly on ``i.start`` does NOT intersect (boundary excluded,
    consistent with the half-open ``find_blocked_interval`` convention),
    while ``end_ts > i.start`` does (the move tail crossed the boundary).

    For unmerged moves, callers should pass ``end_ts == start_ts`` — this
    degenerates to a point lookup matching ``find_blocked_interval``
    semantics.

    Correctness note for overlapping intervals: this helper relies on
    ``find_blocked_interval`` (first branch) to handle the overlapping
    case where ``start_ts`` is inside a longer interval whose
    ``start`` lies before a shorter, later-starting interval (e.g.,
    a long MASK_WINDOW with a nested short secure-field EXCLUDE).
    The forward branch (looking for an interval starting after
    ``start_ts`` but before ``end_ts``) only needs to check
    ``intervals[idx]`` because intervals are sorted by ``start`` —
    if the first interval after ``start_ts`` does not begin before
    ``end_ts``, no later interval will either.
    """
    if not intervals:
        return None
    if _starts is None:
        _starts = [iv.start for iv in intervals]

    # Cheap path: if the start timestamp is inside an interval, return it.
    # Relies on the overlap-aware find_blocked_interval — a point lookup
    # at start_ts that walks backwards through prior overlapping intervals
    # so a long MASK_WINDOW containing start_ts is found even when a
    # shorter, later-starting interval would otherwise mask it.
    hit = find_blocked_interval(start_ts, intervals, _starts)
    if hit is not None:
        return hit

    # Otherwise look for an interval starting after start_ts but before end_ts
    # (the merge spans into a later blocked interval). bisect_right returns
    # the first index whose start > start_ts; we walk forward checking
    # i.start < end_ts (the move tail enters the interval before the head
    # of the move tail). Half-open: i.start == end_ts does not intersect.
    if end_ts <= start_ts:
        return None
    idx = bisect.bisect_right(_starts, start_ts)
    if idx < len(intervals) and intervals[idx].start < end_ts:
        return intervals[idx]
    return None


def null_event_content(event: dict) -> None:
    """Null out sensitive content fields in an event dict in-place (recursive).

    Handles nested structures like key.type inside mouse.drag.children,
    window.switch title/domain fields, and mouse coordinate fields on
    retained mouse events (e.g. a ``mouse.drag`` whose timestamp lands in
    a SCRUB_BLOCK_ACTIONS interval — its content is nulled but the event
    is retained for audit shape; pointer geometry must also be zeroed
    so coarse interaction patterns don't leak).
    """
    for fld in KEYSTROKE_CONTENT_FIELDS:
        if fld in event:
            event[fld] = None
    if event.get("type") == "window.switch":
        event["window_title"] = None
        event["domain"] = None
    # R6/R12: zero positional fields on retained mouse events so drag
    # envelope (start/end coords, displacement, waypoints) doesn't leak
    # from blocked intervals. Recurses into drag children below.
    event_type = event.get("type", "")
    if isinstance(event_type, str) and event_type.startswith("mouse."):
        for fld in MOUSE_COORDINATE_FIELDS:
            if fld in event:
                event[fld] = None
    for child in event.get("children", []):
        null_event_content(child)


# ---------------------------------------------------------------------------
# Element-state cross-reference
# ---------------------------------------------------------------------------


def build_xref_lookup(
    raw_detections: dict[int, dict],
) -> list[ElementStateDetection]:
    """Deduplicate element_state detections and build a cross-reference list.

    Groups by ``(entity_type, original_text.lower())``, collects all
    timestamps where each unique detection appeared, and filters out
    very short detections (< 3 chars).
    """
    grouped: dict[tuple[str, str], dict] = {}
    for row_data in raw_detections.values():
        ts = row_data.get("timestamp")
        if ts is None:
            continue
        for det in row_data.get("detections", []):
            original = det["original_text"]
            if len(original.strip()) < 3:
                continue
            key = (det["entity_type"], original.lower())
            if key not in grouped:
                grouped[key] = {
                    "original_text": original,
                    "entity_type": det["entity_type"],
                    "timestamps": set(),
                }
            grouped[key]["timestamps"].add(ts)

    return [
        ElementStateDetection(
            original_text=v["original_text"],
            entity_type=v["entity_type"],
            timestamps=frozenset(v["timestamps"]),
        )
        for v in grouped.values()
    ]


def collect_xref_from_db(
    db_path: Path,
    pipeline,
    anonymizer,
    *,
    time_range: tuple[float, float] | None = None,
    conn: Connection | None = None,
) -> list[ElementStateDetection]:
    """Read-only collection of element_state xref detections from the DB.

    For the chunk processor path: queries element_state values from
    action_event, runs them through the detection pipeline, and builds
    a cross-reference lookup. Does NOT modify any DB rows.

    Args:
        db_path: Path to the recording database.
        pipeline: Detection pipeline instance.
        anonymizer: Anonymizer instance.
        time_range: Optional (start, end) to scope queries for chunks.
        conn: Optional existing connection (for reuse).
    """
    try:
        if conn is not None:
            return _collect_xref_with_conn(
                conn, pipeline, anonymizer, time_range=time_range,
            )
        with open_recording_db(db_path) as own_conn:
            return _collect_xref_with_conn(
                own_conn, pipeline, anonymizer, time_range=time_range,
            )
    except Exception:
        logger.debug("xref collection failed", exc_info=True)
        return []


def _collect_xref_with_conn(
    conn: Connection,
    pipeline,
    anonymizer,
    *,
    time_range: tuple[float, float] | None,
) -> list[ElementStateDetection]:
    if not has_table(conn, "action_event"):
        return []
    if not has_column(conn, "action_event", "element_state"):
        return []

    if time_range is not None:
        rows = conn.execute(
            "SELECT id, timestamp, element_state FROM action_event "
            "WHERE element_state IS NOT NULL AND timestamp IS NOT NULL "
            "AND timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp",
            (time_range[0], time_range[1]),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, timestamp, element_state FROM action_event "
            "WHERE element_state IS NOT NULL AND timestamp IS NOT NULL "
            "ORDER BY timestamp"
        ).fetchall()

    # Parse each element_state JSON, extract AXValue, run detection
    raw_detections: dict[int, dict] = {}
    _result = ScrubResult()  # throwaway — we only want detections

    for row_id, timestamp, es_raw in rows:
        if not es_raw:
            continue
        try:
            es = json.loads(es_raw) if isinstance(es_raw, str) else es_raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(es, dict):
            continue

        ax_value = es.get("AXValue")
        if not ax_value or not isinstance(ax_value, str) or not ax_value.strip():
            continue

        _, det_result = scrub_text(ax_value, pipeline, anonymizer, result=_result)
        if det_result is not None and det_result.detections:
            per_row_dets = []
            for det in det_result.detections:
                original_text = det_result.normalized_text[det.start:det.end]
                per_row_dets.append({
                    "original_text": original_text,
                    "entity_type": det.entity_type,
                    "score": det.score,
                })
            if per_row_dets:
                raw_detections[row_id] = {
                    "timestamp": timestamp,
                    "detections": per_row_dets,
                }

    return build_xref_lookup(raw_detections)


# ---------------------------------------------------------------------------
# build_scrub_context()
# ---------------------------------------------------------------------------


def build_scrub_context(
    db_path: Path | None,
    evaluator=None,
    classifier=None,
    *,
    time_range: tuple[float, float] | None = None,
    pipeline=None,
    anonymizer=None,
    pixel_ratio: float | None = None,
) -> ScrubContext:
    """Build blocked intervals, secure-field intervals, window events, xref lookup.

    For chunk processor: pass time_range to scope DB queries, and
    pipeline/anonymizer to enable xref detection collection.
    For full scrubber: call without pipeline/anonymizer (xref collected
    during DB scrub and passed directly to scrub_events_jsonl instead).

    Fails gracefully — returns a context with empty collections on error,
    so NLP scrubbing still runs.
    """
    ctx = ScrubContext()

    if evaluator is not None:
        ctx.evaluator = evaluator
    if classifier is not None:
        ctx.classifier = classifier

    if db_path is None:
        return ctx

    if pixel_ratio is not None:
        ctx.pixel_ratio = pixel_ratio

    # Open a read-only connection for context queries
    try:
        with open_recording_db(db_path) as conn:
            # Read pixel_ratio from DB if not provided
            if pixel_ratio is None:
                try:
                    pr_row = conn.execute(
                        "SELECT pixel_ratio FROM recording LIMIT 1"
                    ).fetchone()
                    if pr_row and pr_row[0]:
                        ctx.pixel_ratio = float(pr_row[0])
                except (Exception, ValueError):
                    pass  # keep default 2.0

            # Load window events
            from screencap.privacy.context import load_window_events

            if time_range is not None:
                # Scoped load for chunk processor
                try:
                    if has_table(conn, "window_event"):
                        from screencap.privacy.context import WindowContext, domain_from_url

                        has_browser_url = has_column(conn, "window_event", "browser_url")

                        # Include the last event before range start for initial context
                        if has_browser_url:
                            rows = conn.execute(
                                "SELECT timestamp, app_bundle_id, title, window_id, browser_url "
                                "FROM window_event "
                                "WHERE timestamp IS NOT NULL AND timestamp < ? "
                                "ORDER BY timestamp DESC LIMIT 1",
                                (time_range[0],),
                            ).fetchall()
                            rows += conn.execute(
                                "SELECT timestamp, app_bundle_id, title, window_id, browser_url "
                                "FROM window_event "
                                "WHERE timestamp IS NOT NULL "
                                "AND timestamp >= ? AND timestamp < ? "
                                "ORDER BY timestamp",
                                (time_range[0], time_range[1]),
                            ).fetchall()
                        else:
                            rows = conn.execute(
                                "SELECT timestamp, app_bundle_id, title, window_id "
                                "FROM window_event "
                                "WHERE timestamp IS NOT NULL AND timestamp < ? "
                                "ORDER BY timestamp DESC LIMIT 1",
                                (time_range[0],),
                            ).fetchall()
                            rows += conn.execute(
                                "SELECT timestamp, app_bundle_id, title, window_id "
                                "FROM window_event "
                                "WHERE timestamp IS NOT NULL "
                                "AND timestamp >= ? AND timestamp < ? "
                                "ORDER BY timestamp",
                                (time_range[0], time_range[1]),
                            ).fetchall()

                        for row in rows:
                            domain = None
                            raw_url = row[4] if has_browser_url else None
                            if raw_url:
                                domain = domain_from_url(raw_url)
                            ctx.window_events.append(WindowContext(
                                timestamp=float(row[0]),
                                app_bundle_id=row[1] or "",
                                title=row[2] or "",
                                window_id=row[3] or "",
                                domain=domain,
                                browser_url=raw_url or None,
                            ))
                except Exception:
                    logger.debug("Failed to load scoped window events", exc_info=True)
            else:
                # Full load for scrubber path
                try:
                    ctx.window_events = load_window_events(db_path)
                except Exception:
                    logger.debug("Failed to load window events", exc_info=True)

            # Build blocked-app intervals using scrub-time action set.
            # SCRUB_BLOCK_ACTIONS expands beyond capture-time BLOCK_ACTIONS
            # to also cover MASK_WINDOW/TEXT_REDACT/OCR_FALLBACK so that
            # mouse pointer geometry inside redacted/masked content is
            # suppressed in cloud-bound JSONL (R6/R12).
            if evaluator is not None and classifier is not None and ctx.window_events:
                ctx.blocked_intervals = build_blocked_intervals(
                    ctx.window_events, evaluator, classifier,
                    actions=SCRUB_BLOCK_ACTIONS,
                )

            # Build secure-field intervals
            secure_intervals = build_secure_field_intervals(
                db_path, time_range=time_range, conn=conn,
            )
            if secure_intervals:
                ctx.blocked_intervals = merge_intervals(
                    ctx.blocked_intervals, secure_intervals,
                )

            # Collect xref detections (chunk path only — scrubber collects during DB scrub)
            if pipeline is not None and anonymizer is not None:
                ctx.xref_detections = collect_xref_from_db(
                    db_path, pipeline, anonymizer,
                    time_range=time_range, conn=conn,
                )
    except Exception as e:
        # Loud signal: silent failure here disables ALL pointer-geometry
        # suppression for the recording (empty blocked_intervals → no
        # mouse.move drops, no drag-coord nulling). Operators must see
        # this in logs to investigate the underlying cause (busy SQLite,
        # missing window_event table on older recordings, OOM, etc.).
        logger.warning(
            "build_scrub_context failed; pointer suppression DISABLED "
            "for this recording: %s",
            e,
            exc_info=True,
        )

    return ctx


# ---------------------------------------------------------------------------
# Events JSONL scrubbing
# ---------------------------------------------------------------------------


def _scrub_json_recursive(
    obj: dict | list,
    pipeline,
    anonymizer,
    result: ScrubResult,
    _depth: int = 0,
    collect_detections: list[dict] | None = None,
) -> dict | list:
    """Walk a JSON-parsed structure and scrub all string leaves.

    Mutates obj in-place. Depth-limited to 50.

    When *collect_detections* is not None and a key named ``AXValue`` is
    scrubbed, its detection results are appended for cross-referencing
    against keystrokes.
    """
    if _depth > 50:
        return obj

    items = obj.items() if isinstance(obj, dict) else enumerate(obj)
    for key, value in items:
        if isinstance(value, str) and value.strip():
            obj[key], det_result = scrub_text(value, pipeline, anonymizer, result=result)
            if (
                collect_detections is not None
                and key == "AXValue"
                and det_result is not None
                and det_result.detections
            ):
                for det in det_result.detections:
                    original_text = det_result.normalized_text[det.start : det.end]
                    collect_detections.append({
                        "original_text": original_text,
                        "entity_type": det.entity_type,
                        "score": det.score,
                    })
        elif isinstance(value, (dict, list)):
            _scrub_json_recursive(
                value, pipeline, anonymizer, result, _depth + 1, collect_detections
            )

    return obj


def _process_single_key_type(
    event: dict,
    pipeline,
    anonymizer,
    result: ScrubResult,
    db_redactions: list[dict],
) -> None:
    """Detect secrets in a single key.type event and collect DB redaction info."""
    text = event.get("text", "")
    scrubbed, detection_result = scrub_text(text, pipeline, anonymizer, result=result)

    if not detection_result or not detection_result.detections:
        return

    # Build char_pos → child_index map (key.down children with key_char only)
    char_pos = 0
    pos_to_child: dict[int, int] = {}
    for i, child in enumerate(event.get("children", [])):
        if child.get("type") == "key.down" and child.get("key_char"):
            pos_to_child[char_pos] = i
            char_pos += 1

    # Find children within detection spans
    redact_positions: set[int] = set()
    for det in detection_result.detections:
        for pos in range(det.start, det.end):
            if pos in pos_to_child:
                redact_positions.add(pos)

    # Expand to include paired key.up for each redacted key.down
    redact_child_indices: set[int] = set()
    children = event.get("children", [])
    for pos in redact_positions:
        down_idx = pos_to_child[pos]
        redact_child_indices.add(down_idx)
        for j in range(down_idx + 1, len(children)):
            if children[j].get("type") == "key.up":
                redact_child_indices.add(j)
                break

    # Collect DB info BEFORE nulling (need original key_char for matching)
    for idx in redact_child_indices:
        child = children[idx]
        if child.get("key_char"):
            db_redactions.append(
                {
                    "timestamp": child.get("timestamp"),
                    "key_char": child["key_char"],
                    "type": child.get("type"),
                }
            )

    # Now null in JSONL
    event["text"] = scrubbed
    for idx in redact_child_indices:
        child = children[idx]
        child["key_char"] = None
        child["canonical_key_char"] = None


def _process_key_type_events(
    event: dict,
    pipeline,
    anonymizer,
    result: ScrubResult,
    db_redactions: list[dict],
) -> None:
    """Detect secrets in key.type events (top-level and nested in mouse.drag)."""
    if event.get("type") == "key.type":
        _process_single_key_type(event, pipeline, anonymizer, result, db_redactions)
    elif event.get("type") == "mouse.drag":
        for child in event.get("children", []):
            if child.get("type") == "key.type":
                _process_single_key_type(
                    child, pipeline, anonymizer, result, db_redactions
                )


def _cross_reference_key_type(
    event: dict,
    xref_detections: list[ElementStateDetection],
    result: ScrubResult,
    db_redactions: list[dict],
) -> None:
    """Cross-reference element_state detections against a key.type event.

    Matches detections whose timestamps overlap with the event's children
    time range, then uses word-boundary matching to find and null leaked
    keystrokes that the primary detection pass missed.
    """
    children = event.get("children", [])
    if not children or not xref_detections:
        return

    child_timestamps = [
        c.get("timestamp")
        for c in children
        if c.get("timestamp") is not None
    ]
    if not child_timestamps:
        return
    min_ts = min(child_timestamps)
    max_ts = max(child_timestamps)

    matching_dets = [
        d for d in xref_detections
        if any(min_ts <= ts <= max_ts for ts in d.timestamps)
    ]
    if not matching_dets:
        return

    char_pos = 0
    pos_to_child: dict[int, int] = {}
    text_chars: list[str] = []
    for i, child in enumerate(children):
        if child.get("type") == "key.down" and child.get("key_char"):
            pos_to_child[char_pos] = i
            text_chars.append(child["key_char"])
            char_pos += 1

    current_text = "".join(text_chars)
    if not current_text.strip():
        return

    redact_positions: set[int] = set()
    matched_entity_types: list[tuple[int, int, str]] = []

    for det in matching_dets:
        tokens = det.original_text.split()
        pattern = r"(?<![a-zA-Z0-9])" + re.escape(det.original_text) + r"(?![a-zA-Z0-9])"
        m = re.search(pattern, current_text, re.IGNORECASE)
        if m:
            for pos in range(m.start(), m.end()):
                if pos in pos_to_child:
                    redact_positions.add(pos)
            matched_entity_types.append((m.start(), m.end(), det.entity_type))
            continue

        for token in tokens:
            if len(token.strip()) < 3:
                continue
            token_pattern = (
                r"(?<![a-zA-Z0-9])" + re.escape(token) + r"(?![a-zA-Z0-9])"
            )
            for m in re.finditer(token_pattern, current_text, re.IGNORECASE):
                for pos in range(m.start(), m.end()):
                    if pos in pos_to_child:
                        redact_positions.add(pos)
                matched_entity_types.append((m.start(), m.end(), det.entity_type))

    if not redact_positions:
        return

    redact_child_indices: set[int] = set()
    for pos in redact_positions:
        down_idx = pos_to_child[pos]
        redact_child_indices.add(down_idx)
        for j in range(down_idx + 1, len(children)):
            if children[j].get("type") == "key.up":
                redact_child_indices.add(j)
                break

    for idx in redact_child_indices:
        child = children[idx]
        if child.get("key_char"):
            db_redactions.append({
                "timestamp": child.get("timestamp"),
                "key_char": child["key_char"],
                "type": child.get("type"),
            })

    for idx in redact_child_indices:
        child = children[idx]
        child["key_char"] = None
        child["canonical_key_char"] = None

    text = current_text
    for start, end, entity_type in sorted(matched_entity_types, reverse=True):
        text = text[:start] + f"<{entity_type}>" + text[end:]
    event["text"] = text

    for det in matching_dets:
        result.audit_entries.append(
            AuditEntry(
                timestamp=event.get("timestamp", 0.0),
                surface="keystroke_xref",
                action="TEXT_REDACT",
                reason=ReasonCode.ELEMENT_STATE_XREF,
                evidence_type=f"{det.entity_type}:len={len(det.original_text)}",
            )
        )


def scrub_events_jsonl(
    events_jsonl: Path,
    pipeline,
    anonymizer,
    *,
    ctx: ScrubContext | None = None,
    blocked_intervals: list[BlockedInterval] | None = None,
    xref_detections: list[ElementStateDetection] | None = None,
    db_dir: Path | None = None,
    result: ScrubResult | None = None,
) -> bool:
    """Scrub PII from an events JSONL file. Returns True if errors occurred.

    Shared between the scrubber (full recording scrub) and the chunk processor
    (inline cloud-intent scrub). On error, cleans up the .tmp file but does NOT
    delete/rename the original — callers decide their own error policy.

    When *ctx* is provided, blocked_intervals and xref_detections are taken
    from it (explicit kwargs override ctx values for backward compat).
    """
    _result = result if result is not None else ScrubResult()

    # Resolve intervals/xref from context or explicit args
    _blocked = blocked_intervals
    if _blocked is None and ctx is not None:
        _blocked = ctx.blocked_intervals
    _blocked = _blocked or []
    blocked_starts = [iv.start for iv in _blocked]

    _xref = xref_detections
    if _xref is None and ctx is not None:
        _xref = ctx.xref_detections

    had_errors = False
    db_redactions: list[dict] = []
    tmp_path = str(events_jsonl) + ".tmp"

    with open(events_jsonl, "r", encoding="utf-8") as infile, \
         open(tmp_path, "w", encoding="utf-8") as outfile:
        for raw_line in infile:
            raw_line = raw_line.rstrip("\n")
            if not raw_line.strip():
                outfile.write(raw_line + "\n")
                continue

            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                logger.warning("Malformed JSON line in events.jsonl")
                had_errors = True
                continue

            if event.get("_meta"):
                outfile.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            # R6/R12: Drop standalone mouse.move events whose merged span
            # crosses a blocked interval (SCRUB_BLOCK_ACTIONS). Pointer
            # geometry leaks coarse interaction patterns inside redacted/
            # masked content — coordinates on retained events are unaffected.
            #
            # ``last_timestamp`` is set by ``merge_consecutive_mouse_move_events``
            # when a run of moves is collapsed; the range check covers the
            # case where the merge START lands BEFORE a blocked interval but
            # the merge END (last waypoint) lands INSIDE it. Pre-fix, the
            # point-lookup at ``timestamp`` slipped these merged events past
            # the drop check and leaked in-interval coordinates via ``path``
            # waypoints / ``x``/``y`` (last position).
            event_ts = event.get("timestamp", 0.0)
            if event.get("type") == "mouse.move":
                end_ts = event.get("last_timestamp")
                if end_ts is None:
                    end_ts = event_ts
                if _interval_intersects(
                    event_ts, end_ts, _blocked, blocked_starts,
                ) is not None:
                    continue

            # R6/R12: Filter mouse.move children of mouse.drag events whose
            # merged span crosses blocked intervals. The drag itself is
            # retained (its content is nulled below if the drag is in a
            # blocked interval); only in-interval mouse.move waypoints
            # are dropped from its children list. Same range-overlap
            # semantics as the standalone drop above.
            if event.get("type") == "mouse.drag" and _blocked:
                children = event.get("children")
                if children:
                    event["children"] = [
                        c for c in children
                        if not (
                            c.get("type") == "mouse.move"
                            and _interval_intersects(
                                c.get("timestamp", 0.0),
                                c.get("last_timestamp")
                                    if c.get("last_timestamp") is not None
                                    else c.get("timestamp", 0.0),
                                _blocked,
                                blocked_starts,
                            ) is not None
                        )
                    ]

            # Check blocked-app intervals
            blocked = find_blocked_interval(event_ts, _blocked, blocked_starts)
            if blocked is not None:
                null_event_content(event)
                _result.audit_entries.append(
                    AuditEntry(
                        timestamp=event_ts,
                        surface="event",
                        action=blocked.action.value,
                        reason=blocked.reason,
                    )
                )
                outfile.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            # Targeted key.type detection for combined-text secrets
            _process_key_type_events(
                event, pipeline, anonymizer, _result, db_redactions
            )

            # Cross-reference element_state detections against keystrokes
            if _xref:
                if event.get("type") == "key.type":
                    _cross_reference_key_type(
                        event, _xref, _result, db_redactions
                    )
                elif event.get("type") == "mouse.drag":
                    for child in event.get("children", []):
                        if child.get("type") == "key.type":
                            _cross_reference_key_type(
                                child, _xref, _result, db_redactions
                            )

            # Comprehensive scrub: run recursive walker on ALL events
            _scrub_json_recursive(event, pipeline, anonymizer, _result)

            outfile.write(json.dumps(event, ensure_ascii=False) + "\n")

    # Batch DB redaction (single connection, single commit)
    if db_redactions and db_dir is not None:
        _redact_keystroke_db_rows(db_dir, db_redactions)

    # Atomic rename on success, clean up .tmp on error
    if had_errors:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    else:
        os.rename(tmp_path, str(events_jsonl))

    return had_errors


def _redact_keystroke_db_rows(dst: Path, redactions: list[dict]) -> None:
    """Batch-redact key_char/canonical_key_char in action_event rows."""
    from screencap.catalog import find_db

    db_path = find_db(dst)
    if db_path is None:
        return

    with open_recording_db(db_path, read_only=False) as conn:
        try:
            if not has_table(conn, "action_event"):
                return

            ids_to_redact: list[int] = []
            for r in redactions:
                name = "press" if r["type"] == "key.down" else "release"
                row = conn.execute(
                    "SELECT id FROM action_event "
                    "WHERE name = ? AND timestamp = ? AND key_char = ? LIMIT 1",
                    (name, r["timestamp"], r["key_char"]),
                ).fetchone()
                if row:
                    ids_to_redact.append(row[0])

            if ids_to_redact:
                for i in range(0, len(ids_to_redact), 500):
                    chunk = ids_to_redact[i : i + 500]
                    placeholders = ",".join("?" * len(chunk))
                    conn.execute(
                        f"UPDATE action_event "
                        f"SET key_char = NULL, canonical_key_char = NULL "
                        f"WHERE id IN ({placeholders})",
                        chunk,
                    )
                conn.commit()
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# Transcript scrubbing
# ---------------------------------------------------------------------------


def scrub_transcripts(
    paths: list[Path],
    pipeline,
    anonymizer,
    *,
    result: ScrubResult | None = None,
) -> None:
    """Scrub transcript txt/json files, including words[*].word.

    Handles both .txt (plain text) and .json (structured with text,
    segments[].text, and words[].word) formats.
    """
    _result = result if result is not None else ScrubResult()

    for path in paths:
        if not path.exists():
            continue
        if path.suffix == ".json":
            _scrub_transcript_json(path, pipeline, anonymizer, _result)
        elif path.suffix == ".txt":
            _scrub_transcript_txt(path, pipeline, anonymizer, _result)


def _scrub_transcript_json(
    path: Path,
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> None:
    """Scrub PII from transcript.json, including words[*].word."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read transcript JSON: {e}")
        return

    if isinstance(data, dict):
        if "text" in data and isinstance(data["text"], str):
            data["text"], _ = scrub_text(data["text"], pipeline, anonymizer, result=result)

        if "segments" in data and isinstance(data["segments"], list):
            for seg in data["segments"]:
                if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                    seg["text"], _ = scrub_text(seg["text"], pipeline, anonymizer, result=result)

        # G5: words[*].word — previously missing from chunk processor
        if "words" in data and isinstance(data["words"], list):
            for word_entry in data["words"]:
                if isinstance(word_entry, dict) and isinstance(
                    word_entry.get("word"), str
                ):
                    word_entry["word"], _ = scrub_text(
                        word_entry["word"], pipeline, anonymizer, result=result,
                    )

    # Atomic write
    tmp_path = str(path) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp_path, str(path))


def _scrub_transcript_txt(
    path: Path,
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> None:
    """Scrub PII from transcript.txt."""
    try:
        text = path.read_text(encoding="utf-8")
        scrubbed, _ = scrub_text(text, pipeline, anonymizer, result=result)
        tmp_path = str(path) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(scrubbed)
        os.rename(tmp_path, str(path))
    except OSError as e:
        logger.warning(f"Could not scrub transcript txt: {e}")


# ---------------------------------------------------------------------------
# Screenshot masking
# ---------------------------------------------------------------------------


def _pad_bbox(
    bbox: tuple[int, int, int, int], pad: int, im_w: int, im_h: int,
) -> tuple[int, int, int, int]:
    """Add padding to a bounding box, clamped to image bounds."""
    x, y, w, h = bbox
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(im_w, x + w + pad)
    y2 = min(im_h, y + h + pad)
    return (x1, y1, x2 - x1, y2 - y1)


def ocr_mask_screenshot(
    image_path: Path,
    pipeline: object,
    ocr: object,
    roi: tuple[float, float, float, float] | None = None,
) -> list:
    """OCR a screenshot, detect PII, return mask regions.

    Returns an empty list if no PII is detected.  Raises on OCR failure
    (caller is responsible for fail-closed behavior).

    When *roi* is provided it is passed through to
    ``ocr.recognize(image_path, roi=roi)`` to restrict OCR to a sub-region.
    VisionOcr remaps ROI-relative bounding boxes to full-image coordinates
    internally, so callers do not need offset correction.

    Detection errors are caught **per text block**: if ``pipeline.detect()``
    raises (e.g. ``AllDetectorsFailedError``), the entire text block is masked
    as a precaution rather than escalating to MASK_WINDOW for the whole image.
    """
    from screencap.privacy import normalize_text
    from screencap.privacy.masking import MaskRegion
    from screencap.privacy.ocr import build_offset_map

    result = ocr.recognize(image_path, roi=roi)
    if not result.text_blocks:
        return []

    regions: list[MaskRegion] = []
    for block in result.text_blocks:
        try:
            det_result = pipeline.detect(block.text)
        except Exception:
            # Detection failure on this block — fail closed to full-block mask
            x, y, w, h = _pad_bbox(block.bbox, 3, result.image_width, result.image_height)
            regions.append(MaskRegion(x=x, y=y, width=w, height=h, label="DETECTION_ERROR"))
            continue

        if not det_result.detections:
            continue

        # Build offset mapping: detection offsets are into normalized text,
        # but char_bboxes expects offsets into the original OCR text.
        normalized = normalize_text(block.text)
        if normalized != block.text:
            offset_map = build_offset_map(block.text, normalized)
        else:
            offset_map = None  # identity — no remapping needed

        for det in det_result.detections:
            if offset_map is not None:
                orig_start = offset_map[det.start]
                orig_end = offset_map[det.end]
                orig_len = orig_end - orig_start
            else:
                orig_start = det.start
                orig_len = det.end - det.start

            char_box = block.char_bboxes(orig_start, orig_len)
            if char_box is not None:
                x, y, w, h = _pad_bbox(char_box, 3, result.image_width, result.image_height)
            else:
                # boundingBoxForRange failed — mask entire text block
                x, y, w, h = _pad_bbox(block.bbox, 3, result.image_width, result.image_height)
            regions.append(MaskRegion(x=x, y=y, width=w, height=h, label=det.entity_type))

    return regions


def _active_window_bounds(
    geom: object,
    bundle_id: str,
    pixel_ratio: float,
    img_w: int,
    img_h: int,
) -> tuple[int, int, int, int] | None:
    """Return pixel bounds (x, y, w, h) of the active window, or None.

    Finds the frontmost window matching *bundle_id* in the geometry snapshot.
    Falls back to the first window if no match. Returns None when no geometry.
    """
    if geom is None or not geom.windows:
        return None

    from screencap.privacy.masking import _window_to_pixel_rect

    target = None
    for win in geom.windows:
        if win.get("bundle_id") == bundle_id:
            target = win
            break
    if target is None:
        target = geom.windows[0]

    rect = _window_to_pixel_rect(
        target,
        pixel_ratio,
        geom.display_origin[0],
        geom.display_origin[1],
        img_w,
        img_h,
    )
    if rect is None:
        return None
    x1, y1, x2, y2 = rect
    return (x1, y1, x2 - x1, y2 - y1)


def _active_window_roi(
    bounds: tuple[int, int, int, int],
    img_w: int,
    img_h: int,
) -> tuple[float, float, float, float]:
    """Convert pixel bounds to Vision normalized ROI (bottom-left origin).

    Returns (x, y, width, height) in 0.0-1.0 space, clamped to image bounds.
    Vision coordinate system: origin at bottom-left, y increases upward.
    """
    x, y, w, h = bounds
    x1 = max(0, x) / img_w
    y1 = max(0, y) / img_h
    x2 = min(img_w, x + w) / img_w
    y2 = min(img_h, y + h) / img_h

    roi_w = x2 - x1
    roi_h = y2 - y1

    # Flip Y: Vision origin is bottom-left, PIL origin is top-left
    roi_y = 1.0 - y1 - roi_h

    return (x1, roi_y, roi_w, roi_h)


def mask_screenshots(
    screenshots_dir: Path,
    ctx: ScrubContext,
    *,
    db_path: Path | None = None,
    result: ScrubResult | None = None,
) -> None:
    """Policy-aware screenshot masking with background window support.

    Shared between the full scrubber and the chunk processor. Adopts the
    full scrubber's behavior (associate_screenshot, background masking,
    respect_z_order, MASK_REGION handling, OCR_FALLBACK, EXCLUDE→delete).

    EXCLUDE → delete file.
    MASK_WINDOW → selective mask via geometry, fallback to full-frame.
    MASK_REGION → pane-level structural mask.
    OCR_FALLBACK → OCR-based PII masking, fail closed to MASK_WINDOW.
    TEXT_REDACT / ALLOW → keep file (but check background windows).
    """
    if not screenshots_dir.is_dir():
        return
    if ctx.evaluator is None or ctx.classifier is None:
        return

    _result = result if result is not None else ScrubResult()
    evaluator = ctx.evaluator
    classifier = ctx.classifier

    from screencap.privacy.context import (
        _load_geometry_row,
        associate_screenshot,
        parse_screenshot_timestamp,
    )
    from screencap.privacy.masking import (
        MaskStrategy,
        mask_screenshot,
        window_regions_from_geometry,
    )

    # Try to create OCR + detection pipeline for OCR_FALLBACK.
    _ocr = None
    _pipeline = None
    try:
        from screencap.privacy.ocr import VisionOcr
        _ocr = VisionOcr()
        from screencap.privacy import create_default_pipeline
        _pipeline = create_default_pipeline()
    except ImportError:
        logger.warning(
            "pyobjc-framework-Vision not installed; OCR_FALLBACK will use MASK_WINDOW"
        )
    except Exception:
        logger.warning(
            "Failed to initialize OCR engine; OCR_FALLBACK will use MASK_WINDOW"
        )

    window_timestamps = [w.timestamp for w in ctx.window_events] if ctx.window_events else []

    with ExitStack() as stack:
        # Hoist the window_geometry has_table check above the per-screenshot
        # loop so the loop body uses _load_geometry_row directly (skips the
        # per-call introspection that load_window_geometry would otherwise
        # repeat). ExitStack guarantees the geometry connection closes on any
        # exception inside the loop.
        _geom_conn: Connection | None = None
        has_geometry = False
        if db_path is not None:
            try:
                _geom_conn = stack.enter_context(open_recording_db(db_path))
                has_geometry = has_table(_geom_conn, "window_geometry")
            except Exception:
                _geom_conn = None
                has_geometry = False

        def _maybe_geom(ts: float):
            if _geom_conn is None or not has_geometry:
                return None
            try:
                return _load_geometry_row(_geom_conn, ts)
            except Exception:
                return None

        # dHash cache for OCR dedup — single-entry, local to this invocation.
        # Tighter threshold (5) than capture-time dedup (8) because a false
        # cache hit means reusing OCR results from a slightly different image.
        _DHASH_THRESHOLD = 5
        _prev_hash: int | None = None
        _prev_bounds: tuple[int, int, int, int] | None = None
        _prev_ocr_regions: list | None = None

        for img_path in sorted(screenshots_dir.glob("*.jpg")):
            ts = parse_screenshot_timestamp(img_path.name)
            if ts is None:
                continue

            meta = associate_screenshot(
                ts, ctx.window_events,
                _window_timestamps=window_timestamps,
            )
            ctx_class = classifier.classify(meta)
            decision = evaluator.evaluate(ctx_class, meta)

            actual_action = decision.action

            if decision.action == PrivacyAction.EXCLUDE:
                img_path.unlink()
            elif decision.action == PrivacyAction.MASK_WINDOW:
                selective_applied = False
                if has_geometry:
                    try:
                        geom = _maybe_geom(ts)
                        if geom is not None:
                            from PIL import Image

                            with Image.open(img_path) as probe:
                                img_w, img_h = probe.size
                            regions = window_regions_from_geometry(
                                geom.windows, img_w, img_h, ctx.pixel_ratio,
                                classifier, evaluator,
                                display_origin=geom.display_origin,
                                respect_z_order=True,
                            )
                            if regions:
                                mask_screenshot(
                                    img_path,
                                    ctx_class.context_class,
                                    regions=regions,
                                )
                                selective_applied = True
                            else:
                                selective_applied = True
                    except Exception as exc:
                        logger.debug(
                            f"Selective masking failed for {img_path.name} ({exc})"
                        )

                if not selective_applied:
                    try:
                        mask_screenshot(
                            img_path,
                            ctx_class.context_class,
                            strategy=MaskStrategy.FULL_WINDOW,
                            app_hint=meta.bundle_id,
                        )
                    except Exception:
                        img_path.unlink()
                        actual_action = PrivacyAction.EXCLUDE
            elif decision.action == PrivacyAction.MASK_REGION:
                try:
                    mask_screenshot(
                        img_path,
                        ctx_class.context_class,
                        strategy=MaskStrategy.PANE,
                        app_hint=meta.bundle_id,
                    )
                except Exception:
                    try:
                        mask_screenshot(
                            img_path,
                            ctx_class.context_class,
                            strategy=MaskStrategy.FULL_WINDOW,
                            app_hint=meta.bundle_id,
                        )
                        actual_action = PrivacyAction.MASK_WINDOW
                    except Exception:
                        img_path.unlink()
                        actual_action = PrivacyAction.EXCLUDE
            elif decision.action == PrivacyAction.OCR_FALLBACK:
                if _ocr is not None and _pipeline is not None:
                    try:
                        regions = ocr_mask_screenshot(img_path, _pipeline, _ocr)
                        if regions:
                            mask_screenshot(img_path, ctx_class.context_class, regions=regions)
                            actual_action = PrivacyAction.OCR_FALLBACK
                        else:
                            # No PII detected — leave screenshot as-is
                            actual_action = PrivacyAction.ALLOW
                    except Exception:
                        # Fail closed: OCR error → MASK_WINDOW
                        try:
                            mask_screenshot(
                                img_path,
                                ctx_class.context_class,
                                strategy=MaskStrategy.FULL_WINDOW,
                                app_hint=meta.bundle_id,
                            )
                            actual_action = PrivacyAction.MASK_WINDOW
                        except Exception:
                            img_path.unlink()
                            actual_action = PrivacyAction.EXCLUDE
                else:
                    # Vision not available — fall back to MASK_WINDOW
                    try:
                        mask_screenshot(
                            img_path,
                            ctx_class.context_class,
                            strategy=MaskStrategy.FULL_WINDOW,
                            app_hint=meta.bundle_id,
                        )
                        actual_action = PrivacyAction.MASK_WINDOW
                    except Exception:
                        img_path.unlink()
                        actual_action = PrivacyAction.EXCLUDE

            # Load geometry once — used by both OCR and background masking.
            geom = None
            if has_geometry and img_path.exists():
                geom = _maybe_geom(ts)

            # --- Phase 2: OCR pass on foreground content ---
            # Run BEFORE background masking so OCR sees the original
            # foreground content (bg masking can destroy text).
            ocr_regions: list = []
            ocr_ran = False
            cache_hit = False
            if (
                _ocr is not None
                and _pipeline is not None
                and img_path.exists()
                and decision.action in (PrivacyAction.ALLOW, PrivacyAction.TEXT_REDACT)
                and actual_action not in (PrivacyAction.EXCLUDE, PrivacyAction.MASK_WINDOW)
            ):
                from PIL import Image

                from screencap.engine.dedup import dhash, hamming_distance

                # Open image for dimensions + dHash; skip OCR if unreadable
                current_hash = None
                img_w_ocr = img_h_ocr = 0
                try:
                    with Image.open(img_path) as img:
                        img_w_ocr, img_h_ocr = img.size
                        try:
                            current_hash = dhash(img)
                        except Exception:
                            pass  # dHash failed — skip cache, still run OCR
                except Exception:
                    pass  # image unreadable — skip entire OCR pass

                if img_w_ocr > 0 and img_h_ocr > 0:
                    active_bounds = _active_window_bounds(
                        geom, meta.bundle_id, ctx.pixel_ratio,
                        img_w_ocr, img_h_ocr,
                    )

                    cache_hit = (
                        current_hash is not None
                        and _prev_hash is not None
                        and hamming_distance(current_hash, _prev_hash) <= _DHASH_THRESHOLD
                        and active_bounds == _prev_bounds
                    )

                    if cache_hit and _prev_ocr_regions is not None:
                        ocr_regions = _prev_ocr_regions
                    else:
                        cache_hit = False
                        roi = (
                            _active_window_roi(active_bounds, img_w_ocr, img_h_ocr)
                            if active_bounds
                            else None
                        )
                        try:
                            ocr_regions = ocr_mask_screenshot(
                                img_path, _pipeline, _ocr, roi=roi,
                            )
                            ocr_ran = True
                        except Exception:
                            # Fail-closed: OCR error → MASK_WINDOW
                            try:
                                mask_screenshot(
                                    img_path,
                                    ctx_class.context_class,
                                    strategy=MaskStrategy.FULL_WINDOW,
                                    app_hint=meta.bundle_id,
                                )
                                actual_action = PrivacyAction.MASK_WINDOW
                            except Exception:
                                img_path.unlink()
                                actual_action = PrivacyAction.EXCLUDE

                    # Apply OCR mask regions
                    if ocr_regions and actual_action not in (
                        PrivacyAction.MASK_WINDOW, PrivacyAction.EXCLUDE,
                    ):
                        try:
                            mask_screenshot(
                                img_path, ctx_class.context_class,
                                regions=ocr_regions,
                            )
                        except Exception:
                            try:
                                mask_screenshot(
                                    img_path,
                                    ctx_class.context_class,
                                    strategy=MaskStrategy.FULL_WINDOW,
                                    app_hint=meta.bundle_id,
                                )
                                actual_action = PrivacyAction.MASK_WINDOW
                            except Exception:
                                img_path.unlink()
                                actual_action = PrivacyAction.EXCLUDE

                    # Update dHash cache
                    if current_hash is not None:
                        _prev_hash = current_hash
                        _prev_bounds = active_bounds
                        _prev_ocr_regions = ocr_regions

            # Background masking for screenshots that keep foreground content.
            # Skip when OCR already handled foreground PII — bg masking with
            # incomplete geometry can over-mask the foreground content.
            bg_masked = False
            _skip_bg = ocr_ran or cache_hit
            if not _skip_bg and actual_action in (
                PrivacyAction.ALLOW,
                PrivacyAction.TEXT_REDACT,
                PrivacyAction.OCR_FALLBACK,
            ):
                if has_geometry and img_path.exists() and geom is not None:
                    try:
                        from PIL import Image

                        with Image.open(img_path) as probe:
                            img_w, img_h = probe.size
                        regions = window_regions_from_geometry(
                            geom.windows, img_w, img_h, ctx.pixel_ratio,
                            classifier, evaluator,
                            display_origin=geom.display_origin,
                            mask_actions=_BG_MASK_ACTIONS,
                            respect_z_order=True,
                        )
                        if regions:
                            mask_screenshot(
                                img_path,
                                ctx_class.context_class,
                                regions=regions,
                            )
                            bg_masked = True
                    except Exception as exc:
                        logger.debug(
                            f"Background masking failed for {img_path.name} ({exc})"
                        )
                        try:
                            mask_screenshot(
                                img_path,
                                ctx_class.context_class,
                                strategy=MaskStrategy.FULL_WINDOW,
                                app_hint=meta.bundle_id,
                            )
                            bg_masked = True
                        except Exception:
                            img_path.unlink()
                            actual_action = PrivacyAction.EXCLUDE

            # Build audit trail with OCR detail
            ocr_detail = ""
            if _ocr is not None and decision.action in (
                PrivacyAction.ALLOW, PrivacyAction.TEXT_REDACT,
            ):
                if actual_action in (PrivacyAction.MASK_WINDOW, PrivacyAction.EXCLUDE):
                    ocr_detail = "+ocr_failed"
                elif cache_hit:
                    ocr_detail = f"+ocr_cache_hit({len(ocr_regions)})"
                elif ocr_ran and ocr_regions:
                    ocr_detail = f"+ocr_masked({len(ocr_regions)})"
                elif ocr_ran:
                    ocr_detail = "+ocr_clean"

            _result.audit_entries.append(
                AuditEntry(
                    timestamp=ts,
                    surface="screenshot",
                    action=actual_action.value,
                    reason=f"{decision.reason}+background_windows_masked{ocr_detail}"
                        if bg_masked else f"{decision.reason}{ocr_detail}",
                    context_class=ctx_class.context_class.value,
                    evidence_type=(
                        f"{ctx_class.confidence}"
                        + ("+geometry" if bg_masked else "")
                        + ("+ocr" if (ocr_ran or cache_hit) else "")
                    ),
                )
        )


# ---------------------------------------------------------------------------
# Manifest scrubbing
# ---------------------------------------------------------------------------


def scrub_manifest(
    path: Path,
    pipeline,
    anonymizer,
    *,
    result: ScrubResult | None = None,
) -> None:
    """Scrub manifest text fields (legacy v1 only — v2 has no text fields)."""
    if not path.exists():
        return

    _result = result if result is not None else ScrubResult()
    data = json.loads(path.read_text(encoding="utf-8"))

    # v2 manifests have no text fields to scrub
    if data.get("format_version", 0) >= 2:
        return

    # Legacy manifest: scrub task titles
    from screencap.task_manifest import _derive_task_name

    for task in data.get("tasks", []):
        title = task.get("dominant_title", "")
        if title:
            scrubbed_title, _ = scrub_text(title, pipeline, anonymizer, result=_result)
            task["dominant_title"] = scrubbed_title
            task["derived_name"] = _derive_task_name({
                "bundle_id": task.get("dominant_app", ""),
                "title": scrubbed_title,
            })
    if data.get("tasks"):
        primary = max(
            data["tasks"],
            key=lambda t: t.get("end_ts", 0) - t.get("start_ts", 0),
        )["derived_name"]
        data.setdefault("summary", {})["primary_task"] = primary

    tmp_path = str(path) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp_path, str(path))
