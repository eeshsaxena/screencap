"""HTTP integration tests for the daemon NDJSON event stream."""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
from typing import AsyncIterator

import httpx
import pytest
from starlette.datastructures import QueryParams

from screencap.daemon import errors, schema
from screencap.daemon.event_bus import QUEUE_MAXSIZE


class _StreamRequest:
    def __init__(self, app, path: str = "/v0/events") -> None:
        self.app = app
        query = ""
        if "?" in path:
            _, query = path.split("?", 1)
        self.query_params = QueryParams(query)

    async def is_disconnected(self) -> bool:
        return False


async def _build_app():
    from screencap.daemon.app import build_app

    return build_app()


async def _open_stream(app, path: str = "/v0/events"):
    from screencap.daemon.app import events_stream

    response = await events_stream(_StreamRequest(app, path))
    return response, getattr(response, "body_iterator", None)


async def _read_line(lines: AsyncIterator[str]) -> dict:
    line = await anext(lines)
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line.strip())


@pytest.mark.asyncio
async def test_events_stream_returns_ndjson_subscribed_frame_and_published_events() -> None:
    app = await _build_app()
    response, lines = await _open_stream(app)

    assert response.status_code == 200
    assert response.media_type == "application/x-ndjson"
    first = await _read_line(lines)
    assert first["type"] == "subscribed"
    assert first["schema_version"] == 1
    assert first["cursor"] == app.state.event_bus.current_cursor()

    await app.state.event_bus.publish(
        {"type": "published", "ts": 1.0, "schema_version": 1}
    )
    assert await _read_line(lines) == {
        "type": "published",
        "ts": 1.0,
        "schema_version": 1,
        "cursor": 1,
    }
    await lines.aclose()


@pytest.mark.asyncio
async def test_events_stream_fans_out_to_two_subscribers() -> None:
    app = await _build_app()
    _first_response, first_lines = await _open_stream(app)
    _second_response, second_lines = await _open_stream(app)
    await _read_line(first_lines)
    await _read_line(second_lines)

    await app.state.event_bus.publish(
        {"type": "fanout", "ts": 1.0, "schema_version": 1}
    )

    assert await _read_line(first_lines) == await _read_line(second_lines)
    await first_lines.aclose()
    await second_lines.aclose()


@pytest.mark.asyncio
async def test_events_since_current_attaches_live_only() -> None:
    """``?since=current_cursor`` yields no replay and starts live from the
    next published event. The `subscribed` frame's cursor reflects the
    requested cursor since replay begins strictly after that point."""
    app = await _build_app()
    await app.state.event_bus.publish(
        {"type": "existing", "ts": 1.0, "schema_version": 1}
    )
    current = app.state.event_bus.current_cursor()

    response, lines = await _open_stream(app, f"/v0/events?since={current}")
    assert response.status_code == 200
    assert (await _read_line(lines))["cursor"] == current
    await lines.aclose()


@pytest.mark.asyncio
async def test_events_since_in_retained_window_replays_then_lives() -> None:
    """``?since=N`` with N inside the retained replay window yields every
    event with cursor > N (in ascending order) before live delivery
    resumes. This is the contract the SwiftUI shell depends on to avoid
    missing `recording_started` across the recording.start → events
    subscribe gap."""
    app = await _build_app()
    for index in range(5):
        await app.state.event_bus.publish(
            {"type": f"e{index}", "ts": float(index), "schema_version": 1}
        )
    current = app.state.event_bus.current_cursor()  # == 5

    response, lines = await _open_stream(app, f"/v0/events?since={current - 3}")
    assert response.status_code == 200

    subscribed = await _read_line(lines)
    assert subscribed["type"] == "subscribed"
    assert subscribed["cursor"] == current - 3

    # Replay yields cursors current-2, current-1, current — exactly the
    # events published after the requested cursor.
    replayed = [await _read_line(lines) for _ in range(3)]
    assert [event["cursor"] for event in replayed] == [
        current - 2,
        current - 1,
        current,
    ]

    # Live delivery picks up where replay left off.
    await app.state.event_bus.publish(
        {"type": "live", "ts": 99.0, "schema_version": 1}
    )
    live = await _read_line(lines)
    assert live["type"] == "live"
    assert live["cursor"] == current + 1
    await lines.aclose()


@pytest.mark.asyncio
async def test_account_mismatch_event_replays_to_late_subscriber() -> None:
    """SCR-171: the daemon publishes ``account_mismatch`` during the startup
    sweep, which can run BEFORE a subscriber (MCP server / macOS app) connects.
    A late subscriber that captured a snapshot cursor first and reconnects with
    ``?since=<cursor>`` still receives the event via replay — the contract that
    keeps the push signal from being missed across the connect gap. Published
    BEFORE the subscriber joins on purpose (the production timing the replay
    buffer exists to cover), not after."""
    from screencap import _stderr_events

    app = await _build_app()
    # Snapshot cursor captured BEFORE the sweep publishes.
    snapshot = app.state.event_bus.current_cursor()

    await app.state.event_bus.publish(
        {
            "type": _stderr_events.EVENT_ACCOUNT_MISMATCH,
            "schema_version": _stderr_events.EVENT_SCHEMA_VERSION,
            "ts": 1.0,
            "recording": "rec-A",
            "owner_uid": "uid-A",
            "signed_in_uid": "uid-B",
            "signed_in_email": "b@example.com",
            "stale": False,
        }
    )

    response, lines = await _open_stream(app, f"/v0/events?since={snapshot}")
    assert response.status_code == 200
    subscribed = await _read_line(lines)
    assert subscribed["type"] == "subscribed"

    replayed = await _read_line(lines)
    assert replayed["type"] == _stderr_events.EVENT_ACCOUNT_MISMATCH
    assert replayed["cursor"] == snapshot + 1
    assert replayed["recording"] == "rec-A"
    assert replayed["owner_uid"] == "uid-A"
    assert replayed["signed_in_uid"] == "uid-B"
    assert replayed["signed_in_email"] == "b@example.com"
    assert replayed["stale"] is False
    await lines.aclose()


@pytest.mark.asyncio
async def test_events_since_zero_replays_full_retained_window() -> None:
    """``?since=0`` means "from before any event" and replays everything
    currently retained in the ring."""
    app = await _build_app()
    for index in range(3):
        await app.state.event_bus.publish(
            {"type": f"e{index}", "ts": float(index), "schema_version": 1}
        )

    response, lines = await _open_stream(app, "/v0/events?since=0")
    assert response.status_code == 200
    await _read_line(lines)  # drain `subscribed`
    replayed = [await _read_line(lines) for _ in range(3)]
    assert [event["cursor"] for event in replayed] == [1, 2, 3]
    await lines.aclose()


@pytest.mark.asyncio
async def test_events_since_future_cursor_returns_410_cursor_unknown() -> None:
    """``?since=N`` with N strictly greater than the current cursor is
    a future cursor the daemon has never produced — return 410 with the
    typed `cursor_unknown` envelope."""
    app = await _build_app()
    await app.state.event_bus.publish(
        {"type": "existing", "ts": 1.0, "schema_version": 1}
    )
    current = app.state.event_bus.current_cursor()

    response, _body = await _open_stream(app, f"/v0/events?since={current + 5}")
    assert response.status_code == 410
    assert response.media_type == "application/json"
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["error"] == errors.CURSOR_UNKNOWN
    assert payload["requested_cursor"] == current + 5
    assert payload["schema_version"] == schema._EVENTS_API_VERSION


@pytest.mark.asyncio
async def test_events_since_older_than_retained_window_returns_410() -> None:
    """``?since=N`` with N older than the oldest retained cursor must
    return 410 `cursor_unknown` — the Phase 1 plan promised this contract
    but the code previously took the silent "live from now" fallback
    instead. U2 finally aligns the two."""
    from screencap.daemon.event_bus import REPLAY_BUFFER_SIZE

    app = await _build_app()
    overflow = REPLAY_BUFFER_SIZE + 20
    for index in range(overflow):
        await app.state.event_bus.publish(
            {"type": "burst", "i": index, "schema_version": 1}
        )

    # 10 is well older than the oldest retained cursor (overflow - REPLAY_BUFFER_SIZE + 1).
    response, _body = await _open_stream(app, "/v0/events?since=10")
    assert response.status_code == 410
    payload = json.loads(response.body)
    assert payload["error"] == errors.CURSOR_UNKNOWN
    assert payload["requested_cursor"] == 10


@pytest.mark.asyncio
async def test_events_since_negative_returns_400_invalid_cursor() -> None:
    """Negative cursors are invalid input, not unknown — they map to 400
    `invalid_cursor`, not 410. Preserved from prior behavior."""
    app = await _build_app()
    response, _body = await _open_stream(app, "/v0/events?since=-1")
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["error"] == errors.ERROR_CODE_INVALID_CURSOR
    assert payload["requested_cursor"] == "-1"


@pytest.mark.asyncio
async def test_events_since_non_int_returns_invalid_cursor_error() -> None:
    app = await _build_app()
    response, _body = await _open_stream(app, "/v0/events?since=abc")

    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["error"] == "invalid_cursor"
    assert payload["schema_version"] == schema._EVENTS_API_VERSION


@pytest.mark.asyncio
async def test_snapshot_cursor_comes_from_event_bus_without_advancing() -> None:
    app = await _build_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    async with client:
        await app.state.event_bus.publish({"type": "event", "ts": 1.0, "schema_version": 1})
        first = (await client.get("/v0/session.snapshot")).json()
        second = (await client.get("/v0/session.snapshot")).json()

    assert first["cursor"] == 1
    assert second["cursor"] == 1


@pytest.mark.asyncio
async def test_shutdown_drains_queued_events_before_close_frame() -> None:
    # Drain-to-EOF invariant: events queued before shutdown must reach the
    # subscriber before the synthetic _close frame. Without this, a final
    # `recording_finalized` is lost on SIGTERM during a live recording.
    app = await _build_app()
    _response, lines = await _open_stream(app)
    await _read_line(lines)  # drain `subscribed` frame

    # Queue events directly on the subscriber's queue, then close the bus.
    sub = app.state.event_bus._subscribers[0]
    sub.queue.put_nowait({"type": "queued_one", "ts": 1.0, "schema_version": 1, "cursor": 1})
    sub.queue.put_nowait({"type": "queued_two", "ts": 2.0, "schema_version": 1, "cursor": 2})

    await app.state.event_bus.shutdown()

    assert (await _read_line(lines))["type"] == "queued_one"
    assert (await _read_line(lines))["type"] == "queued_two"
    close = await _read_line(lines)
    assert close["type"] == "_close"
    assert close["reason"] == "shutdown"
    with pytest.raises(StopAsyncIteration):
        await anext(lines)


@pytest.mark.asyncio
async def test_shutdown_emits_close_frame_then_eof() -> None:
    app = await _build_app()
    _response, lines = await _open_stream(app)
    await _read_line(lines)

    await app.state.event_bus.shutdown()

    close = await _read_line(lines)
    assert close["type"] == "_close"
    assert close["reason"] == "shutdown"
    with pytest.raises(StopAsyncIteration):
        await anext(lines)


@pytest.mark.asyncio
async def test_slow_consumer_gets_close_frame_and_fast_consumer_continues() -> None:
    app = await _build_app()
    _slow_response, slow_lines = await _open_stream(app)
    _fast_response, fast_lines = await _open_stream(app)
    await _read_line(slow_lines)
    await _read_line(fast_lines)

    slow_sub = app.state.event_bus._subscribers[0]
    for index in range(QUEUE_MAXSIZE):
        slow_sub.queue.put_nowait({"type": "queued", "cursor": index})

    await app.state.event_bus.publish(
        {"type": "overflow", "ts": 1.0, "schema_version": 1}
    )
    await app.state.event_bus.publish(
        {"type": "after", "ts": 2.0, "schema_version": 1}
    )

    close = await _read_line(slow_lines)
    assert close["type"] == "_close"
    assert close["reason"] == "slow_consumer"

    assert await _read_line(fast_lines) == {
        "type": "overflow",
        "ts": 1.0,
        "schema_version": 1,
        "cursor": 1,
    }
    assert await _read_line(fast_lines) == {
        "type": "after",
        "ts": 2.0,
        "schema_version": 1,
        "cursor": 2,
    }
    await fast_lines.aclose()


@pytest.mark.asyncio
async def test_malformed_publish_payload_still_flows_over_http() -> None:
    app = await _build_app()
    _response, lines = await _open_stream(app)
    await _read_line(lines)

    await app.state.event_bus.publish({"malformed": True})

    assert await _read_line(lines) == {"malformed": True, "cursor": 1}
    await lines.aclose()


@pytest.mark.asyncio
async def test_serve_process_sigterm_drains_close_frame_then_eof(
    serve_process: subprocess.Popen[bytes],
    uds_client_factory,
) -> None:
    async with uds_client_factory() as client:
        async with client.stream("GET", "/v0/events") as response:
            lines = response.aiter_lines()
            assert (await _read_line(lines))["type"] == "subscribed"

            serve_process.send_signal(signal.SIGTERM)

            close = await asyncio.wait_for(_read_line(lines), timeout=5)
            assert close["type"] == "_close"
            assert close["reason"] == "shutdown"
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(lines), timeout=5)
