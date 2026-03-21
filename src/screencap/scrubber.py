"""Create privacy-scrubbed copies of recordings."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sqlite3
from pathlib import Path

from rich.console import Console
from rich.table import Table

from screencap.catalog import find_db
from screencap.config import get_recordings_dir, resolve_recording_dir
from screencap.privacy.actions import KEYSTROKE_CONTENT_FIELDS, PrivacyAction
from screencap.privacy.reasons import AuditEntry, ReasonCode
from screencap.scrub_pipeline import (
    BlockedInterval,
    ElementStateDetection,
    ScrubContext,
    ScrubResult,
    build_blocked_intervals,
    build_scrub_context,
    build_secure_field_intervals,
    build_xref_lookup,
    find_blocked_interval,
    mask_screenshots,
    merge_intervals,
    null_event_content,
    scrub_events_jsonl,
    scrub_manifest,
    scrub_text,
    scrub_transcripts,
    _scrub_json_recursive,
)

console = Console()

# Backward-compat aliases for tests that import private names.
# Pipeline types/functions are re-exported under their old names.
_BlockedInterval = BlockedInterval
_ElementStateDetection = ElementStateDetection
_build_blocked_intervals = build_blocked_intervals
_build_secure_field_intervals = build_secure_field_intervals
_merge_intervals = merge_intervals
_find_blocked_interval = find_blocked_interval
_null_event_content = null_event_content
_build_xref_lookup = build_xref_lookup

# Re-export pipeline's private functions for test compat.
from screencap.scrub_pipeline import (
    _cross_reference_key_type,
    _scrub_transcript_json,
    _scrub_transcript_txt,
)


def _scrub_screenshots_with_policy(
    dst: Path,
    evaluator,
    classifier,
    window_events,
    result: ScrubResult,
    db_path: Path | None = None,
    pixel_ratio: float = 2.0,
) -> None:
    """Backward-compat wrapper — delegates to ``mask_screenshots``."""
    ctx = ScrubContext(
        window_events=window_events,
        evaluator=evaluator,
        classifier=classifier,
        pixel_ratio=pixel_ratio,
    )
    mask_screenshots(
        dst / "screenshots", ctx,
        db_path=db_path, result=result,
    )


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


def _copytree_ignore(directory: str, entries: list[str]) -> set[str]:
    """Ignore callback for shutil.copytree — skip media and derived files."""
    ignored = set()
    for entry in entries:
        if entry in _SKIP_FILES or Path(entry).suffix in _SKIP_EXTENSIONS:
            ignored.add(entry)
    return ignored


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
    if scrubbed == "<SCRUB_FAILED>":
        console.print("  [yellow]Warning: all detectors failed on a field[/]")
    return scrubbed, det_result


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
# Events JSONL scrubbing (file iteration wrapper)
# ---------------------------------------------------------------------------


def _scrub_events_jsonl(
    dst: Path,
    pipeline,
    anonymizer,
    result: ScrubResult,
    blocked_intervals: list[BlockedInterval] | None = None,
    xref_detections: list[ElementStateDetection] | None = None,
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
            require_pii=True,
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
    with console.status("Loading privacy policy and context..."):
        from screencap.config import get_privacy_config
        from screencap.privacy.context import DefaultContextClassifier
        from screencap.privacy.policy import (
            DefaultPolicyEvaluator,
            PrivacyMode,
            _MODE_STRICTNESS,
        )

        privacy_config = get_privacy_config()

        # Use the stricter of current config mode and capture-time intent.
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
                pass

        evaluator = DefaultPolicyEvaluator(privacy_config)
        classifier = DefaultContextClassifier(
            app_classes=privacy_config.app_classes,
        )

        db_path = find_db(dst)

    # Build scrub context (intervals + window events, but NOT xref —
    # xref is collected during DB scrub step and passed to JSONL scrubbing).
    scrub_ctx = ScrubContext()
    with console.status("Building scrub context..."):
        scrub_ctx = build_scrub_context(
            db_path, evaluator, classifier,
        )

    # 10. Policy-aware screenshot routing (with selective masking)
    if evaluator and classifier:
        with console.status("Routing screenshots by policy..."):
            mask_screenshots(
                dst / "screenshots", scrub_ctx,
                db_path=db_path, result=result,
            )

    # 11. Null DB rows during blocked-app intervals
    if scrub_ctx.blocked_intervals:
        with console.status("Nulling blocked-app DB rows..."):
            _null_db_rows_for_intervals(dst, scrub_ctx.blocked_intervals, result)

    # 12. Scrub DB — collect element_state detections for cross-referencing
    with console.status("Scrubbing database..."):
        raw_detections = _scrub_db(dst, pipeline, anonymizer, result)

    # 12b. Build cross-reference lookup from element_state detections
    xref_detections = build_xref_lookup(raw_detections)

    # 13. Scrub combined keystroke sequences in events.jsonl
    with console.status("Scrubbing keystroke sequences..."):
        try:
            _scrub_events_jsonl(
                dst, pipeline, anonymizer, result, scrub_ctx.blocked_intervals,
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
        transcript_paths = sorted(dst.glob("transcript*.json")) + sorted(dst.glob("transcript*.txt"))
        scrub_transcripts(transcript_paths, pipeline, anonymizer, result=result)

    # 15. Scrub metrics (rule-based)
    metrics_path = dst / "system_metrics.json"
    rule_based_redactions = _scrub_metrics(metrics_path, result)

    # 16. Write audit log
    _write_audit_log(dst, result)

    # 17. Print summary
    _print_summary(result, rule_based_redactions)

    # 18. Return result
    return result
