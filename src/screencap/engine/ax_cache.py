"""Async AX query cache — runs element state queries in a background thread.

Decouples accessibility IPC from the event processing pipeline so that
slow AX responses don't block mouse/keyboard event recording.
"""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

_SENTINEL = object()


@dataclass
class _AXRequest:
    """A pending AX query request."""

    request_id: str
    x: int
    y: int
    max_depth: int | None
    event_name: str


@dataclass
class AXQueryCache:
    """Background-threaded AX query executor.

    Usage::

        cache = AXQueryCache(query_fn=window.get_active_element_state)
        cache.start()
        ...
        rid = cache.submit(x, y, max_depth=4, event_name="click")
        # ... continue processing other events ...
        result = cache.get(rid, timeout=0.01)  # None if not ready
        ...
        cache.stop()
    """

    query_fn: Any  # Callable[[int, int, int|None], dict|None]
    _request_q: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=64))
    _results: dict[str, dict | None] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _ready: dict[str, threading.Event] = field(default_factory=dict)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        """Start the background query thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker, name="ax-query-cache", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the worker to stop and wait for it to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def submit(
        self, x: int, y: int, max_depth: int | None = None, event_name: str = ""
    ) -> str:
        """Submit an AX query request.  Returns a request_id for later retrieval."""
        request_id = uuid.uuid4().hex[:12]
        evt = threading.Event()
        with self._lock:
            self._ready[request_id] = evt
        req = _AXRequest(
            request_id=request_id,
            x=x,
            y=y,
            max_depth=max_depth,
            event_name=event_name,
        )
        try:
            self._request_q.put_nowait(req)
        except queue.Full:
            # Queue full — immediately mark as done with empty result
            with self._lock:
                self._results[request_id] = {}
                evt.set()
        return request_id

    def get(self, request_id: str, timeout: float = 0.01) -> dict | None:
        """Retrieve the result for *request_id*.

        Returns ``None`` if the result isn't ready within *timeout* seconds.
        Cleans up internal bookkeeping after retrieval.
        """
        evt = None
        with self._lock:
            evt = self._ready.get(request_id)
        if evt is None:
            return None

        evt.wait(timeout=timeout)

        with self._lock:
            result = self._results.pop(request_id, None)
            self._ready.pop(request_id, None)
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        """Background thread that drains the request queue."""
        while not self._stop_event.is_set():
            try:
                req = self._request_q.get(timeout=0.1)
            except queue.Empty:
                continue

            result: dict | None = {}
            try:
                result = self.query_fn(req.x, req.y, max_depth=req.max_depth)
            except Exception as exc:
                logger.warning(f"AXQueryCache query failed: {exc}")
                result = {}

            with self._lock:
                self._results[req.request_id] = result
                evt = self._ready.get(req.request_id)
                if evt is not None:
                    evt.set()
