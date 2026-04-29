"""Scan recordings directory and load metadata."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from screencap.config import get_recordings_dir
from screencap.recording_db import has_table, open_recording_db

INTENT_FILE = ".recording_intent"


def read_intent(directory: Path) -> str | None:
    """Read the recording intent from a .recording_intent file.

    Returns 'cloud', 'local', 'both', or None if the file is missing/corrupt.
    """
    intent_path = directory / INTENT_FILE
    if not intent_path.exists():
        return None
    try:
        data = json.loads(intent_path.read_text())
        return data.get("destination")
    except Exception:
        return None


class RecordingInfo(NamedTuple):
    name: str
    date: str  # YYYY-MM-DD
    duration: str  # e.g. "2m 34s"
    size_mb: str  # e.g. "48.3 MB"
    has_audio: bool
    transcribed: bool
    uploaded: bool
    drops: dict[str, int] | None = None  # event drop counts, if any
    is_stub: bool = False  # True if media files deleted after upload
    chunks_total: int = 0  # number of video chunks (0 = legacy single-file)
    chunks_uploaded: int = 0  # number of chunks with upload status files
    intent: str | None = None  # "cloud", "local", or None (legacy)
    # Raw values for SwiftUI consumers (Unit 4c). The pre-formatted ``date``
    # / ``duration`` strings remain for backward compatibility with anything
    # that reads the existing JSON; SwiftUI uses these unformatted fields
    # for HH:MM rendering, sorting, and date-bucket grouping.
    started_at: float | None = None  # Unix timestamp from the recording row
    duration_seconds: float | None = None  # raw seconds, source of `duration`


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
    """Find the recording.db in a recording directory."""
    p = directory / "recording.db"
    if p.exists():
        return p
    return None


def _read_recording_meta(db_path: Path) -> tuple[float | None, float | None]:
    """Read (started_timestamp, duration_seconds) from a recording.db."""
    try:
        with open_recording_db(db_path) as conn:
            started: float | None = None
            duration: float | None = None

            if has_table(conn, "recording"):
                row = conn.execute("SELECT timestamp FROM recording LIMIT 1").fetchone()
                started = float(row[0]) if row and row[0] else None
                if started and has_table(conn, "action_event"):
                    ev = conn.execute("SELECT MAX(timestamp) FROM action_event").fetchone()
                    if ev and ev[0] is not None:
                        duration = float(ev[0]) - started

            return started, duration
    except Exception:
        return None, None


def get_seen_bundle_ids(directories: list[Path] | None = None) -> set[str]:
    """Collect distinct app_bundle_id values from recording DBs.

    Args:
        directories: Specific recording dirs to scan. If None, scans all
            recordings in the configured recordings dir.

    Returns:
        Set of bundle ID strings seen across all scanned recordings.
    """
    if directories is None:
        rec_dir = get_recordings_dir()
        if not rec_dir.exists():
            return set()
        directories = [d for d in rec_dir.iterdir() if d.is_dir()]

    seen: set[str] = set()
    for d in directories:
        db_path = find_db(d)
        if db_path is None:
            continue
        try:
            with open_recording_db(db_path) as conn:
                if has_table(conn, "window_event"):
                    rows = conn.execute(
                        "SELECT DISTINCT app_bundle_id FROM window_event "
                        "WHERE app_bundle_id IS NOT NULL AND app_bundle_id != ''"
                    ).fetchall()
                    seen.update(r[0] for r in rows)
        except Exception:
            continue
    return seen


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

        started, duration = _read_recording_meta(db)
        date_str = "—"
        if started:
            date_str = datetime.fromtimestamp(started).strftime("%Y-%m-%d")

        has_audio = (d / "audio.flac").exists() or any(d.glob("audio_*.flac"))
        transcribed = (d / "transcript.txt").exists() or any(d.glob("transcript_*.txt"))
        uploaded_legacy = (d / ".upload_status.json").is_file()

        # Chunk detection — count from local files, status files, and manifests
        chunk_videos = sorted(d.glob("chunk_*.mp4"))
        chunk_status_files = list(d.glob(".chunk_*_status.json"))
        chunk_manifests = sorted(d.glob("chunk_*_manifest.json"))
        chunks_uploaded = len(chunk_status_files)
        # chunks_total: use max of local videos, manifests, and uploaded count
        # (local files may be deleted after upload)
        chunks_total = max(len(chunk_videos), len(chunk_manifests), chunks_uploaded)

        uploaded = uploaded_legacy or chunks_uploaded > 0

        # For stubbed recordings, check chunk status files for audio evidence
        if not has_audio and chunks_uploaded > 0:
            for sf in chunk_status_files:
                try:
                    data = json.loads(sf.read_text())
                    if any("audio_" in f for f in data.get("files", [])):
                        has_audio = True
                        break
                except Exception:
                    pass

        # Stub detection: DB exists but no media files on disk
        has_media = (
            any(d.glob("*.mp4")) or any(d.glob("*.flac"))
            or (d / "video.mp4").exists()
        )
        is_stub = uploaded and not has_media and db is not None

        drops = read_drops(d)
        intent = read_intent(d)

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
                is_stub=is_stub,
                chunks_total=chunks_total,
                chunks_uploaded=chunks_uploaded,
                intent=intent,
                started_at=started,
                duration_seconds=duration,
            )
        )

    return results
