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

console = Console()

# Files to skip during copytree and delete as safety fallback.
_SKIP_FILES = {"audio.flac", "events.jsonl", ".upload_status.json", "viewer.html"}
_SKIP_EXTENSIONS = {".mp4"}


@dataclass
class ScrubResult:
    """Summary of a scrub operation."""

    output_dir: Path = field(default_factory=Path)
    entity_counts: Counter = field(default_factory=Counter)
    deleted_files: list[str] = field(default_factory=list)


def _copytree_ignore(directory: str, entries: list[str]) -> set[str]:
    """Ignore callback for shutil.copytree — skip media and derived files."""
    ignored = set()
    for entry in entries:
        if entry in _SKIP_FILES or Path(entry).suffix in _SKIP_EXTENSIONS:
            ignored.add(entry)
    return ignored


def _scrub_text(
    text: str,
    pipeline,
    anonymizer,
    result: ScrubResult,
) -> str:
    """Run text through the detection pipeline and anonymize.

    On AllDetectorsFailedError, returns '<SCRUB_FAILED>' and prints a warning.
    """
    if not text or not text.strip():
        return text

    # Deferred import — only needed here.
    from screencap.privacy import AllDetectorsFailedError

    try:
        detection_result = pipeline.detect(text)
    except AllDetectorsFailedError:
        console.print("  [yellow]Warning: all detectors failed on a field[/]")
        return "<SCRUB_FAILED>"

    scrubbed = anonymizer.anonymize(
        detection_result.normalized_text,
        detection_result.detections,
    )

    for det in detection_result.detections:
        result.entity_counts[det.entity_type] += 1

    return scrubbed


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
            obj[key] = _scrub_text(value, pipeline, anonymizer, result)
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
        scrubbed = _scrub_text(text, pipeline, anonymizer, result)
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
            data["text"] = _scrub_text(data["text"], pipeline, anonymizer, result)

        if "segments" in data and isinstance(data["segments"], list):
            for seg in data["segments"]:
                if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                    seg["text"] = _scrub_text(seg["text"], pipeline, anonymizer, result)

        if "words" in data and isinstance(data["words"], list):
            for word_entry in data["words"]:
                if isinstance(word_entry, dict) and isinstance(
                    word_entry.get("word"), str
                ):
                    word_entry["word"] = _scrub_text(
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
        scrubbed = _scrub_text(text, pipeline, anonymizer, result)
        path.write_text(scrubbed, encoding="utf-8")
    except OSError as e:
        console.print(f"  [yellow]Warning: could not scrub transcript.txt: {e}[/]")


# ---------------------------------------------------------------------------
# System metrics scrubbing (rule-based)
# ---------------------------------------------------------------------------


def _scrub_metrics(path: Path, result: ScrubResult) -> None:
    """Redact hostname and WiFi identifiers from system_metrics.json."""
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

    except (json.JSONDecodeError, OSError) as e:
        console.print(
            f"  [yellow]Warning: could not scrub system_metrics.json: {e}[/]"
        )


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------


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

    # 3. Validate deps — create pipeline BEFORE any filesystem mutation
    with console.status("Loading privacy detection engine..."):
        pipeline = create_default_pipeline(pii_engine=pii_engine)

    # 4. Create anonymizer
    anonymizer = Anonymizer()

    result = ScrubResult()

    # 5. Handle existing scrubbed dir
    recordings_dir = get_recordings_dir()
    dst = recordings_dir / f"{name}-scrubbed"
    if dst.exists():
        console.print(
            f"  [yellow]Warning: {dst.name}/ already exists — replacing[/]"
        )
        shutil.rmtree(dst)

    # 6. Copy (skipping media/derived files)
    with console.status(f"Copying {name} → {name}-scrubbed ..."):
        shutil.copytree(src, dst, symlinks=True, ignore=_copytree_ignore)

    result.output_dir = dst

    # 7. Safety fallback deletion — remove any files that survived the ignore callback
    deleted_files: list[str] = []
    for pattern_or_name in ("audio.flac", "events.jsonl", ".upload_status.json", "viewer.html"):
        p = dst / pattern_or_name
        if p.exists():
            p.unlink()
            deleted_files.append(pattern_or_name)
    for mp4 in dst.glob("*.mp4"):
        mp4.unlink()
        deleted_files.append(mp4.name)

    # Track which files were skipped/deleted (union of ignore + fallback)
    all_skipped = set()
    # Check what was in the source but not in dest
    for f in src.iterdir():
        if f.name in _SKIP_FILES or f.suffix in _SKIP_EXTENSIONS:
            all_skipped.add(f.name)
    for name_del in deleted_files:
        all_skipped.add(name_del)
    result.deleted_files = sorted(all_skipped)

    # 8. Scrub DB
    with console.status("Scrubbing database..."):
        _scrub_db(dst, pipeline, anonymizer, result)

    # 9-10. Scrub transcripts
    with console.status("Scrubbing transcripts..."):
        _scrub_transcript_json(dst / "transcript.json", pipeline, anonymizer, result)
        _scrub_transcript_txt(dst / "transcript.txt", pipeline, anonymizer, result)

    # 11. Scrub metrics (rule-based)
    rule_based_redactions: list[str] = []
    metrics_path = dst / "system_metrics.json"
    if metrics_path.exists():
        try:
            data = json.loads(metrics_path.read_text(encoding="utf-8"))
            static = data.get("static", {})
            if "hostname" in static:
                rule_based_redactions.append("hostname")
            wifi = static.get("wifi", {})
            if "ssid" in wifi:
                rule_based_redactions.append("wifi.ssid")
            if "bssid" in wifi:
                rule_based_redactions.append("wifi.bssid")
        except (json.JSONDecodeError, OSError):
            pass
    _scrub_metrics(metrics_path, result)

    # 12. Print summary
    _print_summary(result, rule_based_redactions)

    # 13. Return result
    return result
