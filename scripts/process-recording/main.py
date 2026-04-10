"""Cloud Run service: combine chunked recordings into task-segmented sessions.

Triggered by Eventarc when recording.db lands on GCS. Reads per-chunk manifests,
merges cross-chunk tasks, combines video/audio per task via ffmpeg, slices events
& transcripts, and writes everything to sessions/{name}/.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import functions_framework
from google.cloud import storage

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

BUCKET = os.environ.get("SCREENCAP_BUCKET", "screencap-recordings")
PROCESSOR_VERSION = "2.1.0"
MAX_ACTIVITY_ENTRIES = 200  # cap activity timeline entries for LLM context
DEFAULT_REST_THRESHOLD = 120.0
_LLM_ENRICHED_FIELDS = ("name", "description", "category", "apps_used", "confidence")
MAX_MERGE_GAP = 5.0  # max seconds between consecutive chunk boundaries
SLUG_MAX = 60

_CATEGORY_PREFIX = {
    "development": "dev",
    "communication": "com",
    "research": "res",
    "admin": "adm",
    "creative": "cre",
    "other": "oth",
}

# Map _classify_app() output → task category for idle-gap/v1 fallback
_APP_CAT_TO_TASK_CAT = {
    "CODE": "development",
    "BROWSER": "research",
    "CHAT": "communication",
    "EMAIL": "communication",
    "DOCS": "creative",
    "DESIGN": "creative",
    "MEDIA": "other",
    "SYSTEM": "admin",
    "SOCIAL": "other",
    "OTHER": "other",
}

_storage_client: storage.Client | None = None


def _client() -> storage.Client:
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client()
    return _storage_client


def _bucket() -> storage.Bucket:
    return _client().bucket(BUCKET)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 40) -> str:
    s = text.lower()
    # Strip PII placeholder tags like <PERSON>, <EMAIL>, etc.
    s = re.sub(r"<[A-Z_]+>", "", s)
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return (s or "untitled")[:max_len]


def _clean_title(title: str) -> str:
    """Clean a window title for display: strip PII tags, version suffixes, noise."""
    if not title:
        return ""
    # Remove PII placeholder tags
    title = re.sub(r"\s*<[A-Z_]+>\s*", " ", title).strip()
    # Remove common noise suffixes
    for suffix in (" — Ghostty", " - Ghostty", " — Terminal", " - Terminal"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    # Remove version strings like "v0.11.0", "v1.2.0"
    title = re.sub(r"\s*v\d+\.\d+\.\d+\s*", " ", title).strip()
    return title


def _derive_display_name(
    recording_name: str,
    tasks: list[dict],
    summary: dict | None,
    segmentation_method: str,
) -> str:
    """Generate a human-readable display name for the recording session.

    Priority:
    1. LLM summary overview → extract key theme
    2. Primary task name (if LLM-named)
    3. Dominant app + activity description
    4. Formatted timestamp from recording name
    """
    # If LLM provided a summary, derive from key_accomplishments or primary_focus
    if segmentation_method == "llm" and summary:
        accomplishments = summary.get("key_accomplishments", [])
        if accomplishments and accomplishments[0]:
            name = accomplishments[0].strip()
            if len(name) > 50:
                name = name[:47] + "..."
            return name
        # Use first task name if LLM-generated
        for t in tasks:
            if t.get("name"):
                return t["name"]

    # Fallback: build from dominant app/category info
    if tasks:
        # Collect unique app names
        apps = []
        for t in tasks:
            app_name = t.get("dominant_app_name", "")
            if app_name and app_name not in apps:
                apps.append(app_name)

        # Collect unique categories
        cats = []
        for t in tasks:
            cat = t.get("category", "other")
            if cat not in cats:
                cats.append(cat)

        _CAT_ACTIVITY = {
            "development": "Development",
            "research": "Research & Browsing",
            "communication": "Communication",
            "admin": "System Administration",
            "creative": "Creative Work",
            "other": "Session",
        }

        primary_cat = cats[0] if cats else "other"
        activity = _CAT_ACTIVITY.get(primary_cat, "Session")

        if apps:
            top_apps = apps[:2]
            return f"{activity} in {', '.join(top_apps)}"
        return activity

    # Last resort: format the recording name timestamp
    m = re.match(r"rec-(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})", recording_name)
    if m:
        y, mo, d, h, mi, s = m.groups()
        return f"Session {y}-{mo}-{d} {h}:{mi}"
    return recording_name


def _duration_human(secs: float) -> str:
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _download_blob(blob_name: str, dest: Path) -> bool:
    """Download a GCS object to a local path. Returns True on success."""
    try:
        blob = _bucket().blob(blob_name)
        blob.download_to_filename(str(dest))
        return True
    except Exception:
        log.warning("Failed to download %s", blob_name, exc_info=True)
        return False


def _upload_blob(blob_name: str, src: Path, content_type: str = "application/octet-stream") -> None:
    blob = _bucket().blob(blob_name)
    blob.upload_from_filename(str(src), content_type=content_type)


def _upload_json(blob_name: str, obj: dict | list) -> None:
    blob = _bucket().blob(blob_name)
    blob.upload_from_string(
        json.dumps(obj, indent=2, ensure_ascii=False),
        content_type="application/json",
    )


def _upload_text(blob_name: str, text: str) -> None:
    blob = _bucket().blob(blob_name)
    blob.upload_from_string(text, content_type="text/plain; charset=utf-8")


def _blob_exists(blob_name: str) -> bool:
    return _bucket().blob(blob_name).exists()


def _blob_bytes(blob_name: str) -> bytes | None:
    try:
        return _bucket().blob(blob_name).download_as_bytes()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

def _list_manifests(recording_name: str) -> list[str]:
    """List chunk_NNNN_manifest.json blobs sorted by chunk index."""
    prefix = f"recordings/{recording_name}/"
    blobs = _client().list_blobs(BUCKET, prefix=prefix)
    manifests = []
    for b in blobs:
        fname = b.name.split("/")[-1]
        if re.match(r"^chunk_\d{4}_manifest\.json$", fname):
            manifests.append(b.name)
    manifests.sort()
    return manifests


def _load_manifest(blob_name: str) -> dict | None:
    data = _blob_bytes(blob_name)
    if data is None:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        log.warning("Invalid JSON in manifest %s", blob_name)
        return None


# ---------------------------------------------------------------------------
# Cross-chunk task merging
# ---------------------------------------------------------------------------

def _merge_tasks(manifests: list[dict], rest_threshold: float) -> list[dict]:
    """Merge tasks across chunks. Returns list of MergedTask dicts."""
    merged: list[dict] = []
    current: dict | None = None

    for manifest in manifests:
        chunk_idx = manifest["chunk_index"]
        chunk_start = manifest["chunk_start"]
        chunk_end = manifest["chunk_end"]
        tasks = manifest.get("tasks", [])

        for i, task in enumerate(tasks):
            is_last_in_chunk = i == len(tasks) - 1
            entry = {
                "start_ts": task["start_ts"],
                "end_ts": task["end_ts"],
                "event_count": task["event_count"],
                "dominant_app": task["dominant_app"],
                "dominant_app_name": task.get("dominant_app_name", ""),
                "dominant_title": task.get("dominant_title", ""),
                "dominant_pct": task.get("dominant_pct", 0.0),
                "all_apps": dict(task.get("all_apps", {})),
                "derived_name": task.get("derived_name", "untitled"),
                "rest_after_s": task.get("rest_after_s", 0.0),
                "source_chunks": [{
                    "chunk_index": chunk_idx,
                    "chunk_start": chunk_start,
                    "chunk_end": chunk_end,
                    "start_ts": task["start_ts"],
                    "end_ts": task["end_ts"],
                    "start_offset_s": task["start_ts"] - chunk_start,
                    "end_offset_s": task["end_ts"] - chunk_start,
                }],
                "_is_last_in_chunk": is_last_in_chunk,
                "_chunk_end": chunk_end,
            }

            if current is None:
                current = entry
                continue

            # Check merge conditions
            should_merge = (
                current["_is_last_in_chunk"]
                and current["rest_after_s"] == 0
                and abs(current["_chunk_end"] - chunk_start) <= MAX_MERGE_GAP
                and (entry["start_ts"] - current["end_ts"]) < rest_threshold
            )

            if should_merge:
                # Extend current task
                current["end_ts"] = entry["end_ts"]
                current["event_count"] += entry["event_count"]
                # Union all_apps
                for app, dur in entry["all_apps"].items():
                    current["all_apps"][app] = current["all_apps"].get(app, 0) + dur
                current["source_chunks"].extend(entry["source_chunks"])
                current["rest_after_s"] = entry["rest_after_s"]
                current["_is_last_in_chunk"] = entry["_is_last_in_chunk"]
                current["_chunk_end"] = entry["_chunk_end"]
                # Recompute dominant app
                if current["all_apps"]:
                    dom = max(current["all_apps"], key=current["all_apps"].get)
                    total = sum(current["all_apps"].values())
                    current["dominant_app"] = dom
                    parts = dom.split(".")
                    current["dominant_app_name"] = parts[-1] if parts else dom
                    current["dominant_pct"] = round(
                        current["all_apps"][dom] / total * 100, 1
                    ) if total else 0.0
                # Recompute derived_name from new dominant
                app_slug = _slugify(current["dominant_app_name"])
                title_slug = _slugify(current.get("dominant_title", ""))
                current["derived_name"] = (
                    f"{app_slug}-{title_slug}" if title_slug and title_slug != "untitled"
                    else app_slug
                )
            else:
                # Finalize current, start new
                merged.append(current)
                current = entry

    if current is not None:
        merged.append(current)

    # Clean internal fields
    for t in merged:
        t.pop("_is_last_in_chunk", None)
        t.pop("_chunk_end", None)

    return merged


# ---------------------------------------------------------------------------
# Folder naming
# ---------------------------------------------------------------------------

def _assign_folder_names(tasks: list[dict]) -> list[str]:
    """Assign 000_slug folder names, deduplicating as needed."""
    seen: dict[str, int] = {}
    folders: list[str] = []
    for i, task in enumerate(tasks):
        slug = task["derived_name"][:SLUG_MAX]
        if slug in seen:
            seen[slug] += 1
            slug = f"{slug}-{seen[slug]}"
        else:
            seen[slug] = 0
        cat = task.get("category", "other")
        prefix = _CATEGORY_PREFIX.get(cat, "oth")
        folders.append(f"{prefix}_{i:03d}_{slug}")
    return folders


# ---------------------------------------------------------------------------
# Events slicing
# ---------------------------------------------------------------------------

def _slice_events(
    task: dict,
    recording_name: str,
    tmpdir: Path,
) -> tuple[str, int]:
    """Read events JSONL from source chunks, filter to task time range.

    Returns (jsonl_text, event_count).
    """
    lines: list[str] = []

    chunk_indices = {sc["chunk_index"] for sc in task["source_chunks"]}
    for idx in sorted(chunk_indices):
        blob_name = f"recordings/{recording_name}/events_{idx:04d}.jsonl"
        data = _blob_bytes(blob_name)
        if data is None:
            continue
        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = evt.get("timestamp") or evt.get("ts") or evt.get("time")
            if ts is None:
                lines.append(line)  # keep events without timestamp
                continue
            if task["start_ts"] <= ts < task["end_ts"]:
                lines.append(line)

    # Sort by timestamp
    def _sort_key(line: str) -> float:
        try:
            evt = json.loads(line)
            return evt.get("timestamp") or evt.get("ts") or evt.get("time") or 0
        except Exception:
            return 0

    lines.sort(key=_sort_key)
    return "\n".join(lines) + ("\n" if lines else ""), len(lines)


# ---------------------------------------------------------------------------
# Transcript slicing
# ---------------------------------------------------------------------------

def _slice_transcript(
    task: dict,
    recording_name: str,
) -> tuple[str, list[dict], bool]:
    """Slice transcript segments for this task from source chunks.

    Returns (plain_text, segments_list, has_transcript).
    """
    all_segments: list[dict] = []

    for sc in task["source_chunks"]:
        idx = sc["chunk_index"]
        chunk_start = sc["chunk_start"]

        # Try JSON transcript first
        blob_name = f"recordings/{recording_name}/transcript_{idx:04d}.json"
        data = _blob_bytes(blob_name)
        if data is None:
            continue

        try:
            transcript_data = json.loads(data)
        except json.JSONDecodeError:
            continue

        segments = transcript_data.get("segments", [])
        task_start_rel = task["start_ts"] - chunk_start
        task_end_rel = task["end_ts"] - chunk_start

        for seg in segments:
            seg_start = seg.get("start", 0)
            seg_end = seg.get("end", 0)
            if seg_end > task_start_rel and seg_start < task_end_rel:
                all_segments.append(seg)

    if not all_segments:
        return "", [], False

    plain = " ".join(s.get("text", "").strip() for s in all_segments).strip()
    return plain, all_segments, True


# ---------------------------------------------------------------------------
# Video / audio merging via ffmpeg
# ---------------------------------------------------------------------------

def _ffmpeg_extract(
    input_path: Path,
    output_path: Path,
    start_s: float,
    end_s: float,
) -> bool:
    """Extract a time range from a media file using stream copy."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_s:.3f}",
        "-to", f"{end_s:.3f}",
        "-i", str(input_path),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("ffmpeg extract failed: %s", e)
        return False


def _ffmpeg_concat(segment_paths: list[Path], output_path: Path) -> bool:
    """Concatenate media segments via ffmpeg concat demuxer."""
    list_path = output_path.parent / f"{output_path.stem}_concat.txt"
    with open(list_path, "w") as f:
        for p in segment_paths:
            f.write(f"file '{p}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("ffmpeg concat failed: %s", e)
        return False
    finally:
        list_path.unlink(missing_ok=True)


def _merge_media_for_task(
    task: dict,
    recording_name: str,
    tmpdir: Path,
    chunk_cache: dict[int, Path],
    media_type: str,  # "video" or "audio"
) -> Path | None:
    """Download chunk media, extract per-chunk segments, concat into one file.

    Returns path to merged file or None on failure.
    media_type: "video" → chunk_NNNN.mp4, "audio" → audio_NNNN.flac
    """
    if media_type == "video":
        pattern = "chunk_{idx:04d}.mp4"
        ext = ".mp4"
    else:
        pattern = "audio_{idx:04d}.flac"
        ext = ".flac"

    segments: list[Path] = []

    for sc in task["source_chunks"]:
        idx = sc["chunk_index"]
        chunk_start = sc["chunk_start"]

        # Download chunk if not cached
        if idx not in chunk_cache:
            fname = pattern.format(idx=idx)
            blob_name = f"recordings/{recording_name}/{fname}"
            if not _blob_exists(blob_name):
                log.info("Missing %s for chunk %d", media_type, idx)
                continue
            local = tmpdir / f"dl_{fname}"
            if not _download_blob(blob_name, local):
                continue
            chunk_cache[idx] = local

        chunk_path = chunk_cache.get(idx)
        if chunk_path is None:
            continue

        # Compute offsets within this chunk
        start_offset = max(0.0, sc["start_ts"] - chunk_start)
        end_offset = sc["end_ts"] - chunk_start

        # Extract segment
        seg_path = tmpdir / f"seg_{idx:04d}_{media_type}{ext}"
        if _ffmpeg_extract(chunk_path, seg_path, start_offset, end_offset):
            if seg_path.stat().st_size > 0:
                segments.append(seg_path)
            else:
                seg_path.unlink(missing_ok=True)

    if not segments:
        return None

    # Single segment → use directly
    if len(segments) == 1:
        return segments[0]

    # Multiple → concat
    merged = tmpdir / f"merged_{media_type}{ext}"
    if _ffmpeg_concat(segments, merged):
        # Clean up segments
        for p in segments:
            p.unlink(missing_ok=True)
        return merged

    # Concat failed, fall back to first segment
    return segments[0] if segments else None


# ---------------------------------------------------------------------------
# Process a single task
# ---------------------------------------------------------------------------

def _process_task(
    task: dict,
    folder: str,
    recording_name: str,
    tmpdir: Path,
    video_cache: dict[int, Path],
    audio_cache: dict[int, Path],
    sessions_prefix: str,
) -> dict:
    """Process one merged task: media merge, events, transcript. Upload to GCS.

    Returns task.json dict.
    """
    task_prefix = f"{sessions_prefix}tasks/{folder}/"

    # --- Video ---
    has_video = False
    video_size = 0
    video_path = _merge_media_for_task(
        task, recording_name, tmpdir, video_cache, "video",
    )
    if video_path and video_path.exists():
        video_size = video_path.stat().st_size
        _upload_blob(f"{task_prefix}task_video.mp4", video_path, "video/mp4")
        has_video = True
        video_path.unlink(missing_ok=True)

    # --- Audio ---
    has_audio = False
    audio_path = _merge_media_for_task(
        task, recording_name, tmpdir, audio_cache, "audio",
    )
    if audio_path and audio_path.exists():
        _upload_blob(f"{task_prefix}task_audio.flac", audio_path, "audio/flac")
        has_audio = True
        audio_path.unlink(missing_ok=True)

    # --- Events ---
    events_text, event_count = _slice_events(task, recording_name, tmpdir)
    has_events = event_count > 0
    if events_text:
        _upload_text(f"{task_prefix}events.jsonl", events_text)

    # --- Transcript ---
    plain, segments, has_transcript = _slice_transcript(task, recording_name)
    _upload_text(f"{task_prefix}transcript.txt", plain)
    if segments:
        _upload_json(f"{task_prefix}transcript_segments.json", segments)

    # --- task.json ---
    duration = task["end_ts"] - task["start_ts"]

    # Build source_chunks with offsets
    source_chunks_detail = []
    for sc in task["source_chunks"]:
        source_chunks_detail.append({
            "chunk_index": sc["chunk_index"],
            "start_ts": sc["start_ts"],
            "end_ts": sc["end_ts"],
            "start_offset_s": sc["start_offset_s"],
            "end_offset_s": sc["end_offset_s"],
        })

    task_meta = {
        "folder": folder,
        "start_ts": task["start_ts"],
        "end_ts": task["end_ts"],
        "duration_s": round(duration, 1),
        "duration_human": _duration_human(duration),
        "event_count": event_count,
        "dominant_app": task.get("dominant_app", ""),
        "dominant_app_name": task.get("dominant_app_name", ""),
        "dominant_title": task.get("dominant_title", ""),
        "dominant_pct": task.get("dominant_pct", 0.0),
        "derived_name": task.get("derived_name", "untitled"),
        "source_chunks": source_chunks_detail,
        "merged_across_chunks": len(task["source_chunks"]) > 1,
        "has_transcript": has_transcript,
        "has_video": has_video,
        "has_audio": has_audio,
        "has_events": has_events,
        "video_size_mb": round(video_size / (1024 * 1024), 1) if video_size else 0,
        "rest_after_s": task.get("rest_after_s", 0.0),
        "all_apps": task.get("all_apps", {}),
    }

    # LLM-enriched fields (present when segmentation_method == "llm")
    for llm_field in _LLM_ENRICHED_FIELDS:
        if task.get(llm_field):
            task_meta[llm_field] = task[llm_field]

    _upload_json(f"{task_prefix}task.json", task_meta)
    return task_meta


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def _check_idempotency(sessions_prefix: str, trigger_id: str) -> bool:
    """Return True if this recording was already processed with same trigger.

    ``trigger_id`` is either the MD5 of recording.db or the sentinel_id from
    recording_complete.json.
    """
    status_blob = f"{sessions_prefix}_processing_status.json"
    data = _blob_bytes(status_blob)
    if data is None:
        return False
    try:
        status = json.loads(data)
        existing_id = status.get("trigger_id") or status.get("source_db_md5")
        if existing_id != trigger_id:
            return False
        # "complete" blocks reprocessing; "complete_empty" does NOT (manifests may arrive later)
        return status.get("status") == "complete"
    except json.JSONDecodeError:
        return False


# ---------------------------------------------------------------------------
# Chunk cache management
# ---------------------------------------------------------------------------

def _cleanup_chunk_cache(
    cache: dict[int, Path],
    needed_indices: set[int],
) -> None:
    """Delete cached chunk files no longer needed by any remaining task."""
    to_remove = []
    for idx, path in cache.items():
        if idx not in needed_indices:
            path.unlink(missing_ok=True)
            to_remove.append(idx)
    for idx in to_remove:
        del cache[idx]


# ---------------------------------------------------------------------------
# App category classification (inline — Cloud Run doesn't have screencap pkg)
# ---------------------------------------------------------------------------

# Synced from screencap.privacy.context BUNDLE_ID_MAP + BROWSER_BUNDLE_IDS.
# ContextClass mapping: CODE_EDITOR_TERMINAL→CODE, EMAIL→EMAIL, CHAT→CHAT,
# CALENDAR→CHAT, VIDEO_CALL→CHAT, ADMIN_CONSOLE→CODE, BANKING→OTHER,
# PASSWORD_MANAGER→OTHER, browsers→BROWSER.
_BUNDLE_CATEGORY: dict[str, str] = {
    # CODE — editors, terminals, IDEs, DB tools
    "com.microsoft.VSCode": "CODE",
    "com.microsoft.VSCodeInsiders": "CODE",
    "com.apple.Terminal": "CODE",
    "com.googlecode.iterm2": "CODE",
    "co.zeit.hyper": "CODE",
    "dev.warp.Warp-Stable": "CODE",
    "com.mitchellh.ghostty": "CODE",
    "io.alacritty": "CODE",
    "net.kovidgoyal.kitty": "CODE",
    "com.github.nicegraphic.rio": "CODE",
    "com.jetbrains.intellij": "CODE",
    "com.jetbrains.pycharm": "CODE",
    "com.jetbrains.WebStorm": "CODE",
    "com.jetbrains.goland": "CODE",
    "com.jetbrains.CLion": "CODE",
    "com.jetbrains.rider": "CODE",
    "com.jetbrains.rubymine": "CODE",
    "com.jetbrains.datagrip": "CODE",
    "com.sublimetext.4": "CODE",
    "com.sublimetext.3": "CODE",
    "com.sublimehq.Sublime-Merge": "CODE",
    "com.todesktop.230313mzl4w4u92": "CODE",  # Cursor
    "dev.zed.Zed": "CODE",
    "com.github.atom": "CODE",
    "com.panic.Nova": "CODE",
    "com.barebones.bbedit": "CODE",
    "com.coteditor.CotEditor": "CODE",
    "com.macromates.TextMate": "CODE",
    "com.apple.dt.Xcode": "CODE",
    "com.neovide.neovide": "CODE",
    "org.gnu.Emacs": "CODE",
    "com.codeux.irc.textual5": "CODE",
    "abnerworks.Typora": "CODE",
    "com.github.Electron": "CODE",
    # CODE — admin/DB consoles
    "com.amazon.awsvpnclient": "CODE",
    "com.pgadmin.pgadmin4": "CODE",
    "com.sequel-pro.sequel-pro": "CODE",
    "com.tableplus.TablePlus": "CODE",
    # BROWSER
    "com.apple.Safari": "BROWSER",
    "com.google.Chrome": "BROWSER",
    "org.mozilla.firefox": "BROWSER",
    "com.brave.Browser": "BROWSER",
    "com.operasoftware.Opera": "BROWSER",
    "company.thebrowser.Browser": "BROWSER",  # Arc
    "org.chromium.Chromium": "BROWSER",
    "com.microsoft.edgemac": "BROWSER",
    "com.vivaldi.Vivaldi": "BROWSER",
    "com.nickvision.nicegx.nicegx": "BROWSER",  # Orion
    "org.waterfoxproject.waterfox": "BROWSER",
    "org.torproject.torbrowser": "BROWSER",
    # CHAT — messaging, video calls, calendar
    "com.tinyspeck.slackmacgap": "CHAT",
    "com.hnc.Discord": "CHAT",
    "com.facebook.archon": "CHAT",  # Messenger
    "ru.keepcoder.Telegram": "CHAT",
    "net.whatsapp.WhatsApp": "CHAT",
    "com.apple.MobileSMS": "CHAT",  # Messages
    "com.microsoft.teams2": "CHAT",
    "us.zoom.xos": "CHAT",
    "com.skype.skype": "CHAT",
    "org.whispersystems.signal-desktop": "CHAT",
    "jp.naver.line.mac": "CHAT",
    "com.viber.osx": "CHAT",
    "com.tencent.xinWeChat": "CHAT",
    "im.riot.app": "CHAT",  # Element
    "com.beeper.beeper": "CHAT",
    "com.cisco.webexmeetings": "CHAT",
    "com.google.chat": "CHAT",
    "com.mattermost.desktop": "CHAT",
    "com.wire.WireForOSX": "CHAT",
    # CHAT — video calls
    "us.zoom.xos.meeting": "CHAT",
    "com.google.meet": "CHAT",
    "com.cisco.webex.meetingmanager": "CHAT",
    "com.logmein.GoToMeeting": "CHAT",
    "com.apple.FaceTime": "CHAT",
    # CHAT — calendar
    "com.apple.iCal": "CHAT",
    "com.flexibits.fantastical2.mac": "CHAT",
    "com.flexibits.fantastical": "CHAT",
    "com.busymac.busycal3": "CHAT",
    # EMAIL
    "com.apple.mail": "EMAIL",
    "com.microsoft.Outlook": "EMAIL",
    "com.readdle.smartemail-macos": "EMAIL",
    "com.freron.MailMate": "EMAIL",
    "com.superhuman.electron": "EMAIL",
    "com.mimestream.Mimestream": "EMAIL",
    "it.bloop.airmail2": "EMAIL",
    "com.postbox-inc.postbox": "EMAIL",
    "com.canarymail.mac": "EMAIL",
    "org.mozilla.thunderbird": "EMAIL",
    # DOCS
    "com.apple.iWork.Pages": "DOCS",
    "com.apple.iWork.Numbers": "DOCS",
    "com.apple.iWork.Keynote": "DOCS",
    "com.microsoft.Word": "DOCS",
    "com.microsoft.Excel": "DOCS",
    "com.microsoft.Powerpoint": "DOCS",
    "md.obsidian": "DOCS",
    "com.craft.craft": "DOCS",
    "com.electron.logseq": "DOCS",
    # DESIGN
    "com.figma.Desktop": "DESIGN",
    "com.bohemiancoding.sketch3": "DESIGN",
    # MEDIA
    "com.apple.Music": "MEDIA",
    "com.spotify.client": "MEDIA",
    "com.apple.QuickTimePlayerX": "MEDIA",
    # SYSTEM
    "com.apple.finder": "SYSTEM",
    "com.apple.systempreferences": "SYSTEM",
    "com.apple.ActivityMonitor": "SYSTEM",
}

_DOMAIN_CATEGORY: dict[str, str] = {
    # CODE
    "github.com": "CODE",
    "gitlab.com": "CODE",
    "stackoverflow.com": "CODE",
    "bitbucket.org": "CODE",
    "codepen.io": "CODE",
    "replit.com": "CODE",
    "codesandbox.io": "CODE",
    "jsfiddle.net": "CODE",
    "npmjs.com": "CODE",
    "pypi.org": "CODE",
    "crates.io": "CODE",
    "pkg.go.dev": "CODE",
    "rubygems.org": "CODE",
    "hub.docker.com": "CODE",
    "vercel.com": "CODE",
    "netlify.com": "CODE",
    "heroku.com": "CODE",
    "railway.app": "CODE",
    "console.cloud.google.com": "CODE",
    "console.aws.amazon.com": "CODE",
    "portal.azure.com": "CODE",
    # EMAIL
    "mail.google.com": "EMAIL",
    "outlook.live.com": "EMAIL",
    "outlook.office.com": "EMAIL",
    "outlook.office365.com": "EMAIL",
    "mail.yahoo.com": "EMAIL",
    "mail.proton.me": "EMAIL",
    "app.fastmail.com": "EMAIL",
    # CHAT
    "slack.com": "CHAT",
    "app.slack.com": "CHAT",
    "discord.com": "CHAT",
    "teams.microsoft.com": "CHAT",
    "web.whatsapp.com": "CHAT",
    "web.telegram.org": "CHAT",
    "meet.google.com": "CHAT",
    "zoom.us": "CHAT",
    # DOCS
    "docs.google.com": "DOCS",
    "sheets.google.com": "DOCS",
    "slides.google.com": "DOCS",
    "notion.so": "DOCS",
    "www.notion.so": "DOCS",
    "coda.io": "DOCS",
    "airtable.com": "DOCS",
    "linear.app": "DOCS",
    "clickup.com": "DOCS",
    "asana.com": "DOCS",
    "trello.com": "DOCS",
    "jira.atlassian.com": "DOCS",
    "confluence.atlassian.com": "DOCS",
    # DESIGN
    "figma.com": "DESIGN",
    "www.figma.com": "DESIGN",
    "canva.com": "DESIGN",
    "www.canva.com": "DESIGN",
    "dribbble.com": "DESIGN",
    # MEDIA
    "youtube.com": "MEDIA",
    "www.youtube.com": "MEDIA",
    "open.spotify.com": "MEDIA",
    "music.apple.com": "MEDIA",
    "soundcloud.com": "MEDIA",
    "twitch.tv": "MEDIA",
    "www.twitch.tv": "MEDIA",
    "netflix.com": "MEDIA",
    # SOCIAL
    "twitter.com": "SOCIAL",
    "x.com": "SOCIAL",
    "linkedin.com": "SOCIAL",
    "www.linkedin.com": "SOCIAL",
    "reddit.com": "SOCIAL",
    "www.reddit.com": "SOCIAL",
    "facebook.com": "SOCIAL",
    "www.facebook.com": "SOCIAL",
    "instagram.com": "SOCIAL",
    "www.instagram.com": "SOCIAL",
    "news.ycombinator.com": "SOCIAL",
}

# Bundle-ID prefix heuristics for apps not in the static map.
_BUNDLE_PREFIX_CATEGORY: list[tuple[str, str]] = [
    ("com.jetbrains.", "CODE"),
    ("com.sublimetext.", "CODE"),
    ("com.sublimehq.", "CODE"),
    ("com.apple.dt.", "CODE"),       # Xcode tools (Instruments, etc.)
    ("com.apple.iWork.", "DOCS"),
    ("com.microsoft.Word", "DOCS"),
    ("com.microsoft.Excel", "DOCS"),
    ("com.microsoft.Powerpoint", "DOCS"),
]


def _classify_app(bundle_id: str, title: str = "", domain: str = "") -> str:
    """Classify an app into a category from bundle ID, title, or domain."""
    # 1. Exact bundle ID match
    if bundle_id in _BUNDLE_CATEGORY:
        return _BUNDLE_CATEGORY[bundle_id]
    # 2. Bundle ID prefix heuristics
    for prefix, cat in _BUNDLE_PREFIX_CATEGORY:
        if bundle_id.startswith(prefix):
            return cat
    # 3. Windows exe name classification
    exe_lower = bundle_id.lower()
    if exe_lower.endswith(".exe"):
        exe_name = exe_lower[:-4]
        _EXE_CATEGORY = {
            "code": "CODE", "vscode": "CODE", "cursor": "CODE",
            "cmd": "CODE", "powershell": "CODE", "windowsterminal": "CODE",
            "wt": "CODE", "python": "CODE", "python3": "CODE",
            "node": "CODE", "git-bash": "CODE", "mintty": "CODE",
            "devenv": "CODE",  # Visual Studio
            "notepad": "DOCS", "notepad++": "CODE", "wordpad": "DOCS",
            "chrome": "BROWSER", "firefox": "BROWSER", "msedge": "BROWSER",
            "brave": "BROWSER", "opera": "BROWSER", "vivaldi": "BROWSER",
            "slack": "CHAT", "discord": "CHAT", "teams": "CHAT",
            "zoom": "CHAT", "outlook": "EMAIL",
            "explorer": "SYSTEM", "taskmgr": "SYSTEM",
            "winword": "DOCS", "excel": "DOCS", "powerpnt": "DOCS",
            "screencap": "CODE",
        }
        if exe_name in _EXE_CATEGORY:
            return _EXE_CATEGORY[exe_name]
    # 4. Domain match
    if domain:
        if domain in _DOMAIN_CATEGORY:
            return _DOMAIN_CATEGORY[domain]
        bare = domain.removeprefix("www.")
        if bare in _DOMAIN_CATEGORY:
            return _DOMAIN_CATEGORY[bare]
    return "OTHER"


def _app_name_short(bundle_id: str) -> str:
    """Extract short, human-readable app name from bundle ID or exe name."""
    if not bundle_id:
        return "Unknown"
    # Windows exe names: "Notepad.exe", "screencap.exe", "python.exe"
    if bundle_id.lower().endswith(".exe"):
        return bundle_id[:-4]
    # macOS bundle IDs: "com.mitchellh.ghostty" → "Ghostty"
    parts = bundle_id.split(".")
    name = parts[-1] if len(parts) >= 3 else bundle_id
    # Known display names for common apps
    _DISPLAY_NAMES = {
        "ghostty": "Ghostty",
        "VSCode": "VS Code",
        "VSCodeInsiders": "VS Code Insiders",
        "Terminal": "Terminal",
        "iterm2": "iTerm",
        "Chrome": "Chrome",
        "Safari": "Safari",
        "firefox": "Firefox",
        "slackmacgap": "Slack",
        "Discord": "Discord",
        "finder": "Finder",
        "mail": "Mail",
        "Outlook": "Outlook",
        "teams2": "Teams",
    }
    return _DISPLAY_NAMES.get(name, name.replace("-", " ").title())


# ---------------------------------------------------------------------------
# Shared event iteration
# ---------------------------------------------------------------------------

def _iterate_events(
    recording_name: str,
    manifests: list[dict],
) -> "Generator[dict, None, None]":
    """Yield parsed events from all chunks' JSONL files, in chunk order."""
    for manifest in sorted(manifests, key=lambda m: m["chunk_index"]):
        chunk_idx = manifest["chunk_index"]
        blob_name = f"recordings/{recording_name}/events_{chunk_idx:04d}.jsonl"
        data = _blob_bytes(blob_name)
        if data is None:
            continue
        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not evt.get("_meta"):
                yield evt


# ---------------------------------------------------------------------------
# Activity summary derivation (Step 3)
# ---------------------------------------------------------------------------

def _format_relative_time(seconds: float) -> str:
    """Format seconds as H:MM:SS relative timestamp."""
    h, rem = divmod(int(max(0, seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _parse_relative_time(rel: str) -> float:
    """Parse H:MM:SS relative timestamp back to seconds.

    Returns 0.0 on malformed input instead of crashing.
    """
    try:
        parts = rel.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return float(parts[0])
    except (ValueError, TypeError):
        log.warning("Malformed relative timestamp: %r", rel)
        return 0.0


def _derive_activity_summary(
    recording_name: str,
    manifests: list[dict],
) -> dict | None:
    """Build compact activity summary from events JSONL + transcripts.

    Stream-parses events files line by line for memory efficiency.
    Returns dict with keys: summary, entries, time_map, session_start, session_end.
    Returns None if no events data exists.
    """
    session_start = min(m["chunk_start"] for m in manifests)
    session_end = max(m["chunk_end"] for m in manifests)

    entries: list[dict] = []
    current_entry: dict | None = None
    # Also collect raw data for fallback segmentation (avoids re-reading GCS)
    raw_timestamps: list[float] = []
    raw_window_events: list[dict] = []

    for evt in _iterate_events(recording_name, manifests):
        evt_type = evt.get("type", "")
        ts = evt.get("timestamp", 0)

        # Collect raw data for fallback
        if ts > 0 and evt_type != "mouse.move":
            raw_timestamps.append(ts)
        if evt_type == "window.switch":
            raw_window_events.append({
                "timestamp": ts,
                "bundle_id": evt.get("app_bundle_id", ""),
                "title": evt.get("window_title", ""),
            })

        if evt_type == "window.switch":
            if current_entry is not None:
                current_entry["end_ts"] = ts
                entries.append(current_entry)

            bundle_id = evt.get("app_bundle_id", "")
            title = evt.get("window_title", "")
            domain = evt.get("domain", "") or ""

            current_entry = {
                "start_ts": ts, "end_ts": ts,
                "app": _app_name_short(bundle_id),
                "bundle_id": bundle_id,
                "title": title[:80], "domain": domain,
                "cat": _classify_app(bundle_id, title, domain),
                "typed": [], "shortcuts": [],
                "clicks": 0, "scrolls": 0,
            }

        elif evt_type == "key.type" and current_entry is not None:
            text = evt.get("text", "")
            if text and len(current_entry["typed"]) < 5:
                current_entry["typed"].append(text[:50])

        elif evt_type == "key.shortcut" and current_entry is not None:
            combo = evt.get("text", "") or evt.get("combo", "")
            if combo and len(current_entry["shortcuts"]) < 10:
                current_entry["shortcuts"].append(combo)

        elif evt_type in ("mouse.singleclick", "mouse.doubleclick") and current_entry is not None:
            current_entry["clicks"] += 1

        elif evt_type == "mouse.scroll" and current_entry is not None:
            current_entry["scrolls"] += 1

    # Close final entry
    if current_entry is not None:
        current_entry["end_ts"] = session_end
        entries.append(current_entry)

    if not entries:
        return None

    # Merge consecutive entries with same app+title
    merged: list[dict] = [entries[0]]
    for e in entries[1:]:
        prev = merged[-1]
        if prev["bundle_id"] == e["bundle_id"] and prev["title"] == e["title"]:
            prev["end_ts"] = e["end_ts"]
            prev["typed"].extend(e["typed"])
            prev["shortcuts"].extend(e["shortcuts"])
            prev["clicks"] += e["clicks"]
            prev["scrolls"] += e["scrolls"]
            prev["typed"] = prev["typed"][:5]
            prev["shortcuts"] = prev["shortcuts"][:10]
        else:
            merged.append(e)

    # Build time_map: relative timestamp string → unix timestamp
    time_map: dict[str, float] = {}

    # Build compact timeline for LLM consumption (capped for context limits)
    capped = merged[:MAX_ACTIVITY_ENTRIES]
    if len(merged) > MAX_ACTIVITY_ENTRIES:
        log.info("Activity entries capped: %d → %d", len(merged), MAX_ACTIVITY_ENTRIES)
    timeline: list[dict] = []
    for e in capped:
        rel_start = e["start_ts"] - session_start
        rel_end = e["end_ts"] - session_start
        dur = rel_end - rel_start

        rel_str = _format_relative_time(rel_start)
        time_map[rel_str] = e["start_ts"]

        entry: dict = {
            "t": rel_str, "dur": _duration_human(dur),
            "app": e["app"], "title": e["title"],
        }
        if e["domain"]:
            entry["domain"] = e["domain"]
        entry["cat"] = e["cat"]
        if e["typed"]:
            entry["typed"] = e["typed"]
        if e["shortcuts"]:
            entry["shortcuts"] = e["shortcuts"]
        if e["clicks"]:
            entry["clicks"] = e["clicks"]
        timeline.append(entry)

    # Gather transcript snippets
    sorted_manifests = sorted(manifests, key=lambda m: m["chunk_index"])
    transcript_snippets: list[dict] = []
    for manifest in sorted_manifests:
        chunk_idx = manifest["chunk_index"]
        chunk_start = manifest["chunk_start"]
        blob_name = f"recordings/{recording_name}/transcript_{chunk_idx:04d}.json"
        data = _blob_bytes(blob_name)
        if data is None:
            continue
        try:
            transcript = json.loads(data)
        except json.JSONDecodeError:
            continue
        for seg in transcript.get("segments", [])[:20]:
            abs_ts = chunk_start + seg.get("start", 0)
            rel = abs_ts - session_start
            text = seg.get("text", "").strip()
            if text:
                snippet_rel = _format_relative_time(rel)
                time_map[snippet_rel] = abs_ts
                transcript_snippets.append({"t": snippet_rel, "text": text[:100]})

    # Register session end
    end_rel = _format_relative_time(session_end - session_start)
    time_map[end_rel] = session_end

    summary = {
        "recording": recording_name,
        "duration": _duration_human(session_end - session_start),
        "timeline": timeline,
    }
    if transcript_snippets:
        summary["transcript"] = transcript_snippets

    return {
        "summary": summary,
        "entries": merged,
        "time_map": time_map,
        "session_start": session_start,
        "session_end": session_end,
        # Raw data for fallback segmentation (avoids re-reading GCS)
        "raw_timestamps": raw_timestamps,
        "raw_window_events": raw_window_events,
    }


# ---------------------------------------------------------------------------
# LLM segmentation (Steps 4–5)
# ---------------------------------------------------------------------------

_LLM_PROMPT = """\
You are a productivity analyst examining a computer activity timeline from a screen recording.

ACTIVITY LOG:
{activity_json}

INSTRUCTIONS:
1. Identify the distinct TASKS the user performed. A task is a coherent unit of work —
   not just "used an app" but "what were they trying to accomplish?"
2. Brief app switches (< 30s) mid-task are NOT separate tasks — absorb them.
3. Related activities across different apps are ONE task
   (e.g., "code in VSCode → test in Terminal → check docs in Chrome" = one dev task).
4. Use transcript speech to understand INTENT — "let me check my email" signals a task switch.

For each task return:
- start_time: relative timestamp (matching timeline format, e.g. "0:02:00")
- end_time: relative timestamp
- name: 2-3 words capturing the core task (NOT the app name)
  Good: "Fix login", "Draft roadmap", "Deploy hotfix", "Review PR", "Write tests"
  Bad: "Used VSCode", "Chrome session", "Terminal work", "Coding task"
- description: 3-5 sentences covering what was being worked on, specific actions taken,
  outcomes or blockers encountered, and tools/files involved.
  Be concrete — mention file names, URLs, error messages, or people when visible.
- category: one of [development, communication, research, admin, creative, other]
- apps_used: list of apps involved
- confidence: high | medium | low

Also provide a SESSION SUMMARY:
- overview: 4-6 sentence description of what the user accomplished, including specific
  outcomes, tools used, and any notable blockers or achievements
- primary_focus: the main category of work
- time_breakdown: approximate percentage per category
- key_accomplishments: 2-4 bullet points of specific things completed

Also provide TAGS for the entire recording session:
- tags: 3-8 lowercase hyphenated labels describing the session
  (e.g., "python", "debugging", "email-triage", "code-review", "api-design")
- Capture: languages, frameworks, tools, activity types, and domains
- Use only lowercase letters, numbers, and hyphens

RULES:
- Every second of the recording must be covered by exactly one task (no gaps, no overlaps)
- Name tasks by INTENT not by app name
- A task should be at least 1 minute long
- start_time of first task must be "0:00:00"

Return ONLY valid JSON: {{"tasks": [...], "summary": {{...}}, "tags": [...]}}"""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["development", "communication", "research",
                                 "admin", "creative", "other"],
                    },
                    "apps_used": {"type": "array", "items": {"type": "string"}},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
                "required": ["start_time", "end_time", "name", "description",
                             "category", "apps_used", "confidence"],
            },
        },
        "summary": {
            "type": "object",
            "properties": {
                "overview": {"type": "string"},
                "primary_focus": {"type": "string"},
                "time_breakdown": {"type": "object"},
                "key_accomplishments": {
                    "type": "array", "items": {"type": "string"},
                },
            },
            "required": ["overview", "primary_focus", "time_breakdown",
                         "key_accomplishments"],
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["tasks", "summary", "tags"],
}


def _llm_segment_session(activity_summary: dict) -> dict | None:
    """Call LLM to segment the session into tasks.

    Returns dict with "tasks" and "summary", or None on failure.
    """
    prompt = _LLM_PROMPT.format(
        activity_json=json.dumps(activity_summary, indent=2),
    )
    return _call_llm(prompt)


def _call_llm(prompt: str) -> dict | None:
    """Call LLM: Vertex AI Gemini Flash. Returns parsed JSON or None."""
    result = _call_gemini(prompt)
    if result is not None:
        return result

    log.warning("Gemini failed — falling back to simple segmentation")
    return None


def _call_gemini(prompt: str) -> dict | None:
    """Call Gemini Flash via Google AI API. Returns parsed JSON or None."""
    try:
        from google import genai
        from google.genai import types

        api_key = os.environ.get("GOOGLE_GENAI_API_KEY")
        if not api_key:
            log.info("No GOOGLE_GENAI_API_KEY configured, skipping Gemini")
            return None

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                temperature=0.1,
            ),
        )

        result = json.loads(response.text)
        log.info("Gemini Flash returned %d tasks", len(result.get("tasks", [])))
        return result

    except ImportError:
        log.info("google-genai not installed, skipping Gemini")
        return None
    except Exception:
        log.warning("Gemini call failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# LLM output validation (Step 6)
# ---------------------------------------------------------------------------

_VALID_CATEGORIES = frozenset(
    {"development", "communication", "research", "admin", "creative", "other"}
)

_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_MAX_TAGS = 8


def _validate_tags(raw: list) -> list[str]:
    """Validate and normalize LLM-generated tags."""
    seen: set[str] = set()
    result: list[str] = []
    for t in raw:
        if not isinstance(t, str):
            continue
        t = t.lower().strip()
        if _TAG_RE.match(t) and t not in seen:
            seen.add(t)
            result.append(t)
        if len(result) >= _MAX_TAGS:
            break
    return result


def _validate_llm_tasks(
    llm_result: dict,
    session_start: float,
    session_end: float,
    time_map: dict[str, float],
) -> dict | None:
    """Validate LLM output and convert relative timestamps to Unix.

    Returns dict with "tasks" and "summary", or None if invalid.
    """
    tasks = llm_result.get("tasks", [])
    summary = llm_result.get("summary", {})

    if not tasks:
        log.warning("LLM returned empty tasks list")
        return None

    converted: list[dict] = []

    for i, task in enumerate(tasks):
        for field in ("start_time", "end_time", "name", "description", "category"):
            if field not in task:
                log.warning("Task %d missing field: %s", i, field)
                return None

        start_rel = task["start_time"]
        end_rel = task["end_time"]

        start_unix = time_map.get(start_rel, session_start + _parse_relative_time(start_rel))
        end_unix = time_map.get(end_rel, session_start + _parse_relative_time(end_rel))

        # Clamp to session bounds
        start_unix = max(session_start, min(start_unix, session_end))
        end_unix = max(session_start, min(end_unix, session_end))

        if end_unix <= start_unix:
            log.warning("Task %d has zero or negative duration", i)
            return None

        cat = task.get("category", "other")
        if cat not in _VALID_CATEGORIES:
            cat = "other"

        name = (task.get("name") or f"Task {i + 1}").strip()[:80]

        converted.append({
            "start_ts": start_unix,
            "end_ts": end_unix,
            "name": name,
            "derived_name": _slugify(name),
            "description": (task.get("description") or "")[:600],
            "category": cat,
            "apps_used": task.get("apps_used", []),
            "confidence": task.get("confidence", "medium"),
            # Compatibility defaults for _process_task
            "dominant_app": "",
            "dominant_app_name": "",
            "dominant_title": "",
            "dominant_pct": 0.0,
            "all_apps": {},
            "rest_after_s": 0.0,
            "event_count": 0,
        })

    converted.sort(key=lambda t: t["start_ts"])

    # Check for overlaps (1s tolerance)
    for i in range(len(converted) - 1):
        if converted[i]["end_ts"] > converted[i + 1]["start_ts"] + 1.0:
            log.warning("Tasks %d and %d overlap", i, i + 1)
            return None

    # Compute rest_after_s
    for i in range(len(converted) - 1):
        gap = converted[i + 1]["start_ts"] - converted[i]["end_ts"]
        converted[i]["rest_after_s"] = round(max(0, gap), 1)
    if converted:
        converted[-1]["rest_after_s"] = 0.0

    # Ensure summary has all required fields
    if not summary.get("overview"):
        summary["overview"] = f"Recording with {len(converted)} tasks."
    if not summary.get("primary_focus"):
        cats = [t["category"] for t in converted]
        summary["primary_focus"] = max(set(cats), key=cats.count) if cats else "other"
    if not summary.get("time_breakdown"):
        summary["time_breakdown"] = {}
    if not summary.get("key_accomplishments"):
        summary["key_accomplishments"] = []

    tags = _validate_tags(llm_result.get("tags", []))

    return {"tasks": converted, "summary": summary, "tags": tags}


# ---------------------------------------------------------------------------
# Fallback segmentation + stats summary (Step 7)
# ---------------------------------------------------------------------------

def _simple_segment_from_events(
    recording_name: str,
    manifests: list[dict],
    rest_threshold: float = DEFAULT_REST_THRESHOLD,
    *,
    cached_timestamps: list[float] | None = None,
    cached_window_events: list[dict] | None = None,
) -> list[dict]:
    """Fallback: segment by idle gaps from events JSONL.

    If cached_timestamps/cached_window_events are provided (from a prior
    _derive_activity_summary call), skips re-reading events from GCS.
    """
    session_start = min(m["chunk_start"] for m in manifests)
    session_end = max(m["chunk_end"] for m in manifests)

    if cached_timestamps is not None and cached_window_events is not None:
        timestamps = list(cached_timestamps)
        window_events = list(cached_window_events)
    else:
        timestamps = []
        window_events = []
        for evt in _iterate_events(recording_name, manifests):
            ts = evt.get("timestamp", 0)
            evt_type = evt.get("type", "")

            if evt_type == "window.switch":
                window_events.append({
                    "timestamp": ts,
                    "bundle_id": evt.get("app_bundle_id", ""),
                    "title": evt.get("window_title", ""),
                })
            if ts > 0 and evt_type != "mouse.move":
                timestamps.append(ts)

    if not timestamps:
        return [{
            "start_ts": session_start,
            "end_ts": session_end,
            "derived_name": "recording",
            "category": "other",
            "dominant_app": "", "dominant_app_name": "",
            "dominant_title": "", "dominant_pct": 0.0,
            "all_apps": {}, "rest_after_s": 0.0, "event_count": 0,
            "source_chunks": [
                {
                    "chunk_index": m["chunk_index"],
                    "chunk_start": m["chunk_start"],
                    "chunk_end": m["chunk_end"],
                    "start_ts": m["chunk_start"],
                    "end_ts": m["chunk_end"],
                    "start_offset_s": 0.0,
                    "end_offset_s": m["chunk_end"] - m["chunk_start"],
                }
                for m in sorted(manifests, key=lambda m: m["chunk_index"])
            ],
        }]

    timestamps.sort()
    window_events.sort(key=lambda w: w["timestamp"])

    # Split by idle gaps
    tasks_raw: list[tuple[float, float, int]] = []
    task_start = timestamps[0]
    task_end = task_start
    count = 1
    for ts in timestamps[1:]:
        if ts - task_end > rest_threshold:
            tasks_raw.append((task_start, task_end, count))
            task_start = ts
            count = 0
        task_end = ts
        count += 1
    tasks_raw.append((task_start, task_end, count))

    result: list[dict] = []
    for start, end, evt_count in tasks_raw:
        dom_bundle = ""
        dom_title = ""
        for w in reversed(window_events):
            if w["timestamp"] <= end:
                dom_bundle = w["bundle_id"]
                dom_title = w["title"]
                break

        app_name = _app_name_short(dom_bundle) if dom_bundle else "unknown"
        clean_title = _clean_title(dom_title[:60]) if dom_title else ""
        title_slug = _slugify(clean_title) if clean_title else ""
        derived = (
            f"{_slugify(app_name)}-{title_slug}"
            if title_slug and title_slug != "untitled"
            else _slugify(app_name)
        )

        app_cat = _classify_app(dom_bundle)
        task = {
            "start_ts": start, "end_ts": end,
            "derived_name": derived,
            "category": _APP_CAT_TO_TASK_CAT.get(app_cat, "other"),
            "dominant_app": dom_bundle,
            "dominant_app_name": app_name,
            "dominant_title": dom_title[:80] if dom_title else "",
            "dominant_pct": 0.0, "all_apps": {},
            "rest_after_s": 0.0, "event_count": evt_count,
            "source_chunks": [],
        }
        task["source_chunks"] = _compute_source_chunks(task, manifests)
        result.append(task)

    for i in range(len(result) - 1):
        result[i]["rest_after_s"] = round(
            result[i + 1]["start_ts"] - result[i]["end_ts"], 1,
        )

    return result


def _stats_summary(activity_entries: list[dict], tasks: list[dict]) -> dict:
    """Generate a stats-based summary when LLM is unavailable."""
    cat_time: dict[str, float] = {}
    unique_apps: set[str] = set()
    app_display_names: list[str] = []

    for e in activity_entries:
        dur = e.get("end_ts", 0) - e.get("start_ts", 0)
        cat = e.get("cat", "OTHER")
        cat_time[cat] = cat_time.get(cat, 0) + dur
        if e.get("app"):
            unique_apps.add(e["app"])
            if e["app"] not in app_display_names:
                app_display_names.append(e["app"])

    total_time = sum(cat_time.values()) or 1.0
    time_breakdown = {
        cat: round(dur / total_time * 100)
        for cat, dur in sorted(cat_time.items(), key=lambda x: -x[1])
        if dur / total_time >= 0.05
    }

    dominant_cat = max(cat_time, key=cat_time.get) if cat_time else "other"

    # Build a descriptive overview from task data
    task_cat = _APP_CAT_TO_TASK_CAT.get(dominant_cat, "other")
    _CAT_LABELS = {
        "development": "development work",
        "research": "web research and browsing",
        "communication": "communication",
        "admin": "system administration",
        "creative": "creative work",
        "other": "general computing",
    }
    focus_label = _CAT_LABELS.get(task_cat, "general work")
    top_apps = app_display_names[:4]
    app_str = ", ".join(top_apps) if top_apps else "various applications"
    total_dur = _duration_human(total_time)

    overview = f"Session focused on {focus_label} using {app_str}."
    if len(tasks) > 1:
        overview += f" Contained {len(tasks)} distinct activities over {total_dur}."

    return {
        "overview": overview,
        "primary_focus": dominant_cat.lower(),
        "time_breakdown": time_breakdown,
        "key_accomplishments": [],
    }


# ---------------------------------------------------------------------------
# Task → chunk mapping (Step 8)
# ---------------------------------------------------------------------------

def _compute_source_chunks(task: dict, manifests: list[dict]) -> list[dict]:
    """Map a task's time range to source chunk indices and offsets."""
    source_chunks = []
    for m in sorted(manifests, key=lambda m: m["chunk_index"]):
        chunk_start = m["chunk_start"]
        chunk_end = m["chunk_end"]
        overlap_start = max(task["start_ts"], chunk_start)
        overlap_end = min(task["end_ts"], chunk_end)
        if overlap_end > overlap_start:
            source_chunks.append({
                "chunk_index": m["chunk_index"],
                "chunk_start": chunk_start,
                "chunk_end": chunk_end,
                "start_ts": overlap_start,
                "end_ts": overlap_end,
                "start_offset_s": overlap_start - chunk_start,
                "end_offset_s": overlap_end - chunk_start,
            })
    return source_chunks


def _map_tasks_to_chunks(tasks: list[dict], manifests: list[dict]) -> list[dict]:
    """Map LLM task boundaries to source chunk indices."""
    for task in tasks:
        if not task.get("source_chunks"):
            task["source_chunks"] = _compute_source_chunks(task, manifests)
    return tasks


# ---------------------------------------------------------------------------
# v2 manifest processing orchestrator (Step 9)
# ---------------------------------------------------------------------------

def _process_v2_manifests(
    recording_name: str,
    manifests: list[dict],
) -> tuple[list[dict], str, dict | None, list[str]]:
    """Process v2 manifests: try LLM segmentation, fallback to idle-gap.

    Returns (tasks, segmentation_method, summary_or_None, tags).
    """
    tasks = None
    summary = None
    tags: list[str] = []
    segmentation_method = "idle"
    activity_data = None

    # v2 manifests always attempt LLM segmentation. The manifest format
    # itself is the control: v2 = try LLM, v1 = legacy _merge_tasks().
    log.info("%s: attempting LLM segmentation", recording_name)
    activity_data = _derive_activity_summary(recording_name, manifests)

    if activity_data:
        log.info(
            "%s: activity summary — %d entries, %d timeline items",
            recording_name,
            len(activity_data["entries"]),
            len(activity_data["summary"]["timeline"]),
        )
        llm_result = _llm_segment_session(activity_data["summary"])

        if llm_result:
            try:
                validated = _validate_llm_tasks(
                    llm_result,
                    activity_data["session_start"],
                    activity_data["session_end"],
                    activity_data["time_map"],
                )
            except Exception:
                log.warning("%s: LLM validation crashed", recording_name, exc_info=True)
                validated = None
            if validated:
                tasks = _map_tasks_to_chunks(validated["tasks"], manifests)
                summary = validated["summary"]
                tags = validated.get("tags", [])
                segmentation_method = "llm"
                log.info("%s: LLM segmentation → %d tasks", recording_name, len(tasks))
            else:
                log.warning("%s: LLM output failed validation", recording_name)
        else:
            log.warning("%s: LLM call returned None", recording_name)
    else:
        log.warning("%s: no activity data for LLM", recording_name)

    # Fallback
    if tasks is None:
        log.info("%s: using simple idle-gap segmentation", recording_name)
        tasks = _simple_segment_from_events(
            recording_name, manifests,
            cached_timestamps=activity_data.get("raw_timestamps") if activity_data else None,
            cached_window_events=activity_data.get("raw_window_events") if activity_data else None,
        )
        activity_entries = activity_data["entries"] if activity_data else []
        summary = _stats_summary(activity_entries, tasks)
        segmentation_method = "idle"
        log.info("%s: simple segmentation → %d tasks", recording_name, len(tasks))

    return tasks, segmentation_method, summary, tags


# ---------------------------------------------------------------------------
# Cross-recording session index
# ---------------------------------------------------------------------------

def _update_session_index(
    recording_name: str,
    timeline: dict,
    tags: list[str],
    max_retries: int = 3,
) -> None:
    """Upsert this recording into sessions/_index.json with optimistic locking."""
    index_blob_name = "sessions/_index.json"

    for attempt in range(max_retries):
        try:
            blob = _bucket().blob(index_blob_name)
            try:
                blob.reload()
                raw = blob.download_as_bytes()
                index = json.loads(raw)
                generation = blob.generation
            except Exception:
                index = {"version": 1, "recordings": {}}
                generation = 0  # blob doesn't exist yet

            # Build entry from timeline data
            summary = timeline.get("summary", {})
            task_entries = timeline.get("tasks", [])
            index["recordings"][recording_name] = {
                "show_on_website": timeline.get("show_on_website", True),
                "display_name": timeline.get("display_name"),
                "processed_at": timeline.get("processed_at"),
                "segmentation_method": timeline.get("segmentation_method"),
                "total_tasks": timeline.get("total_tasks", 0),
                "total_duration_s": timeline.get("total_duration_s", 0),
                "primary_focus": summary.get("primary_focus", "other"),
                "overview": (summary.get("overview") or "")[:600],
                "categories": sorted(set(
                    t.get("category", "other") for t in task_entries
                )),
                "tags": tags,
                "task_folders": [
                    {"folder": t.get("folder", ""), "category": t.get("category", "other")}
                    for t in task_entries
                ],
            }
            index["updated_at"] = datetime.now(timezone.utc).isoformat()
            index["total_recordings"] = len(index["recordings"])

            blob.upload_from_string(
                json.dumps(index, indent=2, ensure_ascii=False),
                content_type="application/json",
                if_generation_match=generation,
            )
            log.info("Updated session index for %s", recording_name)
            return

        except Exception as exc:
            if "PreconditionFailed" in type(exc).__name__ or "412" in str(exc):
                log.info("Index write conflict (attempt %d/%d), retrying",
                         attempt + 1, max_retries)
                continue
            log.warning("Failed to update session index: %s", exc)
            return  # Non-retryable error — don't block processing

    log.warning("Failed to update session index after %d retries", max_retries)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@functions_framework.cloud_event
def process_recording(cloud_event):
    """Eventarc handler: triggered when an object is finalized in GCS."""
    data = cloud_event.data
    object_name = data["name"]

    # Guard: only process trigger files
    TRIGGER_FILES = {"recording.db", "recording_complete.json"}
    parts = object_name.split("/")
    if len(parts) != 3 or parts[0] != "recordings" or parts[2] not in TRIGGER_FILES:
        log.info("Ignoring non-trigger object: %s", object_name)
        return

    trigger_file = parts[2]
    recording_name = parts[1]
    log.info("Processing recording: %s (trigger: %s)", recording_name, trigger_file)

    sessions_prefix = f"sessions/{recording_name}/"

    # Determine trigger ID for idempotency
    if trigger_file == "recording_complete.json":
        raw = _blob_bytes(object_name)
        if raw is None:
            log.error("Cannot download sentinel %s", object_name)
            return
        try:
            sentinel = json.loads(raw)
        except json.JSONDecodeError:
            log.error("Invalid sentinel JSON: %s", object_name)
            return
        trigger_id = sentinel.get("sentinel_id", "")
        chunks_expected = sentinel.get("chunks_expected", 0)
        show_on_website = sentinel.get("show_on_website")
    else:
        # Legacy recording.db trigger
        db_data = _blob_bytes(object_name)
        if db_data is None:
            log.error("Cannot download %s", object_name)
            return
        trigger_id = _md5_bytes(db_data)
        chunks_expected = 0
        show_on_website = None

    # Resolve visibility: sentinel field is authoritative; fall back to
    # _unlisted marker blob only for legacy sentinels that lack the field.
    if show_on_website is None:
        unlisted_blob = _bucket().blob(f"recordings/{recording_name}/_unlisted")
        show_on_website = not unlisted_blob.exists()

    # Idempotency check
    if _check_idempotency(sessions_prefix, trigger_id):
        log.info("Already processed %s (trigger_id=%s), skipping", recording_name, trigger_id)
        return

    started_at = datetime.now(timezone.utc).isoformat()

    # Load manifests — for sentinel triggers, retry briefly if none found yet
    # (chunks may still be landing in GCS due to minor propagation delay)
    manifest_blobs = _list_manifests(recording_name)
    if not manifest_blobs and trigger_file == "recording_complete.json" and chunks_expected > 0:
        import time as _time
        for _retry in range(1, 4):  # up to 3 retries, 15s apart = 45s max
            log.info(
                "%s: 0/%d manifests found, waiting 15s (retry %d/3)...",
                recording_name, chunks_expected, _retry,
            )
            _time.sleep(15)
            manifest_blobs = _list_manifests(recording_name)
            if manifest_blobs:
                log.info(
                    "%s: found %d manifests after retry %d",
                    recording_name, len(manifest_blobs), _retry,
                )
                break

    if not manifest_blobs:
        log.warning("No manifests found for %s — writing provisional status (will retry on next trigger upload)", recording_name)
        # Write "complete_empty" status — this does NOT block reprocessing.
        # If manifests arrive later and a trigger file is re-uploaded (e.g. via
        # `screencap upload`), the idempotency check will allow reprocessing.
        _upload_json(f"{sessions_prefix}_processing_status.json", {
            "status": "complete_empty",
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "processor_version": PROCESSOR_VERSION,
            "trigger_id": trigger_id,
            "trigger_file": trigger_file,
            "source_db_md5": trigger_id if trigger_file == "recording.db" else None,
            "chunks_found": 0,
            "manifests_found": 0,
            "note": "No manifests found. Will reprocess on next trigger upload.",
        })
        return

    # Sentinel-triggered: verify all expected manifests are present
    if trigger_file == "recording_complete.json" and chunks_expected > 0:
        # Retry if some manifests are still missing
        if len(manifest_blobs) < chunks_expected:
            import time as _time
            for _retry in range(1, 4):
                log.info(
                    "%s: %d/%d manifests found, waiting 15s (retry %d/3)...",
                    recording_name, len(manifest_blobs), chunks_expected, _retry,
                )
                _time.sleep(15)
                manifest_blobs = _list_manifests(recording_name)
                if len(manifest_blobs) >= chunks_expected:
                    log.info("%s: all %d manifests found after retry %d",
                             recording_name, len(manifest_blobs), _retry)
                    break

        if len(manifest_blobs) < chunks_expected:
            log.warning(
                "%s: expected %d manifests but found %d — provisional status for retry",
                recording_name, chunks_expected, len(manifest_blobs),
            )
            _upload_json(f"{sessions_prefix}_processing_status.json", {
                "status": "complete_empty",
                "started_at": started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "processor_version": PROCESSOR_VERSION,
                "trigger_id": trigger_id,
                "trigger_file": trigger_file,
                "chunks_found": len(manifest_blobs),
                "chunks_expected": chunks_expected,
                "manifests_found": len(manifest_blobs),
                "note": f"Expected {chunks_expected} manifests, found {len(manifest_blobs)}. "
                        "Will reprocess on recording.db upload via screencap upload.",
            })
            return

    manifests = []
    for blob_name in manifest_blobs:
        m = _load_manifest(blob_name)
        if m is not None:
            manifests.append(m)

    if not manifests:
        log.error("All manifests failed to load for %s", recording_name)
        return

    # --- Route by manifest format version ---
    format_version = manifests[0].get("format_version", 0)
    segmentation_method = "idle"
    session_summary = None

    if format_version >= 2:
        # v2 manifests — LLM segmentation path (with idle-gap fallback)
        merged_tasks, segmentation_method, session_summary, session_tags = _process_v2_manifests(
            recording_name, manifests,
        )
        log.info(
            "%s: v2 path — %d chunks, %d tasks (%s)",
            recording_name, len(manifests), len(merged_tasks), segmentation_method,
        )
    else:
        # Legacy v1 manifests — existing cross-chunk merge
        session_tags: list[str] = []
        tasks_before = sum(len(m.get("tasks", [])) for m in manifests)
        rest_threshold = manifests[0].get("rest_threshold_secs", DEFAULT_REST_THRESHOLD)
        merged_tasks = _merge_tasks(manifests, rest_threshold)
        for t in merged_tasks:
            if "category" not in t:
                app_cat = _classify_app(t.get("dominant_app", ""))
                t["category"] = _APP_CAT_TO_TASK_CAT.get(app_cat, "other")
        log.info(
            "%s: v1 path — %d chunks, %d tasks before merge, %d after",
            recording_name, len(manifests), tasks_before, len(merged_tasks),
        )

    # Assign folder names
    folders = _assign_folder_names(merged_tasks)

    # Process tasks with /tmp management
    with tempfile.TemporaryDirectory(prefix="screencap_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        video_cache: dict[int, Path] = {}
        audio_cache: dict[int, Path] = {}

        timeline_tasks: list[dict] = []
        total_video_bytes = 0
        total_active_s = 0.0

        for task_idx, (task, folder) in enumerate(zip(merged_tasks, folders)):
            log.info("Processing task %d/%d: %s", task_idx + 1, len(merged_tasks), folder)

            task_meta = _process_task(
                task, folder, recording_name, tmpdir,
                video_cache, audio_cache, sessions_prefix,
            )

            # Build timeline entry
            timeline_entry = {k: v for k, v in task_meta.items() if k != "all_apps"}
            timeline_entry["index"] = task_idx

            # Add LLM-enriched fields if present on the source task
            for llm_field in _LLM_ENRICHED_FIELDS:
                if task.get(llm_field):
                    timeline_entry[llm_field] = task[llm_field]

            timeline_tasks.append(timeline_entry)
            total_video_bytes += int(task_meta.get("video_size_mb", 0) * 1024 * 1024)
            total_active_s += task_meta.get("duration_s", 0)

            # Cleanup chunk cache
            remaining_chunks: set[int] = set()
            for future_task in merged_tasks[task_idx + 1:]:
                for sc in future_task["source_chunks"]:
                    remaining_chunks.add(sc["chunk_index"])
            _cleanup_chunk_cache(video_cache, remaining_chunks)
            _cleanup_chunk_cache(audio_cache, remaining_chunks)

    # Compute total duration
    total_duration = 0.0
    if merged_tasks:
        total_duration = merged_tasks[-1]["end_ts"] - merged_tasks[0]["start_ts"]

    # Generate display name
    display_name = _derive_display_name(
        recording_name, timeline_tasks, session_summary, segmentation_method,
    )

    # Build timeline.json
    timeline = {
        "recording_name": recording_name,
        "display_name": display_name,
        "show_on_website": show_on_website,
        "segmentation_method": segmentation_method,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "processor_version": PROCESSOR_VERSION,
        "total_tasks": len(merged_tasks),
        "total_chunks": len(manifests),
        "total_duration_s": round(total_duration, 1),
        "total_active_s": round(total_active_s, 1),
        "tags": session_tags,
        "tasks": timeline_tasks,
    }

    # Add session summary (from LLM or stats-based fallback)
    if session_summary:
        timeline["summary"] = session_summary

    # Legacy field for v1 compat
    if format_version < 2:
        timeline["rest_threshold_secs"] = manifests[0].get(
            "rest_threshold_secs", DEFAULT_REST_THRESHOLD,
        )

    _upload_json(f"{sessions_prefix}timeline.json", timeline)

    if not show_on_website:
        try:
            _upload_text(f"{sessions_prefix}_unlisted", "")
            log.info("Uploaded _unlisted marker for %s", recording_name)
        except Exception:
            log.warning("Failed to upload _unlisted marker for %s", recording_name, exc_info=True)

    try:
        _update_session_index(recording_name, timeline, session_tags)
    except Exception:
        log.warning("Index update failed for %s", recording_name, exc_info=True)

    # Upload processing status
    _upload_json(f"{sessions_prefix}_processing_status.json", {
        "status": "complete",
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "processor_version": PROCESSOR_VERSION,
        "trigger_id": trigger_id,
        "trigger_file": trigger_file,
        "source_db_md5": trigger_id if trigger_file == "recording.db" else None,
        "chunks_found": len(manifests),
        "manifests_found": len(manifest_blobs),
        "tasks_processed": len(merged_tasks),
        "segmentation_method": segmentation_method,
        "total_video_bytes": total_video_bytes,
    })

    log.info(
        "Done processing %s: %d tasks, %d chunks, method=%s",
        recording_name, len(merged_tasks), len(manifests), segmentation_method,
    )
