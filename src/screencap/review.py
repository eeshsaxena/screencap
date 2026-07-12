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
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rich.console import Console
from rich.markup import escape

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
#
# Bumped to 3 in SCR-166: added the additive ``timing_status`` (ok|locked|
# corrupt) field. The pre-existing ``timing_error`` boolean is retained as a
# back-compat alias (True for both locked and corrupt), so an older Swift
# consumer that only reads ``timing_error`` keeps its current behavior.
#
# Bumped to 4 (captured-events summary): the inspect envelope gained the
# additive, optional ``blocked_intervals`` (capture-time EXCLUDE-only "not
# captured" spans) and ``protected_intervals`` (the full ``SCRUB_BLOCK_ACTIONS``
# set the in-viewer digest suppresses). Both are empty-representable and
# inspect-only; the shared constant also stamps ``prepare_review_data``'s
# envelope, which gains no new field — harmless, since no consumer hard-gates on
# the value and the Swift decoder ignores unknown keys.
REVIEW_SCHEMA_VERSION = 4


class ReviewPrepareError(RuntimeError):
    """A non-recoverable failure during review-data preparation."""


class ReviewPrepareBusy(ReviewPrepareError):
    """A *transient* failure: the recording is still being finalized.

    Raised when the local navigation video can't be materialized because the
    terminal stage holds the per-recording ``terminal_lock`` (post-stop
    finalization). Unlike a plain ``ReviewPrepareError`` (a genuine "can't
    process this video" corruption), this resolves on its own once finalization
    completes — the CLI marks the envelope ``retryable`` so the caller retries
    instead of surfacing a scary "could not load" failure. A subclass so every
    existing ``except ReviewPrepareError`` site still catches it.
    """


# The read-only inspect path waits only briefly for the ``terminal_lock``: a
# view opened right after stop races the (usually sub-second) finalization, so
# failing fast + retrying beats blocking on the full concat/eviction ceiling.
_INSPECT_LOCK_TIMEOUT = 5.0


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


def _resolve_event_files(rec_dir: Path) -> list[Path]:
    """Resolve a recording dir's actual event file set.

    Dir-agnostic: works on the scrubbed copy (review — the bytes that ship) and
    on the original dir (inspect — the local events to look at). Per-chunk
    ``events_*.jsonl`` when present (what a chunked recording carries), otherwise
    the combined ``events.jsonl``. Files renamed ``*.scrub_failed`` are
    intentionally excluded — they are ineligible for upload, so the review must
    not point at them either (a no-op on the original dir, which has none).
    """
    chunk_files = sorted(rec_dir.glob("events_*.jsonl"))
    if chunk_files:
        return chunk_files
    combined = rec_dir / "events.jsonl"
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


def _prepare_recording_video(
    name: str, *, lock_timeout: float | None = None
) -> tuple[Path, Path, bool]:
    """Shared local-video preparation for both ``review-data`` and
    ``inspect-data``: resolve the recording dir and prepare the local
    navigation video (concat ``chunk_*.mp4`` → remediate ``yuv444p`` for AVKit),
    all in-process via PyAV on the ORIGINAL recording dir.

    Returns ``(rec_dir, video_path, remediated)``. Raises ``ReviewPrepareError``
    in the structurally distinct flavors the SwiftUI shell tells apart: an
    invalid / path-traversal name, a missing recording, a recording carrying no
    video to review, and a genuine "can't process this video" PyAV failure
    (never a missing-binary message — R9). A ``ReviewPrepareBusy`` (a subclass)
    is raised instead when the ``terminal_lock`` is held by an in-flight
    finalization — a *transient* "still finalizing" condition the caller retries,
    NOT a corruption. The video is a *local* navigation aid that never uploads,
    so it is prepared from and left at the original dir.

    ``lock_timeout`` (seconds) bounds the wait for ``_ensure_single_video``'s
    ``terminal_lock``; ``None`` keeps the viewer/concat default. The inspect path
    passes a short value so a view opened DURING post-stop finalization fails
    fast as ``ReviewPrepareBusy`` (retried) rather than blocking on the lock.
    """
    from screencap.config import get_recordings_dir, resolve_recording_dir
    from screencap.engine.video import remediate_pixfmt_for_review
    from screencap.terminal_stage import TerminalStageBusy
    from screencap.viewer import _ensure_single_video

    try:
        rec_dir = resolve_recording_dir(name)
    except ValueError as e:
        # Invalid / path-traversal name — distinct from a decode failure and
        # raised before any path emission (a bad request touches nothing).
        raise ReviewPrepareError(str(e)) from e

    if not rec_dir.exists():
        raise ReviewPrepareError(f"Recording not found: {name}")

    # fail_loud=True: a chunk-concat failure must propagate (the HTML viewer
    # swallows it; review/inspect need correctness). The caught set spans
    # RuntimeError/ValueError (the engine's documented failures) plus OSError
    # (PyAV's av.error.OSError family and os.replace/mux write errors), so no
    # failure mode escapes as a raw traceback past the envelope.
    #
    # TerminalStageBusy is caught FIRST (it is a RuntimeError, so the generic
    # arm below would otherwise mislabel a still-finalizing recording as
    # "can't process this video" corruption): re-raise it as the transient,
    # retryable ReviewPrepareBusy so the caller waits it out instead.
    lock_kw = {} if lock_timeout is None else {"lock_timeout": lock_timeout}
    try:
        _ensure_single_video(rec_dir, fail_loud=True, **lock_kw)
    except TerminalStageBusy as e:
        raise ReviewPrepareBusy(
            "Recording is still finalizing — try again in a moment."
        ) from e
    except (RuntimeError, ValueError, OSError) as e:
        raise ReviewPrepareError(f"can't process this video: {e}") from e

    if not (rec_dir / "video.mp4").exists():
        # No chunks and no merged/symlinked video.mp4 — the recording carries no
        # video to review (video disabled, or an action-gated recording where no
        # action fired). Structurally distinct from missing/decode failures.
        raise ReviewPrepareError(f"Recording has no video to review: {name}")

    try:
        video_path, remediated = remediate_pixfmt_for_review(rec_dir)
    except (RuntimeError, ValueError, OSError) as e:
        raise ReviewPrepareError(f"can't process this video: {e}") from e

    # Defense-in-depth path containment, applied once here so BOTH review-data
    # and inspect-data inherit it (SCR-189): refuse to emit a video_path that
    # escapes the recordings root via a symlink inside the recording dir — the
    # AVKit player would otherwise be handed an out-of-tree absolute path. Runs
    # before any scrub, so review-data fails fast on an escape.
    _assert_within_recordings_root(video_path, get_recordings_dir())

    return rec_dir, video_path, remediated


def _read_recording_timing(
    rec_dir: Path,
) -> tuple[float | None, float | None, Literal["ok", "locked", "corrupt"]]:
    """Shared timing read for the timeline pane's coordinate space, from the
    original ``recording.db`` (scrubbing nulls content, not timestamps).

    Returns ``(started_at, duration_seconds, timing_status)``. Both timing
    values are nullable — a playable recording may have no action events
    (``_read_recording_meta`` returns ``None``). ``timing_status`` (SCR-166)
    disambiguates the null three ways: ``"ok"`` (clean read, including a benign
    event-free recording or an absent DB), ``"locked"`` (a transient lock that
    resolves once the writer releases), or ``"corrupt"`` (an unreadable DB).
    """
    from screencap.catalog import _read_recording_meta, find_db

    db_path = find_db(rec_dir)
    started_at: float | None = None
    duration_seconds: float | None = None
    timing_status: Literal["ok", "locked", "corrupt"] = "ok"
    if db_path is not None:
        started_at, duration_seconds, timing_status = _read_recording_meta(db_path)
    return started_at, duration_seconds, timing_status


def _collapse_intervals_ms(
    intervals: list, win_start: float, win_end: float
) -> list[dict[str, int]]:
    """Clip ``BlockedInterval``s to ``[win_start, win_end)``, collapse overlaps,
    and round to integer ms.

    A clean, non-double-counting set for the summary's count + total-span
    reassurance line. Merges in float space before rounding (mirrors the
    ``day_segments`` ms rounding) so overlapping same-app spans don't inflate the
    "not captured" total.
    """
    clipped: list[tuple[float, float]] = []
    for iv in intervals:
        lo = max(iv.start, win_start)
        hi = min(iv.end, win_end)
        if hi > lo:
            clipped.append((lo, hi))
    clipped.sort()

    merged: list[list[float]] = []
    for lo, hi in clipped:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [
        {"start_ms": int(round(lo * 1000)), "end_ms": int(round(hi * 1000))}
        for lo, hi in merged
    ]


def _read_inspect_blocked_intervals(
    rec_dir: Path, started_at: float | None, duration_seconds: float | None,
) -> tuple[list[dict[str, int]], list[dict[str, int]]]:
    """Return ``(blocked_intervals, protected_intervals)`` for the inspect summary.

    ``blocked_intervals`` — capture-time **EXCLUDE**-only spans (an app fully
    excluded from screenshot capture). Secure-field spans are EXCLUDE-tagged in
    the interval model but leave the screenshot captured (they null keystrokes,
    not frames), so they are omitted here: this is the honest "provably not
    captured" set the summary's blocked line shows.

    ``protected_intervals`` — the full ``SCRUB_BLOCK_ACTIONS`` set plus the
    fail-closed residuals (EXCLUDE, MASK_WINDOW/REGION, TEXT_REDACT/OCR, coverage
    gaps, NULL-column ambiguity). The in-viewer digest drops every event inside
    these before counting/grouping, matching the repo's ALLOW-only index
    discipline so a masked app's window title is never surfaced.
    ``blocked_intervals`` is a subset of ``protected_intervals``.

    Reuses the day-timeline derivation over the INTACT local ``recording.db`` — a
    pure on-disk read, no daemon (KTD2). Strictly fail-open: a derivation error, a
    missing DB, or a recording with no resolvable time window (nullable timing —
    the video-only / event-free case) yields empty lists, matching
    ``day_segments``' read-surface posture.
    """
    if started_at is None:
        return [], []

    from screencap.catalog import find_db

    db_path = find_db(rec_dir)
    if db_path is None:
        return [], []

    win_start = float(started_at)
    win_end = win_start + float(duration_seconds or 0.0)
    if win_end <= win_start:
        return [], []

    from screencap.backfill.skip_intervals import (
        build_classifier_evaluator,
        derive_skip_intervals,
    )
    from screencap.privacy.actions import PrivacyAction
    from screencap.privacy.reasons import ReasonCode
    from screencap.scrubber import build_scrub_context

    try:
        classifier, evaluator = build_classifier_evaluator(rec_dir)
        # Canonical, ACTIONED intervals — one per window event, each tagged with
        # its real action (build_blocked_intervals never collapses actions;
        # merge_intervals only concatenates + sorts). The EXCLUDE subset, minus
        # secure-field (which blocks keystrokes, not the screenshot), is the
        # honest "not captured" set.
        ctx = build_scrub_context(
            db_path, evaluator, classifier, time_range=(win_start, win_end),
        )
        excluded = [
            iv
            for iv in ctx.blocked_intervals
            if iv.action == PrivacyAction.EXCLUDE
            and iv.reason != ReasonCode.SECURE_FIELD_DETECTED
        ]
        # Full protected + fail-closed residual set for the digest suppression.
        protected = derive_skip_intervals(
            db_path,
            classifier=classifier,
            evaluator=evaluator,
            time_range=(win_start, win_end),
            require_canonical=False,  # fail-open read surface
        )
    except Exception:
        console.print(
            "[yellow]inspect-data: blocked-interval derivation unavailable[/yellow]"
        )
        return [], []

    return (
        _collapse_intervals_ms(excluded, win_start, win_end),
        _collapse_intervals_ms(protected, win_start, win_end),
    )


# ---------------------------------------------------------------------------
# Clip-range scoping (SCR-219, U4)
#
# The Day timeline's clip range arrives as epoch **milliseconds**
# (``currentDayMs`` / ``pendingSeekMs`` in Swift are epoch-seconds × 1000),
# while the recording DB's action/window timestamps and the flat
# ``screenshots/{ts}.jpg`` names are epoch **seconds**. ``_resolve_clip_range``
# is the single conversion seam: ``None`` (whole-recording) or ``(start_s,
# end_s)`` in the DB/screenshot space.
# ---------------------------------------------------------------------------


def _resolve_clip_range(
    clip_start_ms: int | None, clip_end_ms: int | None
) -> tuple[float, float] | None:
    """Normalize the optional clip bounds to ``(start_seconds, end_seconds)``.

    Both bounds present → the half-open ``[start, end)`` window in epoch
    seconds (the DB/screenshot space). Both absent → ``None`` (whole-recording,
    today's behavior unchanged). Exactly one present is a caller contract error
    — a partial range would silently fall back to a whole-recording review,
    exactly the "reviews more than the user meant to clip" leak R7 forbids — so
    it raises ``ReviewPrepareError``.
    """
    if clip_start_ms is None and clip_end_ms is None:
        return None
    if clip_start_ms is None or clip_end_ms is None:
        raise ReviewPrepareError(
            "clip range requires both clip_start_ms and clip_end_ms"
        )
    return (clip_start_ms / 1000.0, clip_end_ms / 1000.0)


def _resolve_screenshots(
    scrubbed_shots_dir: Path, clip_range: tuple[float, float] | None
) -> list[str]:
    """Resolve the masked-screenshot truth-set, filtered to the clip range.

    Whole-recording (``clip_range is None``) returns every masked
    ``screenshots/*.jpg`` — byte-for-byte today's behavior. A clip range keeps
    only the frames whose timestamp lies in ``[start, end)`` (frames are named
    ``{epoch_seconds}.jpg``); an unparseable name is dropped from a scoped set
    (it cannot be proven in-range). An empty / absent dir yields ``[]``.
    """
    if not scrubbed_shots_dir.is_dir():
        return []
    paths = sorted(scrubbed_shots_dir.glob("*.jpg"))
    if clip_range is not None:
        from screencap.redaction.geometry import parse_screenshot_timestamp

        start_s, end_s = clip_range
        paths = [
            p
            for p in paths
            if (ts := parse_screenshot_timestamp(p.name)) is not None
            and start_s <= ts < end_s
        ]
    return [str(p.resolve()) for p in paths]


def _scoped_clip_events(
    scrubbed_dir: Path, rec_dir: Path, clip_range: tuple[float, float]
) -> list[str]:
    """Export the scrubbed events for ``[start, end)`` into a scoped JSONL.

    Slices the SCRUBBED copy's ``recording.db`` via ``export_chunk_events``'s
    half-open ``[start, end)`` selection (so the reviewed events are the exact
    scrubbed set the upload path ships, just range-scoped) and writes them to a
    dot-prefixed ``.clip_review_events.jsonl`` inside the scrubbed dir. The
    dot-prefix keeps the scoped file out of both the ``events_*.jsonl`` glob a
    later whole-recording review resolves and the upload dotfile filter.

    Passes the sanctioned cloud window filter (``build_cloud_window_filter``,
    ``cloud_bound=True`` → forces PUBLIC) belt-and-braces over the
    already-scrubbed DB, satisfying the export-callsite privacy guard.
    """
    from screencap.enforcement.window_filter import build_cloud_window_filter
    from screencap.export import export_chunk_events
    from screencap.exporter import build_export_metadata, write_events_jsonl

    start_s, end_s = clip_range
    events = export_chunk_events(
        scrubbed_dir,
        start_s,
        end_s,
        window_filter=build_cloud_window_filter(
            cloud_bound=True, capture_dir=rec_dir,
        ),
        materialized=True,
    )
    out_path = scrubbed_dir / ".clip_review_events.jsonl"
    write_events_jsonl(out_path, events, build_export_metadata(exclude_moves=False))
    return [str(out_path.resolve())]


def _redaction_evidence_from_disk(scrubbed_dir: Path) -> dict:
    """Rebuild the export-safe redaction evidence from the on-disk audit log.

    Used only on the cache-reuse path, where no fresh ``ScrubResult`` is
    produced. ``privacy_audit.json`` persists the per-decision ``entries``, the
    ``blocked_intervals``, and the ``fail_closed`` markers in the SAME
    export-safe shape ``_build_redaction_evidence`` emits, so the review keeps
    its markers/intervals across a reuse. The per-entity ``summary`` (the
    ``entity_counts`` tally) is NOT persisted in the audit log, so it degrades
    to ``{}`` on reuse — an accepted, non-gating loss (the summary is optional
    UI evidence; the consent set is the screenshots + in-range events). An
    absent/unreadable audit log yields empty, matching a clean recording.
    """
    empty = {"summary": {}, "markers": [], "blocked_intervals": [], "fail_closed": []}
    audit_path = scrubbed_dir / "privacy_audit.json"
    if not audit_path.exists():
        return empty
    try:
        data = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty

    from screencap.privacy.actions import PrivacyAction

    allow = PrivacyAction.ALLOW.value
    entries = data.get("entries") or []
    return {
        "summary": {},
        "markers": [
            {"t": e.get("timestamp"), "category": e.get("reason", "")}
            for e in entries
            if e.get("action") != allow
        ],
        "blocked_intervals": data.get("blocked_intervals") or [],
        "fail_closed": [
            {"t": m.get("timestamp"), "surface": m.get("surface", "")}
            for m in (data.get("fail_closed") or [])
        ],
    }


def prepare_review_data(
    name: str,
    *,
    clip_start_ms: int | None = None,
    clip_end_ms: int | None = None,
) -> dict:
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
    decodes them as ``Double?`` (upstream plan U6/U8). The companion
    ``timing_status`` (SCR-166) disambiguates the null three ways — ``"ok"``
    (clean read, including a benign event-free recording), ``"locked"`` (a
    transient lock that resolves once the writer releases), or ``"corrupt"``
    (an unreadable ``recording.db``) — so the consumer can tell a temporary
    lock from corruption from an event-free recording. ``timing_error``
    (SCR-107) is kept as the back-compat boolean alias: ``True`` for both
    non-``"ok"`` states.

    ``clip_start_ms`` / ``clip_end_ms`` (SCR-219, U4) optionally scope the
    consent surface to a single clip's ``[start, end)`` range, in epoch
    milliseconds (the Day-timeline space; the DB/screenshot timestamps are
    epoch seconds, so they are divided by 1000). When both are present the
    masked-screenshot truth-set and the events are filtered to the range and an
    additive ``clip_video_capture_blocked_only: true`` honesty flag is emitted
    (the exported clip video is capture-blocked but its in-window text is not
    masked — the note the Swift consent surface renders). When both are absent
    the envelope is byte-for-byte the whole-recording shape (no new key, no
    filtering); passing exactly one is a contract error. The new field is
    additive/optional — a Swift consumer must ignore it, never gate readiness on
    it (see the review-data-nullable-timing learning).

    Independently of the clip range, a fresh existing ``<name>-scrubbed`` (a
    current ``.scrub_complete`` sentinel, reusable per the upload guard) is
    REUSED — the whole-recording scrub is skipped (KTD4, blunting the cost of
    re-scrubbing an N-hour recording per clip); its redaction evidence is then
    rebuilt from the on-disk audit log (the per-entity ``summary`` degrades to
    ``{}`` — it is not persisted — while markers/intervals survive).
    """
    from screencap.config import get_recordings_dir

    clip_range = _resolve_clip_range(clip_start_ms, clip_end_ms)

    # Resolve the recording dir + prepare the local navigation video (shared
    # with inspect-data via _prepare_recording_video).
    rec_dir, video_path, remediated = _prepare_recording_video(name)

    # Scrub-before-review (R1/R2/R7): prepare the exact post-hoc upload payload
    # — masked screenshots + scrubbed events/DB/transcript — so the bytes
    # reviewed are the bytes uploaded. All status/progress is forced to stderr
    # (the scrubber prints to its own stdout-bound console); stdout stays the
    # JSON envelope only. The ScrubResult is the redaction-evidence source.
    from screencap.scrubber import is_scrubbed_copy_reusable, recording_scrub_lock

    recordings_root = get_recordings_dir()
    # rec_dir is <root>/<name>; its sibling <name>-scrubbed is the cloud copy.
    scrubbed_dir = rec_dir.parent / f"{name}-scrubbed"

    # Hold the per-recording scrub lock across the ENTIRE prepare critical
    # section — reuse-check → export → recovery → scrub AND the scrubbed-dir
    # read-back — so a concurrent re-scrub (a second review window, or
    # `screencap upload`) can't mutate/delete the <name>-scrubbed dir between the
    # scrub and the path resolution, yielding a torn read or a spurious failure
    # (todo 005/006). _prepare_scrubbed_copy scrubs with _already_locked=True (we
    # hold the lock; scrub_recording must not re-acquire it — a same-process
    # flock would deadlock). The in-memory redaction evidence + the captured path
    # lists are then assembled into the envelope after the lock is released.
    with recording_scrub_lock(name):
        # Cache-reuse (KTD4): a fresh, current, upload-equivalent <name>-scrubbed
        # is reused as-is — skip the whole-recording scrub (the exact reuse guard
        # `screencap upload` and the terminal stage use). On reuse there is no
        # fresh ScrubResult, so the redaction evidence is rebuilt from the
        # on-disk audit log. Otherwise scrub as today.
        if is_scrubbed_copy_reusable(rec_dir, scrubbed_dir):
            console.print(
                f"  Reusing fresh scrubbed copy at "
                f"[dim]{scrubbed_dir.name}/[/dim] (skipping re-scrub)."
            )
            redaction = _redaction_evidence_from_disk(scrubbed_dir)
        else:
            scrub_result = _prepare_scrubbed_copy(name, rec_dir, _already_locked=True)
            scrubbed_dir = scrub_result.output_dir
            redaction = _build_redaction_evidence(scrub_result)

        # Defense-in-depth path containment before emitting any scrubbed path.
        _assert_within_recordings_root(scrubbed_dir, recordings_root)

        if clip_range is not None:
            # Clip-scoped consent (R7): the events are re-sliced from the scrubbed
            # copy to [start, end) via export_chunk_events' half-open selection —
            # nothing outside the clip reaches the consent set.
            events_paths = _scoped_clip_events(scrubbed_dir, rec_dir, clip_range)
        else:
            # Resolve the event source to the scrubbed dir's ACTUAL file set
            # (per-chunk when chunked — what ships). Faithfulness by construction:
            # the review reads the same files the scrubbed dir contains.
            event_files = _resolve_event_files(scrubbed_dir)
            if not event_files:
                # No event files survived (e.g. all were fail-closed deleted
                # during scrub). Surface a clean, honest failure rather than an
                # ok:true envelope with a null events_path — the latter would trip
                # the Swift readiness guard into a generic "Failed to prepare
                # recording." (A clip range is exempt: an empty in-range slice is
                # a legitimate empty consent set, handled above.)
                raise ReviewPrepareError(
                    f"could not prepare review events: no reviewable events for {name}"
                )
            events_paths = [str(p.resolve()) for p in event_files]

        # Scrubbed (masked) screenshots — the "what actually uploads" visual (R15),
        # filtered to the clip range when one is present.
        screenshots = _resolve_screenshots(scrubbed_dir / "screenshots", clip_range)

        # Evidence built while still holding the lock — _build_coverage globs the
        # scrubbed dir for transcripts.
        coverage = _build_coverage(scrubbed_dir, screenshots)

    # Timing metadata for the timeline pane's coordinate space, read from the
    # original DB (scrubbing nulls content, not timestamps). Nullable — see the
    # docstring; a playable recording may have no action events.
    #
    # `timing_status` (SCR-166) disambiguates the null three ways: "ok" (benign
    # event-free recording, or absent DB), "locked" (a transient lock held by an
    # active recording / concurrent writer — resolves on its own), or "corrupt"
    # (an unreadable recording.db). The Swift consumer renders a "temporarily
    # unavailable" advisory for a lock vs a "metadata couldn't be read" advisory
    # for corruption, instead of presenting either as a clean, event-free
    # review. `timing_error` (SCR-107) is kept as the back-compat boolean older
    # consumers read — True for both non-"ok" states.
    started_at, duration_seconds, timing_status = _read_recording_timing(rec_dir)

    envelope = {
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
        # SCR-166: tri-state "ok" | "locked" | "corrupt" — lets the consumer tell
        # a transient lock from corruption from a benign event-free recording.
        "timing_status": timing_status,
        # SCR-107 back-compat: True for both non-"ok" states, so an older
        # consumer that only reads this boolean still surfaces the advisory.
        "timing_error": timing_status != "ok",
        "video_pixfmt_remediated": remediated,
    }
    if clip_range is not None:
        # Additive, clip-only honesty flag (KTD4): the exported clip video is
        # capture-blocked (window-level) but its in-window on-screen text is NOT
        # masked — less redacted than these preview screenshots — and it leaves
        # to external recipients. Present (True) only for a clip; absent for a
        # whole-recording review so that envelope stays byte-for-byte unchanged.
        # Optional — the Swift consumer must not gate readiness on it.
        envelope["clip_video_capture_blocked_only"] = True
    return envelope


def prepare_inspect_data(name: str) -> dict:
    """Read-only LOCAL playback envelope — the no-scrub sibling of
    ``prepare_review_data`` for the native inspect window ("just looking").

    Same envelope *shape* the Swift decoder already understands, but sourced
    entirely from the ORIGINAL recording dir with **no scrub**: the local
    navigation video (shared core), the local events, and the timing coordinate
    space. Masking is an upload concept, so this never runs the scrubber, never
    acquires ``recording_scrub_lock``, never produces a ``<name>-scrubbed`` dir,
    and emits no redaction/coverage evidence — the looking surface opens in
    normal CLI latency, not the minutes a scrub can take.

    Events go through ``ensure_canonical_events`` exactly as the review/upload
    export does (``include_network`` off), so the inspect surface never shows
    network rows the upload path excludes. ``screenshots`` is empty — the
    inspect window is video-first and renders no masked-screenshot pane.

    Raises ``ReviewPrepareError`` in the same structurally distinct flavors as
    ``prepare_review_data`` for the video pipeline (invalid name / missing
    recording / no video / "can't process this video"). Unlike review, a
    recording with **no events** is not a failure — the video is still worth
    looking at — so ``events_path`` may be ``None`` and ``events_paths`` empty,
    and a failed events export is logged and tolerated rather than fatal; the
    Swift readiness guard gates on the video, not the events.
    """
    from screencap.config import get_recordings_dir

    recordings_root = get_recordings_dir()

    # Resolve the recording dir + prepare the local navigation video (shared
    # with review-data). No scrub, no scrub-lock. The short ``_INSPECT_LOCK_TIMEOUT``
    # makes a view opened during post-stop finalization fail fast as
    # ``ReviewPrepareBusy`` (retried by the shell) instead of hanging on the
    # terminal_lock the finalization holds.
    rec_dir, video_path, remediated = _prepare_recording_video(
        name, lock_timeout=_INSPECT_LOCK_TIMEOUT
    )

    # Local events from the ORIGINAL dir: ensure_canonical_events self-gates
    # (no-op when chunked; exports the combined events.jsonl from the DB
    # otherwise, with the same include_network=False config upload uses), then
    # resolve the per-chunk / combined set. Inspect is video-first and
    # read-only, so a failed export (e.g. a corrupt recording.db) is logged and
    # tolerated — it must not block looking at the video — leaving an empty
    # event set / timeline rather than failing the whole window.
    try:
        _export_canonical_events(rec_dir)
    except Exception as e:  # noqa: BLE001 — tolerate any export failure (video-first)
        # Escape the dynamic exception text (SCR-117/169): a markup
        # metacharacter in the message (a bracketed path / SQLite identifier)
        # would otherwise raise rich.MarkupError here, which — not being a
        # ReviewPrepareError — would escape this tolerant handler and the CLI's
        # envelope guard, crashing with a raw traceback on the JSON channel.
        console.print(f"[yellow]inspect-data: events unavailable: {escape(str(e))}[/yellow]")
    event_files = _resolve_event_files(rec_dir)
    events_paths = [str(p.resolve()) for p in event_files]

    # Defense-in-depth: refuse to emit any event path that escapes the
    # recordings root — a symlink inside the dir could otherwise put an
    # out-of-tree absolute path into the envelope that the parser would read.
    # (video_path is guarded once in _prepare_recording_video, shared with
    # review-data — SCR-189.)
    for p in event_files:
        _assert_within_recordings_root(p, recordings_root)

    started_at, duration_seconds, timing_status = _read_recording_timing(rec_dir)

    # Blocked/protected intervals for the in-viewer "what was recorded" summary,
    # re-derived from the intact local recording.db (fail-open, no daemon).
    blocked_intervals, protected_intervals = _read_inspect_blocked_intervals(
        rec_dir, started_at, duration_seconds,
    )

    return {
        "ok": True,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "video_path": str(video_path.resolve()),
        "events_path": events_paths[0] if events_paths else None,
        "events_paths": events_paths,
        # Video-first: the inspect window has no masked-screenshot pane, so no
        # frame paths are emitted.
        "screenshots": [],
        # No scrub ran → no redaction/coverage evidence (explicitly null; the
        # Swift Optional fields decode cleanly and inspect renders no evidence).
        "redaction": None,
        "coverage": None,
        # Additive, inspect-only (captured-events summary). ``blocked_intervals``:
        # capture-time EXCLUDE-only "not captured" spans for the summary's blocked
        # line. ``protected_intervals``: the full SCRUB_BLOCK_ACTIONS set the
        # digest suppresses (a superset of blocked_intervals). Empty-representable.
        "blocked_intervals": blocked_intervals,
        "protected_intervals": protected_intervals,
        "started_at": started_at,
        "duration_seconds": duration_seconds,
        "timing_status": timing_status,
        "timing_error": timing_status != "ok",
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
