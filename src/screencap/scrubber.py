"""Create privacy-scrubbed copies of recordings."""

from __future__ import annotations

import json
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
    browser_events=None,
) -> list[_BlockedInterval]:
    """Build intervals where the frontmost app triggers EXCLUDE or MASK_WINDOW.

    Each window event defines a period from its timestamp to the next
    window event's timestamp (or infinity for the last event).
    """
    from screencap.privacy.context import (
        BROWSER_BUNDLE_IDS,
        find_nearest_browser,
    )
    from screencap.privacy.policy import FrameMetadata

    if not window_events:
        return []

    intervals: list[_BlockedInterval] = []
    browser_events = browser_events or []

    # Pre-compute browser timestamps once for all lookups
    browser_timestamps = [b.timestamp for b in browser_events]

    for i, we in enumerate(window_events):
        end_ts = (
            window_events[i + 1].timestamp
            if i + 1 < len(window_events)
            else float("inf")
        )

        # Build FrameMetadata directly — we already have the window event
        domain: str | None = None
        if we.app_bundle_id in BROWSER_BUNDLE_IDS and browser_events:
            browser = find_nearest_browser(
                browser_events, we.timestamp,
                _timestamps=browser_timestamps,
            )
            domain = browser.domain if browser else None

        meta = FrameMetadata(
            bundle_id=we.app_bundle_id,
            window_title=we.title,
            domain=domain,
            timestamp=we.timestamp,
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
    import bisect

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
    browser_events,
    result: ScrubResult,
) -> None:
    """Route screenshot files by policy/context.

    EXCLUDE → delete file.
    MASK_WINDOW → apply full-window structural mask (phase 4).
    MASK_REGION → apply pane-level structural mask.
    OCR_FALLBACK → fail closed to MASK_WINDOW (no OCR engine available).
    TEXT_REDACT / ALLOW → keep file.
    """
    from screencap.privacy.context import (
        associate_screenshot,
        parse_screenshot_timestamp,
    )
    from screencap.privacy.masking import MaskStrategy, mask_screenshot

    screenshots_dir = dst / "screenshots"
    if not screenshots_dir.is_dir():
        return

    # Pre-compute timestamp lists once for all screenshot lookups
    window_timestamps = [w.timestamp for w in window_events] if window_events else []
    browser_timestamps = [b.timestamp for b in browser_events] if browser_events else []

    for img_path in sorted(screenshots_dir.glob("*.jpg")):
        ts = parse_screenshot_timestamp(img_path.name)
        if ts is None:
            continue

        meta = associate_screenshot(
            ts, window_events, browser_events,
            _window_timestamps=window_timestamps,
            _browser_timestamps=browser_timestamps,
        )
        ctx = classifier.classify(meta)
        decision = evaluator.evaluate(ctx, meta)

        # Track the actual action taken — may differ from decision if
        # masking fails and falls back to deletion.
        actual_action = decision.action

        if decision.action == PrivacyAction.EXCLUDE:
            img_path.unlink()
        elif decision.action == PrivacyAction.MASK_WINDOW:
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

        result.audit_entries.append(
            AuditEntry(
                timestamp=ts,
                surface="screenshot",
                action=actual_action.value,
                reason=decision.reason,
                context_class=ctx.context_class.value,
                evidence_type=ctx.confidence,
            )
        )


# ---------------------------------------------------------------------------
# Blocked-app event handling
# ---------------------------------------------------------------------------


def _null_event_content(event: dict) -> None:
    """Null out sensitive content fields in an event dict in-place (recursive).

    Handles nested structures like key.type inside mouse.drag.children.
    """
    for field in KEYSTROKE_CONTENT_FIELDS:
        if field in event:
            event[field] = None
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

        for iv in intervals:
            if iv.end == float("inf"):
                ae_sql = (
                    f"UPDATE action_event SET {ae_set_clause} "
                    "WHERE timestamp >= ?"
                )
                ae_params = (iv.start,)
                we_sql = (
                    "UPDATE window_event SET "
                    "title = NULL, state = NULL "
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
                    "UPDATE window_event SET "
                    "title = NULL, state = NULL "
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
) -> dict | list:
    """Walk a JSON-parsed structure and scrub all string leaves.

    Mutates obj in-place. Depth-limited to 50.
    """
    if _depth > 50:
        return obj

    items = obj.items() if isinstance(obj, dict) else enumerate(obj)
    for key, value in items:
        if isinstance(value, str) and value.strip():
            obj[key], _ = _scrub_text(value, pipeline, anonymizer, result)
        elif isinstance(value, (dict, list)):
            _scrub_json_recursive(value, pipeline, anonymizer, result, _depth + 1)

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
) -> None:
    """Scrub a JSON blob column using separate read/write cursors."""
    read_cur = conn.cursor()
    write_cur = conn.cursor()
    read_cur.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL")
    for row_id, json_str in read_cur:
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
            _scrub_json_recursive(data, pipeline, anonymizer, result)
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
) -> None:
    """Scrub a JSON column, silently skipping if the column doesn't exist."""
    try:
        _scrub_json_column(conn, table, col, pipeline, anonymizer, result)
    except sqlite3.OperationalError:
        pass


def _scrub_recording_schema(
    conn: sqlite3.Connection,
    tables: set[str],
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> None:
    """Scrub a recording.db schema."""
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
        _try_scrub_json_column(conn, "action_event", "element_state", pipeline, anonymizer, result)

    if "window_event" in tables:
        _try_scrub_text_column(conn, "window_event", "title", pipeline, anonymizer, result)
        _try_scrub_json_column(conn, "window_event", "state", pipeline, anonymizer, result)

    if "browser_event" in tables:
        _try_scrub_json_column(conn, "browser_event", "message", pipeline, anonymizer, result)


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
) -> None:
    """Scrub all text surfaces in the recording database."""
    db_path = find_db(dst)
    if db_path is None:
        console.print("  [yellow]Warning: no database found — skipping DB scrubbing[/]")
        return

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
            _scrub_recording_schema(conn, tables, pipeline, anonymizer, result)
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


def _scrub_events_jsonl(
    dst: Path,
    pipeline,
    anonymizer,
    result: ScrubResult,
    blocked_intervals: list[_BlockedInterval] | None = None,
) -> None:
    """Scrub combined keystroke sequences in events.jsonl and map back to DB.

    1. Checks each event against blocked-app intervals — nulls content
       for events during EXCLUDE/MASK_WINDOW periods.
    2. Detects secrets in key.type event text fields (combined keystrokes).
    3. Redacts key_char in JSONL children (both key.down and key.up).
    4. Runs _scrub_json_recursive on ALL JSONL events for comprehensive PII scrub.
    5. Maps redactions back to action_event DB rows.
    6. Writes atomically (.tmp + rename).
    7. Deletes file on any processing error (fail-safe).
    """
    import os

    # Handle both legacy (events.jsonl) and chunked (events_NNNN.jsonl) layouts
    event_files = sorted(dst.glob("events*.jsonl"))
    if not event_files:
        return

    for events_jsonl in event_files:
        _scrub_single_events_jsonl(
            events_jsonl, dst, pipeline, anonymizer, result, blocked_intervals,
        )


def _scrub_single_events_jsonl(
    events_jsonl: Path,
    dst: Path,
    pipeline,
    anonymizer,
    result: ScrubResult,
    blocked_intervals: list[_BlockedInterval] | None = None,
) -> None:
    """Scrub a single events JSONL file."""
    import os

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
                result.audit_entries.append(
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
                event, pipeline, anonymizer, result, db_redactions
            )

            # Comprehensive scrub: run recursive walker on ALL events
            _scrub_json_recursive(event, pipeline, anonymizer, result)

            outfile.write(json.dumps(event, ensure_ascii=False) + "\n")

    # Batch DB redaction (single connection, single commit)
    if db_redactions:
        _redact_keystroke_db_rows(dst, db_redactions)

    # Atomic write or delete on error
    if had_errors:
        events_jsonl.unlink(missing_ok=True)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        console.print(
            "  [yellow]Warning: events.jsonl deleted due to processing errors[/]"
        )
    else:
        os.rename(tmp_path, str(events_jsonl))


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------


def _write_audit_log(dst: Path, result: ScrubResult) -> None:
    """Write export-safe audit log to the scrubbed recording directory."""
    if not result.audit_entries:
        return
    import dataclasses

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
        pii_engine: "presidio", "datafog", or None (auto-detect).

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
            load_browser_events,
            load_window_events,
        )
        from screencap.privacy.policy import (
            DefaultPolicyEvaluator,
            PrivacyMode,
            _MODE_STRICTNESS,
        )

        privacy_config = get_privacy_config()

        # Use the stricter of current config mode and capture-time intent
        intent_path = dst / ".recording_intent"
        if intent_path.exists():
            try:
                intent_data = json.loads(intent_path.read_text(encoding="utf-8"))
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
        # if the DB lacks window/browser event tables (older recordings).
        try:
            db_path = find_db(dst)
            window_events = load_window_events(db_path) if db_path else []
            browser_events = load_browser_events(db_path) if db_path else []

            blocked_intervals = _build_blocked_intervals(
                window_events, evaluator, classifier, browser_events
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
            browser_events = []

    # 10. Policy-aware screenshot routing
    if evaluator and classifier:
        with console.status("Routing screenshots by policy..."):
            _scrub_screenshots_with_policy(
                dst, evaluator, classifier, window_events, browser_events, result
            )

    # 11. Null DB rows during blocked-app intervals
    if blocked_intervals:
        with console.status("Nulling blocked-app DB rows..."):
            _null_db_rows_for_intervals(dst, blocked_intervals, result)

    # 12. Scrub DB
    with console.status("Scrubbing database..."):
        _scrub_db(dst, pipeline, anonymizer, result)

    # 13. Scrub combined keystroke sequences in events.jsonl
    with console.status("Scrubbing keystroke sequences..."):
        try:
            _scrub_events_jsonl(
                dst, pipeline, anonymizer, result, blocked_intervals
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
