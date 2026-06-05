"""Prepare a recording for native (SwiftUI) review.

The SwiftUI review window's preparation step calls
``screencap review-data --json <name>`` and consumes the returned paths.
This module owns the orchestration; all video processing runs in-process
via PyAV (``screencap.engine.video``), so native review works on a machine
with no ``ffmpeg``/``ffprobe`` on PATH — the Finder/Launchpad-launched
``.app`` gets the minimal GUI PATH and cannot reach a brew-installed
binary anyway (plan SCR-97).

The window must review *exactly what ``screencap upload`` ships*, so this
runs the scrub ahead of the consent decision and points the review at the
scrubbed copy's own files (reviewed == uploaded):

1. Local video (R15) — ``_ensure_single_video`` (idempotent in-process PyAV
   concat of ``chunk_*.mp4`` into ``video.mp4``, ``fail_loud=True``) then
   ``remediate_pixfmt_for_review`` (the recorder writes ``yuv444p`` which
   AVKit rejects; re-encode once into a sibling ``.video_review.mp4``). The
   video is a *local* navigation aid that never uploads, so it is prepared
   from and left at the ORIGINAL recording dir.
2. Canonical events export (``_export_canonical_events``) into the source
   dir with the upload config (``exclude_moves=False``), so the scrubbed
   copy carries the exact event set upload ships.
3. Cloud-bound recovery → scrub (``_prepare_scrubbed_copy``), mirroring the
   upload loop's load-bearing ``_recover_chunk_metadata(cloud_bound=True)``
   → ``scrub_recording`` ordering. The envelope's event + screenshot paths
   then resolve to the scrubbed copy's actual file set.

Returns a JSON-serializable dict the downstream consumers (the SwiftUI
shell, tests) decode. Failures raise ``ReviewPrepareError`` in structurally
distinct flavors — invalid name, missing recording, "can't process this
video" (PyAV decode), "could not prepare review events" (export/recovery),
and "could not prepare a safe version for review" (total scrub failure) —
never a missing-binary ("install ffmpeg") message (R9). A total scrub
failure leaves no reusable scrubbed dir.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from screencap.scrubber import ScrubResult

# Progress/status goes to STDERR; stdout is reserved for the JSON envelope the
# SwiftUI shell parses. The scrubber's own module console resolves ``sys.stdout``
# dynamically, so the scrub call below is additionally wrapped in
# ``redirect_stdout(sys.stderr)`` to keep stdout clean.
console = Console(stderr=True)

# Bumped to 2 in U3: the envelope gained additive, optional redaction-evidence
# and coverage fields plus the scrubbed screenshot set. All new fields are
# nullable/empty-representable; Swift decodes them with safe defaults and never
# gates readiness on them (see the nullable-timing learning).
REVIEW_SCHEMA_VERSION = 2


class ReviewPrepareError(RuntimeError):
    """A non-recoverable failure during review-data preparation."""


def _export_canonical_events(rec_dir: Path) -> None:
    """Export the canonical combined ``events.jsonl`` into the *source* dir
    before scrub, so the scrubbed copy contains the exact event set
    ``screencap upload`` ships (reviewed == uploaded).

    Delegates to ``exporter.ensure_canonical_events`` — the single shared gate +
    config (skip when chunked, ``exclude_moves=False``, ``include_network`` off)
    so the review and upload export paths cannot drift. Review has no
    ``--force``, so an existing ``events.jsonl`` is trusted as-is.
    """
    # Deferred import — exporter.py pulls engine modules that are heavier
    # than the `screencap --help` path tolerates.
    from screencap.exporter import ensure_canonical_events

    ensure_canonical_events(rec_dir)


def _resolve_scrubbed_event_files(scrubbed_dir: Path) -> list[Path]:
    """Resolve the scrubbed dir's actual event file set — the bytes that ship.

    Per-chunk ``events_*.jsonl`` when present (what a chunked recording
    uploads), otherwise the combined ``events.jsonl``. Files renamed
    ``*.scrub_failed`` are intentionally excluded — they are ineligible for
    upload, so the review must not point at them either.
    """
    chunk_files = sorted(scrubbed_dir.glob("events_*.jsonl"))
    if chunk_files:
        return chunk_files
    combined = scrubbed_dir / "events.jsonl"
    return [combined] if combined.exists() else []


def _build_redaction_evidence(scrub_result: ScrubResult) -> dict:
    """Build the export-safe redaction-evidence payload from a ``ScrubResult``.

    Two levels of evidence (R8) plus risky-moment + fail-closed data (R13/R14),
    all category/timestamp only — never a redacted value:

    - ``summary``: entity type → count (the per-recording "removed/protected"
      tally).
    - ``markers``: per-moment redaction markers ``{t, category}`` at audit
      timestamps.
    - ``blocked_intervals``: risky-moment intervals ``{start, end, action,
      reason}`` (``end`` null when open-ended).
    - ``fail_closed``: ``{t, surface}`` markers for content the scrubber could
      not analyze and removed to be safe.
    """
    # Reuse the scrubber's serializer so the envelope's intervals match the
    # on-disk privacy_audit.json byte-for-byte (inf end → null).
    from screencap.privacy.actions import PrivacyAction
    from screencap.scrubber import _blocked_interval_to_dict

    # Per-moment markers fire only where something was actually redacted/masked.
    # mask_screenshots emits an AuditEntry for EVERY frame it processes —
    # including clean ALLOW frames — so mapping all entries would draw a
    # "redaction" tick at essentially every screenshot, over-reporting the R8
    # evidence. Exclude the non-redacting ALLOW action from the marker channel.
    _ALLOW = PrivacyAction.ALLOW.value
    return {
        "summary": dict(scrub_result.entity_counts),
        "markers": [
            {"t": e.timestamp, "category": e.reason}
            for e in scrub_result.audit_entries
            if e.action != _ALLOW
        ],
        "blocked_intervals": [
            _blocked_interval_to_dict(iv) for iv in scrub_result.blocked_intervals
        ],
        "fail_closed": [
            {"t": m["timestamp"], "surface": m.get("surface", "")}
            for m in scrub_result.fail_closed_redactions
        ],
    }


def _build_coverage(scrubbed_dir: Path, screenshots: list[str]) -> dict:
    """Structured R9 coverage facts the UI renders honest copy from.

    Not hardcoded UI strings — booleans the transparency layer maps to copy,
    so the disclosure can emphasize the one fact requiring operator action
    (allowed-app on-screen PII in screenshots is not auto-redacted) over the
    benign ones (video/audio never upload; transcript uploads scrubbed).
    """
    has_transcript = (
        any(scrubbed_dir.glob("transcript*.json"))
        or any(scrubbed_dir.glob("transcript*.txt"))
    )
    return {
        "video_local_only": True,
        "audio_local_only": True,
        "transcript_uploaded_scrubbed": has_transcript,
        "screenshots_uploaded": bool(screenshots),
        # The blind spot: on-screen PII inside allowed apps is the operator's
        # to verify — the scrubber masks blocked apps, not content within
        # allowed ones.
        "allowed_app_screenshot_pii_manual_review": True,
    }


def _assert_within_recordings_root(path: Path, recordings_root: Path) -> None:
    """Defense-in-depth: refuse to emit a path that is not a strict descendant
    of the recordings root, even after ``resolve_recording_dir`` validated the
    name (guards a scrubbed-dir path that somehow escaped via a symlink)."""
    resolved = path.resolve()
    if recordings_root.resolve() not in resolved.parents:
        raise ReviewPrepareError(
            "prepared review directory is outside the recordings root"
        )


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
    from screencap.config import get_recordings_dir, resolve_recording_dir
    from screencap.engine.video import remediate_pixfmt_for_review
    from screencap.viewer import _ensure_single_video

    try:
        rec_dir = resolve_recording_dir(name)
    except ValueError as e:
        # Invalid / path-traversal name — distinct from a decode failure
        # (no "can't process this video" prefix, no missing-binary text).
        # Raised before any scrub or path emission (R11 — a bad request
        # touches nothing).
        raise ReviewPrepareError(str(e)) from e

    if not rec_dir.exists():
        raise ReviewPrepareError(f"Recording not found: {name}")

    # Video pipeline (R15): concat chunks → remediate pixel format, all
    # in-process via PyAV, operating on the ORIGINAL recording dir. The video
    # is a local navigation aid that never uploads, so it is prepared from and
    # left at the original — not the scrubbed copy. A genuine PyAV
    # decode/process failure becomes the structural "can't process this video"
    # state (R9) — never a missing-binary error, and distinct from the
    # resolve/missing/no-video errors. The caught set spans
    # RuntimeError/ValueError (the engine's documented failures) plus OSError
    # (PyAV's av.error.OSError family and os.replace/mux write errors), so no
    # failure mode escapes as a raw traceback past the envelope.
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

    # Scrub-before-review (R1/R2/R7): prepare the exact post-hoc upload payload
    # — masked screenshots + scrubbed events/DB/transcript — so the bytes
    # reviewed are the bytes uploaded. All status/progress is forced to stderr
    # (the scrubber prints to its own stdout-bound console); stdout stays the
    # JSON envelope only. The ScrubResult is the redaction-evidence source.
    from screencap.scrubber import recording_scrub_lock

    recordings_root = get_recordings_dir()

    # Hold the per-recording scrub lock across the ENTIRE prepare critical
    # section — export → recovery → scrub AND the scrubbed-dir read-back — so a
    # concurrent re-scrub (a second review window, or `screencap upload`) can't
    # mutate/delete the <name>-scrubbed dir between the scrub and the path
    # resolution, yielding a torn read or a spurious failure (todo 005/006).
    # _prepare_scrubbed_copy scrubs with _already_locked=True (we hold the lock;
    # scrub_recording must not re-acquire it — a same-process flock would
    # deadlock). The in-memory redaction evidence + the captured path lists are
    # then assembled into the envelope after the lock is released.
    with recording_scrub_lock(name):
        scrub_result = _prepare_scrubbed_copy(name, rec_dir, _already_locked=True)
        scrubbed_dir = scrub_result.output_dir

        # Defense-in-depth path containment before emitting any scrubbed path.
        _assert_within_recordings_root(scrubbed_dir, recordings_root)

        # Resolve the event source to the scrubbed dir's ACTUAL file set
        # (per-chunk when chunked — what ships). Faithfulness by construction:
        # the review reads the same files the scrubbed dir contains.
        event_files = _resolve_scrubbed_event_files(scrubbed_dir)
        if not event_files:
            # No event files survived (e.g. all were fail-closed deleted during
            # scrub). Surface a clean, honest failure rather than an ok:true
            # envelope with a null events_path — the latter would trip the Swift
            # readiness guard into a generic "Failed to prepare recording."
            raise ReviewPrepareError(
                f"could not prepare review events: no reviewable events for {name}"
            )
        events_paths = [str(p.resolve()) for p in event_files]

        # Scrubbed (masked) screenshots — the "what actually uploads" visual (R15).
        scrubbed_shots_dir = scrubbed_dir / "screenshots"
        screenshots = (
            [str(p.resolve()) for p in sorted(scrubbed_shots_dir.glob("*.jpg"))]
            if scrubbed_shots_dir.is_dir()
            else []
        )

        # Evidence built while still holding the lock — _build_coverage globs the
        # scrubbed dir for transcripts; _build_redaction_evidence is in-memory.
        redaction = _build_redaction_evidence(scrub_result)
        coverage = _build_coverage(scrubbed_dir, screenshots)

    # Timing metadata for the timeline pane's coordinate space, read from the
    # original DB (scrubbing nulls content, not timestamps). Nullable — see the
    # docstring; a playable recording may have no action events.
    db_path = find_db(rec_dir)
    started_at: float | None = None
    duration_seconds: float | None = None
    if db_path is not None:
        started_at, duration_seconds = _read_recording_meta(db_path)

    return {
        "ok": True,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "video_path": str(video_path.resolve()),
        # `events_path` is the primary scrubbed events file (events_paths[0]),
        # kept for single-file consumers; `events_paths` is the authoritative
        # set the review parses.
        "events_path": events_paths[0] if events_paths else None,
        "events_paths": events_paths,
        "screenshots": screenshots,
        # Additive, optional evidence (U3) — empty-representable so a recording
        # with no redactions still yields a valid ok:true envelope.
        "redaction": redaction,
        "coverage": coverage,
        "started_at": started_at,
        "duration_seconds": duration_seconds,
        "video_pixfmt_remediated": remediated,
    }


def _prepare_scrubbed_copy(
    name: str, rec_dir: Path, *, _already_locked: bool = False,
) -> "ScrubResult":
    """Run canonical export → cloud-bound recovery → scrub, returning the
    ``ScrubResult`` (its ``output_dir`` is the scrubbed dir; the rest is the
    redaction-evidence source). A total scrub failure is a structural
    preparation failure (``ReviewPrepareError``) leaving no reusable
    (sentinel'd) dir behind.

    The recovery → scrub ordering mirrors the upload loop's load-bearing
    sequence so the prepared dir is upload-equivalent (the scrub-layer pointer
    suppression only protects recovered cloud-bound JSONL when this holds).

    ``_already_locked`` is threaded to ``scrub_recording`` — the caller
    (``prepare_review_data``) holds ``recording_scrub_lock`` across this whole
    call plus the read-back, so the scrub must not re-acquire it.
    """
    from screencap.scrubber import scrub_recording

    # Canonical pre-scrub export into the source dir (config-matched to upload).
    # Catch broadly: export_recording can raise sqlite3.Error / ValueError from
    # a corrupt recording.db in addition to ExportError / OSError. Any of these
    # must become a clean ReviewPrepareError — an uncaught traceback on stdout
    # would corrupt the JSON channel the SwiftUI shell parses. (Mirrors the
    # broad catch on the recovery + scrub sub-steps below.)
    try:
        _export_canonical_events(rec_dir)
    except Exception as e:
        raise ReviewPrepareError(f"could not prepare review events: {e}") from e

    # All sub-steps print progress; force stdout → stderr so the envelope on
    # real stdout stays clean even though the scrubber's console is stdout-bound.
    with contextlib.redirect_stdout(sys.stderr):
        # Cloud-bound recovery, mirroring the upload loop (cloud_bound=True is
        # REQUIRED on the call — a forgotten arg is a TypeError, not fail-open).
        # Deferred import of the shared recovery leaf module (no longer reaching
        # into cli, which imports this module — the old cycle is gone).
        from screencap.recovery import _recover_chunk_metadata

        try:
            _recover_chunk_metadata(rec_dir, console, force=False, cloud_bound=True)
        except Exception as e:
            raise ReviewPrepareError(f"could not prepare review events: {e}") from e

        # Lazy scrub. Any failure here is structural: no reusable scrubbed dir
        # (no completion sentinel) — distinct from per-field fail-closed (data).
        try:
            # cloud_bound_recovery=True: recovery ran just above, so the
            # completion sentinel records it and `screencap upload` can reuse
            # this exact dir (reviewed == uploaded) instead of re-scrubbing.
            scrub_result = scrub_recording(
                name, cloud_bound_recovery=True, _already_locked=_already_locked,
            )
        except Exception as e:
            raise ReviewPrepareError(
                f"could not prepare a safe version for review: {e}"
            ) from e

    return scrub_result
