"""Unit tests for the daemon event bus primitive."""

from __future__ import annotations

import pytest

from screencap.daemon.event_bus import EventBus, QUEUE_MAXSIZE


async def _next_event(sub) -> dict:
    return await sub.queue.get()


@pytest.mark.asyncio
async def test_subscriber_receives_published_events_in_order_with_cursors() -> None:
    bus = EventBus()
    sub = await bus.subscribe()

    await bus.publish({"type": "one", "ts": 1.0, "schema_version": 1})
    await bus.publish({"type": "two", "ts": 2.0, "schema_version": 1})
    await bus.publish({"type": "three", "ts": 3.0, "schema_version": 1})

    events = [await _next_event(sub) for _ in range(3)]

    assert [event["type"] for event in events] == ["one", "two", "three"]
    assert [event["cursor"] for event in events] == [1, 2, 3]
    assert bus.current_cursor() == 3


@pytest.mark.asyncio
async def test_two_subscribers_receive_independent_copies_in_same_order() -> None:
    bus = EventBus()
    first = await bus.subscribe()
    second = await bus.subscribe()

    await bus.publish({"type": "first", "ts": 1.0, "schema_version": 1})
    await bus.publish({"type": "second", "ts": 2.0, "schema_version": 1})

    first_events = [await _next_event(first) for _ in range(2)]
    second_events = [await _next_event(second) for _ in range(2)]

    assert first_events == second_events
    assert [event["cursor"] for event in first_events] == [1, 2]
    assert first_events[0] is not second_events[0]


@pytest.mark.asyncio
async def test_subscribe_records_current_cursor_without_replay() -> None:
    bus = EventBus()
    await bus.publish({"type": "preexisting", "ts": 1.0, "schema_version": 1})

    sub = await bus.subscribe()
    await bus.publish({"type": "after", "ts": 2.0, "schema_version": 1})

    assert sub.cursor_at_subscribe == 1
    event = await _next_event(sub)
    assert event["type"] == "after"
    assert event["cursor"] == 2


@pytest.mark.asyncio
async def test_slow_consumer_is_closed_and_other_subscribers_continue() -> None:
    bus = EventBus()
    slow = await bus.subscribe()
    fast = await bus.subscribe()

    for index in range(QUEUE_MAXSIZE):
        slow.queue.put_nowait({"type": "queued", "cursor": index})

    await bus.publish({"type": "overflow", "ts": 1.0, "schema_version": 1})
    await bus.publish({"type": "next", "ts": 2.0, "schema_version": 1})

    assert slow.closed.is_set()
    assert slow.close_reason == "slow_consumer"
    assert [await _next_event(fast), await _next_event(fast)] == [
        {"type": "overflow", "ts": 1.0, "schema_version": 1, "cursor": 1},
        {"type": "next", "ts": 2.0, "schema_version": 1, "cursor": 2},
    ]


@pytest.mark.asyncio
async def test_shutdown_closes_every_subscriber() -> None:
    bus = EventBus()
    first = await bus.subscribe()
    second = await bus.subscribe()

    await bus.shutdown()

    assert first.closed.is_set()
    assert first.close_reason == "shutdown"
    assert second.closed.is_set()
    assert second.close_reason == "shutdown"


@pytest.mark.asyncio
async def test_publish_does_not_validate_payload_shape() -> None:
    bus = EventBus()
    sub = await bus.subscribe()

    await bus.publish({"malformed": True})

    assert await _next_event(sub) == {"malformed": True, "cursor": 1}
