"""Prepare a recording for native (SwiftUI) review (plan U2).

The SwiftUI review window's preparation step calls
``screencap review-data --json <name>`` and consumes the returned paths
to a single playable video and an ``events.jsonl`` file. This module
owns the orchestration:

1. ``_ensure_single_video`` (from viewer.py) — idempotent ffmpeg concat
   of ``chunk_*.mp4`` files into ``video.mp4`` when needed.
2. Pixel-format remediation — the recorder writes H.264 with
   ``yuv444p`` (``src/screencap/engine/config.py``), which AVKit's
   hardware decoder rejects on most Macs. When the source is yuv444p,
   we re-encode once into a sibling ``video_review.mp4`` (sentinel-
   gated so the work runs at most once per recording). The original
   ``video.mp4`` is unchanged — the upload pipeline continues to ship
   it as-is to preserve the lossless training corpus.
3. ``events.jsonl`` — auto-export via ``exporter.export_recording`` if
   absent, with ``exclude_moves=True`` so the timeline pane only sees
   discrete events (mouse.click, key.type, window.switch, …) rather
   than the noisy mouse-move trail.

Returns a JSON-serializable dict with the envelope downstream consumers
(the SwiftUI shell, tests) decode.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REVIEW_SCHEMA_VERSION = 1

# Filename used for the AVKit-compatible re-encoded video. Sibling to the
# original `video.mp4`. The upload pipeline excludes this file by name
# via `upload._UPLOAD_EXCLUDE_NAMES` — the lossless `video.mp4` is the
# canonical training-corpus artifact; `video_review.mp4` is review-only.
REVIEW_VIDEO_FILENAME = "video_review.mp4"

# Sentinel that records "this recording's review video has already been
# remediated". A second `screencap review-data` invocation short-circuits
# the re-encode. The leading dot also keeps the sentinel out of
# `list_recording_files` (uploader skips dotfiles).
REVIEW_VIDEO_SENTINEL = ".review_video_pixfmt"

# AVKit / AVFoundation's hardware H.264 decoder is documented to support
# yuv420p and yuv422p. yuv444p (High 4:4:4 Predictive profile) is
# rejected on most Macs (black frames or load failure). Keep this list
# tight — the recorder's default is yuv444p so this gate fires often
# enough to matter.
_AVKIT_COMPATIBLE_PIXFMTS = frozenset({"yuv420p", "yuv422p"})


class ReviewPrepareError(RuntimeError):
    """A non-recoverable failure during review-data preparation."""


def _probe_video_pixfmt(video_path: Path) -> str | None:
    """Return the video stream's pixel format, or None if ffprobe is
    unavailable or the probe fails.

    The fallback (returning None) is treated by ``ensure_review_video``
    as "we cannot prove the source is AVKit-compatible" — so we
    remediate anyway. That's safer than gambling on playability.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=pix_fmt",
                "-of", "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    pix_fmt = result.stdout.strip()
    return pix_fmt or None


def _reencode_for_avkit(source: Path, destination: Path) -> None:
    """Re-encode ``source`` to ``destination`` with yuv420p so AVKit can
    play it. H.264 + yuv420p is the universal-compatibility combination.

    Uses ``-preset veryfast`` because review playback quality is not the
    training corpus (the original ``video.mp4`` retains full quality)
    and a fast preset keeps the preparation state brief.
    """
    tmp_destination = destination.with_suffix(destination.suffix + ".tmp")
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(source),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", "23",
        # Drop audio: the recorder writes audio as a separate .flac
        # alongside, and re-encoding audio here would just add cost
        # for no benefit. AVKit gates audio playback on the video file.
        "-an",
        str(tmp_destination),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as e:
        raise ReviewPrepareError(
            "ffmpeg not found — install ffmpeg to enable native review playback."
        ) from e
    except subprocess.TimeoutExpired as e:
        # Clean up the partial output so a retry doesn't see a half-file.
        tmp_destination.unlink(missing_ok=True)
        raise ReviewPrepareError(
            f"ffmpeg re-encode timed out after 10 minutes for {source.name}"
        ) from e
    if result.returncode != 0:
        tmp_destination.unlink(missing_ok=True)
        # Tail the ffmpeg stderr so the error message is actionable
        # without dumping multi-MB of progress logs into the JSON envelope.
        tail = (result.stderr or "")[-400:]
        raise ReviewPrepareError(f"ffmpeg re-encode failed: {tail}")
    os.replace(tmp_destination, destination)


def ensure_review_video(rec_dir: Path) -> tuple[Path, bool]:
    """Return ``(playable_video_path, remediated)``.

    Assumes ``_ensure_single_video`` has already produced ``video.mp4``
    (or symlinked the single chunk). When the source is AVKit-
    incompatible (yuv444p, or ffprobe-not-available-treated-as-suspect),
    produces ``video_review.mp4`` once per recording and returns that
    path instead. Idempotent: the sentinel file prevents re-encoding on
    repeat invocations.
    """
    source_video = rec_dir / "video.mp4"
    if not source_video.exists():
        raise ReviewPrepareError(
            f"video.mp4 not found in {rec_dir.name} — recording may be empty or corrupted"
        )

    sentinel = rec_dir / REVIEW_VIDEO_SENTINEL
    review_video = rec_dir / REVIEW_VIDEO_FILENAME

    # Sentinel exists and the remediated file is on disk → skip the
    # whole probe-and-reencode cost.
    if sentinel.exists() and review_video.exists():
        return review_video, True

    pix_fmt = _probe_video_pixfmt(source_video)
    if pix_fmt in _AVKIT_COMPATIBLE_PIXFMTS:
        # Source is already playable. Write a sentinel that records the
        # decision so future invocations skip even the ffprobe call.
        try:
            sentinel.write_text(json.dumps({"pix_fmt": pix_fmt, "remediated": False}))
        except OSError:
            # Best-effort — losing the sentinel just means we probe again
            # next time, which is cheap. Do not fail the whole prepare.
            pass
        return source_video, False

    # pix_fmt is yuv444p, unknown (probe failed), or any other format we
    # haven't proven compatible. Remediate.
    if pix_fmt is None:
        sys.stderr.write(
            "warning: ffprobe unavailable or probe failed; "
            "remediating video for AVKit compatibility as a precaution\n"
        )
    _reencode_for_avkit(source_video, review_video)
    try:
        sentinel.write_text(
            json.dumps({"pix_fmt": pix_fmt or "unknown", "remediated": True})
        )
    except OSError:
        pass
    return review_video, True


def _ensure_events_jsonl(rec_dir: Path) -> Path:
    """Return path to a usable ``events.jsonl``.

    If absent, exports one with ``exclude_moves=True`` so the timeline
    pane only sees discrete events. Mirrors the auto-export pattern
    used by the upload command (``cli/__init__.py`` upload block).
    """
    events_path = rec_dir / "events.jsonl"
    if events_path.exists():
        return events_path

    # Deferred import — exporter.py pulls engine modules that are heavier
    # than the `screencap --help` path tolerates.
    from screencap.exporter import build_export_metadata, export_recording

    meta = build_export_metadata(exclude_moves=True)
    export_recording(
        rec_dir,
        str(events_path),
        exclude_moves=True,
        metadata=meta,
    )
    return events_path


def prepare_review_data(name: str) -> dict:
    """Run the full review-data preparation pipeline for ``name``.

    Returns a JSON-serializable envelope. Raises ``ReviewPrepareError``
    for non-recoverable failures (missing recording, missing video,
    ffmpeg failure). The CLI wrapper translates these into the standard
    ``{"ok": false, "error": ...}`` envelope with a non-zero exit code.
    """
    from screencap.catalog import _read_recording_meta, find_db
    from screencap.config import resolve_recording_dir
    from screencap.viewer import _ensure_single_video

    try:
        rec_dir = resolve_recording_dir(name)
    except ValueError as e:
        raise ReviewPrepareError(str(e)) from e

    if not rec_dir.exists():
        raise ReviewPrepareError(f"Recording not found: {name}")

    # Step 1: concat chunked videos into a single playable file (idempotent).
    _ensure_single_video(rec_dir)

    # Step 2: pixel-format remediation for AVKit.
    video_path, remediated = ensure_review_video(rec_dir)

    # Step 3: events.jsonl (auto-export if missing, filtered).
    events_path = _ensure_events_jsonl(rec_dir)

    # Step 4: timing metadata for the timeline pane's coordinate space.
    db_path = find_db(rec_dir)
    started_at: float | None = None
    duration_seconds: float | None = None
    if db_path is not None:
        started_at, duration_seconds = _read_recording_meta(db_path)

    return {
        "ok": True,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "video_path": str(video_path.resolve()),
        "events_path": str(events_path.resolve()),
        "started_at": started_at,
        "duration_seconds": duration_seconds,
        "video_pixfmt_remediated": remediated,
    }
