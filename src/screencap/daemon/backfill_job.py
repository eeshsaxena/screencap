"""Single-task backfill job holder on ``app.state`` (U5, SCR-178).

The daemon runs the U4 backfill engine (:func:`screencap.backfill.engine.run_backfill`)
as ONE Supervisor-style background task. This module owns that task: a single
:class:`BackfillJob` instance lives at ``app.state.backfill_job`` and is driven by
the three ``/v0/backfill.*`` verbs.

Lifecycle
---------
- :meth:`start` is **idempotent**: if a run is already in flight it returns the
  existing job's status snapshot rather than spawning a second task (one job at a
  time — avoids double OCR load + content-index lock contention).
- The engine runs blocking work via ``asyncio.to_thread``. Its ``progress_cb``
  fires on that worker thread, so it CANNOT ``await bus.publish(...)`` directly.
  We capture the running loop at start and bridge thread→loop with
  ``asyncio.run_coroutine_threadsafe(bus.publish(...), loop)`` (mirrors the
  Supervisor's async-publish discipline, adapted for a thread origin).
- :meth:`cancel` sets the ``threading.Event`` stop flag the engine polls.
- :meth:`status` returns a snapshot the handlers wrap in an envelope.
- :meth:`is_running` feeds the idle-shutdown busy predicate so the daemon never
  idle-exits mid-run.

Progress payload privacy (R9)
-----------------------------
Progress / status carry ONLY ``(state, done, skipped, failed, total,
current_unit_index)`` — ``current_unit_index`` is an OPAQUE ordinal. The recording
directory name is NEVER published: the EventBus is readable by any same-EUID
subscriber (including the MCP ``/v0/events`` subscription), and a dir name encodes
timing/context. This matches the class-name-only/pointer-only bar of the existing
read verbs.

Auto-resume rule
----------------
:func:`should_auto_resume` encodes the policy: only a ``PAUSED`` ledger with
pending units may auto-resume on daemon boot; a ``CANCELLED`` ledger must NOT
(it honors the user's last intent — re-trigger is an explicit ``backfill.start``).
Boot-time auto-resume wiring itself is out of U5's scope; the predicate + its test
encode the rule so a future boot hook cannot get it wrong.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from screencap import _stderr_events
from screencap.backfill.ledger import BackfillLedger, RunState

if TYPE_CHECKING:
    from screencap.backfill.engine import BackfillSummary
    from screencap.daemon.event_bus import EventBus

logger = logging.getLogger(__name__)

# Event types broadcast on the EventBus. The terminal type is chosen from the
# engine's returned ``RunState`` so a subscriber can tell completed/paused/cancelled
# apart without re-querying. ``backfill.progress`` carries the live snapshot.
EVENT_BACKFILL_PROGRESS = "backfill.progress"
EVENT_BACKFILL_COMPLETED = "backfill.completed"
EVENT_BACKFILL_PAUSED = "backfill.paused"
EVENT_BACKFILL_CANCELLED = "backfill.cancelled"
EVENT_BACKFILL_FAILED = "backfill.failed"

_TERMINAL_EVENT_FOR_STATE = {
    RunState.COMPLETED: EVENT_BACKFILL_COMPLETED,
    RunState.PAUSED: EVENT_BACKFILL_PAUSED,
    RunState.CANCELLED: EVENT_BACKFILL_CANCELLED,
}


@dataclass(frozen=True)
class BackfillStatusSnapshot:
    """Privacy-safe status snapshot — the wire shape of ``backfill.status``.

    Carries ONLY the run state + frozen-denominator counts + an opaque ordinal.
    Never the recording directory name (R9).
    """

    state: str
    done: int
    skipped: int
    failed: int
    total: int
    current_unit_index: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "done": self.done,
            "skipped": self.skipped,
            "failed": self.failed,
            "total": self.total,
            "current_unit_index": self.current_unit_index,
        }


# Run-level state surfaced when no run has ever been started in this process and
# the ledger carries no prior run row.
STATE_IDLE = "idle"


def should_auto_resume(ledger: BackfillLedger) -> bool:
    """True iff a daemon boot may auto-resume this ledger's run.

    Only a ``PAUSED`` run with at least one ``PENDING`` unit qualifies. A
    ``CANCELLED`` run must NOT auto-resume (honors the user's last intent); a
    ``COMPLETED`` run has nothing to do; a never-seeded ledger has no work.
    """
    if ledger.run_state() != RunState.PAUSED:
        return False
    # PAUSED but already fully terminal → nothing pending to resume.
    return not ledger.is_complete()


class BackfillJob:
    """Owns the single in-flight backfill task + its stop flag + loop reference."""

    def __init__(self, bus: "EventBus", *, ledger: BackfillLedger | None = None) -> None:
        self._bus = bus
        self._ledger = ledger if ledger is not None else BackfillLedger()
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Last progress snapshot observed from the worker thread (opaque ordinal
        # included). Updated under the GIL from the worker; read by ``status``.
        self._last_done = 0
        self._last_skipped = 0
        self._last_failed = 0
        self._last_total = 0
        self._last_unit_index = 0
        # The terminal state of the most recent run (set when the task finishes).
        self._terminal_state: str | None = None
        # Set True while the engine call is in flight; status reports "running".
        self._running = False

    # ------------------------------------------------------------------
    # Public API (driven by the daemon verbs).
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """True while a run is in flight (feeds the idle-shutdown busy predicate)."""
        return self._running

    def start(self, **engine_kwargs: Any) -> BackfillStatusSnapshot:
        """Start a run, or return the in-flight run's status if one exists.

        Idempotent: a second ``start`` while a run is in flight does NOT spawn a
        second task — it returns the current snapshot. ``engine_kwargs`` are
        injectable knobs forwarded to ``run_backfill`` (budget_s,
        max_frames_per_recording, recordings_dir, ocr, store_path) — used by
        tests; production calls with none.
        """
        if self._running:
            return self.status()

        self._loop = asyncio.get_running_loop()
        self._stop_event = threading.Event()
        self._terminal_state = None
        self._running = True
        self._task = asyncio.create_task(self._run(engine_kwargs))
        return self.status()

    def cancel(self) -> BackfillStatusSnapshot:
        """Signal the engine to stop. Returns the (still-running) snapshot.

        Sets the ``threading.Event`` the engine polls between units/frames. The
        run transitions to ``cancelled`` when the worker returns; the ledger
        records partial progress. A no-op (returns the current snapshot) if no
        run is in flight.
        """
        if self._running:
            self._stop_event.set()
        return self.status()

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Signal an in-flight run to stop and await the worker, bounded.

        Called from the daemon lifespan teardown (``app.py``). Without this, a
        backfill OCR worker thread launched via ``asyncio.to_thread`` is orphaned
        on shutdown: a ``to_thread`` worker is NOT cancellable from the loop side,
        so it would keep polling frames and writing ``content_index.db`` after the
        event loop has closed. We set the ``threading.Event`` the engine polls
        between units/frames, then await the task within ``timeout`` so any
        terminal event still publishes to the (still-live) bus. On timeout we stop
        waiting and let process exit reclaim the thread — teardown must never hang.
        A no-op when no run is in flight.
        """
        task = self._task
        if task is None or task.done():
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(task, timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "backfill did not stop within %.1fs of shutdown signal; "
                "leaving the worker thread to process exit",
                timeout,
            )
        except Exception:
            # The task's own ``_run`` swallows engine errors, but guard the await
            # side so a teardown surprise never aborts daemon shutdown.
            logger.debug("awaiting backfill task during shutdown raised", exc_info=True)

    def status(self) -> BackfillStatusSnapshot:
        """Return the current privacy-safe status snapshot.

        Prefers the live run state while in flight; otherwise the most recent
        terminal state, falling back to the ledger's persisted run state (so a
        status query after a daemon restart still reflects a prior run), then
        ``idle`` when nothing has ever run.
        """
        if self._running:
            state = RunState.RUNNING.value
        elif self._terminal_state is not None:
            state = self._terminal_state
        else:
            persisted = self._ledger.run_state()
            state = persisted.value if persisted is not None else STATE_IDLE
        # When not actively running, prefer the ledger's authoritative counts
        # (cross-process / post-restart truth) over the in-memory last-progress.
        if self._running:
            done, skipped, failed, total = (
                self._last_done,
                self._last_skipped,
                self._last_failed,
                self._last_total,
            )
        else:
            done, skipped, failed, total = self._ledger.progress()
        return BackfillStatusSnapshot(
            state=state,
            done=done,
            skipped=skipped,
            failed=failed,
            total=total,
            current_unit_index=self._last_unit_index,
        )

    # ------------------------------------------------------------------
    # Internal run driver.
    # ------------------------------------------------------------------

    async def _run(self, engine_kwargs: dict[str, Any]) -> None:
        from screencap.backfill.engine import run_backfill

        try:
            summary = await asyncio.to_thread(
                run_backfill,
                stop_event=self._stop_event,
                progress_cb=self._on_progress,
                ledger=self._ledger,
                **engine_kwargs,
            )
            await self._on_terminal(summary)
        except Exception:
            # The engine is documented never to raise, but an unexpected failure
            # (e.g. to_thread plumbing, a bug) must still leave the daemon up and
            # report a terminal failed state rather than wedging "running".
            logger.exception("backfill job crashed; reporting failed state")
            await self._publish_failed()
        finally:
            self._running = False

    def _on_progress(
        self, done: int, skipped: int, failed: int, total: int, unit_index: int
    ) -> None:
        """Engine ``progress_cb`` — runs on the ``to_thread`` worker thread.

        Records the snapshot for ``status`` and bridges the publish onto the
        captured event loop. NEVER receives or forwards the recording dir name
        (R9): ``unit_index`` is an opaque ordinal.
        """
        self._last_done = done
        self._last_skipped = skipped
        self._last_failed = failed
        self._last_total = total
        self._last_unit_index = unit_index
        self._bridge_publish(
            EVENT_BACKFILL_PROGRESS,
            state=RunState.RUNNING.value,
            done=done,
            skipped=skipped,
            failed=failed,
            total=total,
            current_unit_index=unit_index,
        )

    async def _on_terminal(self, summary: "BackfillSummary") -> None:
        """Publish the terminal event for a completed/paused/cancelled run."""
        self._last_done = summary.done
        self._last_skipped = summary.skipped
        self._last_failed = summary.failed
        self._last_total = summary.total
        self._terminal_state = summary.state.value
        event_type = _TERMINAL_EVENT_FOR_STATE.get(
            summary.state, EVENT_BACKFILL_COMPLETED
        )
        await self._bus.publish(
            self._event(
                event_type,
                state=summary.state.value,
                done=summary.done,
                skipped=summary.skipped,
                failed=summary.failed,
                total=summary.total,
                current_unit_index=self._last_unit_index,
            )
        )

    async def _publish_failed(self) -> None:
        self._terminal_state = "failed"
        await self._bus.publish(
            self._event(
                EVENT_BACKFILL_FAILED,
                state=self._terminal_state,
                done=self._last_done,
                skipped=self._last_skipped,
                failed=self._last_failed,
                total=self._last_total,
                current_unit_index=self._last_unit_index,
            )
        )

    def _bridge_publish(self, event_type: str, **fields: Any) -> None:
        """Schedule ``bus.publish`` on the captured loop from any thread.

        ``run_coroutine_threadsafe`` is the thread→loop bridge: the engine's
        ``progress_cb`` runs on the ``to_thread`` worker, but the EventBus is
        async and loop-affine. We fire-and-forget (drop the returned future) —
        a progress event is best-effort telemetry; losing one must never stall
        or crash the worker. Exceptions inside the coroutine surface on the loop,
        not the worker.
        """
        loop = self._loop
        if loop is None:
            return
        event = self._event(event_type, **fields)
        try:
            asyncio.run_coroutine_threadsafe(self._bus.publish(event), loop)
        except Exception:
            # Loop closed/closing (daemon shutting down) or any other scheduling
            # failure — fire-and-forget telemetry must never let an exception
            # escape onto the worker thread. Drop the event.
            logger.debug("backfill progress publish skipped (loop unavailable)")

    @staticmethod
    def _event(event_type: str, **fields: Any) -> dict[str, Any]:
        return {
            "type": event_type,
            "schema_version": _stderr_events.EVENT_SCHEMA_VERSION,
            "ts": time.time(),
            **fields,
        }


__all__ = [
    "BackfillJob",
    "BackfillStatusSnapshot",
    "should_auto_resume",
    "STATE_IDLE",
    "EVENT_BACKFILL_PROGRESS",
    "EVENT_BACKFILL_COMPLETED",
    "EVENT_BACKFILL_PAUSED",
    "EVENT_BACKFILL_CANCELLED",
    "EVENT_BACKFILL_FAILED",
]
