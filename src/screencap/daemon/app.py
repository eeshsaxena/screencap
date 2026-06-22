"""Minimal ASGI app for the ScreenCap daemon."""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from starlette.applications import Starlette
from starlette.datastructures import State
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from screencap import _stderr_events
from screencap.daemon import errors, schema
from screencap.daemon.event_bus import CursorOutOfRangeError, EventBus
from screencap.pidfile import CLAIMANT_DAEMON

if TYPE_CHECKING:
    from screencap.daemon.permission_probe import GrantState

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
        state._grant_cache: dict[str, GrantState] | None = None
        state._grant_cache_at = 0.0
    return state


async def _warm_grant_cache(app: Starlette) -> None:
    """Populate the grant cache once at startup (run as a background task)."""
    try:
        await _current_grants(app)
    except Exception:  # noqa: BLE001 - never let cache warming break the daemon
        logger.debug("grant cache warm-up failed", exc_info=True)


async def _current_grants(app: Starlette) -> dict[str, GrantState]:
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
    from screencap.daemon import permission_probe

    # Mirror every other read-only verb's defensive try/except: a raise on the
    # grants probe path must never turn the readiness probe into a 500 (which
    # drops the app to its CLI fallback). Fail open to all-indeterminate.
    try:
        grants = await _current_grants(request.app)
    except Exception:  # noqa: BLE001 - readiness probe must never 500
        logger.debug("daemon.info grant probe failed", exc_info=True)
        grants = permission_probe.indeterminate_result()
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


async def auth_whoami(request: Request) -> JSONResponse:
    """SCR-148: report the cloud account currently signed in on this daemon.

    Read-only and same-EUID gated like every other ``/v0`` verb. Pairs with
    ``recording.list``'s ``owner_uid`` so an agent can compare a recording's
    pinned owner against the live signed-in uid and tell a permanent
    account-mismatch block apart from a transient upload failure.

    Fails OPEN to ``signed_in=false`` (never a 500): ``auth.whoami`` is built
    not to raise, but an unexpected Keychain error must degrade like every other
    read verb rather than drop a polling client to its error path.
    """
    from screencap import auth

    try:
        info = await asyncio.to_thread(auth.whoami)
    except Exception:  # noqa: BLE001 — a read verb must never 500
        logger.debug("auth.whoami probe failed", exc_info=True)
        info = {"signed_in": False}
    return JSONResponse(
        schema.envelope(
            schema_version=schema._AUTH_WHOAMI_API_VERSION,
            signed_in=bool(info.get("signed_in")),
            uid=info.get("uid"),
            email=info.get("email"),
            stale=bool(info.get("stale")),
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

        # Bound the *HTTP response* on the TCC registration. The underlying
        # `register_permission` (which can block on `CGEventTapCreate` /
        # `CGRequestScreenCaptureAccess` TCC syscalls) keeps running to
        # completion in the thread pool — `wait_for` only abandons the await,
        # it cannot cancel the off-loop work — but the client gets a prompt,
        # bounded ack instead of an indefinitely hung request. On timeout we
        # report `already_granted=False`; the app re-probes for the
        # authoritative post-grant state regardless.
        try:
            already_granted = await asyncio.wait_for(
                asyncio.to_thread(
                    permission_register.register_permission, parsed.permission
                ),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            _audit("ok")
            return JSONResponse(
                schema.envelope(
                    schema_version=schema._PERMISSION_REQUEST_API_VERSION,
                    permission=parsed.permission,
                    already_granted=False,
                )
            )

        # A successful registration may have produced a fresh grant; invalidate
        # the short grant cache under the grant lock so the next daemon.info /
        # pre-spawn start gate re-probes instead of serving the pre-grant
        # snapshot for the remainder of its TTL.
        grant_state = _grant_holder(request.app)
        async with grant_state._grant_lock:
            grant_state._grant_cache = None
            grant_state._grant_cache_at = 0.0

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


def _run_content_search(
    query: str, recording: str | None, limit: int | None,
) -> dict[str, Any]:
    """Blocking content-index search, run off the event loop via to_thread.

    Never opens/creates the store when it does not exist yet (a read must not
    spawn an empty PII store) — surfaces ``not_indexed`` instead. The store's
    own ``search`` is fail-soft (corrupt → ``store_unavailable``), so this never
    raises for an unreadable store.
    """
    from screencap.content_index import ContentIndex, IndexState, default_index_path

    path = default_index_path()
    if not path.exists():
        return {"hits": [], "index_state": IndexState.NOT_INDEXED.value}

    kwargs: dict[str, Any] = {}
    if limit is not None:
        kwargs["limit"] = limit
    with ContentIndex(path) as store:
        result = store.search(query, recording=recording, **kwargs)
    return {
        "hits": [
            {
                "recording": h.recording,
                "timestamp_ms": h.timestamp_ms,
                "snippet": h.snippet,
                "score": h.score,
            }
            for h in result.hits
        ],
        "index_state": result.index_state.value,
    }


async def content_search(request: Request) -> JSONResponse:
    """``POST /v0/content.search`` — ranked on-screen-text snippets + pointers.

    Read-only over the global content index (SCR-118). Pointer-only response
    (the model is structurally incapable of carrying a media path or image
    bytes — R8). The caller-supplied ``recording`` filter is routed through the
    canonical name validator (traversal-safe) before use; the FTS ``MATCH`` is
    bound + phrase-escaped inside ``content_index``. A missing/corrupt store
    fails soft via ``index_state`` rather than 500-ing. Deliberately NOT in
    ``_ACTIVITY_PATHS`` — idle-shutdown is kept alive by the MCP-held
    subscription (U5), preserving the cron-polling protection.
    """
    from screencap.daemon._name_validation import validate_recording_name

    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        parsed = schema.ContentSearchRequest.model_validate(body)
        recording = parsed.recording
        if recording is not None:
            validate_recording_name(recording)

        limit = _clamp_limit(parsed.limit)
        result = await asyncio.to_thread(
            _run_content_search, parsed.query, recording, limit,
        )
        return JSONResponse(
            schema.envelope(
                schema_version=schema._CONTENT_SEARCH_API_VERSION,
                hits=result["hits"],
                index_state=result["index_state"],
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._CONTENT_SEARCH_API_VERSION,
            request=request,
        )


# SCR-118 query bounds (DoS guards enforced at the daemon, not only in the MCP
# tool layer, so a direct UDS caller cannot drive an unbounded scan).
_QUERY_MAX_RECORDINGS = 200
_QUERY_DEFAULT_LIMIT = 50
_QUERY_MAX_LIMIT = 200


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return _QUERY_DEFAULT_LIMIT
    return max(1, min(int(limit), _QUERY_MAX_LIMIT))


def _iter_recording_dirs(recording: str | None) -> list[Path]:
    """Candidate recording dirs to scan (validated single, or all, capped)."""
    from screencap.config import get_recordings_dir, resolve_recording_dir

    if recording is not None:
        d = resolve_recording_dir(recording)
        return [d] if d.is_dir() else []
    base = get_recordings_dir()
    # A missing recordings dir (fresh install, no recordings yet) must return an
    # empty list, not 500 via iterdir raising FileNotFoundError.
    if not base.is_dir():
        return []
    dirs = [
        d for d in sorted(base.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]
    return dirs[:_QUERY_MAX_RECORDINGS]


def _parse_chunk_index(name: str) -> int:
    """``transcript_0007.txt`` → 7; a bare ``transcript.txt`` → 0."""
    stem = name[: -len(".txt")] if name.endswith(".txt") else name
    _, _, tail = stem.partition("_")
    try:
        return int(tail)
    except ValueError:
        return 0


def _run_transcript_search(
    query: str, recording: str | None, limit: int,
) -> list[dict[str, Any]]:
    """Keyword scan over the STRICT scrubbed ``transcript_*.txt`` only.

    Never reads the rich per-word ``transcript_*.json`` (an R7 leak) nor a
    ``*.txt.scrub_failed`` file (the suffix is appended, so a ``*.txt`` glob
    already excludes it; the explicit suffix check is belt-and-suspenders).
    """
    from screencap.content_index import like_snippet

    needle = query.strip().lower()
    if not needle:
        # An empty/whitespace needle matches every file ("" in text is always
        # True) — that would dump the whole transcript corpus, not search it.
        return []
    hits: list[dict[str, Any]] = []
    for rec_dir in _iter_recording_dirs(recording):
        candidates = sorted(rec_dir.glob("transcript_*.txt"))
        bare = rec_dir / "transcript.txt"
        if bare.exists():
            candidates.append(bare)
        for path in candidates:
            if path.suffix != ".txt" or path.name.endswith(".scrub_failed"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle in text.lower():
                hits.append({
                    "recording": rec_dir.name,
                    "chunk_index": _parse_chunk_index(path.name),
                    "snippet": like_snippet(text, query, width=120, collapse_newlines=True),
                })
                if len(hits) >= limit:
                    return hits
    return hits


def _run_timeline_query(
    start_ms: int | None,
    end_ms: int | None,
    app: str | None,
    recording: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Structured app/window/time rows from window_event across recordings.

    Authoritative (event tables, no OCR/redaction loss). Schema-tolerant via
    has_table/has_column; never touches OCR or the content index. ``browser_url``
    is intentionally not selected (v1 omits it — see TimelineRow).
    """
    from screencap.content_index import escape_like
    from screencap.recording_db import has_column, has_table, open_recording_db

    start_s = start_ms / 1000.0 if start_ms is not None else None
    end_s = end_ms / 1000.0 if end_ms is not None else None
    app_like = f"%{escape_like(app.lower())}%" if app else None

    rows: list[dict[str, Any]] = []
    for rec_dir in _iter_recording_dirs(recording):
        db_path = rec_dir / "recording.db"
        if not db_path.is_file():
            continue
        try:
            with open_recording_db(db_path, read_only=True) as conn:
                if not has_table(conn, "window_event") or not has_column(
                    conn, "window_event", "timestamp"
                ):
                    continue
                has_name = has_column(conn, "window_event", "app_name")
                has_bundle = has_column(conn, "window_event", "app_bundle_id")
                has_title = has_column(conn, "window_event", "title")
                if app_like and not (has_name or has_bundle):
                    continue

                name_expr = "app_name" if has_name else "NULL"
                bundle_expr = "app_bundle_id" if has_bundle else "NULL"
                title_expr = "title" if has_title else "NULL"
                sql = (
                    f"SELECT timestamp, {name_expr}, {bundle_expr}, {title_expr} "
                    "FROM window_event WHERE timestamp IS NOT NULL"
                )
                params: list[Any] = []
                if start_s is not None:
                    sql += " AND timestamp >= ?"
                    params.append(start_s)
                if end_s is not None:
                    sql += " AND timestamp < ?"
                    params.append(end_s)
                if app_like:
                    clauses = []
                    if has_name:
                        clauses.append("lower(app_name) LIKE ? ESCAPE '\\'")
                        params.append(app_like)
                    if has_bundle:
                        clauses.append("lower(app_bundle_id) LIKE ? ESCAPE '\\'")
                        params.append(app_like)
                    sql += " AND (" + " OR ".join(clauses) + ")"
                sql += " ORDER BY timestamp LIMIT ?"
                params.append(limit)
                for ts, app_name, bundle, title in conn.execute(sql, params):
                    rows.append({
                        "recording": rec_dir.name,
                        "timestamp_ms": int(float(ts) * 1000),
                        "app": app_name or bundle,
                        "title": title,
                    })
        except Exception:
            # A single unreadable/older recording must not fail the whole query.
            logger.debug("timeline.query skipped %s", rec_dir.name, exc_info=True)
            continue

    # Cross-recording wall-clock order, bounded to `limit` (each recording
    # already returned ≤limit rows; take the globally-earliest `limit` without
    # a full sort of the gathered set).
    return heapq.nsmallest(limit, rows, key=lambda r: r["timestamp_ms"])


async def transcript_search(request: Request) -> JSONResponse:
    """``POST /v0/transcript.search`` — keyword scan over scrubbed transcripts."""
    from screencap.daemon._name_validation import validate_recording_name

    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        parsed = schema.TranscriptSearchRequest.model_validate(body)
        if parsed.recording is not None:
            validate_recording_name(parsed.recording)
        limit = _clamp_limit(parsed.limit)
        hits = await asyncio.to_thread(
            _run_transcript_search, parsed.query, parsed.recording, limit,
        )
        return JSONResponse(
            schema.envelope(
                schema_version=schema._TRANSCRIPT_SEARCH_API_VERSION,
                hits=hits,
                coverage="best_effort",
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._TRANSCRIPT_SEARCH_API_VERSION,
            request=request,
        )


async def timeline_query(request: Request) -> JSONResponse:
    """``POST /v0/timeline.query`` — structured app/window/time rows (authoritative)."""
    from screencap.daemon._name_validation import validate_recording_name

    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        parsed = schema.TimelineQueryRequest.model_validate(body)
        if parsed.recording is not None:
            validate_recording_name(parsed.recording)
        if (
            parsed.start_ms is not None
            and parsed.end_ms is not None
            and parsed.start_ms > parsed.end_ms
        ):
            return JSONResponse(
                errors.error_envelope(
                    schema_version=schema._TIMELINE_QUERY_API_VERSION,
                    error=errors.INVALID_RANGE,
                ),
                status_code=400,
            )
        limit = _clamp_limit(parsed.limit)
        rows = await asyncio.to_thread(
            _run_timeline_query,
            parsed.start_ms, parsed.end_ms, parsed.app, parsed.recording, limit,
        )
        return JSONResponse(
            schema.envelope(
                schema_version=schema._TIMELINE_QUERY_API_VERSION,
                rows=rows,
                coverage="authoritative",
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._TIMELINE_QUERY_API_VERSION,
            request=request,
        )


def build_app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/v0/daemon.info", daemon_info, methods=["GET"]),
            Route("/v0/recording.list", recording_list, methods=["GET"]),
            Route("/v0/auth.whoami", auth_whoami, methods=["GET"]),
            Route("/v0/session.snapshot", session_snapshot, methods=["GET"]),
            Route("/v0/events", events_stream, methods=["GET"]),
            Route("/v0/recording.start", recording_start, methods=["POST"]),
            Route("/v0/recording.stop", recording_stop, methods=["POST"]),
            Route("/v0/permission.request", permission_request, methods=["POST"]),
            Route("/v0/content.search", content_search, methods=["POST"]),
            Route("/v0/transcript.search", transcript_search, methods=["POST"]),
            Route("/v0/timeline.query", timeline_query, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    # The bus is app-scoped, not module-global, so tests and embedded daemon
    # instances do not share cursors or subscribers.
    app.state.event_bus = EventBus()
    return app
