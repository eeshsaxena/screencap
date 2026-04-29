"""Structured stderr lifecycle event emitter.

Stdlib-only module: importable from spawn workers and the recording hot
loop without pulling Click + rich Console into the worker process.

The SwiftUI ``RecorderController.spawn`` parses these line-buffered JSON
events off the screencap subprocess's stderr to drive UI state transitions.
The schema is the cross-language contract — see
``docs/research/2026-04-28-stderr-event-schema.md``.

Active events (emitted in v1):
  started, lock_contended, recording_finalized, disk_full,
  permission_lost, stopped, menubar_neutralized_by_env

Reserved events (schema documented, NOT emitted in v1 — todo 004):
  chunk_finalized — wiring deferred to a follow-up that touches
  chunk_processor.py. SwiftUI consumers should treat absence as
  informational, not authoritative; do not block on it.

Exit codes (terminal exit_code on the ``stopped`` event matches the
process exit code): 0=clean, 2=lock-held, 3=permission_lost, 4=disk_full,
5=user-initiated force-quit, 1=generic failure.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

_EVENT_SCHEMA_VERSION = 1


# Event-type constants (todo 026). Use these everywhere instead of raw
# string literals so a typo at an emit site fails at import time, renaming
# is a single-site change, and tests share the same names as production.
EVENT_STARTED = "started"
EVENT_LOCK_CONTENDED = "lock_contended"
EVENT_CHUNK_FINALIZED = "chunk_finalized"
EVENT_RECORDING_FINALIZED = "recording_finalized"
EVENT_DISK_FULL = "disk_full"
EVENT_PERMISSION_LOST = "permission_lost"
EVENT_STOPPED = "stopped"
EVENT_MENUBAR_NEUTRALIZED_BY_ENV = "menubar_neutralized_by_env"


def resolve_claimant() -> str:
    """Resolve the lock claimant identity from the SCREENCAP_PARENT env var.

    "swiftui" when spawned by the SwiftUI app (via SCREENCAP_PARENT=swiftui),
    "cli" otherwise. Centralized here (todo 030) so the two production sites
    (session.py SessionController and recorder.py legacy start path) cannot
    drift apart.
    """
    import os
    return "swiftui" if os.environ.get("SCREENCAP_PARENT") == "swiftui" else "cli"


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
        "schema_version": _EVENT_SCHEMA_VERSION,
        **fields,
    }
    try:
        sys.stderr.write(json.dumps(payload) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


__all__ = [
    "emit_event",
    "resolve_claimant",
    "_EVENT_SCHEMA_VERSION",
    "EVENT_STARTED",
    "EVENT_LOCK_CONTENDED",
    "EVENT_CHUNK_FINALIZED",
    "EVENT_RECORDING_FINALIZED",
    "EVENT_DISK_FULL",
    "EVENT_PERMISSION_LOST",
    "EVENT_STOPPED",
    "EVENT_MENUBAR_NEUTRALIZED_BY_ENV",
]
