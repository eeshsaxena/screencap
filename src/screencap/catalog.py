"""Scan recordings directory and load metadata."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from screencap.config import get_recordings_dir

DB_NAMES = ("recording.db", "capture.db")


class RecordingInfo(NamedTuple):
    name: str
    date: str  # YYYY-MM-DD
    duration: str  # e.g. "2m 34s"
    size_mb: str  # e.g. "48.3 MB"
    has_audio: bool
    transcribed: bool
    uploaded: bool
    drops: dict[str, int] | None = None  # event drop counts, if any


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "—"
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def _dir_size_mb(p: Path) -> str:
    total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    if total < 1024 * 1024:
        return f"{total / 1024:.1f} KB"
    return f"{total / (1024 * 1024):.1f} MB"


def read_drops(directory: Path) -> dict[str, int] | None:
    """Read event drop counts from profiling.json, if present."""
    profiling = directory / "profiling.json"
    if not profiling.exists():
        return None
    try:
        data = json.loads(profiling.read_text())
        drops = data.get("drops")
        if isinstance(drops, dict) and any(v > 0 for v in drops.values()):
            return drops
    except Exception:
        pass
    return None


def find_db(directory: Path) -> Path | None:
    """Find the SQLite DB in a recording directory (recording.db or capture.db)."""
    for name in DB_NAMES:
        p = directory / name
        if p.exists():
            return p
    return None


def _read_recording_meta(db_path: Path) -> tuple[float | None, float | None]:
    """Read (started_timestamp, duration_seconds) from the capture DB.

    Supports two schemas:
      - recording.db: table=recording (timestamp), events in action_event
      - capture.db:   table=capture  (started_at, ended_at), events in events
    """
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        # Detect schema by checking which tables exist
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}

        started = None
        duration = None

        if "capture" in tables:
            # capture.db schema (started_at / ended_at)
            cur.execute("SELECT started_at, ended_at FROM capture LIMIT 1")
            row = cur.fetchone()
            if row:
                started = float(row[0]) if row[0] else None
                if started and row[1]:
                    duration = float(row[1]) - started
                elif started and "events" in tables:
                    cur.execute("SELECT MAX(timestamp) FROM events")
                    ev = cur.fetchone()
                    if ev and ev[0] is not None:
                        duration = float(ev[0]) - started

        elif "recording" in tables:
            # recording.db schema (timestamp)
            cur.execute("SELECT timestamp FROM recording LIMIT 1")
            row = cur.fetchone()
            started = float(row[0]) if row and row[0] else None
            if started and "action_event" in tables:
                cur.execute("SELECT MAX(timestamp) FROM action_event")
                ev = cur.fetchone()
                if ev and ev[0] is not None:
                    duration = float(ev[0]) - started

        conn.close()
        return started, duration
    except Exception:
        return None, None


def list_recordings(recordings_dir: Path | None = None) -> list[RecordingInfo]:
    """Scan recordings directory and return metadata for each."""
    if recordings_dir is None:
        recordings_dir = get_recordings_dir()

    results = []
    if not recordings_dir.exists():
        return results

    for d in sorted(recordings_dir.iterdir()):
        if not d.is_dir():
            continue
        db = find_db(d)
        if db is None:
            continue
        # Skip scrubbed copies
        if d.name.endswith("-scrubbed"):
            continue

        started, duration = _read_recording_meta(db)
        date_str = "—"
        if started:
            date_str = datetime.fromtimestamp(started).strftime("%Y-%m-%d")

        has_audio = (d / "audio.flac").exists()
        transcribed = (d / "transcript.txt").exists()
        uploaded = (d / ".upload_status.json").is_file()

        drops = read_drops(d)

        results.append(
            RecordingInfo(
                name=d.name,
                date=date_str,
                duration=_fmt_duration(duration),
                size_mb=_dir_size_mb(d),
                has_audio=has_audio,
                transcribed=transcribed,
                uploaded=uploaded,
                drops=drops,
            )
        )

    return results
