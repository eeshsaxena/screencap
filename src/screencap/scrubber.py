"""Create privacy-scrubbed copies of recordings."""

from __future__ import annotations

import bisect
import dataclasses
import json
import os
import re
import shutil
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from screencap.catalog import find_db
from screencap.config import get_recordings_dir, resolve_recording_dir
from screencap.privacy.actions import BLOCK_ACTIONS, KEYSTROKE_CONTENT_FIELDS, PrivacyAction
from screencap.privacy.policy import DEFAULT_TRANSITION_HOLD_SECONDS
from screencap.privacy.reasons import AuditEntry, ReasonCode

# Actions that trigger masking for background windows. Everything except ALLOW —
# we can't text-redact or OCR a partial screenshot region, so masking is the
# only safe option for background windows.
_BG_MASK_ACTIONS = frozenset({
    PrivacyAction.EXCLUDE,
    PrivacyAction.MASK_WINDOW,
    PrivacyAction.MASK_REGION,
    PrivacyAction.TEXT_REDACT,
    PrivacyAction.OCR_FALLBACK,
})

console = Console()


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
        from screencap.privacy import normalize_text

        names = {normalize_text(app["name"]).lower() for app in apps if app.get("name")}
        return frozenset(names)
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
        console.print(f"  [yellow]Warning: could not read app allowlist: {e}[/]")
        return frozenset()


# Files to skip during copytree and delete as safety fallback.
_SKIP_FILES = {".upload_status.json", "viewer.html"}
_SKIP_EXTENSIONS = {".mp4", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus"}


@dataclass
class ScrubResult:
    """Summary of a scrub operation."""

    output_dir: Path = field(default_factory=Path)
    entity_counts: Counter = field(default_factory=Counter)
    deleted_files: list[str] = field(default_factory=list)
    audit_entries: list[AuditEntry] = field(default_factory=list)


def _copytree_ignore(directory: str, entries: list[str]) -> set[str]:
    """Ignore callback for shutil.copytree — skip media and derived files."""
    ignored = set()
    for entry in entries:
        if entry in _SKIP_FILES or Path(entry).suffix in _SKIP_EXTENSIONS:
            ignored.add(entry)
    return ignored


# ---------------------------------------------------------------------------
# Blocked-app interval building
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BlockedInterval:
    """Time interval where a blocked app was frontmost."""

    start: float
    end: float
    action: PrivacyAction
    reason: str


def _build_blocked_intervals(
    window_events,
    evaluator,
    classifier,
) -> list[_BlockedInterval]:
    """Build intervals where the frontmost app triggers EXCLUDE or MASK_WINDOW.

    Each window event defines a period from its timestamp to the next
    window event's timestamp (or infinity for the last event).
    """
    from screencap.privacy.policy import FrameMetadata

    if not window_events:
        return []

    intervals: list[_BlockedInterval] = []

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

        if decision.action in BLOCK_ACTIONS:
            intervals.append(
                _BlockedInterval(
                    start=we.timestamp,
                    end=end_ts,
                    action=decision.action,
                    reason=decision.reason,
                )
            )

    return intervals


def _build_secure_field_intervals(
    db_path: Path | None,
    hold_seconds: float = DEFAULT_TRANSITION_HOLD_SECONDS,
) -> list[_BlockedInterval]:
    """Build blocked intervals from action events with AXSecureTextField.

    Scans element_state JSON for AXRole or AXSubrole == "AXSecureTextField".
    Each detection creates a blocked interval starting at the event timestamp
    and lasting hold_seconds. Adjacent/overlapping intervals are merged.
    """
    if db_path is None:
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "action_event" not in tables:
            return []

        ae_cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(action_event)").fetchall()
        }
        if "element_state" not in ae_cols:
            return []

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
        merged: list[_BlockedInterval] = []
        cur_start, cur_end = raw_intervals[0]
        for start, end in raw_intervals[1:]:
            if start <= cur_end:
                cur_end = max(cur_end, end)
            else:
                merged.append(_BlockedInterval(
                    start=cur_start,
                    end=cur_end,
                    action=PrivacyAction.EXCLUDE,
                    reason=ReasonCode.SECURE_FIELD_DETECTED,
                ))
                cur_start, cur_end = start, end
        merged.append(_BlockedInterval(
            start=cur_start,
            end=cur_end,
            action=PrivacyAction.EXCLUDE,
            reason=ReasonCode.SECURE_FIELD_DETECTED,
        ))
        return merged
    finally:
        conn.close()


def _merge_intervals(
    *interval_lists: list[_BlockedInterval],
) -> list[_BlockedInterval]:
    """Concatenate and sort interval lists by start time.

    Overlapping intervals are tolerated — _find_blocked_interval only
    needs to answer "is this timestamp blocked?" and bisect handles
    overlaps correctly for that purpose.
    """
    all_intervals: list[_BlockedInterval] = []
    for ivs in interval_lists:
        all_intervals.extend(ivs)
    all_intervals.sort(key=lambda iv: iv.start)
    return all_intervals


def _find_blocked_interval(
    timestamp: float,
    intervals: list[_BlockedInterval],
    _starts: list[float] | None = None,
) -> _BlockedInterval | None:
    """Return the blocked interval containing timestamp, or None.

    Pass _starts (pre-computed [iv.start for iv in intervals]) to avoid
    rebuilding the list on every call.
    """
    if not intervals:
        return None
    if _starts is None:
        _starts = [iv.start for iv in intervals]
    idx = bisect.bisect_right(_starts, timestamp) - 1
    if idx >= 0 and intervals[idx].start <= timestamp < intervals[idx].end:
        return intervals[idx]
    return None


# ---------------------------------------------------------------------------
# Screenshot routing
# ---------------------------------------------------------------------------


def _scrub_screenshots_with_policy(
    dst: Path,
    evaluator,
    classifier,
    window_events,
    result: ScrubResult,
    db_path: Path | None = None,
    pixel_ratio: float = 2.0,
) -> None:
    """Route screenshot files by policy/context.

    EXCLUDE → delete file.
    MASK_WINDOW → selectively mask sensitive window regions using stored
        geometry. Falls back to full-window mask when geometry unavailable.
    MASK_REGION → apply pane-level structural mask.
    OCR_FALLBACK → fail closed to MASK_WINDOW (no OCR engine available).
    TEXT_REDACT / ALLOW → keep file.
    """
    from screencap.privacy.context import (
        associate_screenshot,
        load_window_geometry,
        parse_screenshot_timestamp,
    )
    from screencap.privacy.masking import (
        MaskStrategy,
        mask_screenshot,
        window_regions_from_geometry,
    )

    screenshots_dir = dst / "screenshots"
    if not screenshots_dir.is_dir():
        return

    # Pre-compute timestamp lists once for all screenshot lookups
    window_timestamps = [w.timestamp for w in window_events] if window_events else []

    # Open a shared connection for geometry lookups (avoids per-screenshot overhead)
    _geom_conn = None
    if db_path is not None:
        try:
            _geom_conn = sqlite3.connect(str(db_path))
        except sqlite3.OperationalError:
            pass

    for img_path in sorted(screenshots_dir.glob("*.jpg")):
        ts = parse_screenshot_timestamp(img_path.name)
        if ts is None:
            continue

        meta = associate_screenshot(
            ts, window_events,
            _window_timestamps=window_timestamps,
        )
        ctx = classifier.classify(meta)
        decision = evaluator.evaluate(ctx, meta)

        # Track the actual action taken — may differ from decision if
        # masking fails and falls back to deletion.
        actual_action = decision.action

        if decision.action == PrivacyAction.EXCLUDE:
            img_path.unlink()
        elif decision.action == PrivacyAction.MASK_WINDOW:
            # Attempt selective masking using per-screenshot window geometry.
            # Falls back to full-frame masking when geometry is unavailable.
            selective_applied = False
            if _geom_conn is not None:
                try:
                    geom = load_window_geometry(db_path, ts, conn=_geom_conn)
                    if geom is not None:
                        from PIL import Image

                        with Image.open(img_path) as probe:
                            img_w, img_h = probe.size
                        regions = window_regions_from_geometry(
                            geom.windows, img_w, img_h, pixel_ratio,
                            classifier, evaluator,
                            display_origin=geom.display_origin,
                            respect_z_order=True,
                        )
                        if regions:
                            mask_screenshot(
                                img_path,
                                ctx.context_class,
                                regions=regions,
                            )
                            selective_applied = True
                        # No regions = no sensitive windows visible; keep as-is
                        else:
                            selective_applied = True
                except Exception as exc:
                    console.print(
                        f"  [yellow]Warning: selective masking failed for "
                        f"{img_path.name} ({exc}) — falling back to full-frame[/]"
                    )

            if not selective_applied:
                # Fallback: full-frame masking (original behavior)
                try:
                    mask_screenshot(
                        img_path,
                        ctx.context_class,
                        strategy=MaskStrategy.FULL_WINDOW,
                        app_hint=meta.bundle_id,
                    )
                except Exception as exc:
                    console.print(
                        f"  [yellow]Warning: masking failed for {img_path.name} "
                        f"({exc}) — deleting for safety[/]"
                    )
                    img_path.unlink()
                    actual_action = PrivacyAction.EXCLUDE
        elif decision.action == PrivacyAction.MASK_REGION:
            try:
                mask_screenshot(
                    img_path,
                    ctx.context_class,
                    strategy=MaskStrategy.PANE,
                    app_hint=meta.bundle_id,
                )
            except Exception as exc:
                console.print(
                    f"  [yellow]Warning: region masking failed for {img_path.name} "
                    f"({exc}) — applying full-window mask[/]"
                )
                try:
                    mask_screenshot(
                        img_path,
                        ctx.context_class,
                        strategy=MaskStrategy.FULL_WINDOW,
                        app_hint=meta.bundle_id,
                    )
                    actual_action = PrivacyAction.MASK_WINDOW
                except Exception:
                    img_path.unlink()
                    actual_action = PrivacyAction.EXCLUDE
        elif decision.action == PrivacyAction.OCR_FALLBACK:
            # No OCR engine available — fail closed to full-window mask
            try:
                mask_screenshot(
                    img_path,
                    ctx.context_class,
                    strategy=MaskStrategy.FULL_WINDOW,
                    app_hint=meta.bundle_id,
                )
                actual_action = PrivacyAction.MASK_WINDOW
            except Exception:
                img_path.unlink()
                actual_action = PrivacyAction.EXCLUDE

        # --- Background masking for ALLOW / TEXT_REDACT ---
        # The foreground app is safe, but sensitive background windows
        # (Slack, Mail, etc.) may be visible behind it.
        bg_masked = False
        if decision.action in (PrivacyAction.ALLOW, PrivacyAction.TEXT_REDACT):
            if _geom_conn is not None and img_path.exists():
                try:
                    geom = load_window_geometry(db_path, ts, conn=_geom_conn)
                    if geom is not None:
                        from PIL import Image

                        with Image.open(img_path) as probe:
                            img_w, img_h = probe.size
                        regions = window_regions_from_geometry(
                            geom.windows, img_w, img_h, pixel_ratio,
                            classifier, evaluator,
                            display_origin=geom.display_origin,
                            mask_actions=_BG_MASK_ACTIONS,
                            respect_z_order=True,
                        )
                        if regions:
                            mask_screenshot(
                                img_path,
                                ctx.context_class,
                                regions=regions,
                            )
                            bg_masked = True
                except Exception as exc:
                    # Fail-closed: geometry found sensitive windows but masking
                    # failed — fall back to full-frame mask rather than leaking.
                    console.print(
                        f"  [yellow]Warning: background masking failed for "
                        f"{img_path.name} ({exc}) — applying full-frame mask[/]"
                    )
                    try:
                        mask_screenshot(
                            img_path,
                            ctx.context_class,
                            strategy=MaskStrategy.FULL_WINDOW,
                            app_hint=meta.bundle_id,
                        )
                        bg_masked = True
                    except Exception:
                        img_path.unlink()
                        actual_action = PrivacyAction.EXCLUDE

        result.audit_entries.append(
            AuditEntry(
                timestamp=ts,
                surface="screenshot",
                action=actual_action.value,
                reason=f"{decision.reason}+background_windows_masked"
                    if bg_masked else decision.reason,
                context_class=ctx.context_class.value,
                evidence_type=f"{ctx.confidence}+geometry"
                    if bg_masked else ctx.confidence,
            )
        )

    if _geom_conn is not None:
        _geom_conn.close()


# ---------------------------------------------------------------------------
# Blocked-app event handling
# ---------------------------------------------------------------------------


def _null_event_content(event: dict) -> None:
    """Null out sensitive content fields in an event dict in-place (recursive).

    Handles nested structures like key.type inside mouse.drag.children,
    and window.switch title/domain fields.
    """
    for field in KEYSTROKE_CONTENT_FIELDS:
        if field in event:
            event[field] = None
    if event.get("type") == "window.switch":
        event["window_title"] = None
        event["domain"] = None
    for child in event.get("children", []):
        _null_event_content(child)


def _null_db_rows_for_intervals(
    dst: Path, intervals: list[_BlockedInterval], result: ScrubResult
) -> None:
    """Null out action_event and window_event columns during blocked-app intervals."""
    if not intervals:
        return
    db_path = find_db(dst)
    if db_path is None:
        return

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        # Build SET clause from canonical field list, skipping columns
        # absent in older recordings.
        ae_cols: set[str] = set()
        if "action_event" in tables:
            ae_cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(action_event)").fetchall()
            }
        null_fields = sorted(f for f in KEYSTROKE_CONTENT_FIELDS if f in ae_cols)
        ae_set_clause = ", ".join(f"{f} = NULL" for f in null_fields)

        # Build window_event SET clause — include browser_url if column exists
        we_null_cols = ["title", "state"]
        if "window_event" in tables:
            we_cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(window_event)").fetchall()
            }
            if "browser_url" in we_cols:
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

            if "action_event" in tables and ae_set_clause:
                conn.execute(ae_sql, ae_params)

            if "window_event" in tables:
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
    finally:
        conn.close()


def _scrub_text(
    text: str,
    pipeline,
    anonymizer,
    result: ScrubResult,
):
    """Run text through the detection pipeline and anonymize.

    Returns (scrubbed_text, detection_result). detection_result is None when
    text was empty, whitespace-only, or all detectors failed.

    On AllDetectorsFailedError, returns ('<SCRUB_FAILED>', None).
    """
    if not text or not text.strip():
        return text, None

    # Deferred import — only needed here.
    from screencap.privacy import AllDetectorsFailedError

    try:
        detection_result = pipeline.detect(text)
    except AllDetectorsFailedError:
        console.print("  [yellow]Warning: all detectors failed on a field[/]")
        return "<SCRUB_FAILED>", None
    except Exception as exc:
        console.print(
            f"  [yellow]Warning: detection pipeline error ({type(exc).__name__}) "
            f"— failing closed[/]"
        )
        return "<SCRUB_FAILED>", None

    scrubbed = anonymizer.anonymize(
        detection_result.normalized_text,
        detection_result.detections,
    )

    for det in detection_result.detections:
        result.entity_counts[det.entity_type] += 1

    return scrubbed, detection_result


# ---------------------------------------------------------------------------
# JSON recursive walker
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
            obj[key], det_result = _scrub_text(value, pipeline, anonymizer, result)
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


# ---------------------------------------------------------------------------
# DB scrubbing
# ---------------------------------------------------------------------------


def _scrub_json_column(
    conn: sqlite3.Connection,
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
    has_timestamp = False
    if row_detection_collector is not None:
        try:
            read_cur.execute(
                f"SELECT id, {col}, timestamp FROM {table} WHERE {col} IS NOT NULL"
            )
            has_timestamp = True
        except sqlite3.OperationalError:
            read_cur.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL")
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
    conn: sqlite3.Connection,
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


def _try_scrub_text_column(
    conn: sqlite3.Connection,
    table: str,
    col: str,
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> None:
    """Scrub a text column, silently skipping if the column doesn't exist."""
    try:
        _scrub_text_column(conn, table, col, pipeline, anonymizer, result)
    except sqlite3.OperationalError:
        pass


def _try_scrub_json_column(
    conn: sqlite3.Connection,
    table: str,
    col: str,
    pipeline,
    anonymizer,
    result: ScrubResult,
    row_detection_collector: dict[int, dict] | None = None,
) -> None:
    """Scrub a JSON column, silently skipping if the column doesn't exist."""
    try:
        _scrub_json_column(
            conn, table, col, pipeline, anonymizer, result,
            row_detection_collector=row_detection_collector,
        )
    except sqlite3.OperationalError:
        pass


def _scrub_recording_schema(
    conn: sqlite3.Connection,
    tables: set[str],
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> dict[int, dict]:
    """Scrub a recording.db schema.

    Returns a dict mapping row_id → {timestamp, detections} for
    element_state AXValue detections (used for keystroke cross-referencing).
    """
    element_state_detections: dict[int, dict] = {}

    if "recording" in tables:
        _try_scrub_text_column(conn, "recording", "task_description", pipeline, anonymizer, result)

    if "action_event" in tables:
        for col in (
            "key_char",
            "canonical_key_char",
            "key_name",
            "canonical_key_name",
            "active_segment_description",
            "available_segment_descriptions",
        ):
            _try_scrub_text_column(conn, "action_event", col, pipeline, anonymizer, result)
        _try_scrub_json_column(
            conn, "action_event", "element_state", pipeline, anonymizer, result,
            row_detection_collector=element_state_detections,
        )

    if "window_event" in tables:
        _try_scrub_text_column(conn, "window_event", "title", pipeline, anonymizer, result)
        _try_scrub_json_column(conn, "window_event", "state", pipeline, anonymizer, result)
        _try_scrub_text_column(conn, "window_event", "browser_url", pipeline, anonymizer, result)

    return element_state_detections


def _scrub_capture_schema(
    conn: sqlite3.Connection,
    tables: set[str],
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> None:
    """Scrub a capture.db schema."""
    if "capture" in tables:
        _try_scrub_text_column(conn, "capture", "task_description", pipeline, anonymizer, result)

    if "events" in tables:
        _try_scrub_json_column(conn, "events", "data", pipeline, anonymizer, result)


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
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}

        # Delete unscrubable binary/audio data from DB
        if "screenshot" in tables:
            cur.execute("DELETE FROM screenshot")
            result.deleted_files.append("screenshot table (binary BLOBs)")
        if "audio_info" in tables:
            cur.execute("DELETE FROM audio_info")
            result.deleted_files.append("audio_info table (spoken words)")

        if "capture" in tables:
            _scrub_capture_schema(conn, tables, pipeline, anonymizer, result)
        elif "recording" in tables:
            element_state_detections = _scrub_recording_schema(
                conn, tables, pipeline, anonymizer, result
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
    finally:
        conn.close()

    return element_state_detections


# ---------------------------------------------------------------------------
# Element-state cross-reference lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ElementStateDetection:
    """A PII detection from an element_state AXValue field.

    Used to cross-reference against keystrokes whose timestamps
    fall within the same action_event rows.
    """

    original_text: str
    entity_type: str
    timestamps: frozenset[float]


def _build_xref_lookup(
    raw_detections: dict[int, dict],
) -> list[_ElementStateDetection]:
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
        _ElementStateDetection(
            original_text=v["original_text"],
            entity_type=v["entity_type"],
            timestamps=frozenset(v["timestamps"]),
        )
        for v in grouped.values()
    ]


# ---------------------------------------------------------------------------
# Transcript scrubbing
# ---------------------------------------------------------------------------


def _scrub_transcript_json(
    path: Path,
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> None:
    """Scrub PII from transcript.json."""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        console.print(f"  [yellow]Warning: could not read transcript.json: {e}[/]")
        return

    if isinstance(data, dict):
        if "text" in data and isinstance(data["text"], str):
            data["text"], _ = _scrub_text(data["text"], pipeline, anonymizer, result)

        if "segments" in data and isinstance(data["segments"], list):
            for seg in data["segments"]:
                if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                    seg["text"], _ = _scrub_text(seg["text"], pipeline, anonymizer, result)

        if "words" in data and isinstance(data["words"], list):
            for word_entry in data["words"]:
                if isinstance(word_entry, dict) and isinstance(
                    word_entry.get("word"), str
                ):
                    word_entry["word"], _ = _scrub_text(
                        word_entry["word"], pipeline, anonymizer, result
                    )

    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _scrub_transcript_txt(
    path: Path,
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> None:
    """Scrub PII from transcript.txt."""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
        scrubbed, _ = _scrub_text(text, pipeline, anonymizer, result)
        path.write_text(scrubbed, encoding="utf-8")
    except OSError as e:
        console.print(f"  [yellow]Warning: could not scrub transcript.txt: {e}[/]")


# ---------------------------------------------------------------------------
# System metrics scrubbing (rule-based)
# ---------------------------------------------------------------------------


def _scrub_metrics(path: Path, result: ScrubResult) -> list[str]:
    """Redact hostname and WiFi identifiers from system_metrics.json.

    Returns list of redacted field names (e.g. ["hostname", "wifi.ssid"]).
    """
    if not path.exists():
        return []
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

        return redacted

    except (json.JSONDecodeError, OSError) as e:
        console.print(
            f"  [yellow]Warning: could not scrub system_metrics.json: {e}[/]"
        )
        return []


# ---------------------------------------------------------------------------
# Events JSONL scrubbing (combined keystroke detection)
# ---------------------------------------------------------------------------


def _process_single_key_type(
    event: dict,
    pipeline,
    anonymizer,
    result: ScrubResult,
    db_redactions: list[dict],
) -> None:
    """Detect secrets in a single key.type event and collect DB redaction info."""
    text = event.get("text", "")
    scrubbed, detection_result = _scrub_text(
        text, pipeline, anonymizer, result
    )

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
        # Find the paired key.up (typically the next child)
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


def _redact_keystroke_db_rows(dst: Path, redactions: list[dict]) -> None:
    """Batch-redact key_char/canonical_key_char in action_event rows.

    Uses a single DB connection and single commit for all redactions.
    Matches by (timestamp, key_char, event_name) composite key.
    """
    db_path = find_db(dst)
    if db_path is None:
        return

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "action_event" not in tables:
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
    finally:
        conn.close()


def _cross_reference_key_type(
    event: dict,
    xref_detections: list[_ElementStateDetection],
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

    # Get time range from children
    child_timestamps = [
        c.get("timestamp")
        for c in children
        if c.get("timestamp") is not None
    ]
    if not child_timestamps:
        return
    min_ts = min(child_timestamps)
    max_ts = max(child_timestamps)

    # Find matching detections: any whose timestamps overlap this event's range
    matching_dets = [
        d for d in xref_detections
        if any(min_ts <= ts <= max_ts for ts in d.timestamps)
    ]
    if not matching_dets:
        return

    # Build current text from non-null key_char children (skip already-nulled)
    # Also build pos_to_child map: character position → child index
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

    # Track positions to redact (set of char positions in current_text)
    redact_positions: set[int] = set()
    matched_entity_types: list[tuple[int, int, str]] = []  # (start, end, entity_type)

    for det in matching_dets:
        # Split detection text into tokens for matching
        tokens = det.original_text.split()

        # First try matching the full string
        pattern = r"(?<![a-zA-Z0-9])" + re.escape(det.original_text) + r"(?![a-zA-Z0-9])"
        m = re.search(pattern, current_text, re.IGNORECASE)
        if m:
            for pos in range(m.start(), m.end()):
                if pos in pos_to_child:
                    redact_positions.add(pos)
            matched_entity_types.append((m.start(), m.end(), det.entity_type))
            continue

        # If no full match, try individual tokens
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

    # Expand to include paired key.up for each redacted key.down
    redact_child_indices: set[int] = set()
    for pos in redact_positions:
        down_idx = pos_to_child[pos]
        redact_child_indices.add(down_idx)
        for j in range(down_idx + 1, len(children)):
            if children[j].get("type") == "key.up":
                redact_child_indices.add(j)
                break

    # Collect DB redaction info BEFORE nulling
    for idx in redact_child_indices:
        child = children[idx]
        if child.get("key_char"):
            db_redactions.append({
                "timestamp": child.get("timestamp"),
                "key_char": child["key_char"],
                "type": child.get("type"),
            })

    # Null in JSONL
    for idx in redact_child_indices:
        child = children[idx]
        child["key_char"] = None
        child["canonical_key_char"] = None

    # Update text field: replace matched spans with <ENTITY_TYPE> tags.
    # Use current_text (rebuilt from non-null children) because offsets in
    # matched_entity_types were computed against it, not event["text"]
    # which may have been modified by the primary detection pass.
    text = current_text
    for start, end, entity_type in sorted(matched_entity_types, reverse=True):
        text = text[:start] + f"<{entity_type}>" + text[end:]
    event["text"] = text

    # Audit entries
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


def _scrub_events_jsonl(
    dst: Path,
    pipeline,
    anonymizer,
    result: ScrubResult,
    blocked_intervals: list[_BlockedInterval] | None = None,
    xref_detections: list[_ElementStateDetection] | None = None,
) -> None:
    """Scrub combined keystroke sequences in events.jsonl and map back to DB.

    Dispatches to ``scrub_events_jsonl()`` for each events file, then handles
    error cleanup (deletes the original file on processing errors).
    """
    # Handle both legacy (events.jsonl) and chunked (events_NNNN.jsonl) layouts
    event_files = sorted(dst.glob("events*.jsonl"))
    if not event_files:
        return

    for events_file in event_files:
        had_errors = scrub_events_jsonl(
            events_file,
            pipeline,
            anonymizer,
            blocked_intervals=blocked_intervals,
            xref_detections=xref_detections,
            db_dir=dst,
            result=result,
        )
        if had_errors:
            events_file.unlink(missing_ok=True)
            console.print(
                "  [yellow]Warning: events.jsonl deleted due to processing errors[/]"
            )


def scrub_events_jsonl(
    events_jsonl: Path,
    pipeline,
    anonymizer,
    *,
    blocked_intervals: list[_BlockedInterval] | None = None,
    xref_detections: list[_ElementStateDetection] | None = None,
    db_dir: Path | None = None,
    result: ScrubResult | None = None,
) -> bool:
    """Scrub PII from an events JSONL file. Returns True if errors occurred.

    Shared between the scrubber (full recording scrub) and the chunk processor
    (inline cloud-intent scrub). On error, cleans up the .tmp file but does NOT
    delete/rename the original — callers decide their own error policy.

    Args:
        events_jsonl: Path to the events JSONL file to scrub in-place.
        pipeline: Detection pipeline instance.
        anonymizer: Anonymizer instance.
        blocked_intervals: Optional list of blocked-app time intervals.
        xref_detections: Optional element_state cross-reference detections.
        db_dir: If provided, batch-redact keystroke DB rows in this directory.
        result: If provided, accumulate entity counts and audit entries.

    Returns:
        True if processing errors occurred, False on success.
    """
    # Use a local ScrubResult if caller doesn't need counts/audit.
    _result = result if result is not None else ScrubResult()

    blocked_intervals = blocked_intervals or []
    blocked_starts = [iv.start for iv in blocked_intervals]
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
                console.print(
                    "  [yellow]Warning: malformed JSON line in events.jsonl[/]"
                )
                had_errors = True
                continue

            if event.get("_meta"):
                outfile.write(json.dumps(event, ensure_ascii=False) + "\n")
                continue

            # Check blocked-app intervals
            event_ts = event.get("timestamp", 0.0)
            blocked = _find_blocked_interval(event_ts, blocked_intervals, blocked_starts)
            if blocked is not None:
                _null_event_content(event)
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
            if xref_detections:
                if event.get("type") == "key.type":
                    _cross_reference_key_type(
                        event, xref_detections, _result, db_redactions
                    )
                elif event.get("type") == "mouse.drag":
                    for child in event.get("children", []):
                        if child.get("type") == "key.type":
                            _cross_reference_key_type(
                                child, xref_detections, _result, db_redactions
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


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------


def _write_audit_log(dst: Path, result: ScrubResult) -> None:
    """Write export-safe audit log to the scrubbed recording directory."""
    if not result.audit_entries:
        return
    entries = [dataclasses.asdict(e) for e in result.audit_entries]
    (dst / "privacy_audit.json").write_text(
        json.dumps(entries, indent=2), encoding="utf-8"
    )


def _print_summary(result: ScrubResult, rule_based_redactions: list[str]) -> None:
    """Print a Rich summary of the scrub operation."""
    console.print(f"\n[bold green]Scrub complete:[/] {result.output_dir.name}/\n")

    if result.entity_counts:
        console.print("  [bold]Entities detected:[/]")
        for entity_type, count in result.entity_counts.most_common():
            console.print(f"    {entity_type:<20s} {count}")
    else:
        console.print("  No PII or secrets detected in text surfaces.")

    if rule_based_redactions:
        console.print(f"\n  [bold]Rule-based redactions:[/]")
        console.print(f"    system_metrics.json: {', '.join(rule_based_redactions)}")

    if result.deleted_files:
        console.print(f"\n  [bold]Removed from scrubbed copy:[/]")
        # Separate file deletions from DB table deletions
        file_dels = [f for f in result.deleted_files if "table" not in f]
        db_dels = [f for f in result.deleted_files if "table" in f]
        if file_dels:
            console.print(f"    {', '.join(file_dels)}")
        if db_dels:
            console.print(f"\n  [bold]Deleted from DB:[/]")
            for d in db_dels:
                console.print(f"    {d}")

    console.print()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def scrub_recording(
    name: str,
    pii_engine: str | None = None,
) -> ScrubResult:
    """Copy a recording and scrub PII from the copy.

    Never mutates the original recording directory.

    Args:
        name: Recording name (directory name under recordings/).
        pii_engine: "presidio", "presidio-gliner", or None (auto-detect).

    Returns:
        ScrubResult with entity counts and deleted files.

    Raises:
        FileNotFoundError: Recording directory does not exist.
        ImportError: Privacy dependencies not installed.
        ValueError: Invalid recording name (path traversal).
    """
    # Privacy imports deferred here — not at module level — so that
    # screencap --help works without privacy deps installed.
    from screencap.privacy import Anonymizer, create_default_pipeline

    # 1. Validate name (raises ValueError on path traversal)
    src = resolve_recording_dir(name)

    # 2. Check source exists
    if not src.exists():
        raise FileNotFoundError(f"Recording not found: {name}")

    # 3. Build app-name allowlist from source metrics (before any mutation)
    app_allowlist = _build_app_allowlist(src / "system_metrics.json")

    # 4. Validate deps — create pipeline BEFORE any filesystem mutation
    with console.status("Loading privacy detection engine..."):
        pipeline = create_default_pipeline(
            pii_engine=pii_engine,
            person_allowlist=app_allowlist,
        )

    # 5. Create anonymizer
    anonymizer = Anonymizer()

    result = ScrubResult()

    # 6. Handle existing scrubbed dir
    recordings_dir = get_recordings_dir()
    dst = recordings_dir / f"{name}-scrubbed"
    if dst.exists():
        console.print(
            f"  [yellow]Warning: {dst.name}/ already exists — replacing[/]"
        )
        shutil.rmtree(dst)

    # 7. Copy (skipping media/derived files)
    with console.status(f"Copying {name} → {name}-scrubbed ..."):
        shutil.copytree(src, dst, symlinks=False, ignore=_copytree_ignore)

    result.output_dir = dst

    # 8. Safety fallback deletion — remove any files that survived the ignore callback
    deleted_files: list[str] = []
    for skip_name in _SKIP_FILES:
        p = dst / skip_name
        if p.exists():
            p.unlink()
            deleted_files.append(skip_name)
    for ext in _SKIP_EXTENSIONS:
        for f in dst.glob(f"*{ext}"):
            f.unlink()
            deleted_files.append(f.name)

    # Track which files were skipped/deleted (union of ignore + fallback)
    all_skipped = set()
    # Check what was in the source but not in dest
    for f in src.iterdir():
        if f.name in _SKIP_FILES or f.suffix in _SKIP_EXTENSIONS:
            all_skipped.add(f.name)
    for name_del in deleted_files:
        all_skipped.add(name_del)
    result.deleted_files = sorted(all_skipped)

    # 9. Load policy/context for policy-aware scrubbing
    blocked_intervals: list[_BlockedInterval] = []
    with console.status("Loading privacy policy and context..."):
        # Phase 1: Load policy engine — hard fail if unavailable, since
        # we cannot determine what to scrub without a policy.
        from screencap.config import get_privacy_config
        from screencap.privacy.context import (
            DefaultContextClassifier,
            load_window_events,
        )
        from screencap.privacy.policy import (
            DefaultPolicyEvaluator,
            PrivacyMode,
            _MODE_STRICTNESS,
        )

        privacy_config = get_privacy_config()

        # Use the stricter of current config mode and capture-time intent.
        # Cloud-intent recordings always force public mode for scrubbing
        # parity with the chunk processor's export-time privacy filter.
        intent_path = dst / ".recording_intent"
        if intent_path.exists():
            try:
                intent_data = json.loads(intent_path.read_text(encoding="utf-8"))
                destination = intent_data.get("destination", "")
                if destination == "cloud":
                    from dataclasses import replace as _dc_replace
                    privacy_config = _dc_replace(
                        privacy_config, mode=PrivacyMode.PUBLIC,
                    )
                else:
                    intent_mode = PrivacyMode(intent_data["privacy_mode"])
                    if _MODE_STRICTNESS[intent_mode] < _MODE_STRICTNESS[privacy_config.mode]:
                        from dataclasses import replace as _dc_replace
                        privacy_config = _dc_replace(privacy_config, mode=intent_mode)
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                pass  # Missing or malformed intent — use current config

        evaluator = DefaultPolicyEvaluator(privacy_config)
        classifier = DefaultContextClassifier(
            app_classes=privacy_config.app_classes,
        )

        # Phase 2: Load recording-specific context — gracefully fall back
        # if the DB lacks window event tables (older recordings).
        try:
            db_path = find_db(dst)
            window_events = load_window_events(db_path) if db_path else []

            blocked_intervals = _build_blocked_intervals(
                window_events, evaluator, classifier
            )

            # Build secure-field intervals from AXSecureTextField in element_state
            secure_field_intervals = _build_secure_field_intervals(db_path)
            if secure_field_intervals:
                blocked_intervals = _merge_intervals(
                    blocked_intervals, secure_field_intervals
                )
        except Exception as exc:
            console.print(
                f"  [yellow]Warning: could not load recording context ({exc}) — "
                f"policy-aware screenshot routing will be skipped[/]"
            )
            window_events = []

    # 10. Policy-aware screenshot routing (with selective masking)
    if evaluator and classifier:
        # Read pixel_ratio from recording DB for Retina scaling
        _pixel_ratio = 2.0  # safe default for Retina Macs
        if db_path:
            try:
                with sqlite3.connect(str(db_path)) as _pr_conn:
                    _pr_row = _pr_conn.execute(
                        "SELECT pixel_ratio FROM recording LIMIT 1"
                    ).fetchone()
                    if _pr_row and _pr_row[0]:
                        _pixel_ratio = float(_pr_row[0])
            except (sqlite3.OperationalError, ValueError):
                pass

        with console.status("Routing screenshots by policy..."):
            _scrub_screenshots_with_policy(
                dst, evaluator, classifier, window_events, result,
                db_path=db_path, pixel_ratio=_pixel_ratio,
            )

    # 11. Null DB rows during blocked-app intervals
    if blocked_intervals:
        with console.status("Nulling blocked-app DB rows..."):
            _null_db_rows_for_intervals(dst, blocked_intervals, result)

    # 12. Scrub DB — collect element_state detections for cross-referencing
    with console.status("Scrubbing database..."):
        raw_detections = _scrub_db(dst, pipeline, anonymizer, result)

    # 12b. Build cross-reference lookup from element_state detections
    xref_detections = _build_xref_lookup(raw_detections)

    # 13. Scrub combined keystroke sequences in events.jsonl
    with console.status("Scrubbing keystroke sequences..."):
        try:
            _scrub_events_jsonl(
                dst, pipeline, anonymizer, result, blocked_intervals,
                xref_detections=xref_detections,
            )
        except Exception as exc:
            console.print(
                f"  [yellow]Warning: events JSONL scrubbing failed ({exc}) — "
                f"deleting for safety[/]"
            )
            for f in dst.glob("events*.jsonl"):
                f.unlink(missing_ok=True)

    # 14. Scrub transcripts (legacy + chunked layouts)
    with console.status("Scrubbing transcripts..."):
        for tj in sorted(dst.glob("transcript*.json")):
            _scrub_transcript_json(tj, pipeline, anonymizer, result)
        for tt in sorted(dst.glob("transcript*.txt")):
            _scrub_transcript_txt(tt, pipeline, anonymizer, result)

    # 15. Scrub metrics (rule-based)
    metrics_path = dst / "system_metrics.json"
    rule_based_redactions = _scrub_metrics(metrics_path, result)

    # 16. Write audit log
    _write_audit_log(dst, result)

    # 17. Print summary
    _print_summary(result, rule_based_redactions)

    # 18. Return result
    return result
