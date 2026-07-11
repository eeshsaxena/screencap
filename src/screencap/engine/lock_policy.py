"""``LockPolicy`` seam (SCR-40, slice 4 of SCR-31).

Promotes the ``_skip_pidfile``-gated lock + identity bundle in
``_run_screen_recorder`` to a pluggable policy. Post-Phase-2 the
daemon is the sole engine spawner and owns the process-exclusive
pidfile claim itself (see ``daemon/supervisor.py``); the engine
subprocess therefore runs with ``InheritLock`` — claim/register/
release are no-ops, but per-recording identity files are still
written so downstream catalog / upload / scrubber / recovery
consumers find them.
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
    masked_video_upload: bool | None = None,
) -> None:
    """Identity-file writer used by ``InheritLock``.

    Kept module-level so future ``LockPolicy`` implementations
    (e.g., from the engine-topology spike) can reuse the same
    payload writer. Schema matches the wrapper-era payload at
    ``recorder.py`` so downstream consumers (catalog, upload,
    scrubber, recovery) need no changes.

    ``masked_video_upload`` is the FROZEN masked-video-upload decision for this
    recording (SCR-125 R-SCR125-A). The caller resolves it ONCE at start (the
    same value capture-time ``block_video`` uses) and threads it here so it is
    durable on disk; the live ``chunk_processor`` upload and ``terminal_stage``
    masking read THIS frozen value, so a mid-recording flip of the mutable
    global cannot make capture-blocking and upload-masking disagree. ``None``
    (no value threaded) falls back to the global at write time — still a
    start-time read, kept for call-site / test back-compat.
    """
    (capture_dir / ".recording_id").write_text(request.name)

    if request.cloud_intent and request.keep_local:
        destination = "both"
    elif request.cloud_intent:
        destination = "cloud"
    else:
        destination = "local"

    # Resolve the destination + retention policy ONCE and freeze it into
    # per-recording state (U3). This is the monetization seam's freeze point:
    # a later config/plan-tier change cannot retroactively re-route this
    # recording because the resolved policy is now durable on disk. Imported
    # locally to keep the engine-subprocess import surface small.
    from screencap.pipeline_policy import resolve_policy

    resolved = resolve_policy(destination=destination)

    if masked_video_upload is None:
        from screencap.config import get_masked_video_upload_enabled

        masked_video_upload = get_masked_video_upload_enabled()

    from screencap.config import get_cloud_e2ee_enabled

    intent = {
        # Bumped 1 -> 2: adds the frozen resolved-policy fields
        # (retention_policy, retention_params). Readers tolerate v1 (absent
        # fields) as sparse-but-valid — see catalog.read_intent_policy.
        "version": 2,
        "destination": destination,
        "retention_policy": resolved.retention_policy.value,
        "retention_params": dict(resolved.params),
        # SCR-125: freeze the masked-video-upload decision per recording so a
        # mid-recording global flip cannot ship unmasked rich video (the live
        # upload + terminal stage read this, never the mutable global).
        "masked_video_upload": bool(masked_video_upload),
        # SCR-220 (KTD-4): freeze the cloud-E2EE decision per recording so a
        # mid-life flag flip cannot downgrade an "encrypted" recording to a
        # plaintext upload. Every upload seam reads THIS frozen bit — the
        # live flag's only role is seeding it here at recording start.
        # Schema-additive: older intents lack the field → treated as False.
        "cloud_e2ee": bool(get_cloud_e2ee_enabled()),
        "privacy_mode": privacy_mode,
        "show_on_website": request.show_on_website,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": request.intent_source,
    }
    (capture_dir / ".recording_intent").write_text(json.dumps(intent, indent=2))


class LockPolicy(Protocol):
    """Process-exclusive lock + per-recording identity ownership."""

    def claim(self, capture_dir: Path) -> None: ...

    def write_identity(
        self,
        capture_dir: Path,
        *,
        request: RecordingRequest,
        privacy_mode: str,
        masked_video_upload: bool | None = None,
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

    def claim(self, capture_dir: Path) -> None:
        pass

    def write_identity(
        self,
        capture_dir: Path,
        *,
        request: RecordingRequest,
        privacy_mode: str,
        masked_video_upload: bool | None = None,
    ) -> None:
        _write_identity_files(
            capture_dir, request=request, privacy_mode=privacy_mode,
            masked_video_upload=masked_video_upload,
        )

    def register_children(
        self, capture_dir: Path, child_pids: list[dict],
    ) -> None:
        pass

    def release(self) -> None:
        pass
