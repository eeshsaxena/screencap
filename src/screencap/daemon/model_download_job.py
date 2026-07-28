"""Single-task model-download job holder on ``app.state`` (U5, SCR-239).

Drives the U4 download+verify engine (:func:`screencap.models.download.download_model`)
as ONE Supervisor-style background task, mirroring
:mod:`screencap.daemon.backfill_job`. A single :class:`ModelDownloadJob` lives at
``app.state.model_download_job`` and is driven by the three ``/v0/model.download.*``
verbs plus the read-only ``/v0/model.status``.

Lifecycle
---------
- :meth:`start` is **idempotent**: a second start while a download is in flight
  returns the in-flight snapshot rather than spawning a second task.
- The engine runs blocking work via ``asyncio.to_thread``; its ``progress_cb``
  fires off-loop on that one worker thread (the engine watches its staging dir
  from there while the fetch runs on a thread of its own), so it bridges
  thread→loop with ``run_coroutine_threadsafe`` (like the backfill job).
  Progress is **throttled** (≥1% delta or ≥0.5 s apart) so the sampled readings
  of a ~2 GB transfer can't flood ``/v0/events``.
- A fetch that stops producing bytes is failed by the engine's stall guard, so
  a wedged transfer reaches a terminal ``failed`` + ``stalled:…`` reason instead
  of leaving this job ``running`` — and the daemon alive — forever.
- :meth:`cancel` sets the ``threading.Event`` the engine polls between steps.
- :meth:`is_running` feeds the idle-shutdown busy predicate so the daemon never
  idle-exits mid-download.

Progress payload privacy
------------------------
Payloads carry only ``(state, model_id, bytes_done, bytes_total, reason)`` — a
public model id, byte counters, and a class-name reason. No recording context
exists here at all (matches the ``backfill.*`` recording-name-free bar).
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

logger = logging.getLogger(__name__)

EVENT_MODEL_DOWNLOAD_PROGRESS = "model.download.progress"
EVENT_MODEL_DOWNLOAD_COMPLETED = "model.download.completed"
EVENT_MODEL_DOWNLOAD_FAILED = "model.download.failed"
EVENT_MODEL_DOWNLOAD_CANCELLED = "model.download.cancelled"

_TERMINAL_EVENT_FOR_STATE = {
    "installed": EVENT_MODEL_DOWNLOAD_COMPLETED,
    "failed": EVENT_MODEL_DOWNLOAD_FAILED,
    "cancelled": EVENT_MODEL_DOWNLOAD_CANCELLED,
}

STATE_IDLE = "idle"
STATE_DOWNLOADING = "downloading"

# Throttle: publish a progress event only when the percentage advanced by at
# least this many points OR at least this long has elapsed since the last one.
_MIN_PERCENT_DELTA = 1.0
_MIN_INTERVAL_S = 0.5


@dataclass(frozen=True)
class ModelDownloadStatus:
    """The wire shape of ``model.download.status`` — no recording context."""

    state: str
    model_id: str | None
    bytes_done: int
    bytes_total: int
    reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "model_id": self.model_id,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "reason": self.reason,
        }


class ModelDownloadJob:
    """Owns the single in-flight download task + its stop flag + loop reference."""

    def __init__(self, bus: "EventBus") -> None:
        self._bus = bus
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._model_id: str | None = None
        self._bytes_done = 0
        self._bytes_total = 0
        self._terminal_state: str | None = None
        self._terminal_reason: str | None = None
        # Throttle bookkeeping. Written off-loop, but the engine reports every
        # reading — sampled and terminal alike — from the single `asyncio.to_thread`
        # worker, so only one thread is ever in `_should_emit` at a time;
        # GIL-guarded.
        self._last_emit_pct = -1.0
        self._last_emit_t = 0.0
        self._last_emit_bytes = -1

    # -- Public API (driven by the daemon verbs) --------------------------

    def is_running(self) -> bool:
        return self._running

    def start(self, model_id: str | None = None, **engine_kwargs: Any) -> ModelDownloadStatus:
        """Start a download, or return the in-flight snapshot if one exists."""
        if self._running:
            return self.status()
        self._loop = asyncio.get_running_loop()
        self._stop_event = threading.Event()
        self._model_id = model_id
        self._bytes_done = 0
        self._bytes_total = 0
        self._terminal_state = None
        self._terminal_reason = None
        self._last_emit_pct = -1.0
        self._last_emit_t = 0.0
        self._last_emit_bytes = -1
        self._running = True
        self._task = asyncio.create_task(self._run(model_id, engine_kwargs))
        return self.status()

    def cancel(self) -> ModelDownloadStatus:
        if self._running:
            self._stop_event.set()
        return self.status()

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Signal an in-flight download to stop and await the worker, bounded."""
        task = self._task
        if task is None or task.done():
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(task, timeout)
        except asyncio.TimeoutError:
            logger.warning("model download did not stop within %.1fs of shutdown", timeout)
        except Exception:
            logger.debug("awaiting model-download task during shutdown raised", exc_info=True)

    def status(self) -> ModelDownloadStatus:
        if self._running:
            state = STATE_DOWNLOADING
        elif self._terminal_state is not None:
            state = self._terminal_state
        else:
            state = STATE_IDLE
        return ModelDownloadStatus(
            state=state,
            model_id=self._model_id,
            bytes_done=self._bytes_done,
            bytes_total=self._bytes_total,
            reason=self._terminal_reason,
        )

    # -- Internal run driver ----------------------------------------------

    async def _run(self, model_id: str | None, engine_kwargs: dict[str, Any]) -> None:
        from screencap.models.download import ModelNotPinnedError, download_model

        try:
            result = await asyncio.to_thread(
                download_model,
                model_id,
                progress_cb=self._on_progress,
                stop_event=self._stop_event,
                **engine_kwargs,
            )
            await self._on_terminal(result)
        except ModelNotPinnedError:
            # The shipped default model is PLACEHOLDER-pinned until release QA, so
            # this is the expected outcome of a v1 download attempt — surface a
            # distinct, greppable, non-retryable reason instead of the opaque
            # "job-crashed" the broad handler would report.
            logger.info("model download refused: model not release-pinned")
            await self._publish_terminal("failed", "not-release-pinned")
        except Exception:
            logger.exception("model download job crashed; reporting failed")
            await self._publish_terminal("failed", "job-crashed")
        finally:
            self._running = False

    def _on_progress(self, bytes_done: int, bytes_total: int) -> None:
        """Engine ``progress_cb`` — runs off-loop (see the module docstring)."""
        self._bytes_done = bytes_done
        self._bytes_total = bytes_total
        if not self._should_emit(bytes_done, bytes_total):
            return
        self._bridge_publish(
            EVENT_MODEL_DOWNLOAD_PROGRESS,
            state=STATE_DOWNLOADING,
            model_id=self._model_id,
            bytes_done=bytes_done,
            bytes_total=bytes_total,
            reason=None,
        )

    def _should_emit(self, bytes_done: int, bytes_total: int) -> bool:
        # Unchanged byte count → never emit. The engine samples bytes-on-disk on a
        # fixed cadence, so a stalled transfer keeps reporting the same number; the
        # elapsed-time branch below would otherwise re-publish an identical payload
        # to every /v0/events subscriber twice a second for as long as the stall
        # lasts. Time is a heartbeat for *slow* progress, not for no progress.
        if bytes_done == self._last_emit_bytes:
            return False
        now = time.monotonic()
        pct = (100.0 * bytes_done / bytes_total) if bytes_total else 0.0
        if (
            pct - self._last_emit_pct >= _MIN_PERCENT_DELTA
            or now - self._last_emit_t >= _MIN_INTERVAL_S
        ):
            self._last_emit_pct = pct
            self._last_emit_t = now
            self._last_emit_bytes = bytes_done
            return True
        return False

    async def _on_terminal(self, result: Any) -> None:
        self._bytes_done = self._bytes_total  # terminal reflects completion
        state = getattr(result, "state", "failed")
        self._terminal_state = state
        self._terminal_reason = getattr(result, "reason", None)
        await self._publish_terminal(state, self._terminal_reason)

    async def _publish_terminal(self, state: str, reason: str | None) -> None:
        self._terminal_state = state
        self._terminal_reason = reason
        event_type = _TERMINAL_EVENT_FOR_STATE.get(state, EVENT_MODEL_DOWNLOAD_FAILED)
        await self._bus.publish(
            self._event(
                event_type,
                state=state,
                model_id=self._model_id,
                bytes_done=self._bytes_done,
                bytes_total=self._bytes_total,
                reason=reason,
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
            logger.debug("model-download progress publish skipped (loop unavailable)")

    @staticmethod
    def _event(event_type: str, **fields: Any) -> dict[str, Any]:
        return {
            "type": event_type,
            "schema_version": _stderr_events.EVENT_SCHEMA_VERSION,
            "ts": time.time(),
            **fields,
        }


__all__ = [
    "ModelDownloadJob",
    "ModelDownloadStatus",
    "STATE_IDLE",
    "STATE_DOWNLOADING",
    "EVENT_MODEL_DOWNLOAD_PROGRESS",
    "EVENT_MODEL_DOWNLOAD_COMPLETED",
    "EVENT_MODEL_DOWNLOAD_FAILED",
    "EVENT_MODEL_DOWNLOAD_CANCELLED",
]
