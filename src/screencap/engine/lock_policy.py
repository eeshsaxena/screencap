"""``LockPolicy`` seam (SCR-40, slice 4 of SCR-31).

Promotes the ``_skip_pidfile``-gated lock + identity bundle in
``_run_screen_recorder`` to a pluggable policy. ``ClaimLock`` is the
standalone CLI's exclusive-process owner; ``InheritLock`` is what
``SessionController`` workers use because the parent already claimed.

The interface decomposes "this recording's identity and exclusivity"
into four lifecycle hooks because the existing setup window has two
natural anchor points (before and after ``capture_dir.mkdir`` /
privacy_config) — fewer methods would force re-ordering.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from rich.console import Console

if TYPE_CHECKING:
    from screencap.engine.screen_recorder import RecordingRequest

_console = Console()


def _write_identity_files(
    capture_dir: Path,
    *,
    request: RecordingRequest,
    privacy_mode: str,
) -> None:
    """Shared identity-file writer for ``ClaimLock`` and ``InheritLock``.

    Per-recording identity is independent of who owns the process lock,
    so both policies emit the same files. Schema matches the wrapper-era
    payload at ``recorder.py`` so downstream consumers (catalog, upload,
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


class ClaimLock:
    """Standalone-CLI lock policy: orphan check + claim + write identity + register + release."""

    def claim(self, capture_dir: Path, *, force_clean: bool) -> None:
        from screencap import pidfile
        from screencap._stderr_events import EVENT_LOCK_CONTENDED, emit_event

        orphans = pidfile.find_orphaned_processes()
        if orphans:
            if force_clean:
                _console.print(
                    f"[yellow]Cleaning up {len(orphans)} orphaned process(es) "
                    "from a previous recording...[/yellow]"
                )
                pidfile.terminate_processes(orphans, force=True)
                pidfile.delete_pidfile()
            else:
                _console.print(
                    f"[yellow]Warning:[/yellow] Found {len(orphans)} orphaned "
                    "process(es) from a previous recording.\n"
                    "  Run 'screencap stop' to clean them up, or pass --force to auto-clean."
                )
                raise SystemExit(1)

        try:
            # Phase 2 U1 made the daemon the sole engine spawner — this
            # ``ClaimLock`` policy is exercised only via the daemon's
            # engine subprocess path, which runs with ``InheritLock``
            # in practice. The fallback claimant if this is ever wired
            # directly is ``CLAIMANT_DAEMON`` so lock metadata reads
            # consistently downstream.
            pidfile.claim_lock(capture_dir, claimant=pidfile.CLAIMANT_DAEMON)
        except pidfile.LockContended as exc:
            try:
                emit_event(EVENT_LOCK_CONTENDED, owner=exc.owner)
            except Exception:
                pass
            raise SystemExit(2) from None

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
        from screencap import pidfile

        pidfile.write_pidfile(capture_dir, child_pids)

    def release(self) -> None:
        from screencap import pidfile

        pidfile.delete_pidfile()


class InheritLock:
    """Session-worker lock policy: parent owns the lock, worker writes its own identity."""

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
