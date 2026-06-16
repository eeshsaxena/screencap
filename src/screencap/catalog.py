"""Scan recordings directory and load metadata."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from screencap.config import get_recordings_dir
from screencap.recording_db import has_table, open_recording_db

INTENT_FILE = ".recording_intent"

# SCR-116: the cloud account uid that OWNS a recording, pinned at start. A
# dotfile, so ``upload.list_recording_files`` (which skips ``.``-prefixed names)
# never uploads it — this is local-only ownership metadata.
OWNER_UID_FILE = ".cloud_owner_uid"


def write_owner_uid(directory: Path, uid: str) -> None:
    """Pin the cloud account uid that owns this recording (SCR-116).

    Written once by the daemon at token-staging time — the daemon is the only
    component that can read the Keychain / mint the token, so it is the only one
    that knows the uid. The terminal stage reads it back to refuse converging a
    recording into a *different* account's namespace after a mid-lifecycle
    account switch (``screencap logout && login`` as another user).
    """
    (directory / OWNER_UID_FILE).write_text(uid)


def read_owner_uid(directory: Path) -> str | None:
    """Read the pinned owner uid, or ``None`` when absent.

    ``None`` for a legacy recording (predating the pin) or a local recording
    (never tied to an account) — callers treat that as "ownership unknown" and
    fall back to their existing behavior rather than refusing.
    """
    try:
        uid = (directory / OWNER_UID_FILE).read_text().strip()
    except OSError:
        return None
    return uid or None


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


def read_masked_video_upload(directory: Path) -> bool | None:
    """Read the FROZEN ``masked_video_upload`` decision from ``.recording_intent``.

    The masked-video-upload flag is resolved ONCE at recording start and frozen
    into ``.recording_intent`` (``engine/lock_policy._write_identity_files``), so
    a mid-recording flip of the mutable global cannot make capture-time blocking
    and the live/terminal upload-time masking disagree (the rich-video leak
    window — SCR-125 R-SCR125-A). Both the live ``chunk_processor`` upload and the
    ``terminal_stage`` masking read THIS frozen value, never the global.

    Returns the frozen bool, or ``None`` when the file is missing/corrupt or the
    field is absent (a legacy intent written before SCR-125). The caller falls
    back to the mutable global only for those legacy recordings — see
    ``pipeline_chunk_ops.get_frozen_masked_video_upload``.
    """
    intent_path = directory / INTENT_FILE
    if not intent_path.exists():
        return None
    try:
        data = json.loads(intent_path.read_text())
    except Exception:
        return None
    val = data.get("masked_video_upload")
    return bool(val) if isinstance(val, bool) else None


def read_intent_policy(directory: Path):
    """Read the frozen :class:`~screencap.pipeline_policy.ResolvedPolicy`.

    Returns the policy frozen into ``.recording_intent`` at routing time
    (U3), or ``None`` when the file is missing/corrupt OR is a legacy
    ``version: 1`` intent written before the retention fields existed
    (sparse-but-valid — the caller should fall back to a config default or
    treat retention as keep-forever). The frozen value is authoritative: it
    is NOT re-resolved against current config, so a config change after the
    recording was routed does not change this recording's policy.
    """
    intent_path = directory / INTENT_FILE
    if not intent_path.exists():
        return None
    try:
        data = json.loads(intent_path.read_text())
    except Exception:
        return None
    from screencap.pipeline_policy import ResolvedPolicy

    return ResolvedPolicy.from_dict(data)


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
    # R14 (U9): True for a chunked recording (the unified pipeline's canonical
    # on-disk shape going forward), False for a legacy single-file recording.
    # Survives stubbing — derived from chunk videos, manifests, AND legacy
    # status files, so an uploaded-and-evicted chunked recording (no local
    # chunk_*.mp4 left) is still flagged chunked. Consumers (review/viewer,
    # SwiftUI) read this to choose the chunked vs. legacy single-file path.
    is_chunked: bool = False
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


def _ledger_has_uploaded_chunk(db_path: Path) -> bool | None:
    """True iff the U1 pipeline ledger records ≥1 confirmed-uploaded chunk.

    READ-ONLY probe of the ``pipeline_chunk_state`` table: ``upload_state ==
    'uploaded'`` (an ``EVICTED`` chunk keeps its ``UPLOADED`` upload_state per
    the U1 contract, so an uploaded-then-evicted recording still reads True).

    Returns ``None`` when there is no ledger to consult — the table does not
    exist (legacy / not-yet-seeded recording) or the DB is unreadable — so the
    caller falls back to the file-presence heuristics (R14). Deliberately does
    NOT migrate the schema: catalog listing is read-only and must never ALTER a
    recording.db (that would change its content hash and defeat the
    scrubbed-copy reuse check on the upload path, mirroring
    ``terminal_stage._open_ledger_readonly``).
    """
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        has_ledger = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='pipeline_chunk_state'"
        ).fetchone()
        if has_ledger is None:
            return None  # no ledger — fall back to file-presence heuristics.
        return (
            conn.execute(
                "SELECT 1 FROM pipeline_chunk_state "
                "WHERE upload_state='uploaded' LIMIT 1"
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        return None
    finally:
        conn.close()


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
        # R14 (U9): chunked vs. legacy single-file. Derived from the same
        # eviction-surviving evidence as chunks_total, so a stubbed chunked
        # recording (no chunk_*.mp4 left, but manifests/status files remain)
        # is still flagged chunked. A legacy single-file recording has none of
        # these -> is_chunked False and stays listable/readable via video.mp4.
        is_chunked = chunks_total > 0

        # Consult the U1 ledger (read-only) when present: the unified pipeline
        # uploads from the source dir and records confirmed uploads in
        # `pipeline_chunk_state`, so a chunked recording can be genuinely
        # uploaded (and later evicted) WITHOUT the legacy `.chunk_*_status.json`
        # markers the file heuristics key off. `None` = no ledger (legacy / not
        # seeded) -> fall back to file-presence only (R14).
        ledger_uploaded = bool(
            db is not None and _ledger_has_uploaded_chunk(db)
        )

        uploaded = uploaded_legacy or chunks_uploaded > 0 or ledger_uploaded

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

        # Stub detection: DB exists but no media files on disk. Hidden mp4/flac
        # are ignored — pathlib glob matches dotfiles, and a lingering review
        # artifact (.video_review.mp4) must never mask a stub (R6). A real
        # video.mp4 is non-hidden, so the *.mp4 glob already covers it.
        #
        # Derived from CHUNKS (R2), never from a single `video.mp4` being the
        # record: the `*.mp4` glob counts surviving `chunk_*.mp4` as media, so a
        # chunked recording with chunks still on disk is correctly NOT a stub
        # even though it has no merged `video.mp4` (that is a derived on-demand
        # artifact — see `viewer._ensure_single_video`).
        #
        # Ledger-aware (U10): an uploaded chunked recording whose media was
        # evicted post-upload-confirm is a LEGITIMATE stub. Folding
        # `ledger_uploaded` into `uploaded` above makes the unified pipeline's
        # uploaded-then-evicted recording read as uploaded even without legacy
        # `.chunk_*_status.json` markers, so the `uploaded and not has_media`
        # rule below classifies it correctly. The ledger NEVER manufactures a
        # FALSE stub: a FAILED chunk (fail-closed) is never UPLOADED so it cannot
        # set `ledger_uploaded`, and a locally-evicted recording (LOCAL_DONE ->
        # EVICTED, never uploaded) keeps `uploaded == False` — neither is a stub.
        has_media = (
            any(not p.name.startswith(".") for p in d.glob("*.mp4"))
            or any(not p.name.startswith(".") for p in d.glob("*.flac"))
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
                is_chunked=is_chunked,
                started_at=started,
                duration_seconds=duration,
            )
        )

    return results
