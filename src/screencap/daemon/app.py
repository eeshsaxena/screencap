"""Minimal ASGI app for the ScreenCap daemon."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from screencap import _stderr_events
from screencap.daemon import errors, schema
from screencap.daemon.event_bus import CursorOutOfRangeError, EventBus
from screencap.pidfile import CLAIMANT_DAEMON

logger = logging.getLogger(__name__)

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


async def recording_list(request: Request) -> JSONResponse:
    from screencap import catalog

    try:
        recordings = await asyncio.to_thread(catalog.list_recordings)
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
        return _internal_error_response(
            exc,
            schema_version=schema._LIST_API_VERSION,
            request=request,
        )

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
) -> dict[str, Any]:
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

    def _read_lock_state() -> tuple[bool, dict[str, Any] | None]:
        # Pair the active/metadata read in one off-loop call so the event
        # loop sees a single thread bounce per check, not two — and so the
        # 50ms retry doesn't multiply into four blocking calls on the loop.
        return pidfile.lock_is_active(), pidfile.read_lock_metadata()

    active, metadata = await asyncio.to_thread(_read_lock_state)

    if active and metadata is None:
        await asyncio.sleep(0.05)
        active, metadata = await asyncio.to_thread(_read_lock_state)

    if active and metadata is None:
        return JSONResponse(
            _empty_snapshot(is_recording=None, cursor=cursor, recovering=recovering)
        )

    if not active or metadata is None:
        return JSONResponse(
            _empty_snapshot(is_recording=False, cursor=cursor, recovering=recovering)
        )

    claimant = metadata.get("claimant")
    daemon_owned = claimant == CLAIMANT_DAEMON
    # `recording_started_at` is the per-recording timestamp (cli.py and the
    # daemon both set it on session start). Long-lived holders like
    # SessionController claim the lock between recordings — they hold the
    # flock with metadata but no `recording_started_at`. Mirror the
    # `pidfile.py` invariant: gate `is_recording` on the per-recording
    # timestamp, not flock activity alone, so SwiftUI doesn't show a phantom
    # "another process is recording" banner during those gaps.
    recording_started_at = metadata.get("recording_started_at")
    if recording_started_at is None:
        return JSONResponse(
            _empty_snapshot(is_recording=False, cursor=cursor, recovering=recovering)
        )

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
    request: Request | None = None,
) -> JSONResponse:
    # Surface the traceback via the daemon stderr log; the wire envelope
    # only carries the exception class name to keep details out of clients.
    path = request.url.path if request is not None else "<unknown>"
    logger.exception("internal error in %s", path, exc_info=exc)
    return JSONResponse(
        errors.error_envelope(
            schema_version=schema_version,
            error=errors.ERROR_CODE_INTERNAL,
            reason=exc.__class__.__name__,
        ),
        status_code=500,
    )


def _api_error_response(exc: errors.DaemonAPIError) -> JSONResponse:
    return JSONResponse(
        exc.envelope(),
        status_code=exc.http_status,
        headers=exc.response_headers() or None,
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
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._RECORDING_START_API_VERSION,
            request=request,
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
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._RECORDING_STOP_API_VERSION,
            request=request,
        )


def _ndjson(payload: dict) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


async def events_stream(request: Request) -> JSONResponse | StreamingResponse:
    bus = request.app.state.event_bus
    since_param = request.query_params.get("since")
    since: int | None = None
    if since_param is not None:
        try:
            since = int(since_param)
        except ValueError:
            return JSONResponse(
                errors.error_envelope(
                    schema_version=schema._EVENTS_API_VERSION,
                    error=errors.ERROR_CODE_INVALID_CURSOR,
                    requested_cursor=since_param,
                ),
                status_code=400,
            )
        if since < 0:
            # Negative cursors are syntactically valid integers but cannot
            # have been produced by the bus's monotonic stamp — reject as
            # invalid rather than silently coercing to "live from now".
            # Echo the raw query-string form so both invalid_cursor
            # envelopes carry `requested_cursor` as a string.
            return JSONResponse(
                errors.error_envelope(
                    schema_version=schema._EVENTS_API_VERSION,
                    error=errors.ERROR_CODE_INVALID_CURSOR,
                    requested_cursor=since_param,
                ),
                status_code=400,
            )

    try:
        sub = await bus.subscribe(since=since)
    except CursorOutOfRangeError as exc:
        # Two cases collapse to the same wire shape: the requested cursor is
        # either ahead of the bus (never produced) or older than the retained
        # replay window (aged out). 410 Gone signals "the cursor cannot be
        # served; retrying without remediation will not help"; daemon_cursor
        # and oldest_retained_cursor let clients resubscribe with a valid
        # in-window cursor without a separate snapshot round-trip.
        return JSONResponse(
            errors.cursor_unknown_envelope(
                requested_cursor=exc.cursor,
                schema_version=schema._EVENTS_API_VERSION,
                daemon_cursor=exc.current,
                oldest_retained_cursor=exc.oldest_retained,
            ),
            status_code=errors.CursorUnknownError.http_status,
        )

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
                    # On `shutdown`, drain any events queued before close —
                    # otherwise the final recording_finalized is lost on SIGTERM
                    # during a live recording. On `slow_consumer` the queue
                    # backlog is exactly what marked them slow; flushing it now
                    # contradicts the close reason and races the consumer.
                    if sub.close_reason == "shutdown":
                        while True:
                            try:
                                event = sub.queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            yield _ndjson(event)
                    # `_close` mirrors the schema_version + cursor shape of
                    # the `subscribed` frame and every published event so
                    # parsers don't special-case the close marker.
                    yield _ndjson(
                        {
                            "type": "_close",
                            "schema_version": _stderr_events.EVENT_SCHEMA_VERSION,
                            "reason": sub.close_reason or "unknown",
                            "ts": time.time(),
                            "cursor": bus.current_cursor(),
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
