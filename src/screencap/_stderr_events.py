"""Structured stderr lifecycle event emitter.

Stdlib-only module: importable from spawn workers and the recording hot
loop without pulling Click + rich Console into the worker process.

The SwiftUI ``RecorderController.spawn`` parses these line-buffered JSON
events off the screencap subprocess's stderr to drive UI state transitions.
The schema is the cross-language contract — see
``docs/research/2026-04-28-stderr-event-schema.md``.

Active events (emitted in v1):
  started, lock_contended, recording_finalized, disk_full,
  permission_lost, permission_required, capture_unhealthy,
  capture_recovered, stopped, menubar_neutralized_by_env,
  matrix_disclosure_required, lock_metadata_write_failed,
  terminated_reason_persist_failed, upload_preparing, upload_started,
  upload_file_done, upload_finished, upload_failed, upload_busy

Reserved events (schema documented, NOT emitted in v1 — todo 004):
  chunk_finalized — wiring deferred to a follow-up that touches
  chunk_processor.py. SwiftUI consumers should treat absence as
  informational, not authoritative; do not block on it.

Exit codes (terminal exit_code on the ``stopped`` event matches the
process exit code): 0=clean, 2=lock-held, 3=permission denied/lost
(start-time ``permission_required`` block OR mid-recording
``permission_lost``), 4=disk_full, 5=user-initiated force-quit,
1=generic failure.

``capture_unhealthy`` (SCR-76) is ADVISORY: it has NO exit code and never
terminates a recording. The engine's mid-recording supervisor emits it once
per detection edge when a reader is demonstrably attempting but producing no
useful output AND the cause is not a ``screen_recording`` denial.
``capture_recovered`` (SCR-100) is its paired clear: the supervisor emits it
once when a reader that previously crossed the unhealthy edge returns healthy
mid-recording,
so the shell can drop the stale advisory instead of letting it linger until the
recording ends. It carries ``reader`` + ``elapsed`` only (no ``reason``); like
``capture_unhealthy`` it is advisory with no exit code. A recover-then-rebreak
re-emits ``capture_unhealthy`` (the engine clears its per-reader emitted flag on
recovery), so the advisory re-shows. Only a
``screen_recording`` denial reuses the terminal-capable ``permission_lost``
(the core screen capture is genuinely dead); an ``accessibility`` /
``input_monitoring`` attribution is a best-guess for a window / action stall
whose true cause is independent of those permissions (SCR-101), so it stays
advisory rather than self-stopping a healthy screen+audio recording. Fields:
  - ``reason``: one of ``CAPTURE_UNHEALTHY_REASONS`` (closed set — never
    runtime-derived text, since it rides the daemon EventBus to any
    same-EUID subscriber).
  - ``reader``: which capture is affected (``"screen"`` / ``"window"`` /
    ``"action"``).
  - ``elapsed``: seconds since recording start.
Tolerant SwiftUI parsers ignore unknown event types, so a shell that has not
yet learned ``capture_unhealthy`` simply no-ops on it.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Literal

EVENT_SCHEMA_VERSION = 1


# Event-type constants (todo 026). Use these everywhere instead of raw
# string literals so a typo at an emit site fails at import time, renaming
# is a single-site change, and tests share the same names as production.
EVENT_STARTED = "started"
EVENT_LOCK_CONTENDED = "lock_contended"
EVENT_CHUNK_FINALIZED = "chunk_finalized"
EVENT_RECORDING_FINALIZED = "recording_finalized"
EVENT_DISK_FULL = "disk_full"
# SCR-218 mid-recording mic mute. Emitted by the audio subsystem AFTER the mic
# stream actually stops/starts (KTD4 confirmed-state), so the daemon's session
# state / snapshot and the app reflect capture that genuinely changed — never a
# request that might have failed. Carry ``muted`` for symmetry, though the event
# type already implies it.
EVENT_AUDIO_MUTED = "audio_muted"
EVENT_AUDIO_UNMUTED = "audio_unmuted"
# SCR-218 R3: an unmute that had to (re)acquire the mic FAILED — the device
# could not be opened (denied / unavailable), so capture stayed muted. ADVISORY:
# no exit code, NEVER terminal — a failed unmute must not tear the recording
# down (unlike ``permission_lost``, which pins terminal exit code 3). The app
# surfaces this as the R3 "unmute never silently fails" error while the
# recording keeps running muted. Carries ``reason`` (closed-set label) so it can
# ride the daemon EventBus without leaking runtime text.
EVENT_AUDIO_UNMUTE_FAILED = "audio_unmute_failed"
# Closed set of ``audio_unmute_failed`` ``reason`` codes. Like the
# capture_unhealthy reasons, these ride the daemon EventBus to any same-EUID
# subscriber, so the field MUST be one of these constants — never interpolated
# runtime text.
AUDIO_UNMUTE_FAILED_REASON_MIC_UNAVAILABLE = "microphone_unavailable"
AUDIO_UNMUTE_FAILED_REASONS = frozenset({
    AUDIO_UNMUTE_FAILED_REASON_MIC_UNAVAILABLE,
})
EVENT_PERMISSION_LOST = "permission_lost"
# Start-time permission block (SCR-142). Re-emitted by the ``screencap start``
# daemon client when the daemon's pre-spawn permission gate rejects the start
# with a ``permission_required`` envelope. Unlike the single-permission
# ``permission_lost`` (a mid-recording revocation), this carries the full
# ``missing`` list of denied permissions (a subset of ``PERMISSION_LABELS``) so
# the CLI-fallback SwiftUI shell can name every missing permission and route
# into the same precise grant flow the daemon transport already uses. Pairs
# with process exit code 3.
EVENT_PERMISSION_REQUIRED = "permission_required"
# Advisory mid-recording capture-health signal (SCR-76). Emitted by the
# engine's supervisor loop when a reader is demonstrably attempting but
# producing no useful output AND the cause is NOT a screen_recording denial
# (only that reuses permission_lost; accessibility / input_monitoring stay
# advisory per SCR-101). ADVISORY: no exit code, never terminal.
EVENT_CAPTURE_UNHEALTHY = "capture_unhealthy"
# Paired recovery/clear for capture_unhealthy (SCR-100). Emitted once when a
# reader that previously crossed the unhealthy edge returns healthy
# mid-recording, so the shell drops the stale advisory instead of holding it
# until the recording ends. ADVISORY: carries reader + elapsed, no reason, no
# exit code, never terminal.
EVENT_CAPTURE_RECOVERED = "capture_recovered"
EVENT_STOPPED = "stopped"
EVENT_MENUBAR_NEUTRALIZED_BY_ENV = "menubar_neutralized_by_env"
# Disclosure / failure-surface events (todo 005, todo 013).
EVENT_MATRIX_DISCLOSURE_REQUIRED = "matrix_disclosure_required"
EVENT_LOCK_METADATA_WRITE_FAILED = "lock_metadata_write_failed"
EVENT_TERMINATED_REASON_PERSIST_FAILED = "terminated_reason_persist_failed"
# Daemon bus events — published by the daemon, not the engine subprocess.
# These ride the same schema version and JSON shape so tolerant clients can
# handle them on the same taxonomy axis as engine stderr events.
EVENT_ENGINE_CRASHED = "engine_crashed"
EVENT_PREVIOUS_SESSION_RECOVERED = "previous_session_recovered"
EVENT_PREVIOUS_SESSION_FORCE_TERMINATED = "previous_session_force_terminated"
# Cloud account-ownership mismatch (SCR-171). ADVISORY: emitted when the daemon's
# terminal-stage resume detects a recording whose pinned owner_uid differs from
# the now-signed-in uid (cloud convergence refused, kept local). Lifts the
# existing startup-sweep refusal onto the bus so a subscriber reacts without
# polling. Carries recording / owner_uid / signed_in_uid (gate-authoritative) +
# whoami-sourced signed_in_email / stale. Never terminal — no consumer may map
# it to a stop/teardown.
EVENT_ACCOUNT_MISMATCH = "account_mismatch"
EVENT_SUBSCRIBED = "subscribed"
# Upload pipeline events (plan U1) — consumed by the SwiftUI review window's
# UploadController to drive progress UI. Emitted from upload.py via the same
# tolerant-reader contract as recorder events: tolerant Swift parsers ignore
# unknown event types, so adding new ones never breaks existing consumers.
# Pre-upload prep heartbeat (SCR-175). Emitted by the interactive ``screencap
# upload`` path from inside ``run_terminal_stage`` during the otherwise-SILENT
# prep — the contended-lock handoff, the GCS reconcile, and the scrub/mask
# ``produce()`` — which all run BEFORE ``upload_started`` (the first transfer
# event). The Swift ``UploadController`` arms a single 120s inactivity watchdog
# that resets ONLY on a parsed event; without a prep-phase heartbeat the lock
# wait is subtracted from the same budget the silent converge draws on and a
# contended-then-released lock can re-trip the watchdog as a false
# ``.failed("upload timed out")`` (SCR-165 shortened the lock wait but left this
# gap). Advisory only: carries ``recording`` + a coarse ``phase`` label; tolerant
# consumers just need *an* event to reset the watchdog (the Swift ``default``
# branch already does). NOT terminal and NOT a progress count.
EVENT_UPLOAD_PREPARING = "upload_preparing"
EVENT_UPLOAD_STARTED = "upload_started"
EVENT_UPLOAD_FILE_DONE = "upload_file_done"
EVENT_UPLOAD_FINISHED = "upload_finished"
EVENT_UPLOAD_FAILED = "upload_failed"
# Terminal, NON-failure outcome (SCR-158): ``screencap upload`` skipped a
# recording because another process held the per-recording terminal-stage lock
# (a finalize, a daemon resume, or a concurrent upload). It is RETRYABLE and
# keeps exit 0, so it is deliberately distinct from ``upload_failed`` (which
# SCR-79 pins to a non-zero exit). The event-first Swift UploadController maps
# it to a retry-friendly state rather than rendering exit-0-without-an-event as
# a hard failure; agents read ``retryable`` to tell it apart from a no-op.
EVENT_UPLOAD_BUSY = "upload_busy"

# Closed set of ``capture_unhealthy`` ``reason`` codes (SCR-76). The reason
# field rides the daemon EventBus to any same-EUID subscriber, so it MUST be
# one of these constants — NEVER interpolate runtime-derived text (exception
# strings, OS errors, paths, window titles) into it.
CAPTURE_UNHEALTHY_REASON_READER_STALLED = "reader_stalled"
CAPTURE_UNHEALTHY_REASON_LISTENER_DEAD = "listener_dead"
CAPTURE_UNHEALTHY_REASONS = frozenset({
    CAPTURE_UNHEALTHY_REASON_READER_STALLED,
    CAPTURE_UNHEALTHY_REASON_LISTENER_DEAD,
})

# TCC permission labels (SCR-76 / SCR-101). Closed set: used as the
# ``permission`` field on permission_lost and as the capture-health
# attribution label. Named so a typo fails at import, renames are
# single-site, and tests share the names with production.
PERMISSION_SCREEN_RECORDING = "screen_recording"
PERMISSION_INPUT_MONITORING = "input_monitoring"
PERMISSION_ACCESSIBILITY = "accessibility"
PERMISSION_LABELS = frozenset({
    PERMISSION_SCREEN_RECORDING,
    PERMISSION_INPUT_MONITORING,
    PERMISSION_ACCESSIBILITY,
})
# Canonical human-readable names for each TCC label. Single source of truth for
# the display strings, so the ``permission_required`` human echo (cli) names
# permissions identically to the SwiftUI shell's ``PrivacyPane.displayName``
# rather than relying on a ``.replace("_", " ").title()`` that would render
# "Input_monitoring" → "Input Monitoring" only by coincidence.
PERMISSION_DISPLAY: dict[str, str] = {
    PERMISSION_SCREEN_RECORDING: "Screen Recording",
    PERMISSION_ACCESSIBILITY: "Accessibility",
    PERMISSION_INPUT_MONITORING: "Input Monitoring",
}
# Type alias mirroring PERMISSION_LABELS for use in type hints.
PermissionLabel = Literal["screen_recording", "input_monitoring", "accessibility"]


def emit_event(event_type: str, **fields: Any) -> None:
    """Write a single JSON line to stderr describing a recorder lifecycle event.

    Always flushes — SwiftUI's line-buffered reader needs immediate delivery.
    Failures are swallowed so a broken stderr never breaks the recorder.
    Every payload carries ``schema_version`` so a SwiftUI parser can detect
    breaking changes at runtime.
    """
    payload = {
        "type": event_type,
        "ts": time.time(),
        "schema_version": EVENT_SCHEMA_VERSION,
        **fields,
    }
    try:
        sys.stderr.write(json.dumps(payload) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


__all__ = [
    "emit_event",
    "EVENT_SCHEMA_VERSION",
    "EVENT_STARTED",
    "EVENT_LOCK_CONTENDED",
    "EVENT_CHUNK_FINALIZED",
    "EVENT_RECORDING_FINALIZED",
    "EVENT_DISK_FULL",
    "EVENT_AUDIO_MUTED",
    "EVENT_AUDIO_UNMUTED",
    "EVENT_AUDIO_UNMUTE_FAILED",
    "AUDIO_UNMUTE_FAILED_REASON_MIC_UNAVAILABLE",
    "AUDIO_UNMUTE_FAILED_REASONS",
    "EVENT_PERMISSION_LOST",
    "EVENT_PERMISSION_REQUIRED",
    "EVENT_CAPTURE_UNHEALTHY",
    "EVENT_CAPTURE_RECOVERED",
    "CAPTURE_UNHEALTHY_REASON_READER_STALLED",
    "CAPTURE_UNHEALTHY_REASON_LISTENER_DEAD",
    "CAPTURE_UNHEALTHY_REASONS",
    "PERMISSION_SCREEN_RECORDING",
    "PERMISSION_INPUT_MONITORING",
    "PERMISSION_ACCESSIBILITY",
    "PERMISSION_LABELS",
    "PERMISSION_DISPLAY",
    "PermissionLabel",
    "EVENT_STOPPED",
    "EVENT_MENUBAR_NEUTRALIZED_BY_ENV",
    "EVENT_MATRIX_DISCLOSURE_REQUIRED",
    "EVENT_LOCK_METADATA_WRITE_FAILED",
    "EVENT_TERMINATED_REASON_PERSIST_FAILED",
    "EVENT_ENGINE_CRASHED",
    "EVENT_PREVIOUS_SESSION_RECOVERED",
    "EVENT_PREVIOUS_SESSION_FORCE_TERMINATED",
    "EVENT_ACCOUNT_MISMATCH",
    "EVENT_SUBSCRIBED",
    "EVENT_UPLOAD_PREPARING",
    "EVENT_UPLOAD_STARTED",
    "EVENT_UPLOAD_FILE_DONE",
    "EVENT_UPLOAD_FINISHED",
    "EVENT_UPLOAD_FAILED",
    "EVENT_UPLOAD_BUSY",
]
