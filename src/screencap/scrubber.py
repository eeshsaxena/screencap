"""Privacy scrubbing for screencap recordings.

The :class:`Scrubber` class is the single source of truth for the scrub
step order. ``run()`` is used by ``screencap scrub`` (post-hoc, full-dir);
``run_chunk()`` is used by the live chunk processor for per-chunk uploads.
The free functions below are module-private building blocks — callers
outside this module should go through :class:`Scrubber`.
"""

from __future__ import annotations

import bisect
import contextlib
import dataclasses
import json
import logging
import os
import re
import shutil
import stat
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from screencap.catalog import find_db
from screencap.config import get_recordings_dir, resolve_recording_dir
from screencap.privacy.actions import (
    BLOCK_ACTIONS,
    KEYSTROKE_CONTENT_FIELDS,
    MOUSE_COORDINATE_FIELDS,
    SCRUB_BLOCK_ACTIONS,
    SCRUB_CONTENT_NULL_ACTIONS,
    PrivacyAction,
)
from screencap.privacy.policy import DEFAULT_TRANSITION_HOLD_SECONDS
from screencap.privacy.reasons import AuditEntry, ReasonCode
from screencap.recording_db import Connection, has_column, has_table, open_recording_db

logger = logging.getLogger(__name__)
console = Console()

# Sentinel written into a field when every detector failed on it — the
# fail-closed posture (see ``scrub_text``). Surfaced as review evidence
# (R14) rather than treated as displayable content.
SCRUB_FAILED_SENTINEL = "<SCRUB_FAILED>"

# Schema version for ``privacy_audit.json``. Bumped when the on-disk audit
# shape changes so downstream readers (review-data) can branch defensively.
AUDIT_SCHEMA_VERSION = 1

# Completion sentinel + provenance for the "reviewed == uploaded" reuse guard
# (U4). Written as the FINAL step of a fully-successful scrub; its presence
# gates reuse and its contents prove the scrubbed copy is current and was built
# the way upload would build it. The leading dot keeps it out of the upload
# file set (dotfile filter), like ``.video_review.mp4``.
SCRUB_SENTINEL_NAME = ".scrub_complete"

# Bump when scrub *behavior* changes so dirs scrubbed by an older scrubber are
# rebuilt rather than reused (a stale-redaction guard). This is the scrubber's
# own logic version, independent of the package release version.
SCRUB_PROVENANCE_VERSION = 1

# Bump when the post-hoc VIDEO-MASKING behavior changes (SCR-126 Fix 2/3) so a
# masked_video/ copy produced by older mask logic is re-masked rather than reused
# or re-uploaded. This is the mask-logic analogue of SCRUB_PROVENANCE_VERSION and
# is recorded in the masked-video provenance (U3) and checked by the reuse +
# convergence-fast-path gates. Version 1 is the initial SCR-126 mask logic
# (real-extent coverage + the fail-closed in-loop span guard).
MASK_PROVENANCE_VERSION = 1

# Source files whose content determines the scrubbed output — hashed into the
# provenance so an upload-path mutation of the original (events re-export, WAL
# checkpoint, chunk recovery) or any other change invalidates reuse. Media and
# screenshots are excluded: they are large and the upload path never mutates
# them (screenshot masking is driven by these DB/event inputs).
#
# ``recording.db-wal`` IS hashed: a committed-but-uncheckpointed change lives in
# the WAL while ``recording.db`` bytes stay identical, so a db-only hash would
# read such a source as "unchanged" and reuse a stale scrubbed copy. The volatile
# ``-shm`` sidecar is deliberately NOT hashed — it is regenerated and churns on
# read-only access, which would force spurious rebuilds without detecting any
# real content change.
_SOURCE_HASH_GLOBS = (
    "recording.db",
    "recording.db-wal",
    "events*.jsonl",
    "transcript*.json",
    "transcript*.txt",
    "system_metrics.json",
    "chunk_*_manifest.json",
    ".recording_intent",
)

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
    """Summary of a scrub operation.

    ``blocked_intervals`` and ``fail_closed_redactions`` are first-class,
    review-consumable evidence (R8/R13/R14): the former drives risky-moment
    flags and the redaction summary, the latter the distinct "couldn't
    analyze — removed to be safe" signal. Both are export-safe — timestamps
    and categories only, never the redacted value.
    """

    output_dir: Path = field(default_factory=Path)
    entity_counts: Counter = field(default_factory=Counter)
    deleted_files: list[str] = field(default_factory=list)
    audit_entries: list[AuditEntry] = field(default_factory=list)
    rule_based_redactions: list[str] = field(default_factory=list)
    blocked_intervals: list[BlockedInterval] = field(default_factory=list)
    # Export-safe markers of fields the scrubber could not analyze and
    # therefore removed (the ``<SCRUB_FAILED>`` sentinel). Each entry is
    # ``{"timestamp": float, "surface": str}`` — never the raw value.
    fail_closed_redactions: list[dict] = field(default_factory=list)


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

    from screencap.redaction import AllDetectorsFailedError

    try:
        detection_result = pipeline.detect(text)
    except AllDetectorsFailedError:
        return SCRUB_FAILED_SENTINEL, None
    except Exception:
        logger.debug("scrub_text: unexpected pipeline error", exc_info=True)
        return SCRUB_FAILED_SENTINEL, None

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
    ``i.start <= end_ts AND i.end > start_ts``. A merged move whose
    ``end_ts`` (last waypoint timestamp) lands exactly on ``i.start``
    DOES intersect — the last waypoint is AT ``i.start``, which is
    inside the half-open ``[i.start, i.end)`` interval (start
    inclusive). The early-return ``end_ts <= start_ts`` handles the
    point-lookup case before we reach the forward branch, so the
    inclusive ``i.start <= end_ts`` check there only triggers when
    ``end_ts > start_ts`` (a real range).

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

    # Otherwise look for an interval starting after start_ts but before
    # end_ts (the merge spans into a later blocked interval).
    # bisect_right returns the first index whose start > start_ts; we
    # walk forward checking i.start <= end_ts. The check is INCLUSIVE
    # because the move's last waypoint is AT end_ts: when
    # end_ts == i.start, the last waypoint lands exactly on the start
    # of the blocked interval, which IS inside the half-open
    # ``[i.start, i.end)`` interval (start inclusive). The early-return
    # ``end_ts <= start_ts`` above handles the point-lookup case before
    # we reach this branch, so ``<=`` here only triggers for real ranges
    # (end_ts > start_ts).
    if end_ts <= start_ts:
        return None
    idx = bisect.bisect_right(_starts, start_ts)
    if idx < len(intervals) and intervals[idx].start <= end_ts:
        return intervals[idx]
    return None


def null_pointer_geometry(event: dict) -> None:
    """Zero mouse coordinate fields on retained mouse events. Recurses.

    Used when a mouse event's timestamp lands inside a SCRUB_BLOCK_ACTIONS
    interval — pointer geometry leaks coarse interaction patterns inside
    redacted/masked content, regardless of whether text content is
    sensitive (TEXT_REDACT/OCR_FALLBACK keystrokes go through PII
    detection; their pointer coordinates do not).
    """
    event_type = event.get("type", "")
    if isinstance(event_type, str) and event_type.startswith("mouse."):
        for fld in MOUSE_COORDINATE_FIELDS:
            if fld in event:
                event[fld] = None
    for child in event.get("children", []):
        null_pointer_geometry(child)


def null_text_content(event: dict) -> None:
    """Null keystroke text + window.switch title/domain. Recurses into children.

    Recurses into ``children`` to cover ``key.type`` nested inside
    ``mouse.drag``.

    Used when an event's timestamp lands inside a SCRUB_CONTENT_NULL_ACTIONS
    interval (EXCLUDE/MASK_WINDOW) — text content is wholesale-suppressed
    in those contexts.
    """
    for fld in KEYSTROKE_CONTENT_FIELDS:
        if fld in event:
            event[fld] = None
    if event.get("type") == "window.switch":
        event["window_title"] = None
        event["domain"] = None
    for child in event.get("children", []):
        null_text_content(child)


def null_event_content(event: dict) -> None:
    """Null both pointer geometry and text content on ``event``.

    Sole surviving caller is ``tests/test_domain_propagation.py``, which uses
    this to assert window-title nulling on ``window.switch`` events.
    Production paths call the granular helpers (``null_pointer_geometry`` /
    ``null_text_content``) directly so the broader SCRUB_BLOCK_ACTIONS
    pointer suppression runs independently of SCRUB_CONTENT_NULL_ACTIONS
    text nulling. Delete once those tests migrate to the granular helpers.
    """
    null_pointer_geometry(event)
    null_text_content(event)


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
            from screencap.redaction.geometry import load_window_events

            if time_range is not None:
                # Scoped load for chunk processor
                try:
                    if has_table(conn, "window_event"):
                        from screencap.privacy.classify import domain_from_url
                        from screencap.redaction.geometry import WindowContext

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


def _write_event_recording_fail_closed(
    event: dict, event_ts: float, outfile, result: ScrubResult
) -> None:
    """Serialize *event*, record a fail-closed marker if it carries the
    sentinel, then write it.

    A field that tripped ``<SCRUB_FAILED>`` (every detector failed, so the
    scrubber removed it to be safe) is export-safe evidence the review UI
    surfaces distinctly (R14). The marker carries the event timestamp and
    surface only — never the field's raw value (which is now the sentinel
    anyway). One marker per event, regardless of how many fields tripped.
    """
    serialized = json.dumps(event, ensure_ascii=False)
    if SCRUB_FAILED_SENTINEL in serialized:
        result.fail_closed_redactions.append(
            {"timestamp": event_ts, "surface": "event"}
        )
    outfile.write(serialized + "\n")


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

            # R6/R12: Drag-aware blocked-interval handling. A drag's
            # parent timestamp is its START — but the drag PATH (children
            # mouse.move waypoints + mouse.up endpoint) can extend INTO
            # a blocked interval even when the start is outside. Pre-fix,
            # the parent ``find_blocked_interval(event_ts)`` check below
            # only fired on drags whose START was inside, leaking the END
            # coordinate ``(x+dx, y+dy)`` plus any non-mouse.move children
            # whose timestamps landed inside the blocked interval.
            #
            # The fix: compute the drag's effective span from its
            # children, range-overlap-test it against the blocked
            # intervals, and if any overlap exists drop EVERY in-interval
            # child (regardless of type — mouse.up/mouse.down/key.type
            # all leak coordinates or content) plus null the parent
            # drag's coordinate fields. Children entirely outside any
            # blocked interval are kept.
            if event.get("type") == "mouse.drag" and _blocked:
                children = event.get("children") or []
                drag_start = event_ts
                drag_end = drag_start
                for c in children:
                    c_ts = c.get("timestamp", drag_start)
                    c_end = c.get("last_timestamp", c_ts)
                    if c_end > drag_end:
                        drag_end = c_end
                    if c_ts > drag_end:
                        drag_end = c_ts

                drag_overlap = _interval_intersects(
                    drag_start, drag_end, _blocked, blocked_starts,
                )
                if drag_overlap is not None:
                    # Drop children whose own timestamp lands inside any
                    # blocked interval. Pointer geometry inside a blocked
                    # interval leaks regardless of event type — a
                    # mouse.up at the end of a drag carries the END
                    # coordinate just as much as the parent's (x+dx, y+dy).
                    kept: list[dict] = []
                    for c in children:
                        c_ts = c.get("timestamp", drag_start)
                        if find_blocked_interval(
                            c_ts, _blocked, blocked_starts,
                        ) is not None:
                            continue
                        # Also drop merged-move children whose own span
                        # extends INTO a blocked interval (covers the
                        # pre-existing case where a merged move starts
                        # outside but ends inside).
                        if c.get("type") == "mouse.move":
                            c_end = c.get("last_timestamp", c_ts)
                            if _interval_intersects(
                                c_ts, c_end, _blocked, blocked_starts,
                            ) is not None:
                                continue
                        kept.append(c)
                    event["children"] = kept

                    # Always null the parent drag's pointer geometry on
                    # overlap — the drag envelope (start/end coords,
                    # displacement) leaks the in-interval portion of the
                    # gesture regardless of action type. ``null_pointer_geometry``
                    # recurses into surviving children, so retained mouse
                    # children also have their coords zeroed.
                    null_pointer_geometry(event)

                    if drag_overlap.action in SCRUB_CONTENT_NULL_ACTIONS:
                        # EXCLUDE / MASK_WINDOW: also null text content
                        # (key.type children, window titles). Recurses
                        # into surviving children.
                        null_text_content(event)
                    else:
                        # TEXT_REDACT / OCR_FALLBACK / MASK_REGION: pointer
                        # is already suppressed; run PII detection on
                        # surviving key.type children before writing the
                        # drag, since the drag is written + ``continue``d
                        # here and would otherwise bypass the PII detection
                        # loop further below.
                        _process_key_type_events(
                            event, pipeline, anonymizer, _result, db_redactions,
                        )
                        if _xref:
                            for child in event.get("children", []):
                                if child.get("type") == "key.type":
                                    _cross_reference_key_type(
                                        child, _xref, _result, db_redactions,
                                    )

                    _result.audit_entries.append(
                        AuditEntry(
                            timestamp=event_ts,
                            surface="event",
                            action=drag_overlap.action.value,
                            reason=drag_overlap.reason,
                        )
                    )
                    _write_event_recording_fail_closed(
                        event, event_ts, outfile, _result
                    )
                    continue

            # Check blocked-app intervals (non-drag events: regular
            # clicks, key events, etc. — for these the parent timestamp
            # IS the event time, so a point-lookup is correct). Drags
            # are handled above with the range-overlap branch.
            blocked = find_blocked_interval(event_ts, _blocked, blocked_starts)
            if blocked is not None:
                # Pointer geometry is suppressed for ANY SCRUB_BLOCK_ACTIONS
                # interval (pointer position leaks coarse patterns
                # regardless of action).
                null_pointer_geometry(event)

                _result.audit_entries.append(
                    AuditEntry(
                        timestamp=event_ts,
                        surface="event",
                        action=blocked.action.value,
                        reason=blocked.reason,
                    )
                )

                if blocked.action in SCRUB_CONTENT_NULL_ACTIONS:
                    # EXCLUDE / MASK_WINDOW: also null keystroke text +
                    # window titles. Don't fall through to PII detection
                    # — content is wholesale-suppressed in these contexts.
                    null_text_content(event)
                    outfile.write(json.dumps(event, ensure_ascii=False) + "\n")
                    continue

                # TEXT_REDACT / OCR_FALLBACK / MASK_REGION: pointer
                # suppressed above; let text fall through to PII
                # detection below so detected entities are scrubbed
                # without wholesale-nulling clean content.

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

            _write_event_recording_fail_closed(event, event_ts, outfile, _result)

    # Batch DB redaction (single connection, single commit)
    if db_redactions and db_dir is not None:
        _redact_keystroke_db_rows(db_dir, db_redactions)

    # Atomic rename on success, clean up .tmp on error
    if had_errors:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            logger.warning(
                "scrub_events_jsonl: failed to remove tmp file %s", tmp_path,
                exc_info=True,
            )
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
    from screencap.privacy.mask_primitives import MaskRegion
    from screencap.redaction import normalize_text
    from screencap.redaction.ocr import build_offset_map

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

    from screencap.privacy.mask_primitives import _window_to_pixel_rect

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

    from screencap.privacy.mask_primitives import window_regions_from_geometry
    from screencap.redaction.geometry import (
        _load_geometry_row,
        associate_screenshot,
        parse_screenshot_timestamp,
    )
    from screencap.redaction.masking import (
        MaskStrategy,
        mask_screenshot,
    )

    # Try to create OCR + detection pipeline for OCR_FALLBACK.
    _ocr = None
    _pipeline = None
    try:
        from screencap.redaction.ocr import VisionOcr
        _ocr = VisionOcr()
        from screencap.redaction import create_default_pipeline
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
            # Runs independently of the foreground OCR pass: the two operate on
            # disjoint regions (OCR scans only the active-window ROI; bg masking
            # masks only sensitive *background* windows). respect_z_order=True
            # already excludes the foreground window from the bg mask, so this
            # cannot over-mask kept foreground content. Gating it on ocr_ran /
            # cache_hit let sensitive background windows leak whenever Vision
            # ran the foreground OCR pass (SCR-110).
            bg_masked = False
            if actual_action in (
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

class Scrubber:
    """Single-source-of-truth orchestrator for scrubbing a recording directory.

    ``run()`` performs the full post-hoc scrub; ``run_chunk()`` performs the
    per-chunk scrub used by the live upload path. Both share one definition
    of the load-bearing step order so callers stop duplicating it.
    """

    def __init__(
        self,
        capture_dir: Path,
        *,
        pipeline,
        anonymizer,
        evaluator=None,
        classifier=None,
        pixel_ratio: float | None = None,
    ) -> None:
        self.capture_dir = capture_dir
        self.pipeline = pipeline
        self.anonymizer = anonymizer
        self.evaluator = evaluator
        self.classifier = classifier
        self.pixel_ratio = pixel_ratio

    def run(self) -> ScrubResult:
        """Scrub the capture directory in-place.

        Caller is responsible for any copy/rename of the source recording.
        """
        result = ScrubResult()
        result.output_dir = self.capture_dir

        db_path = find_db(self.capture_dir)
        ctx = build_scrub_context(db_path, self.evaluator, self.classifier)
        if self.pixel_ratio is not None:
            ctx.pixel_ratio = self.pixel_ratio

        # Surface the computed blocked intervals as review evidence (R13).
        # Already built by build_scrub_context (excluded/masked-app +
        # secure-field intervals); thread them onto the result so the
        # review-data layer can render risky-moment markers without
        # recomputing.
        result.blocked_intervals = list(ctx.blocked_intervals)

        if self.evaluator is not None and self.classifier is not None:
            mask_screenshots(
                self.capture_dir / "screenshots", ctx,
                db_path=db_path, result=result,
            )

        if ctx.blocked_intervals:
            _null_db_rows_for_intervals(
                self.capture_dir, ctx.blocked_intervals, result,
            )

        raw_detections = _scrub_db(
            self.capture_dir, self.pipeline, self.anonymizer, result,
        )
        ctx.xref_detections = build_xref_lookup(raw_detections)

        try:
            _scrub_events_jsonl(
                self.capture_dir, self.pipeline, self.anonymizer, result, ctx=ctx,
            )
        except Exception as exc:
            # Fail-closed: any unexpected error during JSONL scrubbing must
            # not leave a half-scrubbed events file behind.
            console.print(
                f"  [yellow]Warning: events JSONL scrubbing failed ({exc}) — "
                f"deleting for safety[/]"
            )
            for f in self.capture_dir.glob("events*.jsonl"):
                f.unlink(missing_ok=True)
        transcript_paths = sorted(self.capture_dir.glob("transcript*.json")) + sorted(
            self.capture_dir.glob("transcript*.txt")
        )
        scrub_transcripts(transcript_paths, self.pipeline, self.anonymizer, result=result)
        _scrub_metrics(self.capture_dir / "system_metrics.json", result)
        _write_audit_log(self.capture_dir, result)
        return result

    def run_chunk(
        self,
        idx: int,
        start_ts: float,
        end_ts: float,
        transcript_path: Path | None,
    ) -> ScrubResult:
        """Scrub a single chunk's files in-place: events, transcripts, manifest, screenshots.

        Scoped to the time window ``[start_ts, end_ts)``.
        """
        result = ScrubResult()
        result.output_dir = self.capture_dir

        db_path = find_db(self.capture_dir)
        ctx = build_scrub_context(
            db_path,
            self.evaluator,
            self.classifier,
            time_range=(start_ts, end_ts),
            pipeline=self.pipeline,
            anonymizer=self.anonymizer,
            pixel_ratio=self.pixel_ratio,
        )

        # Surface the per-chunk blocked intervals as review/consumer evidence,
        # mirroring run(). This is a WRITE-ONLY signal for downstream consumers
        # (the SCR-118 content-index pass reads result.blocked_intervals to skip
        # EXCLUDE / secure-field frames); the redaction path itself never
        # branches on it, so R5's one-directional invariant holds.
        result.blocked_intervals = list(ctx.blocked_intervals)

        events_path = self.capture_dir / f"events_{idx:04d}.jsonl"
        if events_path.exists():
            try:
                had_errors = scrub_events_jsonl(
                    events_path, self.pipeline, self.anonymizer,
                    ctx=ctx, result=result,
                )
                if had_errors:
                    _rename_scrub_failed(events_path)
            except Exception as e:
                logger.error(
                    f"Chunk {idx}: events JSONL scrub failed, skipping file: {e}"
                )
                _rename_scrub_failed(events_path)

        transcript_paths: list[Path] = []
        if transcript_path is not None and transcript_path.exists():
            transcript_paths.append(transcript_path)
        transcript_json = self.capture_dir / f"transcript_{idx:04d}.json"
        if transcript_json.exists():
            transcript_paths.append(transcript_json)
        if transcript_paths:
            try:
                scrub_transcripts(
                    transcript_paths, self.pipeline, self.anonymizer, result=result,
                )
            except Exception as e:
                logger.error(f"Chunk {idx}: transcript scrub failed: {e}")
                for tp in transcript_paths:
                    if tp.exists():
                        _rename_scrub_failed(tp)

        manifest_path = self.capture_dir / f"chunk_{idx:04d}_manifest.json"
        if manifest_path.exists():
            try:
                scrub_manifest(
                    manifest_path, self.pipeline, self.anonymizer, result=result,
                )
            except Exception as e:
                logger.error(
                    f"Chunk {idx}: manifest scrub failed, skipping file: {e}"
                )
                _rename_scrub_failed(manifest_path)

        screenshots_dir = self.capture_dir / f"chunk_{idx}" / "screenshots"
        if (
            screenshots_dir.is_dir()
            and ctx.evaluator is not None
            and ctx.classifier is not None
        ):
            try:
                mask_screenshots(
                    screenshots_dir, ctx,
                    db_path=db_path, result=result,
                )
            except Exception:
                logger.warning(
                    f"Screenshot masking failed for chunk {idx}", exc_info=True,
                )
                # Fail-closed: delete every unmasked screenshot before upload.
                for img in screenshots_dir.glob("*.jpg"):
                    img.unlink(missing_ok=True)

        return result


def _rename_scrub_failed(path: Path) -> None:
    """Rename a file to mark it ineligible for upload after a scrub failure."""
    try:
        failed_path = path.with_suffix(path.suffix + ".scrub_failed")
        path.rename(failed_path)
    except OSError as e:
        logger.warning(f"Failed to rename {path.name} for scrub failure: {e}")


# ---------------------------------------------------------------------------
# U6 — post-hoc video masking integrated into cloud-copy production.
#
# GATED behind config.get_masked_video_upload_enabled() (default OFF). The flag
# check is the SINGLE gate: when OFF, the masker is NOT invoked and the cloud
# video copy is today's capture-blocked chunk (no scrubber-side masking); when
# ON, each cloud chunk's video is masked post-hoc via video_mask and the masked
# copy is materialized in the <name>-scrubbed/ sibling dir, which is NOT the
# source dir list_recording_files enumerates — so a partial/abandoned masked
# copy can never be picked up unscrubbed.
# ---------------------------------------------------------------------------


def masked_video_dir(scrubbed_dir: Path) -> Path:
    """Return the dir that holds masked cloud video copies.

    A subdir of the ``<name>-scrubbed`` dir. The scrubbed dir is a SIBLING of
    the source recording dir, so it is never walked by
    ``upload.list_recording_files(source_dir)`` — the source-dir enumeration
    cannot pick up a masked (or partial) copy unscrubbed. The terminal stage
    (U7) is the only thing that enumerates the scrubbed dir for upload, and it
    ships a masked copy only when the chunk is ledger-``SCRUBBED``.
    """
    return scrubbed_dir / "masked_video"


def mask_video_chunk_for_cloud(
    chunk_path: Path,
    db_path: Path,
    scrubbed_dir: Path,
    *,
    chunk_index: int,
    start_ts: float,
    end_ts: float,
    chunk_start_abs: float,
    pixel_ratio: float = 2.0,
    classifier: object | None = None,
    evaluator: object | None = None,
    enabled: bool | None = None,
):
    """Produce the cloud-bound video copy for one chunk, gated on the flag.

    THE SINGLE GATE is the masked-video-upload decision. ``enabled`` lets the
    caller pass the FROZEN per-recording value (SCR-125 — the live and terminal
    paths gate on ``.recording_intent``'s ``masked_video_upload``, NOT the
    mutable global, so a mid-recording flip can never make capture-blocking and
    upload-masking disagree). ``enabled=None`` falls back to
    ``config.get_masked_video_upload_enabled()`` (the global) for callers that
    have no frozen value.

    * **OFF (this milestone's default)** — the masker is NOT invoked. Returns
      ``None`` to signal "no scrubber-side video masking happened; the cloud
      video copy is today's capture-blocked chunk." Distinguishing this
      "deliberately did nothing (flag off)" from U6's "did nothing because
      provably safe" and "did nothing because it failed" is exactly the
      no-op-vs-success discipline this unit enforces.

    * **ON (future)** — invokes :func:`video_mask.mask_video_chunk` over the
      chunk's absolute frame span and materializes the result (MASKED or a
      faithful UNMASKED_PROVABLY_SAFE copy) into ``masked_video_dir`` ATOMICALLY
      (temp + rename, whole-chunk gate). On FAILED, no copy is produced and the
      caller marks the chunk FAILED (blocking upload + eviction). Returns the
      :class:`video_mask.MaskOutcome`.

    Args mirror :func:`video_mask.mask_video_chunk`; ``chunk_index`` names the
    output (``chunk_{index:04d}.mp4`` inside the masked-video dir, so the cloud
    set keeps the same chunk filenames).
    """
    if enabled is None:
        from screencap.config import get_masked_video_upload_enabled

        enabled = get_masked_video_upload_enabled()

    if not enabled:
        # Flag OFF: the conservative posture. The masker MUST NOT be in the
        # cloud-upload path; the capture-blocked chunk is the cloud copy.
        return None

    from screencap.video_mask import mask_video_chunk

    out_dir = masked_video_dir(scrubbed_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"chunk_{chunk_index:04d}.mp4"

    outcome = mask_video_chunk(
        chunk_path,
        db_path,
        start_ts=start_ts,
        end_ts=end_ts,
        chunk_start_abs=chunk_start_abs,
        output_path=output_path,
        pixel_ratio=pixel_ratio,
        classifier=classifier,
        evaluator=evaluator,
    )
    # SCR-126 Fix 3 (in-masker secondary purge): on FAILED, a pre-existing masked
    # copy from a PRIOR run (different policy / older mask logic) is NOT touched by
    # mask_video_chunk's case-(b) early return — it would survive and could ship
    # via the rglob upload. Remove it so a failed chunk leaves NO masked copy. (The
    # produce-level closed-set reconcile is the authoritative purge; this is the
    # per-chunk belt.)
    if outcome is not None and not outcome.ok:
        with contextlib.suppress(OSError):
            output_path.unlink(missing_ok=True)
    return outcome


def _build_app_allowlist(metrics_path: Path) -> frozenset[str]:
    """Build a set of app names from system_metrics.json running_applications.

    Returns NFKC-normalized, lowercased app names. Silent on missing file
    (expected for old recordings); warns on corruption.
    """
    if not metrics_path.exists():
        return frozenset()
    try:
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
        apps = data.get("static", {}).get("running_applications") or []
        # Deferred import — only needed here.
        from screencap.redaction import normalize_text

        names = {normalize_text(app["name"]).lower() for app in apps if app.get("name")}
        return frozenset(names)
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
        console.print(f"  [yellow]Warning: could not read app allowlist: {e}[/]")
        return frozenset()


# Files to skip during copytree and delete as safety fallback.
_SKIP_FILES = {".upload_status.json", "viewer.html"}
_SKIP_EXTENSIONS = {".mp4", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus"}


def _copytree_ignore(directory: str, entries: list[str]) -> set[str]:
    """Ignore callback for shutil.copytree — skip media and derived files."""
    ignored = set()
    for entry in entries:
        if entry in _SKIP_FILES or Path(entry).suffix in _SKIP_EXTENSIONS:
            ignored.add(entry)
    return ignored


def _null_db_rows_for_intervals(
    dst: Path, intervals: list[BlockedInterval], result: ScrubResult
) -> None:
    """Null out action_event and window_event columns during blocked-app intervals."""
    if not intervals:
        return
    db_path = find_db(dst)
    if db_path is None:
        return

    with open_recording_db(db_path, read_only=False) as conn:
        try:
            has_actions = has_table(conn, "action_event")
            has_windows = has_table(conn, "window_event")

            # Build SET clause from canonical field list, skipping columns
            # absent in older recordings.
            null_fields: list[str] = []
            if has_actions:
                null_fields = sorted(
                    f for f in KEYSTROKE_CONTENT_FIELDS
                    if has_column(conn, "action_event", f)
                )
            ae_set_clause = ", ".join(f"{f} = NULL" for f in null_fields)

            # Build window_event SET clause — include browser_url if column exists
            we_null_cols = ["title", "state"]
            if has_windows and has_column(conn, "window_event", "browser_url"):
                we_null_cols.append("browser_url")
            we_set_clause = ", ".join(f"{c} = NULL" for c in we_null_cols)

            for iv in intervals:
                if iv.end == float("inf"):
                    ae_sql = (
                        f"UPDATE action_event SET {ae_set_clause} "
                        "WHERE timestamp >= ?"
                    )
                    ae_params = (iv.start,)
                    we_sql = (
                        f"UPDATE window_event SET {we_set_clause} "
                        "WHERE timestamp >= ?"
                    )
                    we_params = (iv.start,)
                else:
                    ae_sql = (
                        f"UPDATE action_event SET {ae_set_clause} "
                        "WHERE timestamp >= ? AND timestamp < ?"
                    )
                    ae_params = (iv.start, iv.end)
                    we_sql = (
                        f"UPDATE window_event SET {we_set_clause} "
                        "WHERE timestamp >= ? AND timestamp < ?"
                    )
                    we_params = (iv.start, iv.end)

                if has_actions and ae_set_clause:
                    conn.execute(ae_sql, ae_params)

                if has_windows:
                    conn.execute(we_sql, we_params)

                result.audit_entries.append(
                    AuditEntry(
                        timestamp=iv.start,
                        surface="db_field",
                        action=iv.action.value,
                        reason=iv.reason,
                    )
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# Scrubber-local wrapper for _scrub_text with console output.
# DB scrubbing functions call this for Rich-style warnings.
def _scrub_text(
    text: str,
    pipeline,
    anonymizer,
    result: ScrubResult,
):
    """Scrubber-local wrapper that adds Rich console warnings on failure."""
    scrubbed, det_result = scrub_text(text, pipeline, anonymizer, result=result)
    if scrubbed == SCRUB_FAILED_SENTINEL:
        console.print("  [yellow]Warning: all detectors failed on a field[/]")
    return scrubbed, det_result


# ---------------------------------------------------------------------------
# DB scrubbing
# ---------------------------------------------------------------------------


def _scrub_json_column(
    conn: Connection,
    table: str,
    col: str,
    pipeline,
    anonymizer,
    result: ScrubResult,
    row_detection_collector: dict[int, dict] | None = None,
) -> None:
    """Scrub a JSON blob column using separate read/write cursors.

    When *row_detection_collector* is not None, AXValue detections from
    each row are stored as ``{row_id: {"timestamp": float, "detections": [...]}}``
    for downstream cross-referencing against keystrokes.
    """
    read_cur = conn.cursor()
    write_cur = conn.cursor()
    # Include timestamp when collecting detections (needed for xref matching).
    # Fall back to no-timestamp query if the column doesn't exist.
    has_timestamp = (
        row_detection_collector is not None
        and has_column(conn, table, "timestamp")
    )
    if has_timestamp:
        read_cur.execute(
            f"SELECT id, {col}, timestamp FROM {table} WHERE {col} IS NOT NULL"
        )
    else:
        read_cur.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL")
    for row in read_cur:
        if has_timestamp:
            row_id, json_str, timestamp = row
        else:
            row_id, json_str = row
            timestamp = None
        if not json_str or not isinstance(json_str, str):
            continue
        try:
            data = json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            console.print(
                f"  [yellow]Warning: malformed JSON in {table}.{col} "
                f"row {row_id} — skipped[/]"
            )
            continue

        if isinstance(data, (dict, list)):
            per_row: list[dict] | None = [] if row_detection_collector is not None else None
            _scrub_json_recursive(
                data, pipeline, anonymizer, result,
                collect_detections=per_row,
            )
            if row_detection_collector is not None and per_row:
                row_detection_collector[row_id] = {
                    "timestamp": timestamp,
                    "detections": per_row,
                }
            scrubbed_json = json.dumps(data)
            if scrubbed_json != json_str:
                write_cur.execute(
                    f"UPDATE {table} SET {col} = ? WHERE id = ?",
                    (scrubbed_json, row_id),
                )


def _scrub_text_column(
    conn: Connection,
    table: str,
    col: str,
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> None:
    """Scrub a plain text column."""
    read_cur = conn.cursor()
    write_cur = conn.cursor()
    read_cur.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL")
    for row_id, text in read_cur:
        if not text or not isinstance(text, str) or not text.strip():
            continue
        scrubbed, _ = _scrub_text(text, pipeline, anonymizer, result)
        if scrubbed != text:
            write_cur.execute(
                f"UPDATE {table} SET {col} = ? WHERE id = ?",
                (scrubbed, row_id),
            )


def _maybe_scrub_text_column(
    conn: Connection,
    table: str,
    col: str,
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> None:
    """Scrub a text column only if the column exists on this recording."""
    if has_column(conn, table, col):
        _scrub_text_column(conn, table, col, pipeline, anonymizer, result)


def _maybe_scrub_json_column(
    conn: Connection,
    table: str,
    col: str,
    pipeline,
    anonymizer,
    result: ScrubResult,
    row_detection_collector: dict[int, dict] | None = None,
) -> None:
    """Scrub a JSON column only if the column exists on this recording."""
    if has_column(conn, table, col):
        _scrub_json_column(
            conn, table, col, pipeline, anonymizer, result,
            row_detection_collector=row_detection_collector,
        )


def _scrub_recording_schema(
    conn: Connection,
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> dict[int, dict]:
    """Scrub a recording.db schema.

    Returns a dict mapping row_id → {timestamp, detections} for
    element_state AXValue detections (used for keystroke cross-referencing).
    """
    element_state_detections: dict[int, dict] = {}

    if has_table(conn, "recording"):
        _maybe_scrub_text_column(conn, "recording", "task_description", pipeline, anonymizer, result)

    if has_table(conn, "action_event"):
        for col in (
            "key_char",
            "canonical_key_char",
            "key_name",
            "canonical_key_name",
            "active_segment_description",
            "available_segment_descriptions",
        ):
            _maybe_scrub_text_column(conn, "action_event", col, pipeline, anonymizer, result)
        _maybe_scrub_json_column(
            conn, "action_event", "element_state", pipeline, anonymizer, result,
            row_detection_collector=element_state_detections,
        )

    if has_table(conn, "window_event"):
        _maybe_scrub_text_column(conn, "window_event", "title", pipeline, anonymizer, result)
        _maybe_scrub_json_column(conn, "window_event", "state", pipeline, anonymizer, result)
        _maybe_scrub_text_column(conn, "window_event", "browser_url", pipeline, anonymizer, result)

    return element_state_detections


def _scrub_db(
    dst: Path,
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> dict[int, dict]:
    """Scrub all text surfaces in the recording database.

    Returns element_state AXValue detections for keystroke cross-referencing.
    """
    db_path = find_db(dst)
    if db_path is None:
        console.print("  [yellow]Warning: no database found — skipping DB scrubbing[/]")
        return {}

    element_state_detections: dict[int, dict] = {}
    with open_recording_db(db_path, read_only=False) as conn:
        try:
            # Delete unscrubable binary/audio data from DB
            if has_table(conn, "screenshot"):
                conn.execute("DELETE FROM screenshot")
                result.deleted_files.append("screenshot table (binary BLOBs)")
            if has_table(conn, "audio_info"):
                conn.execute("DELETE FROM audio_info")
                result.deleted_files.append("audio_info table (spoken words)")

            if has_table(conn, "recording"):
                element_state_detections = _scrub_recording_schema(
                    conn, pipeline, anonymizer, result
                )
            else:
                console.print(
                    "  [yellow]Warning: unrecognized database schema — "
                    "database text was NOT scrubbed[/]"
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return element_state_detections


# ---------------------------------------------------------------------------
# System metrics scrubbing (rule-based)
# ---------------------------------------------------------------------------


def _scrub_metrics(path: Path, result: ScrubResult) -> None:
    """Redact hostname and WiFi identifiers from system_metrics.json.

    Populates ``result.rule_based_redactions`` with the redacted field names
    (e.g. ``["hostname", "wifi.ssid"]``).
    """
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        static = data.get("static", {})
        redacted: list[str] = []

        if "hostname" in static:
            static["hostname"] = "<REDACTED>"
            redacted.append("hostname")

        wifi = static.get("wifi", {})
        if "ssid" in wifi:
            wifi["ssid"] = "<REDACTED>"
            redacted.append("wifi.ssid")
        if "bssid" in wifi:
            wifi["bssid"] = "<REDACTED>"
            redacted.append("wifi.bssid")

        if redacted:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            result.rule_based_redactions.extend(redacted)

    except (json.JSONDecodeError, OSError) as e:
        console.print(
            f"  [yellow]Warning: could not scrub system_metrics.json: {e}[/]"
        )


# ---------------------------------------------------------------------------
# Events JSONL scrubbing (file iteration wrapper)
# ---------------------------------------------------------------------------


def _scrub_events_jsonl(
    dst: Path,
    pipeline,
    anonymizer,
    result: ScrubResult,
    ctx: ScrubContext | None = None,
) -> None:
    """Scrub combined keystroke sequences in events.jsonl and map back to DB.

    Dispatches to ``scrub_events_jsonl()`` from the pipeline for each events
    file, then handles error cleanup (deletes the original file on errors).
    """
    event_files = sorted(dst.glob("events*.jsonl"))
    if not event_files:
        return

    for events_file in event_files:
        had_errors = scrub_events_jsonl(
            events_file,
            pipeline,
            anonymizer,
            ctx=ctx,
            db_dir=dst,
            result=result,
        )
        if had_errors:
            events_file.unlink(missing_ok=True)
            console.print(
                "  [yellow]Warning: events.jsonl deleted due to processing errors[/]"
            )


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------


def _blocked_interval_to_dict(iv: BlockedInterval) -> dict:
    """Export-safe serialization of a blocked interval.

    Open-ended intervals (``end == inf``) serialize ``end`` as JSON null —
    ``json.dumps`` would otherwise emit invalid ``Infinity``. The consumer
    reads null as "to the end of the recording".
    """
    return {
        "start": iv.start,
        "end": None if iv.end == float("inf") else iv.end,
        "action": iv.action.value,
        "reason": iv.reason,
    }


def _write_audit_log(dst: Path, result: ScrubResult) -> None:
    """Write the export-safe audit log to the scrubbed recording directory.

    The payload is an object (not a bare list) carrying three export-safe
    collections: per-decision ``entries`` (the original audit trail),
    ``blocked_intervals`` (risky-moment evidence, R13), and ``fail_closed``
    markers (R14). Guarding on *all three* being empty — not just
    ``entries`` — is load-bearing: a blocked-app recording with zero NER
    entities still has intervals to surface, and the old ``entries``-only
    guard silently dropped them.

    Written at mode 0600 (recording metadata is the same sensitivity class
    as recordings — SECURITY.md), mirroring the daemon audit-log posture: a
    tight umask closes the create-then-chmod window, and ``fchmod`` tightens
    a pre-existing file.
    """
    if not (
        result.audit_entries
        or result.blocked_intervals
        or result.fail_closed_redactions
    ):
        return

    payload = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "entries": [dataclasses.asdict(e) for e in result.audit_entries],
        "blocked_intervals": [
            _blocked_interval_to_dict(iv) for iv in result.blocked_intervals
        ],
        "fail_closed": list(result.fail_closed_redactions),
    }
    body = json.dumps(payload, indent=2).encode("utf-8")

    path = dst / "privacy_audit.json"
    tmp = dst / "privacy_audit.json.tmp"
    old_umask = os.umask(0o077)
    try:
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
    finally:
        os.umask(old_umask)
    try:
        # Re-assert the mode in case the tmp pre-existed at a wider mode
        # (only a same-UID process could have created it, but belt-and-braces).
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            logger.debug("_write_audit_log: fchmod failed", exc_info=True)
        os.write(fd, body)
    finally:
        os.close(fd)
    # Atomic publish: a reader sees either the previous complete audit log or
    # the new one, never a truncated/partial file after a crash mid-write.
    os.replace(tmp, path)


def _print_summary(result: ScrubResult) -> None:
    """Print a Rich summary of the scrub operation."""
    console.print(f"\n[bold green]Scrub complete:[/] {result.output_dir.name}/\n")

    if result.entity_counts:
        console.print("  [bold]Entities detected:[/]")
        for entity_type, count in result.entity_counts.most_common():
            console.print(f"    {entity_type:<20s} {count}")
    else:
        console.print("  No PII or secrets detected in text surfaces.")

    if result.rule_based_redactions:
        console.print("\n  [bold]Rule-based redactions:[/]")
        console.print(
            f"    system_metrics.json: {', '.join(result.rule_based_redactions)}"
        )

    if result.deleted_files:
        console.print("\n  [bold]Removed from scrubbed copy:[/]")
        # Separate file deletions from DB table deletions
        file_dels = [f for f in result.deleted_files if "table" not in f]
        db_dels = [f for f in result.deleted_files if "table" in f]
        if file_dels:
            console.print(f"    {', '.join(file_dels)}")
        if db_dels:
            console.print("\n  [bold]Deleted from DB:[/]")
            for d in db_dels:
                console.print(f"    {d}")

    console.print()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _resolve_privacy_config_for_dir(dst: Path):
    """Build the privacy config for a recording dir, honoring .recording_intent.

    Use the stricter of (current config mode, capture-time intent mode); cloud
    or both destinations force PUBLIC.
    """
    from dataclasses import replace as _dc_replace

    from screencap.config import get_privacy_config
    from screencap.privacy.policy import _MODE_STRICTNESS, PrivacyMode

    privacy_config = get_privacy_config()

    intent_path = dst / ".recording_intent"
    if intent_path.exists():
        try:
            intent_data = json.loads(intent_path.read_text(encoding="utf-8"))
            destination = intent_data.get("destination", "")
            if destination in ("cloud", "both"):
                privacy_config = _dc_replace(
                    privacy_config, mode=PrivacyMode.PUBLIC,
                )
            else:
                intent_mode = PrivacyMode(intent_data["privacy_mode"])
                if (
                    _MODE_STRICTNESS[intent_mode]
                    < _MODE_STRICTNESS[privacy_config.mode]
                ):
                    privacy_config = _dc_replace(privacy_config, mode=intent_mode)
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            pass

    return privacy_config


def _safety_delete_unscrubbable_files(src: Path, dst: Path) -> list[str]:
    """Belt-and-suspenders deletion of media/derived files that copytree skipped.

    Returns the union of file names that were skipped or deleted so callers
    can attribute them in the audit summary.
    """
    deleted: list[str] = []
    for skip_name in _SKIP_FILES:
        p = dst / skip_name
        if p.exists():
            p.unlink()
            deleted.append(skip_name)
    for ext in _SKIP_EXTENSIONS:
        for f in dst.glob(f"*{ext}"):
            f.unlink()
            deleted.append(f.name)

    all_skipped: set[str] = set()
    for f in src.iterdir():
        if f.name in _SKIP_FILES or f.suffix in _SKIP_EXTENSIONS:
            all_skipped.add(f.name)
    all_skipped.update(deleted)
    return sorted(all_skipped)


@contextlib.contextmanager
def recording_scrub_lock(name: str) -> Iterator[None]:
    """Per-recording advisory lock serializing scrub/reuse on one recording.

    The review window is a ``WindowGroup`` (multiple concurrent windows by
    design), and ``screencap upload`` can run alongside it. Without a lock, one
    actor's ``scrub_recording`` (``rmtree`` → ``copytree`` → scrub → sentinel)
    can rmtree the ``<name>-scrubbed`` dir while another's reuse-guard verdict +
    ``upload_recording`` file enumeration is in flight — shipping a half-rebuilt
    or unscrubbed copy. Holding this exclusive ``flock`` across the whole
    rebuild, and across the upload's decide-then-ship critical section, makes
    those mutually exclusive (the plan's "serialize on a scrub lock" option).

    The lock file is a dotfile under the recordings root, so it is never
    uploaded (dotfile filter) and is shared by all same-EUID actors on the
    recording. Best-effort on platforms without ``fcntl`` (a ``nullcontext``).
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return

    # Best-effort: if the lock file can't be created or locked (read-only or
    # sandboxed recordings root, an FS without flock), degrade to unlocked
    # rather than blocking the operation. This is strictly no worse than the
    # pre-lock behavior; log loudly so the lost serialization is visible.
    fd = None
    try:
        recordings = get_recordings_dir()
        recordings.mkdir(parents=True, exist_ok=True)
        lock_path = recordings / f".{name}.scrublock"
        old_umask = os.umask(0o077)
        try:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        finally:
            os.umask(old_umask)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        logger.warning(
            "could not acquire scrub lock for %s; proceeding unlocked", name,
            exc_info=True,
        )
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        yield
        return

    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _compute_source_hash(src_dir: Path) -> str:
    """Content hash of the scrub-relevant source files (see ``_SOURCE_HASH_GLOBS``).

    Deterministic over file name + size + bytes, sorted by name. Used by the
    reuse guard to detect that the original changed since it was scrubbed —
    presence/mtime are unsafe (a killed-mid-scrub dir looks "fresh", and the
    upload path itself mutates the original before scrub).
    """
    import hashlib

    h = hashlib.sha256()
    files: list[Path] = []
    for pattern in _SOURCE_HASH_GLOBS:
        files.extend(src_dir.glob(pattern))
    for f in sorted(set(files), key=lambda p: p.name):
        if not f.is_file():
            continue
        h.update(f.name.encode("utf-8"))
        h.update(b"\0")
        h.update(str(f.stat().st_size).encode("utf-8"))
        h.update(b"\0")
        with open(f, "rb") as fh:
            for chunk in iter(lambda fh=fh: fh.read(65536), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def _write_scrub_sentinel(
    scrubbed_dir: Path, src_dir: Path, *, cloud_bound_recovery: bool
) -> None:
    """Write the completion sentinel + provenance as the final step of a
    successful scrub. Atomic (temp + replace) so a concurrent reader never sees
    a half-written sentinel; mode 0600 (provenance is recording-class metadata).

    Best-effort: a write failure is logged, not raised — the scrub still
    succeeded, the dir just won't be eligible for reuse (upload rebuilds, which
    is safe).
    """
    payload = {
        "scrubber_version": SCRUB_PROVENANCE_VERSION,
        "source_hash": _compute_source_hash(src_dir),
        "cloud_bound_recovery": bool(cloud_bound_recovery),
    }
    body = json.dumps(payload, indent=2).encode("utf-8")
    final = scrubbed_dir / SCRUB_SENTINEL_NAME
    tmp = scrubbed_dir / (SCRUB_SENTINEL_NAME + ".tmp")
    old_umask = os.umask(0o077)
    try:
        fd = os.open(
            str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(tmp, final)
    except OSError:
        logger.warning("could not write scrub sentinel %s", final, exc_info=True)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    finally:
        os.umask(old_umask)


def is_scrubbed_copy_reusable(
    src_dir: Path, scrubbed_dir: Path, *, require_cloud_bound: bool = True
) -> bool:
    """True only if ``scrubbed_dir`` is a complete, current scrub of ``src_dir``
    built the way upload would build it — safe to ship as-is (reviewed ==
    uploaded).

    Requires: the completion sentinel exists; the scrubber version matches; the
    source content hash matches (detecting mutation of the original); and, when
    ``require_cloud_bound``, the cloud-bound recovery flag is set (so a dir
    scrubbed without the load-bearing recovery — e.g. by ``screencap scrub`` —
    is never shipped, which could leak in-interval pointer geometry). Any miss
    → rebuild.
    """
    sentinel = scrubbed_dir / SCRUB_SENTINEL_NAME
    if not scrubbed_dir.is_dir() or not sentinel.exists():
        return False
    try:
        prov = json.loads(sentinel.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if prov.get("scrubber_version") != SCRUB_PROVENANCE_VERSION:
        return False
    if require_cloud_bound and not prov.get("cloud_bound_recovery"):
        return False
    return prov.get("source_hash") == _compute_source_hash(src_dir)


# ---------------------------------------------------------------------------
# SCR-126 Fix 3: masked-video provenance — the reuse / convergence guard for the
# masked_video/ copies. Distinct from .scrub_complete because masking runs AFTER
# the scrub sentinel (step 3) and depends on inputs the scrub source-hash OMITS:
# the source chunk *media* bytes (not in _SOURCE_HASH_GLOBS) and the global
# privacy policy. Dot-prefixed so the upload dotfile filter excludes it.
# ---------------------------------------------------------------------------

MASKED_PROVENANCE_NAME = ".masked_provenance.json"


def _hash_source_chunk(path: Path) -> str:
    """sha256 over (size + bytes) of one source chunk mp4 — the masking input the
    scrub source-hash does not cover."""
    import hashlib

    h = hashlib.sha256()
    h.update(str(path.stat().st_size).encode("utf-8"))
    h.update(b"\0")
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 16), b""):
            h.update(blk)
    return h.hexdigest()


def _compute_geometry_hash(src_dir: Path) -> str:
    """Content hash of the window_geometry timeline ONLY — never the whole DB.

    Scoped to the geometry table so the masked-video reuse check is stable across
    ``pipeline_chunk_state`` ledger churn between masking and upload: a full-DB
    hash would change on every ledger transition (``mark_scrubbed`` /
    upload-confirm) and refuse reuse forever. Returns a stable digest when the
    table/DB is absent or unreadable."""
    import hashlib

    db_path = src_dir / "recording.db"
    if not db_path.exists():
        return hashlib.sha256(b"no-recording-db").hexdigest()
    # Use the module-standard opener (busy_timeout set) so the geometry read does
    # not spuriously fail "database is locked" under concurrent pipeline_chunk_state
    # ledger churn between masking and upload — the exact window this hash spans.
    from screencap.recording_db import Row, open_recording_db

    h = hashlib.sha256()
    try:
        with open_recording_db(db_path, row_factory=Row) as conn:
            cur = conn.execute(
                "SELECT screenshot_timestamp, window_list_json "
                "FROM window_geometry ORDER BY id"
            )
            for row in cur:
                h.update(repr(row["screenshot_timestamp"]).encode("utf-8"))
                h.update(b"\0")
                h.update((row["window_list_json"] or "").encode("utf-8"))
                h.update(b"\0")
    except Exception:  # noqa: BLE001 — missing table / read error → stable digest
        return hashlib.sha256(b"no-geometry-table").hexdigest()
    return h.hexdigest()


def _read_db_pixel_ratio(src_dir: Path) -> float:
    """The retina factor recorded for the recording (scales every mask rect).

    Read from ``recording.pixel_ratio``; default 2.0 when absent/unreadable."""
    db_path = src_dir / "recording.db"
    if not db_path.exists():
        return 2.0
    from screencap.recording_db import Row, open_recording_db

    try:
        with open_recording_db(db_path, row_factory=Row) as conn:
            row = conn.execute("SELECT pixel_ratio FROM recording LIMIT 1").fetchone()
        if row and row["pixel_ratio"] is not None:
            return float(row["pixel_ratio"])
    except Exception:  # noqa: BLE001
        pass
    return 2.0


def _read_privacy_mode() -> str:
    """The active privacy mode — the primary masking-relevant policy axis."""
    try:
        from screencap.config import get_privacy_config

        return str(get_privacy_config().mode.value)
    except Exception:  # noqa: BLE001
        return "internal"


def _masked_inputs_fingerprint(
    src_dir: Path, indices: "Iterable[int]"
) -> dict:
    """The fingerprint of every input that determines the masked-video output:
    per-source-chunk bytes, the geometry timeline, pixel_ratio, the privacy
    policy, the frozen flag, and the mask-logic version. Any change invalidates a
    masked copy (SCR-126 Fix 3 / R4)."""
    from screencap.pipeline_chunk_ops import get_frozen_masked_video_upload

    src_chunks: dict[str, str] = {}
    for idx in indices:
        p = src_dir / f"chunk_{idx:04d}.mp4"
        if p.exists():
            src_chunks[str(idx)] = _hash_source_chunk(p)
    return {
        "mask_provenance_version": MASK_PROVENANCE_VERSION,
        "masked_video_upload": bool(get_frozen_masked_video_upload(src_dir)),
        "pixel_ratio": _read_db_pixel_ratio(src_dir),
        "privacy_mode": _read_privacy_mode(),
        "geometry_hash": _compute_geometry_hash(src_dir),
        "source_chunks": src_chunks,
    }


def write_masked_provenance(
    src_dir: Path, scrubbed_dir: Path, *, masked_indices: "Iterable[int]"
) -> None:
    """Record the masked-video provenance after a complete masking pass.

    Atomic (temp + replace), mode 0600, mirroring :func:`_write_scrub_sentinel`.
    Best-effort: a write failure is logged, not raised — without provenance the
    reuse/fast-path checks simply refuse to trust the copies (rebuild, safe)."""
    indices = sorted(set(masked_indices))
    payload = _masked_inputs_fingerprint(src_dir, indices)
    payload["masked_indices"] = indices
    body = json.dumps(payload, indent=2).encode("utf-8")
    out_dir = masked_video_dir(scrubbed_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / MASKED_PROVENANCE_NAME
    tmp = out_dir / (MASKED_PROVENANCE_NAME + ".tmp")
    old_umask = os.umask(0o077)
    try:
        fd = os.open(
            str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(tmp, final)
    except OSError:
        logger.warning("could not write masked provenance %s", final, exc_info=True)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    finally:
        os.umask(old_umask)


def _read_masked_provenance(scrubbed_dir: Path) -> dict | None:
    prov_path = masked_video_dir(scrubbed_dir) / MASKED_PROVENANCE_NAME
    if not prov_path.exists():
        return None
    try:
        return json.loads(prov_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def is_masked_video_reusable(
    src_dir: Path, scrubbed_dir: Path, *, expected_indices: "Iterable[int]"
) -> bool:
    """True only when the existing ``masked_video/`` set is provably current for
    EVERY expected chunk — present, current mask-logic version, and matching every
    masking input (source bytes, geometry, pixel_ratio, policy, frozen flag). Any
    miss → not reusable (caller purges + re-masks). Closes the stale-copy reuse
    hole (SCR-126 Fix 3 / R4)."""
    expected = sorted(set(expected_indices))
    prov = _read_masked_provenance(scrubbed_dir)
    if prov is None:
        return False
    if prov.get("mask_provenance_version") != MASK_PROVENANCE_VERSION:
        return False
    md = masked_video_dir(scrubbed_dir)
    for idx in expected:
        if not (md / f"chunk_{idx:04d}.mp4").exists():
            return False
    current = _masked_inputs_fingerprint(src_dir, expected)
    for key in (
        "masked_video_upload", "pixel_ratio", "privacy_mode",
        "geometry_hash", "source_chunks",
    ):
        if prov.get(key) != current.get(key):
            return False
    return True


def masked_provenance_version_current(scrubbed_dir: Path) -> bool:
    """True when a masked-video provenance record exists at the CURRENT mask-logic
    version. Used by the terminal-stage convergence fast path (SCR-126 R8): a
    recording converged under OLD mask logic must NOT be declared done — it falls
    through to re-mask. Absent provenance is treated as not-current (fail closed)."""
    prov = _read_masked_provenance(scrubbed_dir)
    return bool(prov) and prov.get("mask_provenance_version") == MASK_PROVENANCE_VERSION


def purge_orphan_masked_videos(
    scrubbed_dir: Path, *, expected_indices: "Iterable[int]"
) -> list[int]:
    """Closed-set reconcile: delete any ``masked_video/chunk_*.mp4`` whose index is
    NOT in the current expected set (the source chunk is gone / was never produced
    this pass). The AUTHORITATIVE stale-copy purge (SCR-126 Fix 3 / H2) — it runs
    on every produce, including the reuse path that skips the wholesale scrub
    rebuild, so a prior run's orphan can never reach the rglob upload set. Returns
    the purged indices."""
    expected = set(expected_indices)
    md = masked_video_dir(scrubbed_dir)
    if not md.is_dir():
        return []
    purged: list[int] = []
    for vf in sorted(md.glob("chunk_*.mp4")):
        try:
            idx = int(vf.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        if idx not in expected:
            with contextlib.suppress(OSError):
                vf.unlink()
                purged.append(idx)
    return purged


def scrub_recording(
    name: str,
    pii_engine: str | None = None,
    *,
    cloud_bound_recovery: bool = False,
    _already_locked: bool = False,
) -> ScrubResult:
    """Copy a recording and scrub PII from the copy.

    Never mutates the original recording directory. Resolves the recording
    name, copies it to a sibling ``-scrubbed`` directory with media/derived
    files filtered out, and delegates the actual scrubbing to ``Scrubber``.

    Args:
        name: Recording name (directory name under recordings/).
        pii_engine: "presidio", "presidio-gliner", or None (auto-detect).
        cloud_bound_recovery: Whether the caller ran
            ``_recover_chunk_metadata(cloud_bound=True)`` before this scrub.
            Recorded in the completion sentinel's provenance; the upload reuse
            guard refuses to ship a dir scrubbed without it (it could leak
            in-interval pointer geometry). ``screencap upload`` and the native
            review path pass ``True``; standalone ``screencap scrub`` leaves it
            ``False``.
        _already_locked: Internal — set ``True`` only when the caller already
            holds ``recording_scrub_lock(name)`` (the upload loop, which keeps
            the lock across reuse-check → scrub → upload). Avoids a same-process
            re-entrant ``flock`` deadlock. Other callers leave it ``False`` so
            the rebuild self-serializes against concurrent scrubs.

    Returns:
        ScrubResult with entity counts and deleted files.

    Raises:
        FileNotFoundError: Recording directory does not exist.
        ImportError: Privacy dependencies not installed.
        ValueError: Invalid recording name (path traversal).
    """
    # Privacy imports deferred here — not at module level — so that
    # screencap --help works without privacy deps installed.
    from screencap.privacy.classify import DefaultContextClassifier
    from screencap.redaction import Anonymizer, create_default_pipeline
    from screencap.privacy.policy import DefaultPolicyEvaluator

    src = resolve_recording_dir(name)
    if not src.exists():
        raise FileNotFoundError(f"Recording not found: {name}")

    app_allowlist = _build_app_allowlist(src / "system_metrics.json")

    with console.status("Loading privacy detection engine..."):
        pipeline = create_default_pipeline(
            pii_engine=pii_engine,
            person_allowlist=app_allowlist,
            require_pii=True,
        )
    anonymizer = Anonymizer()

    # Serialize the whole rebuild (rmtree → copytree → scrub → sentinel) against
    # any concurrent scrub/upload of the same recording. The pipeline load above
    # is read-only and stays outside the lock. ``_already_locked`` skips
    # re-acquiring when the upload loop already holds the lock (avoids a
    # same-process re-entrant flock deadlock).
    lock_cm = contextlib.nullcontext() if _already_locked else recording_scrub_lock(src.name)
    with lock_cm:
        dst = get_recordings_dir() / f"{name}-scrubbed"
        if dst.exists():
            console.print(
                f"  [yellow]Warning: {dst.name}/ already exists — replacing[/]"
            )
            shutil.rmtree(dst)

        with console.status(f"Copying {name} → {name}-scrubbed ..."):
            shutil.copytree(src, dst, symlinks=False, ignore=_copytree_ignore)

        # The scrubbed copy holds the same sensitivity class as the recording
        # (and is the exact payload an operator is about to upload), so lock it
        # to owner-only before writing any scrubbed bytes — and assert the mode
        # held, since a wider dir would expose the about-to-be-prepared copy.
        os.chmod(dst, 0o700)
        dir_mode = stat.S_IMODE(dst.stat().st_mode)
        if dir_mode != 0o700:
            raise RuntimeError(
                f"scrubbed dir {dst.name} has mode {oct(dir_mode)}, expected 0o700"
            )

        pre_deleted = _safety_delete_unscrubbable_files(src, dst)

        with console.status("Loading privacy policy and context..."):
            privacy_config = _resolve_privacy_config_for_dir(dst)
            evaluator = DefaultPolicyEvaluator(privacy_config)
            classifier = DefaultContextClassifier(
                app_classes=privacy_config.app_classes,
            )

        result = Scrubber(
            dst,
            pipeline=pipeline,
            anonymizer=anonymizer,
            evaluator=evaluator,
            classifier=classifier,
        ).run()

        # Merge file-deletion tracking from the copy stage with anything the
        # scrubber added (DB table deletions, etc.) so the summary lists both.
        result.deleted_files = sorted(set(result.deleted_files) | set(pre_deleted))

        _print_summary(result)

        # FINAL step (reached only on a fully-successful scrub — run() raises on
        # a structural failure): mark the dir reusable. Mirrors the chunk-upload
        # sentinel-gating posture — the last write, gated on all prior writes.
        _write_scrub_sentinel(dst, src, cloud_bound_recovery=cloud_bound_recovery)

    return result
