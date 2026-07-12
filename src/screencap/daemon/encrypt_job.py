"""Single-task upgrade-migration job holder on ``app.state`` (SCR-258 U6).

The daemon runs the U6 migration engine (:func:`screencap.migration.run_migration`)
as ONE Supervisor-style background task, mirroring
:class:`screencap.daemon.backfill_job.BackfillJob`. A single :class:`EncryptJob`
instance lives at ``app.state.encrypt_job`` (the hook U9 reserved for pause-on-lock)
and is driven by the three ``/v0/storage.encrypt.*`` verbs.

Lifecycle
---------
- :meth:`start` is **idempotent**: a start while a run is in flight returns the
  existing job's status snapshot rather than spawning a second migration (one at a
  time — copy/verify + a single cutover swap must not interleave).
- The engine runs blocking copy/verify work via ``asyncio.to_thread``; its
  ``progress_cb`` fires on that worker thread, so it bridges thread→loop with
  ``asyncio.run_coroutine_threadsafe(bus.publish(...), loop)`` (the backfill
  discipline).
- :meth:`cancel` sets the ``threading.Event`` the engine polls between recording
  boundaries; the run converges to ``cancelled`` (resumable — the ledger records
  partial progress).
- :meth:`shutdown` is the **U9 lock pause hook**: the ``storage.lock`` handler
  calls it (via ``_signal_background_jobs_to_pause``) to halt the migration at its
  current recording boundary before sealing; unlock auto-resumes it.
- :meth:`is_running` feeds the idle-shutdown busy predicate so the daemon never
  idle-exits mid-migration.

Because the engine needs daemon-resolved seams (the interim/final mountpoints, the
container key, the active-recording query, the ``acquire_migration`` cutover
reservation, the sidecar-writer pause), the job is generic over a
``run_factory``: a zero-arg callable returning the bound
``run(stop_event, progress_cb, ledger)`` function. The ``storage.encrypt.start``
verb (and the unlock / daemon-start auto-resume) supply it, so this module holds
no migration-path or container specifics.

Progress payload privacy (R9)
-----------------------------
Progress / status carry ONLY counts + the run state + the cutover phase — NEVER a
recording directory name. The EventBus is readable by any same-EUID subscriber
(including the MCP ``/v0/events`` subscription), so a dir name (which encodes
timing/context) must never cross this boundary, matching the backfill bar.

Completion suggestion
---------------------
On a COMPLETED run the job emits a one-time ``encrypt.compact_suggested`` event so
the UI can offer ``storage compact`` (the migration leaves reclaimable bands in
the sparse bundle); it is best-effort telemetry like every other event here.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from screencap import _stderr_events
from screencap.migration import (
    MigrationLedger,
    MigrationState,
    MigrationSummary,
    should_auto_resume,
)

if TYPE_CHECKING:
    from screencap.daemon.event_bus import EventBus

logger = logging.getLogger(__name__)

EVENT_ENCRYPT_PROGRESS = "encrypt.progress"
EVENT_ENCRYPT_COMPLETED = "encrypt.completed"
EVENT_ENCRYPT_PAUSED = "encrypt.paused"
EVENT_ENCRYPT_CANCELLED = "encrypt.cancelled"
EVENT_ENCRYPT_FAILED = "encrypt.failed"
# One-time completion suggestion the UI reads to offer ``storage compact`` (KTD-18).
EVENT_ENCRYPT_COMPACT_SUGGESTED = "encrypt.compact_suggested"

_TERMINAL_EVENT_FOR_STATE = {
    MigrationState.COMPLETED: EVENT_ENCRYPT_COMPLETED,
    MigrationState.PAUSED: EVENT_ENCRYPT_PAUSED,
    MigrationState.CANCELLED: EVENT_ENCRYPT_CANCELLED,
}

# Run-level state surfaced when no run has ever been started in this process and
# the ledger carries no prior run row.
STATE_IDLE = "idle"

# Factory returning the bound engine call. Injected by the verb / auto-resume.
RunFactory = Callable[[], Callable[..., MigrationSummary]]


@dataclass(frozen=True)
class EncryptStatusSnapshot:
    """Privacy-safe status snapshot — the wire shape of ``storage.encrypt.status``.

    Counts + run state + cutover phase + a non-identifying pause reason. NEVER a
    recording directory name (R9).
    """

    state: str
    phase: str
    pending: int
    copied: int
    verified: int
    deleted: int
    total: int
    paused_reason: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "phase": self.phase,
            "pending": self.pending,
            "copied": self.copied,
            "verified": self.verified,
            "deleted": self.deleted,
            "total": self.total,
            "paused_reason": self.paused_reason,
        }

    @classmethod
    def from_summary(cls, summary: MigrationSummary) -> "EncryptStatusSnapshot":
        return cls(
            state=summary.state.value,
            phase=summary.phase.value,
            pending=summary.pending,
            copied=summary.copied,
            verified=summary.verified,
            deleted=summary.deleted,
            total=summary.total,
            paused_reason=summary.paused_reason,
        )


class EncryptJob:
    """Owns the single in-flight migration task + its stop flag + loop reference."""

    def __init__(self, bus: "EventBus", *, ledger: MigrationLedger | None = None) -> None:
        self._bus = bus
        self._ledger = ledger if ledger is not None else MigrationLedger()
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_factory: RunFactory | None = None
        # Last summary observed from the worker thread; read by ``status``.
        self._last: MigrationSummary | None = None
        self._terminal_state: str | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API (driven by the daemon verbs + the U9 lock/unlock hooks).
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """True while a run is in flight (feeds the idle-shutdown busy predicate)."""
        return self._running

    def start(self, run_factory: RunFactory | None = None) -> EncryptStatusSnapshot:
        """Start (or resume) a migration, or return the in-flight snapshot.

        Idempotent: a second start while a run is in flight does NOT spawn a
        second task — it returns the current snapshot. ``run_factory`` supplies the
        bound engine call; when omitted the last factory is reused (the unlock /
        daemon-start auto-resume path), so a resume needs no re-plumbing.
        """
        if run_factory is not None:
            self._run_factory = run_factory
        if self._running:
            return self.status()
        if self._run_factory is None:
            # Nothing to run — no plan has ever been supplied this process.
            return self.status()
        self._loop = asyncio.get_running_loop()
        self._stop_event = threading.Event()
        self._terminal_state = None
        self._running = True
        self._task = asyncio.create_task(self._run())
        return self.status()

    def resume_if_pending(self) -> EncryptStatusSnapshot | None:
        """Auto-resume hook (unlock / daemon start) — starts iff work remains.

        Returns the snapshot when it (re)starts, or ``None`` when there is nothing
        to resume (never started this process, cancelled, or fully complete). The
        auto-resume POLICY is :func:`screencap.migration.should_auto_resume`.
        """
        if self._running:
            return self.status()
        if self._run_factory is None:
            return None
        if not should_auto_resume(self._ledger):
            return None
        return self.start()

    def cancel(self) -> EncryptStatusSnapshot:
        """Cancel the in-flight migration at the next recording boundary.

        A user cancel is TERMINAL (never auto-resumed), so it marks the ledger
        CANCELLED *before* setting the stop flag the engine polls — the engine's
        boundary halt preserves an already-CANCELLED run (vs a bare lock-pause
        flag, which leaves the run RUNNING → auto-resumable). Plaintext stays
        intact — deletion only runs post-cutover. A no-op when idle.
        """
        if self._running:
            self._ledger.set_run(state=MigrationState.CANCELLED)
            self._stop_event.set()
        return self.status()

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Pause an in-flight migration + await the worker, bounded (U9 lock hook).

        The ``storage.lock`` handler calls this to halt the migration at its
        current recording boundary before sealing (a ``to_thread`` copy worker is
        NOT cancellable from the loop side, so we set the ``threading.Event`` the
        engine polls, then await within ``timeout`` so any terminal event still
        publishes to the live bus). On timeout we stop waiting and let process exit
        reclaim the thread — teardown/lock must never hang. A no-op when idle.
        """
        task = self._task
        if task is None or task.done():
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "encrypt migration did not stop within %.1fs of the pause signal; "
                "leaving the worker to process exit", timeout,
            )
        except Exception:  # noqa: BLE001 — teardown must never abort on a surprise
            logger.debug("awaiting encrypt task during shutdown raised", exc_info=True)

    def status(self) -> EncryptStatusSnapshot:
        """Return the current privacy-safe status snapshot.

        While in flight, reflects the last worker snapshot; otherwise the ledger's
        authoritative (cross-process / post-restart) summary — so a status query
        after a daemon restart still reflects a prior run — falling back to
        ``idle`` when nothing has ever run.
        """
        if self._running and self._last is not None:
            summary = self._last
        else:
            summary = self._ledger.summary()
        snapshot = EncryptStatusSnapshot.from_summary(summary)
        if not self._running and self._terminal_state is not None:
            snapshot = EncryptStatusSnapshot(
                state=self._terminal_state,
                phase=snapshot.phase,
                pending=snapshot.pending,
                copied=snapshot.copied,
                verified=snapshot.verified,
                deleted=snapshot.deleted,
                total=snapshot.total,
                paused_reason=snapshot.paused_reason,
            )
        elif self._ledger.run_state() is None and not self._running:
            snapshot = EncryptStatusSnapshot(
                state=STATE_IDLE, phase=snapshot.phase, pending=0, copied=0,
                verified=0, deleted=0, total=0, paused_reason=None,
            )
        return snapshot

    # ------------------------------------------------------------------
    # Internal run driver.
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        factory = self._run_factory
        assert factory is not None  # start() guards this
        try:
            run = factory()
            summary = await asyncio.to_thread(
                run,
                stop_event=self._stop_event,
                progress_cb=self._on_progress,
                ledger=self._ledger,
            )
            await self._on_terminal(summary)
        except Exception:  # noqa: BLE001 — a crash must leave the daemon up
            logger.exception("encrypt migration job crashed; reporting failed state")
            await self._publish_failed()
        finally:
            self._running = False

    def _on_progress(self, summary: MigrationSummary) -> None:
        """Engine ``progress_cb`` — runs on the ``to_thread`` worker thread.

        Records the snapshot for ``status`` and bridges the publish onto the
        captured loop. NEVER forwards a recording dir name (R9).
        """
        self._last = summary
        self._bridge_publish(EVENT_ENCRYPT_PROGRESS, **summary.as_payload())

    async def _on_terminal(self, summary: MigrationSummary) -> None:
        self._last = summary
        self._terminal_state = summary.state.value
        event_type = _TERMINAL_EVENT_FOR_STATE.get(
            summary.state, EVENT_ENCRYPT_COMPLETED
        )
        await self._bus.publish(self._event(event_type, **summary.as_payload()))
        # One-time completion suggestion so the UI can offer ``storage compact``.
        if summary.state is MigrationState.COMPLETED:
            await self._bus.publish(self._event(EVENT_ENCRYPT_COMPACT_SUGGESTED))

    async def _publish_failed(self) -> None:
        self._terminal_state = "failed"
        summary = self._ledger.summary()
        await self._bus.publish(
            self._event(
                EVENT_ENCRYPT_FAILED,
                **{**summary.as_payload(), "state": "failed"},
            )
        )

    def _bridge_publish(self, event_type: str, **fields: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        event = self._event(event_type, **fields)
        try:
            asyncio.run_coroutine_threadsafe(self._bus.publish(event), loop)
        except Exception:  # noqa: BLE001 — fire-and-forget telemetry
            logger.debug("encrypt progress publish skipped (loop unavailable)")

    @staticmethod
    def _event(event_type: str, **fields: Any) -> dict[str, Any]:
        return {
            "type": event_type,
            "schema_version": _stderr_events.EVENT_SCHEMA_VERSION,
            "ts": time.time(),
            **fields,
        }


__all__ = [
    "EncryptJob",
    "EncryptStatusSnapshot",
    "STATE_IDLE",
    "EVENT_ENCRYPT_PROGRESS",
    "EVENT_ENCRYPT_COMPLETED",
    "EVENT_ENCRYPT_PAUSED",
    "EVENT_ENCRYPT_CANCELLED",
    "EVENT_ENCRYPT_FAILED",
    "EVENT_ENCRYPT_COMPACT_SUGGESTED",
]
