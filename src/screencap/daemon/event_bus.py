"""In-process daemon event fan-out."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

QUEUE_MAXSIZE = 1024

SLOW_CONSUMER = "slow_consumer"
SHUTDOWN = "shutdown"


@dataclass(eq=False)
class _Subscription:
    """A single event stream subscriber."""

    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    )
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    close_reason: str | None = None
    cursor_at_subscribe: int = 0


class EventBus:
    """Monotonic-cursor event bus with per-subscriber bounded queues.

    Producers call ``publish()`` with an already-parsed event dictionary from
    any source. The bus deliberately does not validate the event taxonomy; it
    only stamps the authoritative cursor and fans out independent copies.
    """

    def __init__(self) -> None:
        self._subscribers: list[_Subscription] = []
        self._cursor = 0
        self._lock = asyncio.Lock()
        self._shutting_down = False

    def current_cursor(self) -> int:
        """Return the current event cursor without advancing it."""
        return self._cursor

    async def subscribe(self) -> _Subscription:
        """Create a subscription at the current cursor.

        Phase 1 has no replay buffer, so the caller only receives events
        published after this method returns.
        """
        async with self._lock:
            sub = _Subscription(cursor_at_subscribe=self._cursor)
            if self._shutting_down:
                self._close(sub, SHUTDOWN)
            else:
                self._subscribers.append(sub)
            return sub

    async def publish(self, event: dict[str, Any]) -> None:
        """Stamp ``event`` with the next cursor and deliver it to subscribers."""
        async with self._lock:
            self._cursor += 1
            stamped = {**event, "cursor": self._cursor}
            survivors: list[_Subscription] = []
            for sub in self._subscribers:
                if sub.closed.is_set():
                    continue
                try:
                    sub.queue.put_nowait(dict(stamped))
                except asyncio.QueueFull:
                    self._close(sub, SLOW_CONSUMER)
                else:
                    survivors.append(sub)
            self._subscribers = survivors

    async def shutdown(self) -> None:
        """Close all subscribers so stream handlers can drain to EOF."""
        async with self._lock:
            self._shutting_down = True
            for sub in self._subscribers:
                self._close(sub, SHUTDOWN)
            self._subscribers = []

    async def remove(self, sub: _Subscription) -> None:
        """Remove a subscription after client disconnect or stream completion."""
        async with self._lock:
            self._close(sub, sub.close_reason)
            self._subscribers = [candidate for candidate in self._subscribers if candidate is not sub]

    @staticmethod
    def _close(sub: _Subscription, reason: str | None) -> None:
        if reason is not None:
            sub.close_reason = reason
        sub.closed.set()


__all__ = [
    "EventBus",
    "QUEUE_MAXSIZE",
    "SLOW_CONSUMER",
    "SHUTDOWN",
]
