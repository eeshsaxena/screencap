"""Structured stderr lifecycle event emitter.

Stdlib-only module: importable from spawn workers and the recording hot
loop without pulling Click + rich Console into the worker process.

The SwiftUI ``RecorderController.spawn`` parses these line-buffered JSON
events off the screencap subprocess's stderr to drive UI state transitions.
The schema is the cross-language contract — see
``docs/research/2026-04-28-stderr-event-schema.md``.

Active events (emitted in v1):
  started, lock_contended, recording_finalized, disk_full,
  permission_lost, capture_unhealthy, stopped, menubar_neutralized_by_env,
  matrix_disclosure_required, lock_metadata_write_failed,
  terminated_reason_persist_failed,
  upload_started, upload_file_done, upload_finished, upload_failed

Reserved events (schema documented, NOT emitted in v1 — todo 004):
  chunk_finalized — wiring deferred to a follow-up that touches
  chunk_processor.py. SwiftUI consumers should treat absence as
  informational, not authoritative; do not block on it.

Exit codes (terminal exit_code on the ``stopped`` event matches the
process exit code): 0=clean, 2=lock-held, 3=permission_lost, 4=disk_full,
5=user-initiated force-quit, 1=generic failure.

``capture_unhealthy`` (SCR-76) is ADVISORY: it has NO exit code and never
terminates a recording. The engine's mid-recording supervisor emits it once
per detection edge when a reader is demonstrably attempting but producing no
useful output AND the cause is not a ``screen_recording`` denial. Only a
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
from typing import Any

EVENT_SCHEMA_VERSION = 1


# Event-type constants (todo 026). Use these everywhere instead of raw
# string literals so a typo at an emit site fails at import time, renaming
# is a single-site change, and tests share the same names as production.
EVENT_STARTED = "started"
EVENT_LOCK_CONTENDED = "lock_contended"
EVENT_CHUNK_FINALIZED = "chunk_finalized"
EVENT_RECORDING_FINALIZED = "recording_finalized"
EVENT_DISK_FULL = "disk_full"
EVENT_PERMISSION_LOST = "permission_lost"
# Advisory mid-recording capture-health signal (SCR-76). Emitted by the
# engine's supervisor loop when a reader is demonstrably attempting but
# producing no useful output AND the cause is NOT a screen_recording denial
# (only that reuses permission_lost; accessibility / input_monitoring stay
# advisory per SCR-101). ADVISORY: no exit code, never terminal.
EVENT_CAPTURE_UNHEALTHY = "capture_unhealthy"
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
EVENT_SUBSCRIBED = "subscribed"
# Upload pipeline events (plan U1) — consumed by the SwiftUI review window's
# UploadController to drive progress UI. Emitted from upload.py via the same
# tolerant-reader contract as recorder events: tolerant Swift parsers ignore
# unknown event types, so adding new ones never breaks existing consumers.
EVENT_UPLOAD_STARTED = "upload_started"
EVENT_UPLOAD_FILE_DONE = "upload_file_done"
EVENT_UPLOAD_FINISHED = "upload_finished"
EVENT_UPLOAD_FAILED = "upload_failed"

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
    "EVENT_PERMISSION_LOST",
    "EVENT_CAPTURE_UNHEALTHY",
    "CAPTURE_UNHEALTHY_REASON_READER_STALLED",
    "CAPTURE_UNHEALTHY_REASON_LISTENER_DEAD",
    "CAPTURE_UNHEALTHY_REASONS",
    "EVENT_STOPPED",
    "EVENT_MENUBAR_NEUTRALIZED_BY_ENV",
    "EVENT_MATRIX_DISCLOSURE_REQUIRED",
    "EVENT_LOCK_METADATA_WRITE_FAILED",
    "EVENT_TERMINATED_REASON_PERSIST_FAILED",
    "EVENT_ENGINE_CRASHED",
    "EVENT_PREVIOUS_SESSION_RECOVERED",
    "EVENT_PREVIOUS_SESSION_FORCE_TERMINATED",
    "EVENT_SUBSCRIBED",
    "EVENT_UPLOAD_STARTED",
    "EVENT_UPLOAD_FILE_DONE",
    "EVENT_UPLOAD_FINISHED",
    "EVENT_UPLOAD_FAILED",
]
