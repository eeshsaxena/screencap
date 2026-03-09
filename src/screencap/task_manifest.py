"""Generate per-chunk task manifests from recording data.

Detects resting periods (gaps > threshold between actions) and groups
activity into tasks with dominant app identification.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Window title parsers — extract meaningful name from window title
TITLE_PARSERS: dict[str, str] = {
    "Visual Studio Code": r"(.+?) — (.+?) —",
    "Code": r"(.+?) — (.+?) —",
    "Google Chrome": r"(.+?) - Google Chrome",
    "Safari": r"(.+?) — (?:.*)",
    "Firefox": r"(.+?) — Mozilla Firefox",
    "Slack": r"(.+?) - (.+?) -",
    "Terminal": r"\w+@\w+: (.+)",
    "iTerm2": r"(.+?) — (.+)",
    "Finder": r"(.+)",
}


def generate_manifest(
    capture_dir: Path,
    chunk_idx: int,
    start_ts: float,
    end_ts: float,
    *,
    rest_threshold: float = 120.0,
    blocked_intervals: list[dict] | None = None,
) -> Path:
    """Generate a task manifest JSON for one chunk.

    Args:
        capture_dir: Recording directory containing recording.db.
        chunk_idx: Zero-based chunk index.
        start_ts: Chunk start timestamp (Unix epoch).
        end_ts: Chunk end timestamp (Unix epoch).
        rest_threshold: Seconds of inactivity to split tasks.
        blocked_intervals: Privacy-blocked video intervals for this chunk.

    Returns:
        Path to the manifest file.
    """
    manifest_path = capture_dir / f"chunk_{chunk_idx:04d}_manifest.json"
    if manifest_path.exists():
        return manifest_path

    db_path = capture_dir / "recording.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row

    try:
        # Get explicit events (clicks, keys, scrolls — not moves)
        events = conn.execute(
            """SELECT timestamp, name FROM action_event
               WHERE timestamp >= ? AND timestamp < ?
                 AND name != 'move'
               ORDER BY timestamp""",
            (start_ts, end_ts),
        ).fetchall()

        # Get window events for dominant app detection
        window_events = conn.execute(
            """SELECT timestamp, title, app_bundle_id, window_id
               FROM window_event
               WHERE timestamp >= ? AND timestamp < ?
               ORDER BY timestamp""",
            (start_ts, end_ts),
        ).fetchall()

        # Also get the last window event before this chunk (for context)
        prev_window = conn.execute(
            """SELECT timestamp, title, app_bundle_id, window_id
               FROM window_event
               WHERE timestamp < ?
               ORDER BY timestamp DESC LIMIT 1""",
            (start_ts,),
        ).fetchone()
    finally:
        conn.close()

    # Build tasks from events
    tasks = _segment_tasks(events, rest_threshold)

    # Compute dominant app for each task
    all_windows = list(window_events)
    if prev_window:
        all_windows.insert(0, prev_window)

    task_list = []
    for task_start, task_end, event_count in tasks:
        dom = _compute_dominant_app(all_windows, task_start, task_end)
        duration = task_end - task_start
        task_list.append({
            "start_ts": task_start,
            "end_ts": task_end,
            "duration_human": _fmt_duration(duration),
            "event_count": event_count,
            "dominant_app": dom.get("bundle_id", ""),
            "dominant_app_name": _app_name_from_bundle(dom.get("bundle_id", "")),
            "dominant_title": dom.get("title", ""),
            "dominant_pct": round(dom.get("pct", 0), 1),
            "all_apps": dom.get("all_apps", {}),
            "derived_name": _derive_task_name(dom),
        })

    # Compute rest_after for each task
    for i in range(len(task_list) - 1):
        task_list[i]["rest_after_s"] = round(
            task_list[i + 1]["start_ts"] - task_list[i]["end_ts"], 1,
        )
    if task_list:
        task_list[-1]["rest_after_s"] = 0

    total_active = sum(t["end_ts"] - t["start_ts"] for t in task_list)
    primary = max(task_list, key=lambda t: t["end_ts"] - t["start_ts"])["derived_name"] if task_list else ""

    manifest = {
        "chunk_index": chunk_idx,
        "chunk_start": start_ts,
        "chunk_end": end_ts,
        "rest_threshold_secs": rest_threshold,
        "tasks": task_list,
        "summary": {
            "total_tasks": len(task_list),
            "total_active_s": round(total_active, 1),
            "total_events": sum(t["event_count"] for t in task_list),
            "primary_task": primary,
            "idle": len(task_list) == 0,
        },
    }

    if blocked_intervals:
        manifest["blocked_intervals"] = blocked_intervals

    manifest_path.write_text(json.dumps(manifest, indent=2))
    logger.info(f"Generated manifest: {manifest_path.name} ({len(task_list)} tasks)")
    return manifest_path


def _segment_tasks(
    events: list, rest_threshold: float,
) -> list[tuple[float, float, int]]:
    """Split events into tasks based on inactivity gaps.

    Returns list of (start_ts, end_ts, event_count) tuples.
    """
    if not events:
        return []

    tasks = []
    task_start = events[0]["timestamp"]
    task_end = task_start
    count = 1

    for i in range(1, len(events)):
        ts = events[i]["timestamp"]
        gap = ts - task_end
        if gap > rest_threshold:
            tasks.append((task_start, task_end, count))
            task_start = ts
            count = 0
        task_end = ts
        count += 1

    tasks.append((task_start, task_end, count))
    return tasks


def _compute_dominant_app(
    window_events: list, task_start: float, task_end: float,
) -> dict:
    """Compute dominant app by wall-clock time for a task interval."""
    if not window_events:
        return {"bundle_id": "", "title": "", "pct": 0, "all_apps": {}}

    # Filter windows relevant to this task
    relevant = []
    for i, w in enumerate(window_events):
        if w["timestamp"] > task_end:
            break
        next_ts = window_events[i + 1]["timestamp"] if i + 1 < len(window_events) else task_end
        # Clip to task boundaries
        win_start = max(w["timestamp"], task_start)
        win_end = min(next_ts, task_end)
        if win_end > win_start:
            relevant.append({
                "bundle_id": w["app_bundle_id"] or "",
                "title": w["title"] or "",
                "duration": win_end - win_start,
            })

    if not relevant:
        # Use last window event before task
        for w in reversed(window_events):
            if w["timestamp"] <= task_start:
                return {
                    "bundle_id": w["app_bundle_id"] or "",
                    "title": w["title"] or "",
                    "pct": 100.0,
                    "all_apps": {w["app_bundle_id"] or "unknown": round(task_end - task_start, 1)},
                }
        return {"bundle_id": "", "title": "", "pct": 0, "all_apps": {}}

    # Sum time per app
    app_time: dict[str, float] = {}
    app_title: dict[str, str] = {}  # keep last title per app
    for r in relevant:
        bid = r["bundle_id"]
        app_time[bid] = app_time.get(bid, 0) + r["duration"]
        app_title[bid] = r["title"]

    total = sum(app_time.values())
    dominant_bid = max(app_time, key=app_time.get)

    return {
        "bundle_id": dominant_bid,
        "title": app_title.get(dominant_bid, ""),
        "pct": (app_time[dominant_bid] / total * 100) if total > 0 else 0,
        "all_apps": {k: round(v, 1) for k, v in app_time.items()},
    }


def _app_name_from_bundle(bundle_id: str) -> str:
    """Extract human-friendly app name from bundle ID."""
    if not bundle_id:
        return ""
    # com.apple.Safari → Safari
    # com.google.Chrome → Chrome
    parts = bundle_id.split(".")
    if len(parts) >= 3:
        return parts[-1]
    return bundle_id


def _derive_task_name(dom: dict) -> str:
    """Derive a short task name from dominant app + window title."""
    app_name = _app_name_from_bundle(dom.get("bundle_id", ""))
    title = dom.get("title", "")

    if not app_name and not title:
        return "unknown"

    # Try title parsers
    for parser_app, pattern in TITLE_PARSERS.items():
        if parser_app.lower() in app_name.lower():
            m = re.search(pattern, title)
            if m:
                extracted = m.group(1).strip()
                slug = _slugify(extracted)
                return f"{_slugify(app_name)}_{slug}"

    # Fallback: app + first 30 chars of title
    if title:
        slug = _slugify(title[:30])
        return f"{_slugify(app_name)}_{slug}" if app_name else slug

    return _slugify(app_name)


def _slugify(s: str) -> str:
    """Convert string to a filesystem-safe slug."""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = s.strip("-")
    return s[:40] or "untitled"


def _fmt_duration(secs: float) -> str:
    """Format seconds as human-readable duration."""
    if secs < 60:
        return f"{secs:.0f}s"
    m = int(secs // 60)
    s = int(secs % 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m:02d}m {s:02d}s"
