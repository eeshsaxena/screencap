"""Prepare a recording for native (SwiftUI) review.

The SwiftUI review window's preparation step calls
``screencap review-data --json <name>`` and consumes the returned paths
to a single playable video and an ``events.jsonl`` file. This module
owns the orchestration; all video processing runs in-process via PyAV
(``screencap.engine.video``), so native review works on a machine with
no ``ffmpeg``/``ffprobe`` on PATH — the Finder/Launchpad-launched
``.app`` gets the minimal GUI PATH and cannot reach a brew-installed
binary anyway (plan SCR-97, U4).

1. ``_ensure_single_video`` (from viewer.py) — idempotent in-process
   PyAV concat of ``chunk_*.mp4`` into ``video.mp4`` when needed. Here
   it runs ``fail_loud=True`` so a genuine merge failure surfaces as the
   structural failure state rather than being swallowed.
2. ``remediate_pixfmt_for_review`` (from engine/video.py) — the recorder
   writes H.264 with ``yuv444p``, which AVKit's hardware decoder rejects
   on most Macs. When the source is not an AVKit-safe 4:2:0 format, the
   engine re-encodes once (libx264, yuv420p) into a sibling
   ``.video_review.mp4``. The leading dot keeps it out of ``screencap
   upload`` (dotfile filter) and its existence is the idempotency gate,
   so the re-encode runs at most once per recording. The original
   ``video.mp4`` is unchanged — upload still ships it as-is to preserve
   the lossless training corpus.
3. ``events.jsonl`` — auto-export via ``exporter.export_recording`` if
   absent, with ``exclude_moves=True`` so the timeline pane only sees
   discrete events (mouse.click, key.type, window.switch, …) rather
   than the noisy mouse-move trail.

Returns a JSON-serializable dict with the envelope downstream consumers
(the SwiftUI shell, tests) decode. A genuine PyAV decode/process failure
raises ``ReviewPrepareError`` with a "can't process this video" message —
structurally distinct from a missing-recording or path-traversal error,
and never a missing-binary ("install ffmpeg") message (R9).
"""

from __future__ import annotations

from pathlib import Path

REVIEW_SCHEMA_VERSION = 1


class ReviewPrepareError(RuntimeError):
    """A non-recoverable failure during review-data preparation."""


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
    for non-recoverable failures, in structurally distinct flavors so the
    SwiftUI shell can tell them apart: a missing recording / invalid name
    (resolved before any video work), a recording that carries no video to
    review, a genuine "can't process this video" PyAV decode/process
    failure, and a failure to prepare the events timeline. The CLI wrapper
    translates any of these into the standard ``{"ok": false, "error":
    ...}`` envelope with a non-zero exit code — never a raw traceback.

    ``started_at`` and ``duration_seconds`` come from
    ``catalog._read_recording_meta``, which returns ``None`` on a DB read
    failure, a missing timestamp, or a recording with no action events.
    They are serialized as JSON ``null`` in that case — a perfectly
    playable recording can still carry null metadata, so the Swift side
    decodes them as ``Double?`` (upstream plan U6/U8).
    """
    from screencap.catalog import _read_recording_meta, find_db
    from screencap.config import resolve_recording_dir
    from screencap.engine.video import remediate_pixfmt_for_review
    from screencap.exporter import ExportError
    from screencap.viewer import _ensure_single_video

    try:
        rec_dir = resolve_recording_dir(name)
    except ValueError as e:
        # Invalid / path-traversal name — distinct from a decode failure
        # (no "can't process this video" prefix, no missing-binary text).
        raise ReviewPrepareError(str(e)) from e

    if not rec_dir.exists():
        raise ReviewPrepareError(f"Recording not found: {name}")

    # Video pipeline (R1, R4, R5): concat chunks → remediate pixel format, all
    # in-process via PyAV. A genuine PyAV decode/process failure becomes the
    # structural "can't process this video" state (R9) — never a missing-binary
    # error, and distinct from the resolve/missing/no-video errors. The caught
    # set spans RuntimeError/ValueError (the engine's documented failures) plus
    # OSError (PyAV's av.error.OSError family and os.replace/mux write errors),
    # so no failure mode escapes as a raw traceback past the envelope.
    try:
        # fail_loud=True: a chunk-concat failure must propagate here (the
        # HTML viewer swallows it; review-data needs correctness).
        _ensure_single_video(rec_dir, fail_loud=True)
    except (RuntimeError, ValueError, OSError) as e:
        raise ReviewPrepareError(f"can't process this video: {e}") from e

    if not (rec_dir / "video.mp4").exists():
        # No chunks and no merged/symlinked video.mp4 — the recording carries
        # no video to review (video disabled, or an action-gated recording
        # where no action fired). Structurally distinct from both a missing
        # recording and a decode failure.
        raise ReviewPrepareError(f"Recording has no video to review: {name}")

    try:
        video_path, remediated = remediate_pixfmt_for_review(rec_dir)
    except (RuntimeError, ValueError, OSError) as e:
        raise ReviewPrepareError(f"can't process this video: {e}") from e

    # events.jsonl (auto-export if missing, filtered to discrete events). A
    # failed export (e.g. a missing/corrupt recording.db) becomes a clean error
    # envelope rather than a raw traceback, upholding the command's contract.
    try:
        events_path = _ensure_events_jsonl(rec_dir)
    except (ExportError, OSError) as e:
        raise ReviewPrepareError(f"could not prepare review events: {e}") from e

    # Timing metadata for the timeline pane's coordinate space. Nullable —
    # see the docstring; a playable recording may have no action events.
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
