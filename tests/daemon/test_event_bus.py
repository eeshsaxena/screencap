"""Unit tests for the daemon event bus primitive."""

from __future__ import annotations

import asyncio

import pytest

from screencap.daemon.event_bus import (
    CursorUnknownError,
    EventBus,
    QUEUE_MAXSIZE,
    REPLAY_BUFFER_SIZE,
)


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


@pytest.mark.asyncio
async def test_subscribe_since_replays_events_after_cursor() -> None:
    bus = EventBus()
    for index in range(1, 6):
        await bus.publish({"type": f"e{index}", "ts": float(index), "schema_version": 1})

    sub = await bus.subscribe(since=2)

    replayed = [await _next_event(sub) for _ in range(3)]
    assert [event["cursor"] for event in replayed] == [3, 4, 5]
    assert [event["type"] for event in replayed] == ["e3", "e4", "e5"]
    assert sub.cursor_at_subscribe == 2


@pytest.mark.asyncio
async def test_subscribe_since_current_yields_no_replay_then_live() -> None:
    bus = EventBus()
    for index in range(1, 6):
        await bus.publish({"type": f"e{index}", "ts": float(index), "schema_version": 1})

    sub = await bus.subscribe(since=5)
    assert sub.queue.empty()

    await bus.publish({"type": "live", "ts": 6.0, "schema_version": 1})
    event = await _next_event(sub)
    assert event == {"type": "live", "ts": 6.0, "schema_version": 1, "cursor": 6}


@pytest.mark.asyncio
async def test_subscribe_since_zero_replays_full_buffer() -> None:
    bus = EventBus()
    for index in range(1, 4):
        await bus.publish({"type": f"e{index}", "ts": float(index), "schema_version": 1})

    sub = await bus.subscribe(since=0)
    replayed = [await _next_event(sub) for _ in range(3)]
    assert [event["cursor"] for event in replayed] == [1, 2, 3]


@pytest.mark.asyncio
async def test_subscribe_since_none_preserves_live_only_behavior() -> None:
    bus = EventBus()
    await bus.publish({"type": "preexisting", "ts": 1.0, "schema_version": 1})

    sub = await bus.subscribe(since=None)
    assert sub.queue.empty()
    assert sub.cursor_at_subscribe == 1

    await bus.publish({"type": "after", "ts": 2.0, "schema_version": 1})
    event = await _next_event(sub)
    assert event["type"] == "after"
    assert event["cursor"] == 2


@pytest.mark.asyncio
async def test_subscribe_since_future_cursor_raises_cursor_unknown() -> None:
    bus = EventBus()
    await bus.publish({"type": "only", "ts": 1.0, "schema_version": 1})

    with pytest.raises(CursorUnknownError) as exc_info:
        await bus.subscribe(since=99)

    assert exc_info.value.cursor == 99
    assert exc_info.value.current == 1


@pytest.mark.asyncio
async def test_subscribe_since_evicted_cursor_raises_cursor_unknown() -> None:
    bus = EventBus()
    overflow = REPLAY_BUFFER_SIZE + 50
    for index in range(1, overflow + 1):
        await bus.publish({"type": f"e{index}", "ts": float(index), "schema_version": 1})

    with pytest.raises(CursorUnknownError) as exc_info:
        await bus.subscribe(since=10)

    assert exc_info.value.cursor == 10
    assert exc_info.value.current == overflow
    assert exc_info.value.oldest_retained is not None
    assert exc_info.value.oldest_retained > 10


@pytest.mark.asyncio
async def test_replay_events_are_independent_copies() -> None:
    bus = EventBus()
    await bus.publish({"type": "shared", "payload": {"nested": "value"}, "schema_version": 1})

    first = await bus.subscribe(since=0)
    second = await bus.subscribe(since=0)

    first_event = await _next_event(first)
    second_event = await _next_event(second)

    assert first_event == second_event
    assert first_event is not second_event
    first_event["mutated"] = True
    assert "mutated" not in second_event


@pytest.mark.asyncio
async def test_replay_buffer_is_bounded() -> None:
    bus = EventBus()
    overflow = REPLAY_BUFFER_SIZE + 50
    for index in range(overflow):
        await bus.publish({"type": "burst", "i": index, "schema_version": 1})

    assert bus.current_cursor() == overflow
    assert bus.oldest_retained_cursor() == overflow - REPLAY_BUFFER_SIZE + 1


@pytest.mark.asyncio
async def test_oldest_retained_cursor_none_when_empty() -> None:
    bus = EventBus()
    assert bus.oldest_retained_cursor() is None


@pytest.mark.asyncio
async def test_shutdown_closes_replay_subscription() -> None:
    bus = EventBus()
    await bus.publish({"type": "before", "ts": 1.0, "schema_version": 1})

    sub = await bus.subscribe(since=0)
    await bus.shutdown()

    assert sub.closed.is_set()
    assert sub.close_reason == "shutdown"


@pytest.mark.asyncio
async def test_concurrent_publish_during_subscribe_orders_replay_before_live() -> None:
    """Replay events must land before any live publish to the new subscriber.

    Subscribing-with-since holds the bus lock while enqueueing replay events,
    so a concurrent ``publish`` cannot interleave. This test confirms that
    invariant by racing a publish against the subscribe call.
    """

    bus = EventBus()
    for index in range(1, 4):
        await bus.publish({"type": f"r{index}", "ts": float(index), "schema_version": 1})

    publish_task = asyncio.create_task(
        bus.publish({"type": "live", "ts": 4.0, "schema_version": 1})
    )
    sub = await bus.subscribe(since=0)
    await publish_task

    events = []
    while True:
        try:
            events.append(sub.queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    cursors = [event["cursor"] for event in events]
    assert cursors == sorted(cursors)
    assert cursors[0] == 1
