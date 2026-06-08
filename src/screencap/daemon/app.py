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
from starlette.datastructures import State
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
    # Warm the TCC grant cache in the BACKGROUND so the daemon starts serving
    # immediately. A fresh probe can take up to ~5s; awaiting it before `yield`
    # would delay the daemon answering its first request — including the
    # install-time readiness probe in launchagent._wait_for_daemon. A daemon.info
    # that arrives before the warm completes falls back to its own cold probe
    # (lock-coalesced via _current_grants), so correctness never depends on the
    # warm finishing first; it only makes the common case fast. Fail-open inside
    # _warm_grant_cache: a probe error must never break the daemon.
    app.state._grant_warm_task = asyncio.create_task(_warm_grant_cache(app))
    try:
        yield
    finally:
        warm_task = getattr(app.state, "_grant_warm_task", None)
        if warm_task is not None and not warm_task.done():
            warm_task.cancel()
        if hasattr(app.state, "supervisor"):
            await app.state.supervisor.shutdown()
        await app.state.event_bus.shutdown()


def _build_string() -> str | None:
    return os.environ.get("SCREENCAP_BUILD")


# How long a probed grant snapshot is served before the next daemon.info call
# triggers a fresh subprocess probe. Short enough that a post-grant toggle
# surfaces within the app's ~5s refresh cadence, long enough that an activation
# refresh + sheet timer firing together collapse to a single spawn.
_GRANT_CACHE_TTL_SECONDS = 2.0


def _grant_holder(app: Starlette) -> State:
    """Lazily attach the grant cache + in-flight lock to ``app.state``.

    Scoped per app instance (not module-global) so tests that build fresh apps
    don't leak cached grants into each other. Lazy init is safe under asyncio:
    the attribute checks and assignments below never await, so two concurrent
    requests can't interleave between the ``hasattr`` and the assignment.
    """
    state = app.state
    if not hasattr(state, "_grant_lock"):
        state._grant_lock = asyncio.Lock()
        state._grant_cache = None
        state._grant_cache_at = 0.0
    return state


async def _warm_grant_cache(app: Starlette) -> None:
    """Populate the grant cache once at startup (run as a background task)."""
    try:
        await _current_grants(app)
    except Exception:  # noqa: BLE001 - never let cache warming break the daemon
        logger.debug("grant cache warm-up failed", exc_info=True)


async def _current_grants(app: Starlette) -> dict[str, str]:
    """Return the daemon's live TCC grant tri-state, served from a short cache.

    The fresh-subprocess probe (U1) runs off the event loop via
    ``asyncio.to_thread``. An ``asyncio.Lock`` plus a TTL re-check coalesces
    concurrent daemon.info calls into a single probe spawn (fork-bomb guard,
    per macos-foundation-process-pipe-pitfalls.md). Freshness uses a monotonic
    clock so a wall-clock step can't distort the TTL window. Fails open to
    indeterminate — never blocks or mislabels on a probe error (R9 / tri-state
    rule: indeterminate is never "missing").
    """
    from screencap.daemon import permission_probe

    state = _grant_holder(app)
    now = time.monotonic()
    if state._grant_cache is not None and (now - state._grant_cache_at) < _GRANT_CACHE_TTL_SECONDS:
        return state._grant_cache

    async with state._grant_lock:
        # A concurrent caller may have refreshed while we waited on the lock.
        now = time.monotonic()
        if (
            state._grant_cache is not None
            and (now - state._grant_cache_at) < _GRANT_CACHE_TTL_SECONDS
        ):
            return state._grant_cache
        grants = await asyncio.to_thread(permission_probe.probe_permissions)
        state._grant_cache = grants
        state._grant_cache_at = time.monotonic()
        return grants


async def daemon_info(request: Request) -> JSONResponse:
    grants = await _current_grants(request.app)
    return JSONResponse(
        schema.envelope(
            schema_version=schema._DAEMON_INFO_API_VERSION,
            build=_build_string(),
            started_at=_STARTED_AT,
            permissions=grants,
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
            for key in ("engine_pid", "frames_written", "started_by"):
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
    from screencap.daemon import audit_log, provenance
    from screencap.daemon._name_validation import validate_recording_name

    # Capture peer identity up front so every exit path (success,
    # typed API error, unhandled exception) can record the audit line
    # with the same descriptor.
    peer = provenance.derive_peer_descriptor_from_asgi_scope(request.scope)
    recording_name: str | None = None

    def _audit(outcome: str) -> None:
        audit_log.record_verb(
            "recording.start",
            peer_pid=peer.pid,
            peer_path=peer.path,
            classification=peer.classification,
            outcome=outcome,
            recording_name=recording_name,
        )

    try:
        body = await request.json()
        caller_supplied = (
            body.get("started_by") if isinstance(body, dict) else None
        )
        parsed = schema.RecordingStartRequest.model_validate(body)
        # Capture the caller-supplied name immediately so an audit line on a
        # later validation failure (e.g., InvalidNameError from path traversal)
        # still records *what was rejected*, not None.
        recording_name = parsed.name

        # Phase 2 U2.3: gate recording names through the canonical
        # validator so path traversal can't leak from agent / CLI / GUI
        # callers into ``~/.screencap/recordings/<name>``. Name is
        # optional (None means daemon auto-generates a timestamp name);
        # only validate when caller supplied a value.
        if parsed.name is not None:
            validate_recording_name(parsed.name)

        # Phase 2 U2: override any caller-supplied ``started_by`` with the
        # server-derived classification from the peer descriptor. The field
        # stays Optional in the request schema (soft-deprecated) so old
        # clients keep working; the daemon owns the authoritative
        # classification on persisted metadata.
        if caller_supplied is not None and caller_supplied != peer.classification:
            logger.debug(
                "ignoring caller-supplied started_by=%r; using server-derived=%r",
                caller_supplied,
                peer.classification,
            )
        parsed = parsed.model_copy(update={"started_by": peer.classification})

        # Pre-spawn permission gate (U6). Reuse U2's cached grant snapshot (no
        # second fresh spawn) and block BEFORE supervisor.spawn claims the
        # pidfile lock — a fresh spawn inside the lock would widen the
        # lock-contended window and risk the app's 10s recording.start timeout.
        # This replaces the old daemon-path behavior (200 OK, then the worker
        # emits permission_lost and crashes): the worker never spawns, so there
        # is no EVENT_STARTED and no duplicate permission_lost for this attempt.
        # The block is TRIGGERED by a denied Screen Recording grant ONLY (the one
        # permission fatal to capture); indeterminate defers to the engine
        # preflight backstop, and Accessibility / Input Monitoring denials
        # warn-and-proceed (they never trigger the block). Once triggered, the
        # reported `missing` list includes every denied required permission — not
        # just Screen Recording — so the app can surface the full picture to the
        # user (the client-side U4 block, which only knows to gate on Screen
        # Recording, names just that one).
        from screencap.daemon import permission_probe

        grants = await _current_grants(request.app)
        if grants.get(permission_probe.PERMISSION_SCREEN_RECORDING) == "denied":
            missing = [
                perm
                for perm in permission_probe.PERMISSION_KEYS
                if grants.get(perm) == "denied"
            ]
            raise errors.PermissionRequiredError(
                missing,
                schema_version=schema._RECORDING_START_API_VERSION,
            )

        result = await request.app.state.supervisor.spawn(parsed)
        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=schema._RECORDING_START_API_VERSION,
                **result,
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(
            exc,
            schema_version=schema._RECORDING_START_API_VERSION,
            request=request,
        )


async def recording_stop(request: Request) -> JSONResponse:
    from screencap.daemon import audit_log, provenance

    peer = provenance.derive_peer_descriptor_from_asgi_scope(request.scope)

    def _audit(outcome: str) -> None:
        audit_log.record_verb(
            "recording.stop",
            peer_pid=peer.pid,
            peer_path=peer.path,
            classification=peer.classification,
            outcome=outcome,
        )

    try:
        parsed = schema.RecordingStopRequest.model_validate(await request.json())
        result = await request.app.state.supervisor.stop(
            force=parsed.force,
            expected_claimant_pid=parsed.expected_claimant_pid,
            expected_started_at=parsed.expected_started_at,
        )
        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=schema._RECORDING_STOP_API_VERSION,
                **result,
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(
            exc,
            schema_version=schema._RECORDING_STOP_API_VERSION,
            request=request,
        )


async def permission_request(request: Request) -> JSONResponse:
    """On-demand daemon-driven TCC registration (U8).

    Runs the registration mechanism for the requested permission *in the
    daemon's own process* (off the event loop via ``asyncio.to_thread``) so the
    Settings entry is attributed to the daemon's TCC identity, not the app's
    (R5; U7 responsible-process caveat). The app calls this, awaits the ack,
    then opens the matching Settings pane.

    A mutating verb on the same trust boundary as ``recording.start`` /
    ``recording.stop`` (``SECURITY.md``): the peer descriptor is derived for the
    audit line, and every exit path (ok, typed error, unhandled) is audited.
    The ``permission`` is validated against the canonical allowlist up front so
    an unexpected value returns a typed ``invalid_permission`` 4xx and never
    reaches the registration dispatch.
    """
    from screencap.daemon import (
        audit_log,
        permission_probe,
        permission_register,
        provenance,
    )

    peer = provenance.derive_peer_descriptor_from_asgi_scope(request.scope)
    requested_permission: str | None = None

    def _audit(outcome: str) -> None:
        audit_log.record_verb(
            "permission.request",
            peer_pid=peer.pid,
            peer_path=peer.path,
            classification=peer.classification,
            outcome=outcome,
            permission=requested_permission,
        )

    try:
        parsed = schema.PermissionRequestRequest.model_validate(await request.json())
        requested_permission = parsed.permission
        if parsed.permission not in permission_probe.PERMISSION_KEYS:
            raise errors.InvalidPermissionError(
                "permission must be one of screen_recording, accessibility, input_monitoring",
                schema_version=schema._PERMISSION_REQUEST_API_VERSION,
            )

        already_granted = await asyncio.to_thread(
            permission_register.register_permission, parsed.permission
        )
        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=schema._PERMISSION_REQUEST_API_VERSION,
                permission=parsed.permission,
                already_granted=already_granted,
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(
            exc,
            schema_version=schema._PERMISSION_REQUEST_API_VERSION,
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
            Route("/v0/permission.request", permission_request, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    # The bus is app-scoped, not module-global, so tests and embedded daemon
    # instances do not share cursors or subscribers.
    app.state.event_bus = EventBus()
    return app
