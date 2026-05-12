"""In-process daemon event fan-out."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

QUEUE_MAXSIZE = 1024
REPLAY_BUFFER_SIZE = 256

SLOW_CONSUMER = "slow_consumer"
SHUTDOWN = "shutdown"


class CursorUnknownError(Exception):
    """Raised when ``subscribe(since=…)`` cannot honor the requested cursor.

    A cursor is unknown when it is either ahead of the bus (the caller has a
    stamp the bus has not yet produced) or behind the bus's retained replay
    window (the corresponding event has aged out of the ring). The HTTP
    boundary maps this to the existing ``cursor_unknown`` envelope (HTTP 410).
    """

    def __init__(self, cursor: int, current: int, oldest_retained: int | None) -> None:
        super().__init__(
            f"cursor {cursor} unknown (current={current}, oldest_retained={oldest_retained})"
        )
        self.cursor = cursor
        self.current = current
        self.oldest_retained = oldest_retained


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

    A bounded replay ring retains the last ``REPLAY_BUFFER_SIZE`` stamped
    events so that late subscribers can pass ``subscribe(since=cursor)`` and
    pick up anything published in the await gap between cursor capture and
    subscription. The ring is bounded by event count, not time — subscribers
    request a cursor and receive whatever is still retained or
    :class:`CursorUnknownError` otherwise.
    """

    def __init__(self) -> None:
        self._subscribers: list[_Subscription] = []
        self._cursor = 0
        self._buffer: deque[dict[str, Any]] = deque(maxlen=REPLAY_BUFFER_SIZE)
        self._lock = asyncio.Lock()
        self._shutting_down = False

    def current_cursor(self) -> int:
        """Return the current event cursor without advancing it."""
        return self._cursor

    def oldest_retained_cursor(self) -> int | None:
        """Return the oldest cursor still present in the replay ring, or None."""
        if not self._buffer:
            return None
        return self._buffer[0]["cursor"]

    async def subscribe(self, since: int | None = None) -> _Subscription:
        """Create a subscription, optionally replaying retained events.

        With ``since=None`` (the default) the subscriber receives only events
        published after this method returns. With ``since`` set the subscriber
        first receives every retained event whose cursor is greater than
        ``since`` (in ascending order) before the live stream begins.

        Raises :class:`CursorUnknownError` when ``since`` is ahead of the
        bus's current cursor or behind the oldest retained event in the ring.
        ``since=0`` is always valid: it means "from before any event" and
        replays whatever is currently in the ring.
        """
        async with self._lock:
            if since is not None:
                if since > self._cursor:
                    raise CursorUnknownError(
                        cursor=since,
                        current=self._cursor,
                        oldest_retained=self.oldest_retained_cursor(),
                    )
                oldest = self.oldest_retained_cursor()
                if oldest is not None and since < oldest - 1:
                    raise CursorUnknownError(
                        cursor=since,
                        current=self._cursor,
                        oldest_retained=oldest,
                    )
                sub = _Subscription(cursor_at_subscribe=since)
            else:
                sub = _Subscription(cursor_at_subscribe=self._cursor)

            if self._shutting_down:
                self._close(sub, SHUTDOWN)
                return sub

            if since is not None:
                for stamped in self._buffer:
                    if stamped["cursor"] > since:
                        sub.queue.put_nowait(dict(stamped))

            self._subscribers.append(sub)
            return sub

    async def publish(self, event: dict[str, Any]) -> None:
        """Stamp ``event`` with the next cursor and deliver it to subscribers."""
        async with self._lock:
            self._cursor += 1
            stamped = {**event, "cursor": self._cursor}
            self._buffer.append(stamped)
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
    "CursorUnknownError",
    "QUEUE_MAXSIZE",
    "REPLAY_BUFFER_SIZE",
    "SLOW_CONSUMER",
    "SHUTDOWN",
]
