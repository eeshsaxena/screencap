"""Minimal ASGI app for the ScreenCap daemon."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from screencap import _stderr_events
from screencap.daemon import errors, schema
from screencap.daemon.event_bus import EventBus

_STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    if not hasattr(app.state, "event_bus"):
        app.state.event_bus = EventBus()
    if not hasattr(app.state, "supervisor"):
        from screencap.daemon.supervisor import Supervisor

        app.state.supervisor = Supervisor(app.state.event_bus)
    try:
        yield
    finally:
        if hasattr(app.state, "supervisor"):
            await app.state.supervisor.shutdown()
        await app.state.event_bus.shutdown()


def _build_string() -> str | None:
    return os.environ.get("SCREENCAP_BUILD")


async def daemon_info(_request: Request) -> JSONResponse:
    return JSONResponse(
        schema.envelope(
            schema_version=schema._DAEMON_INFO_API_VERSION,
            build=_build_string(),
            started_at=_STARTED_AT,
        )
    )


async def recording_list(_request: Request) -> JSONResponse:
    from screencap import catalog

    try:
        recordings = catalog.list_recordings()
    except Exception as exc:
        return JSONResponse(
            errors.catalog_unreadable_envelope(
                reason=str(exc) or exc.__class__.__name__,
                schema_version=schema._LIST_API_VERSION,
            ),
            status_code=500,
        )

    try:
        summaries = []
        for recording in recordings:
            data = recording._asdict()
            model = schema.RecordingSummary
            expected_fields = set(model.model_fields)
            actual_fields = set(data)
            if actual_fields != expected_fields:
                missing = sorted(expected_fields - actual_fields)
                extra = sorted(actual_fields - expected_fields)
                raise RuntimeError(
                    "RecordingInfo and RecordingSummary fields diverged: "
                    f"missing={missing}, extra={extra}"
                )
            summaries.append(model(**data).model_dump(mode="json"))
    except Exception as exc:
        return _internal_error_response(exc, schema_version=schema._LIST_API_VERSION)

    return JSONResponse(
        schema.envelope(
            schema_version=schema._LIST_API_VERSION,
            recordings=summaries,
        )
    )


def _empty_snapshot(
    *,
    is_recording: bool | None,
    cursor: int,
    recovering: bool = False,
) -> dict:
    return schema.envelope(
        schema_version=schema._SNAPSHOT_API_VERSION,
        is_recording=is_recording,
        daemon_owned=False,
        recording_name=None,
        started_at=None,
        claimant=None,
        recovering=recovering,
        cursor=cursor,
    )


async def session_snapshot(request: Request) -> JSONResponse:
    from screencap import pidfile

    # Cursor is the event boundary represented by this snapshot; reads do not
    # advance it.
    cursor = request.app.state.event_bus.current_cursor()
    supervisor = getattr(request.app.state, "supervisor", None)
    recovering = bool(supervisor and supervisor.is_recovering())
    active = pidfile.lock_is_active()
    metadata = pidfile.read_lock_metadata()

    if active and metadata is None:
        await asyncio.sleep(0.05)
        active = pidfile.lock_is_active()
        metadata = pidfile.read_lock_metadata()

    if active and metadata is None:
        return JSONResponse(
            _empty_snapshot(is_recording=None, cursor=cursor, recovering=recovering)
        )

    if not active or metadata is None:
        return JSONResponse(
            _empty_snapshot(is_recording=False, cursor=cursor, recovering=recovering)
        )

    claimant = metadata.get("claimant")
    daemon_owned = claimant == "daemon"
    recording_started_at = metadata.get("recording_started_at")
    if recording_started_at is None:
        recording_started_at = metadata.get("started_at")

    payload = schema.envelope(
        schema_version=schema._SNAPSHOT_API_VERSION,
        is_recording=True,
        daemon_owned=daemon_owned,
        recording_name=metadata.get("recording_name"),
        started_at=recording_started_at,
        claimant=claimant if daemon_owned else None,
        recovering=recovering,
        cursor=cursor,
    )
    if daemon_owned and supervisor is not None:
        current = supervisor.current_session()
        if current:
            for key in ("engine_pid", "frames_written"):
                if current.get(key) is not None:
                    payload[key] = current[key]
    elif not daemon_owned:
        if isinstance(metadata.get("pid"), int):
            payload["claimant_pid"] = metadata["pid"]
        if isinstance(metadata.get("started_at"), (int, float)):
            payload["claimant_started_at"] = float(metadata["started_at"])

    return JSONResponse(payload)


def _internal_error_response(
    exc: BaseException,
    *,
    schema_version: int,
) -> JSONResponse:
    return JSONResponse(
        errors.error_envelope(
            schema_version=schema_version,
            error="internal_error",
            reason=exc.__class__.__name__,
        ),
        status_code=500,
    )


async def recording_start(request: Request) -> JSONResponse:
    try:
        parsed = schema.RecordingStartRequest.model_validate(await request.json())
        result = await request.app.state.supervisor.spawn(parsed)
        return JSONResponse(
            schema.envelope(
                schema_version=schema._RECORDING_START_API_VERSION,
                **result,
            )
        )
    except errors.DaemonAPIError as exc:
        return JSONResponse(exc.envelope(), status_code=exc.http_status)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._RECORDING_START_API_VERSION,
        )


async def recording_stop(request: Request) -> JSONResponse:
    try:
        parsed = schema.RecordingStopRequest.model_validate(await request.json())
        result = await request.app.state.supervisor.stop(
            force=parsed.force,
            expected_claimant_pid=parsed.expected_claimant_pid,
            expected_started_at=parsed.expected_started_at,
        )
        return JSONResponse(
            schema.envelope(
                schema_version=schema._RECORDING_STOP_API_VERSION,
                **result,
            )
        )
    except errors.DaemonAPIError as exc:
        return JSONResponse(exc.envelope(), status_code=exc.http_status)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._RECORDING_STOP_API_VERSION,
        )


def _ndjson(payload: dict) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


async def events_stream(request: Request) -> JSONResponse | StreamingResponse:
    bus = request.app.state.event_bus
    since_param = request.query_params.get("since")
    if since_param is not None:
        try:
            since = int(since_param)
        except ValueError:
            return JSONResponse(
                errors.error_envelope(
                    schema_version=schema._EVENTS_API_VERSION,
                    error="invalid_cursor",
                    requested_cursor=since_param,
                ),
                status_code=400,
            )
        if since != bus.current_cursor():
            return JSONResponse(
                errors.cursor_unknown_envelope(
                    requested_cursor=since,
                    schema_version=schema._EVENTS_API_VERSION,
                ),
                status_code=400,
            )

    sub = await bus.subscribe()

    # Drain-to-EOF discipline: shutdown closes every subscription; each stream
    # handler observes that close, yields a final reason frame, then returns so
    # Starlette closes the HTTP body naturally. Client disconnect/cancellation
    # removes the subscriber in ``finally``.
    async def stream() -> AsyncIterator[bytes]:
        try:
            yield _ndjson(
                {
                    "type": _stderr_events.EVENT_SUBSCRIBED,
                    "schema_version": _stderr_events.EVENT_SCHEMA_VERSION,
                    "ts": time.time(),
                    "cursor": sub.cursor_at_subscribe,
                }
            )
            while True:
                if await request.is_disconnected():
                    break
                if sub.closed.is_set():
                    yield _ndjson(
                        {
                            "type": "_close",
                            "reason": sub.close_reason or "unknown",
                            "ts": time.time(),
                        }
                    )
                    break
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                yield _ndjson(event)
        finally:
            await bus.remove(sub)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def build_app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/v0/daemon.info", daemon_info, methods=["GET"]),
            Route("/v0/recording.list", recording_list, methods=["GET"]),
            Route("/v0/session.snapshot", session_snapshot, methods=["GET"]),
            Route("/v0/events", events_stream, methods=["GET"]),
            Route("/v0/recording.start", recording_start, methods=["POST"]),
            Route("/v0/recording.stop", recording_stop, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    # The bus is app-scoped, not module-global, so tests and embedded daemon
    # instances do not share cursors or subscribers.
    app.state.event_bus = EventBus()
    return app
