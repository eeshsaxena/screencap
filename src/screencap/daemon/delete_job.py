"""Single-task range-delete job holder on ``app.state`` (U8, KTD-3).

The daemon runs the irreversible range-delete core
(:func:`screencap.range_delete.execute_delete`) as ONE Supervisor-style
background task, mirroring :mod:`screencap.daemon.backfill_job`. A single
:class:`DeleteJob` instance lives at ``app.state.delete_job`` and is driven by the
``/v0/delete.start`` (execute mode), ``/v0/delete.status``, and
``/v0/delete.cancel`` verbs. The synchronous ``dry_run`` PREVIEW is served
directly by the handler (a read), not as a job.

Lifecycle
---------
- :meth:`start` is **idempotent**: a second ``start`` while a run is in flight
  returns the existing snapshot rather than spawning a second delete task (one
  destructive job at a time).
- The core runs blocking work via ``asyncio.to_thread``; its ``progress_cb`` fires
  on that worker thread and is bridged to the EventBus with
  ``run_coroutine_threadsafe`` (the backfill pattern).
- :meth:`cancel` sets the ``threading.Event`` the core polls BETWEEN recordings
  (each recording is atomic under its flock — a cancel never tears a delete).
- :meth:`is_running` feeds the idle-shutdown busy predicate so the daemon never
  idle-exits mid-delete.

Progress payload privacy (R9)
-----------------------------
Progress / status carry ONLY ``(state, done, total, current_unit_index,
deleted_chunks, reconfirm_required)`` — all opaque counts / a bool. The recording
directory name is NEVER published (the EventBus is readable by any same-EUID
subscriber, including the MCP subscription). ``reconfirm_required`` is a bool; the
client re-previews (a direct reply) to learn WHICH recordings changed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from screencap import _stderr_events

if TYPE_CHECKING:
    from screencap.daemon.event_bus import EventBus
    from screencap.range_delete import DeleteReport

logger = logging.getLogger(__name__)

EVENT_DELETE_PROGRESS = "delete.progress"
EVENT_DELETE_COMPLETED = "delete.completed"
EVENT_DELETE_RECONFIRM = "delete.reconfirm_required"
EVENT_DELETE_CANCELLED = "delete.cancelled"
EVENT_DELETE_FAILED = "delete.failed"

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_RECONFIRM = "reconfirm_required"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"


@dataclass(frozen=True)
class DeleteStatusSnapshot:
    """Privacy-safe status snapshot — the wire shape of ``delete.status``.

    Carries ONLY the run state + opaque counts + the ``reconfirm_required`` bool.
    Never a recording directory name (R9).
    """

    state: str
    done: int
    total: int
    current_unit_index: int
    deleted_chunks: int
    reconfirm_required: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "done": self.done,
            "total": self.total,
            "current_unit_index": self.current_unit_index,
            "deleted_chunks": self.deleted_chunks,
            "reconfirm_required": self.reconfirm_required,
        }


class DeleteJob:
    """Owns the single in-flight range-delete task + its stop flag + loop ref."""

    def __init__(self, bus: "EventBus") -> None:
        self._bus = bus
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._last_done = 0
        self._last_total = 0
        self._last_unit_index = 0
        self._deleted_chunks = 0
        self._reconfirm = False
        self._terminal_state: str | None = None

    # ------------------------------------------------------------------
    # Public API (driven by the verbs).
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._running

    def start(
        self,
        start_ms: int,
        end_ms: int,
        resolved: dict[str, list[int]],
        **execute_kwargs: Any,
    ) -> DeleteStatusSnapshot:
        """Start an execute run, or return the in-flight run's status if one exists.

        Idempotent: a second ``start`` while a run is in flight does NOT spawn a
        second delete task. ``execute_kwargs`` are test injectables forwarded to
        ``execute_delete`` (``recordings_dir``).
        """
        if self._running:
            return self.status()

        self._loop = asyncio.get_running_loop()
        self._stop_event = threading.Event()
        self._terminal_state = None
        self._last_done = 0
        self._last_total = len(resolved)
        self._last_unit_index = 0
        self._deleted_chunks = 0
        self._reconfirm = False
        self._running = True
        self._task = asyncio.create_task(
            self._run(start_ms, end_ms, resolved, execute_kwargs)
        )
        return self.status()

    def cancel(self) -> DeleteStatusSnapshot:
        """Signal the core to stop between recordings. Returns the snapshot."""
        if self._running:
            self._stop_event.set()
        return self.status()

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Signal an in-flight run to stop and await the worker, bounded.

        Mirrors ``BackfillJob.shutdown``: a ``to_thread`` worker is not cancellable
        from the loop side, so we set the stop flag (honored between recordings)
        and await the task within ``timeout`` so a terminal event still publishes.
        """
        task = self._task
        if task is None or task.done():
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(task, timeout)
        except asyncio.TimeoutError:
            logger.warning("delete job did not stop within %.1fs of shutdown", timeout)
        except Exception:
            logger.debug("awaiting delete task during shutdown raised", exc_info=True)

    def status(self) -> DeleteStatusSnapshot:
        if self._running:
            state = STATE_RUNNING
        elif self._terminal_state is not None:
            state = self._terminal_state
        else:
            state = STATE_IDLE
        return DeleteStatusSnapshot(
            state=state,
            done=self._last_done,
            total=self._last_total,
            current_unit_index=self._last_unit_index,
            deleted_chunks=self._deleted_chunks,
            reconfirm_required=self._reconfirm,
        )

    # ------------------------------------------------------------------
    # Internal run driver.
    # ------------------------------------------------------------------

    async def _run(
        self,
        start_ms: int,
        end_ms: int,
        resolved: dict[str, list[int]],
        execute_kwargs: dict[str, Any],
    ) -> None:
        from screencap.range_delete import execute_delete

        try:
            report = await asyncio.to_thread(
                execute_delete,
                start_ms,
                end_ms,
                resolved,
                stop_event=self._stop_event,
                progress_cb=self._on_progress,
                **execute_kwargs,
            )
            await self._on_terminal(report)
        except Exception:
            logger.exception("delete job crashed; reporting failed state")
            await self._publish_failed()
        finally:
            self._running = False

    def _on_progress(self, done: int, total: int, unit_index: int) -> None:
        """Core ``progress_cb`` — runs on the ``to_thread`` worker thread (R9)."""
        self._last_done = done
        self._last_total = total
        self._last_unit_index = unit_index
        self._bridge_publish(
            EVENT_DELETE_PROGRESS,
            state=STATE_RUNNING,
            done=done,
            total=total,
            current_unit_index=unit_index,
        )

    async def _on_terminal(self, report: "DeleteReport") -> None:
        self._deleted_chunks = report.deleted_chunk_count
        self._reconfirm = report.reconfirm_required
        if self._stop_event.is_set():
            self._terminal_state = STATE_CANCELLED
            event_type = EVENT_DELETE_CANCELLED
        elif report.reconfirm_required:
            self._terminal_state = STATE_RECONFIRM
            event_type = EVENT_DELETE_RECONFIRM
        else:
            self._terminal_state = STATE_COMPLETED
            event_type = EVENT_DELETE_COMPLETED
        await self._bus.publish(
            self._event(
                event_type,
                state=self._terminal_state,
                done=self._last_done,
                total=self._last_total,
                current_unit_index=self._last_unit_index,
                deleted_chunks=self._deleted_chunks,
                reconfirm_required=self._reconfirm,
            )
        )

    async def _publish_failed(self) -> None:
        self._terminal_state = STATE_FAILED
        await self._bus.publish(
            self._event(
                EVENT_DELETE_FAILED,
                state=STATE_FAILED,
                done=self._last_done,
                total=self._last_total,
                current_unit_index=self._last_unit_index,
                deleted_chunks=self._deleted_chunks,
                reconfirm_required=self._reconfirm,
            )
        )

    def _bridge_publish(self, event_type: str, **fields: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        event = self._event(event_type, **fields)
        try:
            asyncio.run_coroutine_threadsafe(self._bus.publish(event), loop)
        except Exception:
            logger.debug("delete progress publish skipped (loop unavailable)")

    @staticmethod
    def _event(event_type: str, **fields: Any) -> dict[str, Any]:
        return {
            "type": event_type,
            "schema_version": _stderr_events.EVENT_SCHEMA_VERSION,
            "ts": time.time(),
            **fields,
        }


__all__ = [
    "DeleteJob",
    "DeleteStatusSnapshot",
    "STATE_IDLE",
    "STATE_RUNNING",
    "STATE_COMPLETED",
    "STATE_RECONFIRM",
    "STATE_CANCELLED",
    "STATE_FAILED",
    "EVENT_DELETE_PROGRESS",
    "EVENT_DELETE_COMPLETED",
    "EVENT_DELETE_RECONFIRM",
    "EVENT_DELETE_CANCELLED",
    "EVENT_DELETE_FAILED",
]
