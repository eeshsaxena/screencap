"""``LockPolicy`` seam (SCR-40, slice 4 of SCR-31).

Promotes the ``_skip_pidfile``-gated lock + identity bundle in
``_run_screen_recorder`` to a pluggable policy. Post-Phase-2 the
daemon is the sole engine spawner and owns the process-exclusive
pidfile claim itself (see ``daemon/supervisor.py``); the engine
subprocess therefore runs with ``InheritLock`` — claim/register/
release are no-ops, but per-recording identity files are still
written so downstream catalog / upload / scrubber / recovery
consumers find them.

The ``LockPolicy`` Protocol is kept as the seam in case the
engine-topology spike (docs/tickets/2026-05-08-engine-topology-spike.md)
re-introduces a different policy variant.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from screencap.engine.screen_recorder import RecordingRequest


def _write_identity_files(
    capture_dir: Path,
    *,
    request: RecordingRequest,
    privacy_mode: str,
) -> None:
    """Identity-file writer used by ``InheritLock``.

    Kept module-level so future ``LockPolicy`` implementations
    (e.g., from the engine-topology spike) can reuse the same
    payload writer. Schema matches the wrapper-era payload at
    ``recorder.py`` so downstream consumers (catalog, upload,
    scrubber, recovery) need no changes.
    """
    (capture_dir / ".recording_id").write_text(request.name)

    if request.cloud_intent and request.keep_local:
        destination = "both"
    elif request.cloud_intent:
        destination = "cloud"
    else:
        destination = "local"

    intent = {
        "version": 1,
        "destination": destination,
        "privacy_mode": privacy_mode,
        "show_on_website": request.show_on_website,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": request.intent_source,
    }
    (capture_dir / ".recording_intent").write_text(json.dumps(intent, indent=2))


class LockPolicy(Protocol):
    """Process-exclusive lock + per-recording identity ownership."""

    def claim(self, capture_dir: Path, *, force_clean: bool) -> None: ...

    def write_identity(
        self,
        capture_dir: Path,
        *,
        request: RecordingRequest,
        privacy_mode: str,
    ) -> None: ...

    def register_children(
        self, capture_dir: Path, child_pids: list[dict],
    ) -> None: ...

    def release(self) -> None: ...


class InheritLock:
    """Engine-subprocess lock policy: daemon supervisor owns the pidfile claim.

    ``claim``, ``register_children``, and ``release`` are no-ops because
    the daemon supervisor already claimed the process-exclusive pidfile
    (``daemon/supervisor.py`` → ``pidfile.claim_lock``). The engine
    subprocess still writes per-recording identity files so downstream
    consumers (catalog, upload, scrubber, recovery) find them.
    """

    def claim(self, capture_dir: Path, *, force_clean: bool) -> None:
        pass

    def write_identity(
        self,
        capture_dir: Path,
        *,
        request: RecordingRequest,
        privacy_mode: str,
    ) -> None:
        _write_identity_files(
            capture_dir, request=request, privacy_mode=privacy_mode,
        )

    def register_children(
        self, capture_dir: Path, child_pids: list[dict],
    ) -> None:
        pass

    def release(self) -> None:
        pass
