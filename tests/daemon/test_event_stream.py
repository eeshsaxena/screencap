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
async def test_events_since_attaches_live_for_stale_cursor_and_rejects_future_cursor() -> None:
    # Cursor protocol: `since` older-than-or-equal-to current attaches live —
    # the snapshot caller is just behind by the events emitted in the gap and
    # catches up on the next live read. Only `since > current_cursor` is
    # genuinely unknown (asking about an event the daemon never published).
    app = await _build_app()
    await app.state.event_bus.publish(
        {"type": "existing", "ts": 1.0, "schema_version": 1}
    )
    current = app.state.event_bus.current_cursor()

    response, lines = await _open_stream(app, f"/v0/events?since={current}")
    assert response.status_code == 200
    assert (await _read_line(lines))["cursor"] == current
    await lines.aclose()

    # Stale cursor (snapshot taken before recent events) — attach live, no error.
    response, lines = await _open_stream(app, f"/v0/events?since={current - 1}")
    assert response.status_code == 200
    assert (await _read_line(lines))["cursor"] == current
    await lines.aclose()

    # Future cursor (asking about events never emitted) — cursor_unknown.
    response, _body = await _open_stream(app, f"/v0/events?since={current + 5}")
    assert response.status_code == 400
    assert response.media_type == "application/json"
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["error"] == errors.CURSOR_UNKNOWN
    assert payload["requested_cursor"] == current + 5
    assert payload["schema_version"] == schema._EVENTS_API_VERSION


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
