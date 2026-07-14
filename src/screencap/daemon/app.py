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

# U5 (conversational recall): the ``chat.answer`` request-path modules are
# imported at module scope (not lazily inside the handler) AND touched eagerly in
# the app lifespan below, mirroring the stale-daemon-after-app-update lesson — a
# request-path module must be resolved at daemon start, not on the first request,
# so a swapped bundle can't leave a half-loaded handler. Both recall modules are
# cloud-free at import time (they read config lazily), so this adds no network /
# credential surface to daemon startup. Bound here so the handler dispatches
# through module-level names (also the seam the U5 verb tests patch).
from screencap.recall.dispatch import ChatAnswer, answer_from_bundle
from screencap.recall.orchestrator import (
    CoverageDescriptor,
    CoverageState,
    PriorTurnPointer,
    QuestionKind,
    build_evidence_bundle,
)

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

        # SCR-214 U2/KTD8: hand the supervisor the SAME start gate the HTTP
        # recording.start path runs, so the internal ambient auto-start is held
        # to the identical permission + paywall checks (a future gate can't be
        # added to one path but missed on the other).
        app.state.supervisor = Supervisor(
            app.state.event_bus,
            start_gate=lambda: enforce_recording_start_gate(app),
        )
    # SCR-228: reconcile a storage migration that crashed between the tree
    # rename and the config flip, so the daemon serves the correct recordings
    # dir from the first request. Fail-open — a reconcile error must never
    # block daemon start.
    try:
        from screencap import config, storage_migration

        await asyncio.to_thread(
            storage_migration.reconcile_pending, config.set_recordings_dir
        )
    except Exception:  # noqa: BLE001 - reconcile must never break startup
        logger.warning("storage-migration reconcile failed", exc_info=True)
    # Search U7: resume/finish a corpus plaintext→encrypted flip that a prior daemon
    # requested but didn't complete (crash-safe). A no-op before any flip is
    # requested; strictly fail-open so a migration hiccup never blocks daemon start.
    try:
        from screencap import corpus_migrate

        await asyncio.to_thread(corpus_migrate.resume_at_daemon_start)
    except Exception:  # noqa: BLE001 - migration must never break startup
        logger.warning("corpus-migration resume failed", exc_info=True)
    # U5: eagerly resolve the ``chat.answer`` request-path recall modules at
    # daemon start (the stale-daemon-after-app-update lesson — a request-path
    # module must be imported before the first request, never lazily inside the
    # handler). They are already imported at this module's top; this reference
    # pins that they are resolved (and fails fast at startup if a swapped bundle
    # left them unimportable) rather than surfacing as a 500 on the first turn.
    assert build_evidence_bundle is not None and answer_from_bundle is not None
    # Warm the TCC grant cache in the BACKGROUND so the daemon starts serving
    # immediately. A fresh probe can take up to ~5s; awaiting it before `yield`
    # would delay the daemon answering its first request — including the
    # install-time readiness probe in launchagent._wait_for_daemon. A daemon.info
    # that arrives before the warm completes falls back to its own cold probe
    # (lock-coalesced via _current_grants), so correctness never depends on the
    # warm finishing first; it only makes the common case fast. Fail-open inside
    # _warm_grant_cache: a probe error must never break the daemon.
    app.state._grant_warm_task = asyncio.create_task(_warm_grant_cache(app))
    # Periodic screenshot-retention sweep (search U5): applies the age/size bound to
    # converged recordings that no recording-lifecycle event revisits. Not
    # auth-gated (runs signed-out); a no-op until a bound is configured. Fail-open —
    # starting it must never break daemon boot.
    try:
        from screencap.daemon.retention_sweep import RetentionSweep

        app.state.retention_sweep = RetentionSweep()
        app.state.retention_sweep.start()
    except Exception:  # noqa: BLE001 - the sweep is best-effort maintenance
        logger.warning("retention sweep failed to start", exc_info=True)
    try:
        yield
    finally:
        warm_task = getattr(app.state, "_grant_warm_task", None)
        if warm_task is not None and not warm_task.done():
            warm_task.cancel()
        retention_sweep = getattr(app.state, "retention_sweep", None)
        if retention_sweep is not None:
            await retention_sweep.shutdown()
        # Stop an in-flight backfill BEFORE closing the bus/loop: its OCR worker
        # runs on a to_thread worker that the loop cannot cancel, so without an
        # explicit stop it would keep writing content_index.db past loop close.
        # Done before event_bus.shutdown() so the run's terminal event can still
        # publish to a live bus. Lazily attached (only after a backfill verb), so
        # it may be absent.
        backfill_job = getattr(app.state, "backfill_job", None)
        if backfill_job is not None:
            await backfill_job.shutdown()
        model_download_job = getattr(app.state, "model_download_job", None)
        if model_download_job is not None:
            await model_download_job.shutdown()
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


def _apply_whoami_to_lease(info: dict[str, Any]) -> None:
    """Reconcile the KTD-4 entitlement lease against a ``whoami`` result (U14).

    Thin adapter onto ``entitlement_lease.reconcile_from_whoami`` — the single
    write policy shared by the regular ``auth.whoami`` touchpoint, the
    ``entitlement.refresh`` re-mint verb, and the CLI post-checkout path — so the
    daemon and CLI can never drift on when the lease is written/cleared/preserved.
    """
    from screencap.daemon import entitlement_lease

    entitlement_lease.reconcile_from_whoami(info)


async def _enforce_recording_subscription_gate() -> None:
    """U8: block ``recording.start`` unless an active tier entitles it (KTD-4).

    No-op unless ``SCREENCAP_LOCAL_PAYWALL_ENFORCE`` is on, so the default path is
    byte-identical to today. When on:

    1. If the KTD-4 lease grants an unexpired paid tier, allow — an offline PAYER
       within the 72h window is never locked out (R8), and no network call is made.
    2. Else attempt ONE fresh ``auth.whoami()`` + :func:`_apply_whoami_to_lease` to
       refresh the lease from current state (an online payer whose lease just
       expired self-heals here; a definitive not-entitled clears; an ambiguous
       stale/offline whoami preserves the lease), then re-read.
    3. If still not entitled, raise :class:`SubscriptionRequiredError` (no spawn).

    Unlike the recall gate this DOES a single whoami on a lease miss — the
    start path is infrequent (once per recording), so a fresh refresh at the
    chokepoint is worth un-gating a just-lapsed-but-now-renewed payer without a
    ~1h wait. A ``whoami`` that raises is swallowed (best-effort): the pre-fetch
    lease read is the source of truth and the gate falls through to it.
    """
    from screencap.config import get_local_paywall_enforced
    from screencap.daemon import entitlement_lease

    if not get_local_paywall_enforced():
        return

    if entitlement_lease.lease_entitled_tier() is not None:
        return

    # Lease miss: refresh from current whoami state once, then re-read.
    from screencap import auth

    try:
        info = await asyncio.to_thread(auth.whoami)
    except Exception:  # noqa: BLE001 — whoami is built not to raise; degrade to lease
        logger.warning("recording.start subscription gate: whoami failed", exc_info=True)
    else:
        _apply_whoami_to_lease(info)

    if entitlement_lease.lease_entitled_tier() is None:
        raise errors.SubscriptionRequiredError(
            schema_version=schema._RECORDING_START_API_VERSION,
        )


async def enforce_recording_start_gate(app: Starlette) -> None:
    """Shared pre-spawn gate for ``recording.start``: permission + paywall (KTD8).

    Extracted from the ``recording.start`` HTTP handler so BOTH it and the
    internal ambient auto-start (``Supervisor``) run the *exact same* gates —
    a future gate added here can't be wired into one path and missed on the
    other (SCR-214 U2 / KTD8). Raises the same typed errors as before.

    Permission gate (U6): reuse U2's cached grant snapshot (no second fresh
    spawn) and block BEFORE ``supervisor.spawn`` claims the pidfile lock — a
    fresh spawn inside the lock would widen the lock-contended window and risk
    the app's 10s ``recording.start`` timeout. This replaces the old daemon-path
    behavior (200 OK, then the worker emits ``permission_lost`` and crashes): the
    worker never spawns, so there is no ``EVENT_STARTED`` and no duplicate
    ``permission_lost`` for this attempt. The block is TRIGGERED by a denied
    Screen Recording grant ONLY (the one permission fatal to capture);
    indeterminate defers to the engine preflight backstop, and Accessibility /
    Input Monitoring denials warn-and-proceed (they never trigger the block).
    Once triggered, the reported ``missing`` list includes every denied required
    permission — not just Screen Recording — so the app can surface the full
    picture.

    Paywall gate (U8, KTD-4): a no-op unless ``SCREENCAP_LOCAL_PAYWALL_ENFORCE``
    is on; raised BEFORE the spawn (typed-error-before-spawn) so a refused start
    never spawns an engine worker.
    """
    from screencap.daemon import permission_probe

    grants = await _current_grants(app)
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

    await _enforce_recording_subscription_gate()


def _check_subscription_for_recall(*, schema_version: int) -> None:
    """U9: block a recall/search verb unless an unexpired entitled lease grants it.

    No-op unless ``SCREENCAP_LOCAL_PAYWALL_ENFORCE`` is on (default path unchanged).
    When on, reads the KTD-4 lease ONLY — deliberately NO per-search ``whoami``
    network call: the lease is kept fresh by the ``auth.whoami`` verb's reconcile and
    by U8's recording-start path, so a per-search refresh would be too chatty for a
    surface hit on every keystroke of autocomplete. Raises
    :class:`SubscriptionRequiredError` when the lease is invalid / expired / cleared;
    an offline PAYER within the lease window still passes (R8).

    Called at the top of the five recall handlers (``content.search`` /
    ``transcript.search`` / ``timeline.query`` / ``frame.nearest`` / ``apps.list``);
    the browse verbs (``recording.list`` / ``timeline.day`` / ``tasks.list`` /
    ``auth.whoami``) never call it.
    """
    from screencap.config import get_local_paywall_enforced
    from screencap.daemon import entitlement_lease

    if not get_local_paywall_enforced():
        return
    if entitlement_lease.lease_entitled_tier() is None:
        raise errors.SubscriptionRequiredError(schema_version=schema_version)


async def auth_whoami(request: Request) -> JSONResponse:
    """SCR-148: report the cloud account currently signed in on this daemon.

    Read-only and same-EUID gated like every other ``/v0`` verb. Pairs with
    ``recording.list``'s ``owner_uid`` so an agent can compare a recording's
    pinned owner against the live signed-in uid and tell a permanent
    account-mismatch block apart from a transient upload failure.

    Fails OPEN to ``signed_in=false`` (never a 500): ``auth.whoami`` is built
    not to raise, but an unexpected Keychain error must degrade like every other
    read verb rather than drop a polling client to its error path.

    Side effect (U14): refreshes the KTD-4 entitlement lease from the result
    (:func:`_apply_whoami_to_lease`). This is the daemon's *regular* lease-refresh
    cadence — a poll whose internal ``_ensure_fresh`` re-mints the ID token re-arms
    offline grace for the U8/U9 gates.
    """
    from screencap import auth

    try:
        info = await asyncio.to_thread(auth.whoami)
    except Exception:  # noqa: BLE001 — a read verb must never 500
        logger.warning("auth.whoami probe failed", exc_info=True)
        info = {"signed_in": False}
    _apply_whoami_to_lease(info)
    return JSONResponse(
        schema.envelope(
            schema_version=schema._AUTH_WHOAMI_API_VERSION,
            signed_in=bool(info.get("signed_in")),
            uid=info.get("uid"),
            email=info.get("email"),
            stale=info.get("stale", False),
            # U6: forward the two-tier entitlement + trial state (the handler
            # previously dropped all three) so the Swift picker (U11) reads the
            # real tier instead of a defaulted one. ``subscribed`` stays the
            # derived cloud signal; ``tier``/``trial_end`` default to None.
            subscribed=bool(info.get("subscribed")),
            tier=info.get("tier"),
            trial_end=info.get("trial_end"),
        )
    )


async def entitlement_refresh(request: Request) -> JSONResponse:
    """U14: force an ID-token re-mint in the daemon's own context, then re-arm the lease.

    The post-checkout signal. A just-converted user's ``tier`` claim only
    re-materializes on a token refresh (~1h buffer), so the app calls this after
    returning from Stripe to un-gate the local recording/recall gates (U8/U9)
    promptly rather than waiting out the buffer. Forces
    ``auth.get_id_token(force_refresh=True)`` — the daemon holds the refresh token,
    so it CAN re-mint (the engine subprocess, which does not, cannot) — then reads
    ``whoami`` and reconciles the KTD-4 lease (:func:`_apply_whoami_to_lease`): a
    now-entitled tier writes/refreshes the lease immediately; a confirmed
    not-entitled clears it; an ambiguous (stale/offline) result preserves it.

    Re-mint happens ONLY on this explicit signal (and the regular ``whoami``
    cadence) — there is no spurious loop that would re-mint on its own. Fails OPEN
    to ``signed_in=false`` (never a 500), mirroring ``auth.whoami``; the lease is
    left untouched on a hard failure so an offline payer is never locked out.
    """
    from screencap import auth

    def _force_refresh_whoami() -> dict[str, Any]:
        # Best-effort re-mint, then read the freshly-materialized state. A refresh
        # failure is not fatal — whoami() reports ``stale`` rather than raising, and
        # the lease reconcile then preserves the current lease.
        try:
            auth.get_id_token(force_refresh=True)
        except Exception:  # noqa: BLE001 — offline / not-signed-in: fall through to whoami
            logger.debug("entitlement.refresh: force re-mint failed", exc_info=True)
        return dict(auth.whoami())

    try:
        info = await asyncio.to_thread(_force_refresh_whoami)
    except Exception:  # noqa: BLE001 — a control verb must never 500
        logger.warning("entitlement.refresh probe failed", exc_info=True)
        info = {"signed_in": False}
    _apply_whoami_to_lease(info)
    return JSONResponse(
        schema.envelope(
            schema_version=schema._ENTITLEMENT_REFRESH_API_VERSION,
            signed_in=bool(info.get("signed_in")),
            uid=info.get("uid"),
            email=info.get("email"),
            stale=info.get("stale", False),
            subscribed=bool(info.get("subscribed")),
            tier=info.get("tier"),
            trial_end=info.get("trial_end"),
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
            # SCR-218 U5: ``muted`` rides the same overlay. It's absent until the
            # first confirmed mute event, so an unmuted (or pre-first-mute)
            # recording omits it and the app defaults to unmuted — the additive,
            # back-compatible contract mirrored on the app decoder side.
            # SCR-214 U4: ``paused`` rides the same additive overlay as ``muted``
            # — absent until the first confirmed pause event, so a never-paused
            # recording omits it and the app defaults to not-paused.
            for key in ("engine_pid", "frames_written", "started_by", "muted",
                        "paused"):
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


def _validation_error_response(*, schema_version: int) -> JSONResponse:
    """A malformed/out-of-bounds request body → typed 400 ``invalid_request``.

    The shared seam for turning a pydantic ``ValidationError`` into a client
    error rather than a generic 500 (SCR-186). Other read verbs still 500 on a
    validation failure; generalizing them is deferred follow-up work.
    """
    return JSONResponse(
        errors.error_envelope(
            schema_version=schema_version, error=errors.INVALID_REQUEST,
        ),
        status_code=400,
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

        # Pre-spawn permission + paywall gates (U6/U8), extracted into a single
        # shared helper so this HTTP path and the internal ambient auto-start
        # (SCR-214 U2) can never diverge — a future gate added to the helper is
        # enforced on both (KTD8). Both raise typed-errors-before-spawn, so a
        # refused start never spawns an engine worker.
        await enforce_recording_start_gate(request.app)

        result = await request.app.state.supervisor.spawn(parsed)
        # U2 (prototype UI): echo the effective audio state so U6/U7 reflect what
        # the engine did. An unspecified (None) request resolves the SAME way the
        # engine does for it — `config.get_audio_default()` (screen_recorder) — so
        # the echo stays truthful when a user set `audio_default=false` rather than
        # falsely reporting audio-on. A stale daemon omits the field entirely; the
        # app treats a missing echo as audio-on.
        if parsed.audio is not None:
            result["audio"] = parsed.audio
        else:
            from screencap.config import get_audio_default

            result["audio"] = get_audio_default()
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


async def recording_mute(request: Request) -> JSONResponse:
    """Set the mic mute state on the running recording (SCR-218 U4).

    Forwards the request to the engine over the stdin control channel (U1) and
    returns the pre-forward bus cursor. It does NOT set mute state itself — the
    engine's confirmed ``audio_muted`` / ``audio_unmuted`` event does (KTD4/U5),
    so the response echoes only the *requested* state and the app must not treat
    it as confirmation. A mutating verb on the same trust boundary as
    ``recording.start`` / ``recording.stop``: the peer descriptor is derived and
    every exit path (ok, typed error, unhandled) is audited.
    """
    from screencap.daemon import audit_log, provenance

    peer = provenance.derive_peer_descriptor_from_asgi_scope(request.scope)

    def _audit(outcome: str) -> None:
        audit_log.record_verb(
            "recording.mute",
            peer_pid=peer.pid,
            peer_path=peer.path,
            classification=peer.classification,
            outcome=outcome,
        )

    try:
        parsed = schema.RecordingMuteRequest.model_validate(await request.json())
        # Capture the cursor BEFORE forwarding so a client can subscribe to
        # /v0/events?since=<cursor> and never miss the confirming event.
        cursor = request.app.state.event_bus.current_cursor()
        forwarded = await request.app.state.supervisor.set_muted(parsed.muted)
        if not forwarded:
            raise errors.NotRecordingError(
                schema_version=schema._RECORDING_MUTE_API_VERSION
            )
        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=schema._RECORDING_MUTE_API_VERSION,
                muted=parsed.muted,
                cursor=cursor,
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(
            exc,
            schema_version=schema._RECORDING_MUTE_API_VERSION,
            request=request,
        )


async def _recording_set_paused(
    request: Request, *, paused: bool, verb: str
) -> JSONResponse:
    """Shared implementation for ``recording.pause`` / ``recording.resume``
    (SCR-214 U4).

    Mirrors ``recording.mute``'s trust-boundary posture end-to-end: it derives a
    peer descriptor and audits every exit path (ok, typed error, unhandled),
    validates ``RecordingPauseRequest``, captures the bus cursor BEFORE forwarding
    (so a client subscribing to ``/v0/events?since=<cursor>`` never misses the
    confirming event), and forwards to the engine via ``supervisor.set_paused``.
    It does NOT set pause state itself — the engine's confirmed
    ``recording_paused`` / ``recording_resumed`` event does (KTD7) — so the
    response echoes only the *requested* state and the app must not treat it as
    confirmation. Unlike mute (audio-only), pause gates the WHOLE capture surface,
    so a paused span records nothing (AE1).

    The request body's ``paused`` is ignored in favor of the verb-derived
    ``paused`` argument, so ``recording.pause`` always pauses and
    ``recording.resume`` always resumes regardless of a mismatched body.
    """
    from screencap.daemon import audit_log, provenance

    peer = provenance.derive_peer_descriptor_from_asgi_scope(request.scope)

    def _audit(outcome: str) -> None:
        audit_log.record_verb(
            verb,
            peer_pid=peer.pid,
            peer_path=peer.path,
            classification=peer.classification,
            outcome=outcome,
        )

    try:
        # Validate the body so a malformed request is a typed 400, not a crash —
        # even though the effective state comes from the verb, not the body.
        schema.RecordingPauseRequest.model_validate(await request.json())
        # Capture the cursor BEFORE forwarding so a client can subscribe to
        # /v0/events?since=<cursor> and never miss the confirming event.
        cursor = request.app.state.event_bus.current_cursor()
        forwarded = await request.app.state.supervisor.set_paused(paused)
        if not forwarded:
            raise errors.NotRecordingError(
                schema_version=schema._RECORDING_PAUSE_API_VERSION
            )
        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=schema._RECORDING_PAUSE_API_VERSION,
                paused=paused,
                cursor=cursor,
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(
            exc,
            schema_version=schema._RECORDING_PAUSE_API_VERSION,
            request=request,
        )


async def recording_pause(request: Request) -> JSONResponse:
    """Pause capture (video + screenshots + audio) on the running recording
    without ending it (SCR-214 U4). See :func:`_recording_set_paused`."""
    return await _recording_set_paused(request, paused=True, verb="recording.pause")


async def recording_resume(request: Request) -> JSONResponse:
    """Resume capture on a paused recording (SCR-214 U4). See
    :func:`_recording_set_paused`."""
    return await _recording_set_paused(request, paused=False, verb="recording.resume")


def _resolve_rename_target(selector: str) -> tuple[Path, Path, float | None] | None:
    """Resolve a ``recording.rename`` selector to ``(dir, db_path, started_at)``.

    Matches by the stable ``.recording_id`` first, then falls back to the
    directory NAME — legacy recordings predating the ``.recording_id`` sidecar
    return ``None`` from ``read_recording_id``, so the name fallback is what keeps
    them renameable. ``started_at`` (capture start epoch, or ``None`` for a legacy
    row with no start row) is read here so the handler can recompute the default
    title on a clear without a second DB open. Returns ``None`` when no recording
    matches. Pure disk IO — the handler runs it via ``asyncio.to_thread``.
    """
    from screencap import catalog
    from screencap.config import get_recordings_dir

    recordings_dir = get_recordings_dir()
    if not recordings_dir.exists():
        return None
    for d in sorted(recordings_dir.iterdir()):
        if not d.is_dir():
            continue
        db = catalog.find_db(d)
        if db is None:
            continue
        if catalog.read_recording_id(d) == selector or d.name == selector:
            started, _duration, _status = catalog._read_recording_meta(db)
            return d, db, started
    return None


async def recording_rename(request: Request) -> JSONResponse:
    """Set or clear a recording's editable display title (editable titles U3).

    A post-hoc, additive mutating verb: it addresses a recording by stable
    ``recording_id`` OR directory name and writes the local-only
    ``recording.title`` (never uploaded — R8). It mirrors ``recording.mute``'s
    trust-boundary posture — the peer descriptor is derived and every exit path
    (ok, typed error, unhandled) is audited — but the audit line records ONLY the
    peer + outcome: the free-text title is deliberately kept OUT of the local
    audit log so user-authored titles never accrue there.

    The title is validated as DISPLAY text (``validate_recording_title`` — unicode
    / emoji ok, control chars rejected, <=200 chars), NOT as a path-safe name. An
    empty title clears the rename. The currently-active recording is refused
    (mid-recording rename is deferred to the HUD) BEFORE any write. The response
    echoes the resolved display title (the stripped user title when set, else the
    freshly-recomputed date/time default so a cleared rename returns the same
    default ``recording.list`` would), ``title_is_user_set``, and the bus cursor
    captured before the write.
    """
    from screencap import catalog, recording_db
    from screencap.daemon import audit_log, provenance
    from screencap.daemon._name_validation import validate_recording_title

    peer = provenance.derive_peer_descriptor_from_asgi_scope(request.scope)

    def _audit(outcome: str) -> None:
        # Peer + outcome ONLY — never the title text (free-text titles must not
        # accrue in the local audit log).
        audit_log.record_verb(
            "recording.rename",
            peer_pid=peer.pid,
            peer_path=peer.path,
            classification=peer.classification,
            outcome=outcome,
        )

    try:
        parsed = schema.RecordingRenameRequest.model_validate(await request.json())
        title = validate_recording_title(parsed.title)

        resolved = await asyncio.to_thread(_resolve_rename_target, parsed.recording_id)
        if resolved is None:
            raise errors.RecordingNotFoundError(
                schema_version=schema._RECORDING_RENAME_API_VERSION
            )
        directory, db_path, started_at = resolved

        # Refuse a rename of the live recording (its writer holds the DB lock and
        # mid-recording rename is a HUD concern) BEFORE capturing the cursor or
        # writing anything.
        active_name = await asyncio.to_thread(catalog._active_recording_name)
        if active_name is not None and directory.name == active_name:
            raise errors.RecordingActiveError(
                schema_version=schema._RECORDING_RENAME_API_VERSION
            )

        # Capture the cursor BEFORE the write so a client can subscribe to
        # /v0/events?since=<cursor> and not miss any follow-on event.
        cursor = request.app.state.event_bus.current_cursor()
        await asyncio.to_thread(recording_db.write_user_title, db_path, title)

        title_is_user_set = bool(title.strip())
        if title_is_user_set:
            display_title = title.strip()
        else:
            # Cleared → return the freshly-resolved default, the SAME value
            # recording.list would now report for this recording.
            display_title = catalog._default_title(started_at, directory.name)

        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=schema._RECORDING_RENAME_API_VERSION,
                title=display_title,
                title_is_user_set=title_is_user_set,
                cursor=cursor,
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(
            exc,
            schema_version=schema._RECORDING_RENAME_API_VERSION,
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


async def permission_cleanup_decoys(request: Request) -> JSONResponse:
    """SCR-200 (U4): identity-scoped decoy/orphan TCC cleanup, daemon-side.

    Removes the orphaned bare ``screencap`` identity and the legacy app's stray
    Screen Recording / Accessibility rows so exactly one "ScreenCap" row remains
    per pane (R5/R6/R8). The destructive ``tccutil`` argv is built through the
    hardened allowlists in ``tcc_cleanup`` — the single source of truth, so the
    GUI install path does not carry a second copy of the reset logic.

    A mutating verb on the same trust boundary as ``permission.request``: audited
    on every exit path. Idempotent and best-effort — a ``tccutil`` non-zero exit
    is swallowed by ``run_decoy_cleanup`` itself, so the verb acks ``ok`` once the
    sweep has run. Callers gate it behind their own first-install/once-per-version
    one-shot so it does not re-fire on every reconnect.
    """
    from screencap.daemon import audit_log, provenance, tcc_cleanup

    peer = provenance.derive_peer_descriptor_from_asgi_scope(request.scope)

    def _audit(outcome: str) -> None:
        audit_log.record_verb(
            "permission.cleanup_decoys",
            peer_pid=peer.pid,
            peer_path=peer.path,
            classification=peer.classification,
            outcome=outcome,
        )

    try:
        # Off the event loop: tccutil can block briefly. run_decoy_cleanup is
        # itself fail-soft, so this only raises on a truly unexpected error.
        await asyncio.to_thread(tcc_cleanup.run_decoy_cleanup)
        _audit("ok")
        return JSONResponse(
            schema.envelope(schema_version=schema._PERMISSION_CLEANUP_API_VERSION)
        )
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(
            exc,
            schema_version=schema._PERMISSION_CLEANUP_API_VERSION,
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

    U5 title union: user-set recording titles are searched FIRST — before the
    index-existence early return — and unioned into the returned hits, so a
    renamed recording is found by a term in its title even when the content
    index is absent (the default — content indexing defaults off) or holds no
    frame match for it, including privacy-blocked / never-indexed recordings
    (AE3). Each title hit is pointer-only like every content hit, but carries the
    sentinel ``timestamp_ms=0`` (NOT a frame pointer) and ``match_source="title"``
    so a caller can tell it apart from an on-screen-text frame hit; content hits
    carry ``match_source="content"``. Titles are deduped against content hits by
    recording: a recording that already has a content-frame hit is NOT also listed
    via its title (the frame hit is richer — it carries a real pointer), so a title
    hit is emitted only for a recording with no content hit.
    """
    from screencap import catalog
    from screencap.content_index import ContentIndex, IndexState, default_index_path

    title_matches = catalog.match_user_titles(
        query, recording=recording, limit=_QUERY_MAX_RECORDINGS
    )

    def _title_hit(rec: str, title: str) -> dict[str, Any]:
        # Sentinel timestamp_ms=0: a title hit is not a frame pointer. match_source
        # steers a caller away from treating it as one (e.g. via frame.nearest).
        return {
            "recording": rec,
            "timestamp_ms": 0,
            "snippet": title,
            "score": 0.0,
            "match_source": "title",
        }

    path = default_index_path()
    if not path.exists():
        # No content index (the default). Title-only hits still return; the
        # index_state honestly reports the absent content store.
        title_only = [_title_hit(rec, title) for rec, title in title_matches]
        return {
            "hits": title_only if limit is None else title_only[:limit],
            "index_state": IndexState.NOT_INDEXED.value,
        }

    kwargs: dict[str, Any] = {}
    if limit is not None:
        kwargs["limit"] = limit
    with ContentIndex(path) as store:
        result = store.search(query, recording=recording, **kwargs)

    content_hits = [
        {
            "recording": h.recording,
            "timestamp_ms": h.timestamp_ms,
            "snippet": h.snippet,
            "score": h.score,
            "match_source": h.match_source,
        }
        for h in result.hits
    ]
    seen = {h["recording"] for h in content_hits}
    title_hits = [
        _title_hit(rec, title)
        for rec, title in title_matches
        if rec not in seen
    ]
    # Ranked content hits first, then title hits; cap the union to the requested
    # `limit` so appending title hits never overruns the caller's page size.
    combined = content_hits + title_hits
    return {
        "hits": combined if limit is None else combined[:limit],
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
        _check_subscription_for_recall(
            schema_version=schema._CONTENT_SEARCH_API_VERSION
        )
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
# SCR-179 apps.list distinct-value cap per kind (bounds the vocabulary so a huge
# library can't drive an unbounded distinct scan; truncation is surfaced, not
# silent). Also the internal per-recording scan cap U2 uses when the user limit
# must be applied AFTER the Python-side hostname filter.
_QUERY_MAX_DISTINCT = 500
_TIMELINE_SCAN_CAP = 5000


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


def _chunk_timing(recording_dir: Path, chunk_index: int) -> tuple[int, int] | None:
    """``chunk_{idx:04d}_manifest.json`` → ``(chunk_start_ms, chunk_duration_ms)``.

    SCR-186 U3: the wall-clock anchor for a chunk-granular transcript hit. Reads
    the per-chunk manifest's ``chunk_start`` / ``chunk_end`` (epoch seconds) and
    converts to ms with the same round-half-away-from-zero rule the frame stems
    use (``frame_resolve._round_half_away``), so a transcript anchor and a frame
    ms never disagree at a half-ms boundary. Local-only read; the manifest carries
    only counts + timing + blocked intervals (no OCR/URL content — R6-safe).

    Returns ``None`` when the manifest is absent/unreadable/incomplete so
    ``transcript.search`` stays best-effort (the timing fields go null, the hit is
    still returned).
    """
    from screencap.frame_resolve import _round_half_away

    manifest = recording_dir / f"chunk_{chunk_index:04d}_manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    start = data.get("chunk_start")
    end = data.get("chunk_end")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    return _round_half_away(start * 1000.0), _round_half_away((end - start) * 1000.0)


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
                chunk_index = _parse_chunk_index(path.name)
                # SCR-186: enrich per-chunk hits with the chunk's wall-clock anchor
                # so the hit is resolvable by frame.nearest. The anchor is
                # chunk-coarse (timestamp_granularity="chunk"): chunks default to
                # 15 min and carry no per-word timing, so chunk_duration_ms is the
                # cap an agent passes to frame.nearest. The bare whole-recording
                # transcript.txt spans every chunk (it is NOT chunk 0 in a
                # multi-chunk recording), so it gets no anchor — stamping it with
                # chunk-0 timing would mislocate a late match. All fields go null
                # when the chunk manifest is absent (best-effort).
                if path.name == "transcript.txt":
                    ts_ms, dur_ms = None, None
                else:
                    ts_ms, dur_ms = _chunk_timing(rec_dir, chunk_index) or (None, None)
                hits.append({
                    "recording": rec_dir.name,
                    "chunk_index": chunk_index,
                    "snippet": like_snippet(text, query, width=120, collapse_newlines=True),
                    "timestamp_ms": ts_ms,
                    "timestamp_granularity": "chunk" if ts_ms is not None else None,
                    "chunk_duration_ms": dur_ms,
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
    has_table/has_column; never touches OCR or the content index.

    The ``app`` filter matches a token against the app name, the bundle id, OR
    the hostname derived from ``browser_url`` (so a site like "github" matches
    browser usage whose app is just "Safari"/"Chrome"). ``browser_url`` is read
    internally ONLY as a filter predicate — it is never selected into a result
    row, and only its hostname (never the path/query, where OAuth codes / session
    tokens live) is ever inspected (see ``_safe_hostname``). The response row
    shape stays exactly ``{recording, timestamp_ms, app, title}`` (TimelineRow).
    """
    from screencap.content_index import escape_like
    from screencap.recording_db import has_column, has_table, open_recording_db

    start_s = start_ms / 1000.0 if start_ms is not None else None
    end_s = end_ms / 1000.0 if end_ms is not None else None
    app_token = app.lower() if app else None
    app_like = f"%{escape_like(app_token)}%" if app_token else None

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
                has_url = has_column(conn, "window_event", "browser_url")
                # A token can match via app name, bundle id, or browser_url
                # hostname. If none of those columns exist, this recording can
                # never match — skip it.
                if app_token and not (has_name or has_bundle or has_url):
                    continue

                name_expr = "app_name" if has_name else "NULL"
                bundle_expr = "app_bundle_id" if has_bundle else "NULL"
                title_expr = "title" if has_title else "NULL"

                params: list[Any] = []
                time_clause = ""
                if start_s is not None:
                    time_clause += " AND timestamp >= ?"
                    params.append(start_s)
                if end_s is not None:
                    time_clause += " AND timestamp < ?"
                    params.append(end_s)

                if app_token and has_url:
                    # Hostname matching can't be expressed in SQL (urlparse is
                    # Python-only) and must never run against the full URL. So we
                    # DROP the in-SQL app filter — otherwise a browser row whose
                    # app_name is "Safari" is dropped before its hostname is ever
                    # examined — scan a bounded set of time-ordered candidates
                    # (browser_url included for INTERNAL use only), filter
                    # app/bundle/host in Python, and apply `limit` AFTER that
                    # filter. The internal scan cap re-establishes the DoS bound
                    # the in-SQL LIMIT otherwise provided.
                    sql = (
                        f"SELECT timestamp, {name_expr}, {bundle_expr}, "
                        f"{title_expr}, browser_url FROM window_event "
                        "WHERE timestamp IS NOT NULL" + time_clause
                        + " ORDER BY timestamp LIMIT ?"
                    )
                    params.append(_TIMELINE_SCAN_CAP)
                    matched = 0
                    for ts, app_name, bundle, title, url in conn.execute(sql, params):
                        host = _safe_hostname(url) if url is not None else None
                        if not _timeline_row_matches(app_token, app_name, bundle, host):
                            continue
                        rows.append({
                            "recording": rec_dir.name,
                            "timestamp_ms": int(float(ts) * 1000),
                            "app": app_name or bundle,
                            "title": title,
                        })
                        matched += 1
                        if matched >= limit:
                            break
                else:
                    # No token, or no browser_url column: the cheaper in-SQL
                    # LIKE + LIMIT path (browser_url is never selected here).
                    sql = (
                        f"SELECT timestamp, {name_expr}, {bundle_expr}, {title_expr} "
                        "FROM window_event WHERE timestamp IS NOT NULL" + time_clause
                    )
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
            # rec_dir.name only — never the raw browser_url (R6 log-hygiene).
            logger.debug("timeline.query skipped %s", rec_dir.name, exc_info=True)
            continue

    # Cross-recording wall-clock order, bounded to `limit` (each recording
    # already returned ≤limit rows; take the globally-earliest `limit` without
    # a full sort of the gathered set).
    return heapq.nsmallest(limit, rows, key=lambda r: r["timestamp_ms"])


def _safe_hostname(raw: object) -> str | None:
    """Extract a bare lowercase hostname from a ``browser_url`` value.

    The raw URL is consumed entirely inside this tight scope and never returned
    or re-raised: any parse failure becomes ``None`` here, so the secret-bearing
    full URL (its path/query carry OAuth codes / session tokens) can never escape
    into a caller frame local that a logged traceback (``exc_info=True``) would
    capture. Only the host — never the path/query — leaves this function.
    """
    from urllib.parse import urlparse

    try:
        host = urlparse(str(raw)).hostname
    except Exception:
        return None
    return host.lower() if host else None


def _timeline_row_matches(
    token: str, app_name: object, bundle: object, host: str | None,
) -> bool:
    """Python-side OR-match for the timeline ``app`` filter: ``token`` (already
    lowercased) is a case-insensitive substring of the app name, the bundle id,
    or the browser_url hostname. ``host`` is pre-lowercased by
    :func:`_safe_hostname`; the raw URL is never passed here."""
    for value in (app_name, bundle):
        if value and token in str(value).lower():
            return True
    return bool(host and token in host)


def _run_apps_list() -> dict[str, Any]:
    """Distinct app names / bundle ids / visited hostnames across recordings.

    SCR-179 vocabulary source (R3). Read-only over each local ``recording.db``;
    schema-tolerant via ``has_table``/``has_column``. Hostnames are derived from
    ``browser_url`` through :func:`_safe_hostname` so no full URL is ever read
    into a frame that could be logged (R6 log-hygiene). Per-kind distinct cap;
    ``truncated`` flags a partial vocabulary instead of silently capping.
    """
    from screencap.recording_db import has_column, has_table, open_recording_db

    app_names: set[str] = set()
    app_bundles: set[str] = set()
    hostnames: set[str] = set()

    for rec_dir in _iter_recording_dirs(None):
        db_path = rec_dir / "recording.db"
        if not db_path.is_file():
            continue
        try:
            with open_recording_db(db_path, read_only=True) as conn:
                if not has_table(conn, "window_event"):
                    continue
                if has_column(conn, "window_event", "app_name"):
                    for (val,) in conn.execute(
                        "SELECT DISTINCT app_name FROM window_event "
                        "WHERE app_name IS NOT NULL AND app_name != '' LIMIT ?",
                        (_QUERY_MAX_DISTINCT,),
                    ):
                        app_names.add(str(val))
                if has_column(conn, "window_event", "app_bundle_id"):
                    for (val,) in conn.execute(
                        "SELECT DISTINCT app_bundle_id FROM window_event "
                        "WHERE app_bundle_id IS NOT NULL AND app_bundle_id != '' LIMIT ?",
                        (_QUERY_MAX_DISTINCT,),
                    ):
                        app_bundles.add(str(val))
                if has_column(conn, "window_event", "browser_url"):
                    for (val,) in conn.execute(
                        "SELECT DISTINCT browser_url FROM window_event "
                        "WHERE browser_url IS NOT NULL AND browser_url != '' LIMIT ?",
                        (_QUERY_MAX_DISTINCT,),
                    ):
                        host = _safe_hostname(val)
                        if host:
                            hostnames.add(host)
        except Exception:
            # One unreadable/older recording must not fail the whole query.
            # rec_dir.name only — never the raw browser_url (R6 log-hygiene).
            logger.debug("apps.list skipped %s", rec_dir.name, exc_info=True)
            continue

    truncated = (
        len(app_names) > _QUERY_MAX_DISTINCT
        or len(app_bundles) > _QUERY_MAX_DISTINCT
        or len(hostnames) > _QUERY_MAX_DISTINCT
    )

    def _cap(values: set[str]) -> list[str]:
        return sorted(values)[:_QUERY_MAX_DISTINCT]

    return {
        "app_names": _cap(app_names),
        "app_bundles": _cap(app_bundles),
        "hostnames": _cap(hostnames),
        "truncated": truncated,
    }


async def apps_list(request: Request) -> JSONResponse:
    """``GET /v0/apps.list`` — vocabulary for the in-app query parser (SCR-179).

    Returns distinct ``app_name`` / ``app_bundle_id`` values and **bare
    hostnames** from ``browser_url`` — never a full URL (the path/query, where
    OAuth codes / session tokens live, never leaves the daemon). The hostname
    list is a same-EUID-only *browsing-profile* artifact derived from the
    local-only ``recording.db`` (R6).

    Read-only. Like the other query verbs it is deliberately NOT in
    ``_ACTIVITY_PATHS`` (status/vocabulary polling must not pin an auto-spawned
    daemon) and is NOT audit-logged (it leaks no capability beyond what the
    same-EUID caller can read from disk directly).
    """
    try:
        _check_subscription_for_recall(schema_version=schema._APPS_LIST_API_VERSION)
        result = await asyncio.to_thread(_run_apps_list)
        return JSONResponse(
            schema.envelope(
                schema_version=schema._APPS_LIST_API_VERSION,
                app_names=result["app_names"],
                app_bundles=result["app_bundles"],
                hostnames=result["hostnames"],
                truncated=result["truncated"],
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._APPS_LIST_API_VERSION,
            request=request,
        )


async def transcript_search(request: Request) -> JSONResponse:
    """``POST /v0/transcript.search`` — keyword scan over scrubbed transcripts."""
    from screencap.daemon._name_validation import validate_recording_name

    try:
        _check_subscription_for_recall(
            schema_version=schema._TRANSCRIPT_SEARCH_API_VERSION
        )
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
        _check_subscription_for_recall(
            schema_version=schema._TIMELINE_QUERY_API_VERSION
        )
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


def _run_frame_nearest(
    recording: str, timestamp_ms: int, staleness_cap_ms: int,
) -> tuple[str, int] | None:
    """Resolve the nearest ALLOW frame for a recording, off the event loop.

    Lists the recording's frames, derives the fail-closed blocked-frame predicate
    (re-read per request — R7, no daemon-launch cache), filters to ALLOW frames,
    and selects the nearest within ``staleness_cap_ms``. Returns ``(stem,
    delta_ms)`` or ``None`` on any legitimate miss (missing dir, no frames, all
    frames blocked / indeterminate geometry, or over cap) — never raises for
    those, so the verb answers a miss rather than a 500.
    """
    from screencap import frame_blocked, frame_resolve
    from screencap.config import resolve_recording_dir

    rec_dir = resolve_recording_dir(recording)
    if not rec_dir.is_dir():
        return None
    frames = frame_resolve.load_frames(rec_dir / "screenshots")
    if not frames:
        return None
    # ALLOW-only filter (R8): never point an agent at a masked/excluded frame.
    # build_is_blocked is fail-closed — an indeterminate recording.db flags every
    # frame, so the eligible set empties and the verb returns a miss.
    is_blocked = frame_blocked.build_is_blocked(rec_dir, [f.ts for f in frames])
    eligible = [f for f in frames if not is_blocked(f.ts)]
    return frame_resolve.nearest_frame(eligible, timestamp_ms, staleness_cap_ms)


async def frame_nearest(request: Request) -> JSONResponse:
    """``POST /v0/frame.nearest`` — resolve a search pointer to the nearest ALLOW frame stem.

    Read-only nearest-frame resolution (SCR-186). Pointer-only response: a bare
    on-disk screenshot ``stem`` + signed ``delta_ms``, never a path or image bytes
    (priv-R8) — the agent expands ``screenshots/<stem>.jpg`` itself. The frame is
    filtered ALLOW-only (a masked/excluded/secure-field frame is never selected;
    indeterminate blocked geometry fails closed to a miss). A malformed or
    out-of-bounds request body returns a typed 400 (``invalid_request``); a
    traversal recording name returns 400 ``invalid_name``; a legitimate miss
    returns ``ok:true`` with null fields. Deliberately NOT in ``_ACTIVITY_PATHS``
    — idle-shutdown stays alive via the MCP-held subscription.
    """
    from pydantic import ValidationError

    from screencap.daemon._name_validation import validate_recording_name

    try:
        _check_subscription_for_recall(
            schema_version=schema._FRAME_NEAREST_API_VERSION
        )
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        try:
            parsed = schema.FrameNearestRequest.model_validate(body)
        except ValidationError:
            return _validation_error_response(
                schema_version=schema._FRAME_NEAREST_API_VERSION,
            )
        validate_recording_name(parsed.recording)
        result = await asyncio.to_thread(
            _run_frame_nearest,
            parsed.recording, parsed.timestamp_ms, parsed.staleness_cap_ms,
        )
        stem, delta_ms = result if result is not None else (None, None)
        from screencap.config import get_corpus_encrypted

        return JSONResponse(
            schema.envelope(
                schema_version=schema._FRAME_NEAREST_API_VERSION,
                stem=stem,
                delta_ms=delta_ms,
                # KTD6: signal encrypted storage so the agent calls frame.read
                # instead of reading the .jpg path directly.
                encrypted=get_corpus_encrypted(),
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._FRAME_NEAREST_API_VERSION,
            request=request,
        )


def _run_frame_read(
    recording: str, stem: str, max_bytes: int,
) -> tuple[bytes, str] | None:
    """Resolve + decrypt an ALLOW, scrubbed still for ``frame.read`` (search U8).

    Fail-closed refusals (return ``None``): a non-numeric ``stem``, missing
    recording/still, a blocked (masked/excluded/secure-field/indeterminate) frame, a
    frame whose chunk has NOT yet been secrets-scrubbed (KTD2 — enforced for the
    encrypted form always, and for a plaintext still once the corpus is encrypted),
    a missing corpus key, or a payload over ``max_bytes``. On success returns
    ``(jpeg_bytes, content_type)``."""
    from screencap import corpus_crypto, frame_blocked, scrub_state, still_io
    from screencap.config import get_corpus_encrypted, resolve_recording_dir

    # Validate the stem is a bare numeric timestamp BEFORE any filesystem access, so
    # a traversal-shaped or non-numeric stem is rejected without stat'ing a raw path.
    try:
        ts = float(stem)
    except ValueError:
        return None

    rec_dir = resolve_recording_dir(recording)
    if not rec_dir.is_dir():
        return None
    screenshots = rec_dir / "screenshots"
    enc_path = screenshots / f"{stem}.jpg.enc"
    plain_path = screenshots / f"{stem}.jpg"
    path = enc_path if enc_path.exists() else (plain_path if plain_path.exists() else None)
    if path is None:
        return None

    # ALLOW-only (R8): never serve a masked/excluded frame; indeterminate → refuse.
    is_blocked = frame_blocked.build_is_blocked(rec_dir, [ts])
    if is_blocked(ts):
        return None

    if still_io.is_encrypted_path(path):
        # KTD2: refuse a frame whose chunk hasn't been secrets-scrubbed yet.
        if not scrub_state.is_frame_scrubbed(rec_dir, round(ts * 1000)):
            return None
        try:
            key = corpus_crypto.load_corpus_key()
            data = still_io.open_still(path, key)
        except Exception:
            return None
    else:
        # A PLAINTEXT still. Once the corpus is meant to be encrypted (post-flip), a
        # surviving plaintext still is a leftover/anomaly (e.g. a not-yet-migrated or
        # in-flight frame) and must be held to the SAME scrub gate — otherwise an
        # unscrubbed secret frame could be served through this branch. Pre-flip
        # (plaintext corpus, agent reads the .jpg path directly) keeps today's behavior.
        if get_corpus_encrypted() and not scrub_state.is_frame_scrubbed(
            rec_dir, round(ts * 1000)
        ):
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None

    if len(data) > max_bytes:
        return None
    return data, "image/jpeg"


async def frame_read(request: Request) -> JSONResponse:
    """``POST /v0/frame.read`` — decrypt-and-serve an ALLOW, scrubbed still (search U8).

    A capability-bearing verb (KTD6): every invocation is audit-logged
    (peer + ok/blocked/error). Returns base64 JPEG bytes for an ALLOW frame from a
    scrubbed chunk, size-capped; refuses blocked frames and unscrubbed chunks
    (fail-closed, ``image_base64: null``). Same-EUID + ALLOW + scrubbed-chunk is the
    trust bar for agents (no present-user check — resolved OQ3); the app's own
    display gates on Touch ID separately (U6). NOT in ``_ACTIVITY_PATHS``."""
    import base64

    from pydantic import ValidationError

    from screencap.daemon import audit_log, provenance
    from screencap.daemon._name_validation import validate_recording_name

    peer = provenance.derive_peer_descriptor_from_asgi_scope(request.scope)
    recording_name: str | None = None

    def _audit(outcome: str) -> None:
        audit_log.record_verb(
            "frame.read",
            peer_pid=peer.pid,
            peer_path=peer.path,
            classification=peer.classification,
            outcome=outcome,
            recording_name=recording_name,
        )

    try:
        _check_subscription_for_recall(schema_version=schema._FRAME_READ_API_VERSION)
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        try:
            parsed = schema.FrameReadRequest.model_validate(body)
        except ValidationError:
            _audit("error")
            return _validation_error_response(schema_version=schema._FRAME_READ_API_VERSION)
        recording_name = parsed.recording
        validate_recording_name(parsed.recording)
        result = await asyncio.to_thread(
            _run_frame_read, parsed.recording, parsed.stem, schema._FRAME_READ_MAX_BYTES,
        )
        if result is None:
            _audit("blocked")
            return JSONResponse(
                schema.envelope(
                    schema_version=schema._FRAME_READ_API_VERSION,
                    image_base64=None,
                    content_type=None,
                )
            )
        data, content_type = result
        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=schema._FRAME_READ_API_VERSION,
                image_base64=base64.b64encode(data).decode("ascii"),
                content_type=content_type,
            )
        )
    except errors.DaemonAPIError as exc:
        _audit("error")
        return _api_error_response(exc)
    except Exception as exc:
        _audit("error")
        return _internal_error_response(
            exc, schema_version=schema._FRAME_READ_API_VERSION, request=request,
        )


def _run_tasks_list(recording: str) -> list[dict[str, Any]]:
    """Read a LOCAL recording's named-task segments, off the event loop.

    Reads the ``pipeline_task_segments`` ledger rows from the recording's
    local-only ``recording.db`` (never uploaded — R4/R8) via
    ``PipelineLedger.read_task_segments`` — the STRUCTURED sink U4 writes
    alongside ``tasks.json``. The ledger is chosen over ``tasks.json`` because it
    is already typed per-task rows (``task_index`` / ``start_ts`` / ``end_ts`` /
    ``name`` / ``category`` / ``confidence``), so the wire shape needs no re-parse
    of the provider's free-text blob, and it keeps the read on the same query
    seam every other consumer uses.

    Returns ``[]`` on any legitimate absence — a missing recording dir, a missing
    ``recording.db`` (legacy / pre-U1 recording), a DB with no ``recording`` row,
    or a recording whose segmentation produced no tasks. Never raises for those,
    so the verb answers an empty list rather than a 500. The ``PrivacyMode`` and
    upload rules are unchanged: this only reads rows the local pipeline already
    persisted; nothing leaves the Mac.
    """
    from screencap.config import resolve_recording_dir
    from screencap.pipeline_state import read_task_segments_wire

    return read_task_segments_wire(resolve_recording_dir(recording))


async def tasks_list(request: Request) -> JSONResponse:
    """``POST /v0/tasks.list`` — a LOCAL recording's named task segments (U10).

    Read-only surface over the U4 local tasks store: the named tasks the
    terminal-stage on-device segmentation persisted for a local recording (or the
    idle-gap heuristic fallback's mechanically-named ones). The app reads these to
    populate the SAME surfaces as cloud-derived tasks — the Journal day view's
    task breakdown and the Library card title/summary — with no cloud round-trip.

    The tasks live in the local-only ``recording.db`` (never uploaded — R4/R8), so
    this verb only ever exposes local-Mac data to the same-EUID caller. A missing
    store / legacy recording / provider miss returns ``ok:true`` with an empty
    ``tasks`` list (never an error), so a recording with no tasks renders
    gracefully. A traversal recording name returns 400 ``invalid_name``; a
    malformed body returns 400 ``invalid_request``. Deliberately NOT in
    ``_ACTIVITY_PATHS`` — a read verb must not reset the idle-shutdown clock.
    """
    from pydantic import ValidationError

    from screencap.daemon._name_validation import validate_recording_name

    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        try:
            parsed = schema.TasksListRequest.model_validate(body)
        except ValidationError:
            return _validation_error_response(
                schema_version=schema._TASKS_LIST_API_VERSION,
            )
        validate_recording_name(parsed.recording)
        rows = await asyncio.to_thread(_run_tasks_list, parsed.recording)
        tasks = [schema.TaskSegment(**row).model_dump() for row in rows]
        return JSONResponse(
            schema.envelope(
                schema_version=schema._TASKS_LIST_API_VERSION,
                recording=parsed.recording,
                tasks=tasks,
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._TASKS_LIST_API_VERSION,
            request=request,
        )


# ---------------------------------------------------------------------------
# tasks.create / update / delete / merge / split — U7 user task CRUD verbs.
#
# Post-hoc MUTATING verbs over the LOCAL-only ``pipeline_task_segments`` store
# (inside ``recording.db`` — never uploaded, R8). Each mirrors ``recording.rename``
# end-to-end: derive the peer descriptor, ``_audit`` on EVERY exit path (ok / typed
# error / unhandled), a validated request model, and typed error responses. Unlike
# ``recording.rename`` they do NOT refuse the live recording — for ambient the user
# curates tasks WHILE the day records, and ``PipelineLedger`` is built to coexist
# with the live engine writer (busy_timeout=10000 + BEGIN IMMEDIATE per write). The
# five verbs ARE mutations, so they join the ``_ACTIVITY_PATHS`` set (like
# recording.stop) in ``daemon/_idle_shutdown.py``.
# ---------------------------------------------------------------------------


def _validate_task_body(model: Any, body: Any, *, schema_version: int) -> Any:
    """Validate a task-CRUD request body → a typed 400 on any malformed input.

    A non-dict body or a pydantic ``ValidationError`` raises
    :class:`errors.InvalidRequestError` (400 ``invalid_request``) so the single
    ``except DaemonAPIError`` path audits the exit — no validation failure skips
    the audit line.
    """
    from pydantic import ValidationError

    if not isinstance(body, dict):
        raise errors.InvalidRequestError(schema_version=schema_version)
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise errors.InvalidRequestError(schema_version=schema_version) from exc


def _validate_task_span(
    start_ts: float, end_ts: float, *, schema_version: int
) -> None:
    """Reject a zero-length / inverted / out-of-range task span (400).

    A valid span is two FINITE, non-negative Unix-second bounds with
    ``end_ts > start_ts``. ``math.isfinite`` rejects the ``NaN`` / ``Infinity``
    tokens Python's ``json`` decoder accepts by default, so a direct UDS caller
    cannot smuggle a non-finite bound past the boundary.
    """
    import math

    if not (math.isfinite(start_ts) and math.isfinite(end_ts)):
        raise errors.InvalidRequestError(schema_version=schema_version)
    if start_ts < 0 or end_ts <= start_ts:
        raise errors.InvalidRequestError(schema_version=schema_version)


def _validate_task_name(name: str, *, schema_version: int) -> str:
    """Validate a task label as display text; require it non-empty.

    Reuses ``validate_recording_title`` (control chars / bidi / length<=200
    rejected → 400 ``invalid_name``), then rejects an empty/whitespace-only
    label with 400 ``invalid_request`` (a task must carry a name). Returns the
    stripped label.
    """
    from screencap.daemon._name_validation import validate_recording_title

    validated = validate_recording_title(name)
    stripped = validated.strip()
    if not stripped:
        raise errors.InvalidRequestError(schema_version=schema_version)
    return stripped


def _resolve_task_ledger(recording: str, schema_version: int):
    """Resolve a recording name to its :class:`PipelineLedger` (off the loop).

    Pure disk IO — the handlers run it via ``asyncio.to_thread``. The recording
    name is validated by ``validate_recording_name`` at the handler boundary
    BEFORE this runs, so ``resolve_recording_dir`` can only produce an in-root
    path. A missing recording dir / ``recording.db`` / recording row raises
    :class:`errors.RecordingNotFoundError` (404) — the mutating counterpart to
    ``tasks.list``'s fail-soft empty list (a write has no empty-result to return).
    """
    import sqlite3

    from screencap.config import resolve_recording_dir
    from screencap.pipeline_state import (
        LedgerError,
        PipelineLedger,
        ensure_pipeline_state_schema,
    )

    try:
        rec_dir = resolve_recording_dir(recording)
    except ValueError as exc:  # defense-in-depth; name is pre-validated
        raise errors.RecordingNotFoundError(schema_version=schema_version) from exc
    db_path = rec_dir / "recording.db"
    if not db_path.exists():
        raise errors.RecordingNotFoundError(schema_version=schema_version)
    try:
        ensure_pipeline_state_schema(db_path)
        return PipelineLedger(db_path)
    except (LedgerError, sqlite3.Error) as exc:
        raise errors.RecordingNotFoundError(schema_version=schema_version) from exc


def _task_span_orphaned(recording: str, ledger, start_ts: float, end_ts: float) -> bool:
    """SCR-214 U8 orphan guard: is ``[start_ts, end_ts)`` over EVICTED footage?

    Resolves the recording dir (the ``recording`` name is pre-validated at the
    handler boundary) and delegates to
    :func:`screencap.retention.task_span_is_orphaned`, which refuses ONLY on
    positive eviction evidence and otherwise fails open — so a task over live /
    legacy footage is never blocked. Pure disk IO; the handler runs it off the
    loop via ``asyncio.to_thread``.
    """
    from screencap.config import resolve_recording_dir
    from screencap.retention import task_span_is_orphaned

    try:
        rec_dir = resolve_recording_dir(recording)
    except ValueError:
        return False  # defense-in-depth; name is pre-validated + ledger already resolved
    return task_span_is_orphaned(rec_dir, ledger, start_ts, end_ts)


def _find_task_segment(ledger, task_index: int):
    """Return the stored task row at ``task_index`` (or ``None``) (SCR-214 U8).

    The counterpart the ``tasks.update`` orphan guard reads to compute an update's
    EFFECTIVE post-update span: a one-sided bound change combines the provided
    bound with this row's stored bound. Pure disk IO; the handler runs it off the
    loop via ``asyncio.to_thread``.
    """
    for row in ledger.read_task_segments():
        if row.task_index == task_index:
            return row
    return None


def _task_crud_peer_audit(request: Request, verb: str):
    """Build the (peer, ``_audit``) pair shared by the five task CRUD handlers.

    Mirrors ``recording.rename``: the peer descriptor is derived once so every
    exit path records the same descriptor, and the audit line carries peer +
    outcome ONLY — never the free-text task label (labels must not accrue in the
    local audit log, same rule as editable titles).
    """
    from screencap.daemon import audit_log, provenance

    peer = provenance.derive_peer_descriptor_from_asgi_scope(request.scope)

    def _audit(outcome: str) -> None:
        audit_log.record_verb(
            verb,
            peer_pid=peer.pid,
            peer_path=peer.path,
            classification=peer.classification,
            outcome=outcome,
        )

    return peer, _audit


async def tasks_create(request: Request) -> JSONResponse:
    """``POST /v0/tasks.create`` — add a USER-authored task span (U7).

    Writes one ``source='user'`` row at a disjoint HIGH ``task_index`` (KTD3) so
    it never collides with the agent's low range and survives the next agent
    re-segmentation. Rejects a zero-length / inverted / out-of-range span and an
    empty/control-char label with a typed 400; a traversal recording name with
    400 ``invalid_name``; an unknown recording with 404; and (U8 orphan guard) a
    span pointing only at already-EVICTED footage with 400 ``invalid_request`` —
    so no unplayable task is persisted. Local-only — the row lives in
    ``recording.db`` and is never uploaded.
    """
    from screencap.daemon._name_validation import validate_recording_name
    from screencap.pipeline_state import TaskSegmentRow

    _v = schema._TASKS_CREATE_API_VERSION
    _peer, _audit = _task_crud_peer_audit(request, "tasks.create")
    try:
        parsed = _validate_task_body(
            schema.TasksCreateRequest, await request.json(), schema_version=_v
        )
        validate_recording_name(parsed.recording)
        name = _validate_task_name(parsed.name, schema_version=_v)
        _validate_task_span(parsed.start_ts, parsed.end_ts, schema_version=_v)

        ledger = await asyncio.to_thread(_resolve_task_ledger, parsed.recording, _v)

        # SCR-214 U8 orphan guard: refuse a task whose span points only at
        # already-EVICTED footage — persisting it would leave an unplayable task.
        # Fails open on live / legacy recordings with no eviction, so normal
        # creation is never blocked.
        if await asyncio.to_thread(
            _task_span_orphaned, parsed.recording, ledger,
            parsed.start_ts, parsed.end_ts,
        ):
            raise errors.InvalidRequestError(schema_version=_v)

        row = TaskSegmentRow(
            task_index=0,  # ignored; insert_task_segment allocates the HIGH index
            start_ts=parsed.start_ts,
            end_ts=parsed.end_ts,
            name=name,
            category=parsed.category,
        )
        idx = await asyncio.to_thread(ledger.insert_task_segment, row)

        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=_v,
                recording=parsed.recording,
                task=schema.TaskSegment(
                    task_index=idx,
                    start_ts=parsed.start_ts,
                    end_ts=parsed.end_ts,
                    name=name,
                    category=parsed.category,
                    confidence=None,
                ).model_dump(),
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(exc, schema_version=_v, request=request)


async def tasks_update(request: Request) -> JSONResponse:
    """``POST /v0/tasks.update`` — rename / re-bound an existing task (U7).

    Every edit marks the row curated (``edited=1``); an agent row is additionally
    RE-HOMED into the HIGH range so the next scoped agent replace preserves it
    (KTD3). Only the provided fields are written. A missing ``task_index`` and a
    provided-but-inverted span each return 400 ``invalid_request``; a traversal
    name 400 ``invalid_name``; an unknown recording 404. Local-only.

    SCR-214 U8 orphan guard (parity with ``tasks.create``): when the request
    supplies a span bound, re-bounding the task onto already-EVICTED footage —
    which ``mark_edited=True`` would then PROTECT as a kept span — is rejected with
    the same 400 ``invalid_request``, so no unplayable task is persisted.
    """
    from screencap.daemon._name_validation import validate_recording_name

    _v = schema._TASKS_UPDATE_API_VERSION
    _peer, _audit = _task_crud_peer_audit(request, "tasks.update")
    try:
        parsed = _validate_task_body(
            schema.TasksUpdateRequest, await request.json(), schema_version=_v
        )
        validate_recording_name(parsed.recording)
        name = (
            _validate_task_name(parsed.name, schema_version=_v)
            if parsed.name is not None
            else None
        )
        # Only cross-validate the span when BOTH bounds are supplied — a partial
        # bound update (one side) is left to the stored counterpart.
        if parsed.start_ts is not None and parsed.end_ts is not None:
            _validate_task_span(parsed.start_ts, parsed.end_ts, schema_version=_v)

        ledger = await asyncio.to_thread(_resolve_task_ledger, parsed.recording, _v)

        # SCR-214 U8 orphan guard (parity with tasks.create): a span re-bound onto
        # footage the 30-day ambient retention already rolled off would persist an
        # unplayable task — and mark_edited=True below would then make that orphan a
        # PROTECTED kept span. When the request supplies a span bound, run the SAME
        # check create runs, over the EFFECTIVE post-update span (a one-sided bound
        # combines with the stored counterpart). Skipped for a name-only edit (no
        # span bound) and when the row is absent (update_task_segment returns None →
        # invalid_request below); fails open on live / legacy footage exactly like
        # create, so a normal re-bound onto surviving footage is never blocked.
        if parsed.start_ts is not None or parsed.end_ts is not None:
            existing = await asyncio.to_thread(
                _find_task_segment, ledger, parsed.task_index
            )
            if existing is not None:
                eff_start = (
                    parsed.start_ts if parsed.start_ts is not None else existing.start_ts
                )
                eff_end = (
                    parsed.end_ts if parsed.end_ts is not None else existing.end_ts
                )
                if await asyncio.to_thread(
                    _task_span_orphaned, parsed.recording, ledger, eff_start, eff_end,
                ):
                    raise errors.InvalidRequestError(schema_version=_v)

        # Always mark_edited: a user touch protects the row from the agent
        # replace, and re-homes an agent row out of the clobbered low range.
        new_index = await asyncio.to_thread(
            lambda: ledger.update_task_segment(
                parsed.task_index,
                name=name,
                start_ts=parsed.start_ts,
                end_ts=parsed.end_ts,
                category=parsed.category,
                mark_edited=True,
            )
        )
        if new_index is None:
            raise errors.InvalidRequestError(schema_version=_v)

        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=_v,
                recording=parsed.recording,
                task_index=new_index,
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(exc, schema_version=_v, request=request)


async def tasks_delete(request: Request) -> JSONResponse:
    """``POST /v0/tasks.delete`` — remove one task by ``task_index`` (U7).

    Idempotent: deleting an already-absent task returns 200 ``deleted:false``
    rather than an error, so a dropped/retried delete converges. A traversal name
    returns 400 ``invalid_name``; an unknown recording 404. Local-only.
    """
    from screencap.daemon._name_validation import validate_recording_name

    _v = schema._TASKS_DELETE_API_VERSION
    _peer, _audit = _task_crud_peer_audit(request, "tasks.delete")
    try:
        parsed = _validate_task_body(
            schema.TasksDeleteRequest, await request.json(), schema_version=_v
        )
        validate_recording_name(parsed.recording)

        ledger = await asyncio.to_thread(_resolve_task_ledger, parsed.recording, _v)
        deleted = await asyncio.to_thread(
            ledger.delete_task_segment, parsed.task_index
        )

        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=_v,
                recording=parsed.recording,
                deleted=bool(deleted),
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(exc, schema_version=_v, request=request)


async def tasks_merge(request: Request) -> JSONResponse:
    """``POST /v0/tasks.merge`` — combine >=2 segments into one, atomically (U7).

    The surviving label is the caller's choice, the span is the union, and the
    result is one ``source='user'`` row (HIGH range) that survives the next agent
    replace — all in a single ``BEGIN IMMEDIATE`` transaction, so a partial
    failure never leaves a half-merge. Fewer than two resolvable segments returns
    400 ``invalid_request``; a traversal name 400 ``invalid_name``; an unknown
    recording 404. Local-only.
    """
    from screencap.daemon._name_validation import validate_recording_name

    _v = schema._TASKS_MERGE_API_VERSION
    _peer, _audit = _task_crud_peer_audit(request, "tasks.merge")
    try:
        parsed = _validate_task_body(
            schema.TasksMergeRequest, await request.json(), schema_version=_v
        )
        validate_recording_name(parsed.recording)
        name = _validate_task_name(parsed.name, schema_version=_v)
        # >=2 DISTINCT targets required — the ledger also re-checks against the
        # rows that actually exist, so a stale index list can't half-merge.
        if len(set(parsed.task_indices)) < 2:
            raise errors.InvalidRequestError(schema_version=_v)

        ledger = await asyncio.to_thread(_resolve_task_ledger, parsed.recording, _v)
        new_index = await asyncio.to_thread(
            lambda: ledger.merge_task_segments(
                parsed.task_indices,
                name=name,
                category=parsed.category,
            )
        )
        if new_index is None:
            raise errors.InvalidRequestError(schema_version=_v)

        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=_v,
                recording=parsed.recording,
                task_index=new_index,
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(exc, schema_version=_v, request=request)


async def tasks_split(request: Request) -> JSONResponse:
    """``POST /v0/tasks.split`` — split one segment into two at ``split_ts`` (U7).

    ``split_ts`` must lie STRICTLY within the segment's span (validated against
    the stored bounds inside the transaction); otherwise, or if the segment is
    absent, returns 400 ``invalid_request``. Produces two ``source='user'`` rows
    (HIGH range) in one atomic transaction — never a half-split. A traversal name
    returns 400 ``invalid_name``; an unknown recording 404. Local-only.
    """
    import math

    from screencap.daemon._name_validation import validate_recording_name

    _v = schema._TASKS_SPLIT_API_VERSION
    _peer, _audit = _task_crud_peer_audit(request, "tasks.split")
    try:
        parsed = _validate_task_body(
            schema.TasksSplitRequest, await request.json(), schema_version=_v
        )
        validate_recording_name(parsed.recording)
        if not math.isfinite(parsed.split_ts):
            raise errors.InvalidRequestError(schema_version=_v)
        name_left = (
            _validate_task_name(parsed.name_left, schema_version=_v)
            if parsed.name_left is not None
            else None
        )
        name_right = (
            _validate_task_name(parsed.name_right, schema_version=_v)
            if parsed.name_right is not None
            else None
        )

        ledger = await asyncio.to_thread(_resolve_task_ledger, parsed.recording, _v)
        result = await asyncio.to_thread(
            lambda: ledger.split_task_segment(
                parsed.task_index,
                parsed.split_ts,
                name_left=name_left,
                name_right=name_right,
            )
        )
        if result is None:
            # No such row, or split_ts not strictly inside the stored span.
            raise errors.InvalidRequestError(schema_version=_v)

        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=_v,
                recording=parsed.recording,
                task_indices=list(result),
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(exc, schema_version=_v, request=request)


# ---------------------------------------------------------------------------
# ambient.status / ambient.set — SCR-214 U12 runtime ambient control (U12
# prerequisite: the macOS app enables/disables + reads always-on ambient at
# runtime). ``ambient.status`` is a READ snapshot (like session.snapshot /
# tasks.list — deliberately NOT in ``_ACTIVITY_PATHS`` so polling it never resets
# the idle-shutdown clock). ``ambient.set`` MUTATES persisted config AND drives the
# supervisor to start/stop ambient NOW, so it mirrors recording.mute's audited
# trust-boundary posture (peer descriptor + ``_audit`` on every exit path, a
# validated model, typed errors) and joins ``_ACTIVITY_PATHS`` (a genuine
# recording-lifecycle mutation, like recording.stop).
# ---------------------------------------------------------------------------


def _ambient_status_payload(supervisor: Any) -> dict[str, Any]:
    """Map ``Supervisor.ambient_state()`` to the app-facing status wire fields.

    Selects exactly the six ``AmbientStatusResponse`` fields (dropping the
    internal ``retry_count`` diagnostic ``ambient_state`` also carries), so
    ``ambient.status`` and ``ambient.set`` return one identical shape.
    """
    state = supervisor.ambient_state()
    return {
        "enabled": bool(state["enabled"]),
        "autostart": bool(state["autostart"]),
        "active": bool(state["active"]),
        "paused": bool(state["paused"]),
        "degraded": state["degraded"],
        "recording": state["recording"],
    }


async def ambient_status(request: Request) -> JSONResponse:
    """``GET /v0/ambient.status`` — the app's runtime view of ambient capture (U12).

    A read-only snapshot of always-on ambient supervision: the persisted
    enabled/autostart toggles plus the live active/paused/degraded state and the
    ambient recording's name (so the app can target ``recording.pause`` /
    ``.resume``). Reads only in-process supervisor state + cached config — no
    recording data leaves the Mac. Deliberately NOT in ``_ACTIVITY_PATHS``: a
    status poll must not keep an auto-spawned daemon alive.
    """
    try:
        payload = _ambient_status_payload(request.app.state.supervisor)
        return JSONResponse(
            schema.envelope(
                schema_version=schema._AMBIENT_STATUS_API_VERSION,
                **payload,
            )
        )
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._AMBIENT_STATUS_API_VERSION,
            request=request,
        )


async def ambient_set(request: Request) -> JSONResponse:
    """``POST /v0/ambient.set`` — enable/disable ambient (+ autostart) at runtime (U12).

    Body ``{enabled?: bool, autostart?: bool}`` — each key is applied only when
    present, so the app can flip ``autostart`` without touching ``enabled``.
    ``enabled=True`` persists the opt-in then drives the supervisor to START
    ambient NOW via the idempotent, gate-checked ``_maybe_autostart_ambient`` (a
    no-op if ambient is already active). ``enabled=False`` persists the opt-out
    FIRST then STOPS the running ambient recording via ``stop_ambient_now`` —
    because config is now False, the engine-exit re-arm won't respawn it.
    ``autostart`` only persists config. Returns the NEW ambient.status payload.

    A MUTATING verb on the same trust boundary as recording.mute: the peer
    descriptor is derived and every exit path (ok / typed error / unhandled) is
    audited. A malformed body (non-dict, or a non-bool value) is a typed 400
    ``invalid_request``. The config writes run off the event loop (tomlkit disk IO).
    """
    from screencap.config import set_ambient_autostart, set_ambient_enabled
    from screencap.daemon import audit_log, provenance

    _v = schema._AMBIENT_SET_API_VERSION
    peer = provenance.derive_peer_descriptor_from_asgi_scope(request.scope)

    def _audit(outcome: str) -> None:
        audit_log.record_verb(
            "ambient.set",
            peer_pid=peer.pid,
            peer_path=peer.path,
            classification=peer.classification,
            outcome=outcome,
        )

    try:
        parsed = _validate_task_body(
            schema.AmbientSetRequest, await request.json(), schema_version=_v
        )
        supervisor = request.app.state.supervisor

        if parsed.enabled is not None:
            # Persist the opt-in/out FIRST (off-loop: tomlkit load/save is disk IO),
            # so both the start/stop below AND the engine-exit re-arm observe the
            # new config value.
            await asyncio.to_thread(set_ambient_enabled, parsed.enabled)
            if parsed.enabled:
                # Idempotent + gate-checked: schedules a detached spawn only when
                # ambient is not already active/pending/degraded (R2).
                supervisor._maybe_autostart_ambient()
            else:
                # Config is already False, so the exit-funnel re-arm will NOT
                # respawn what we stop here.
                await supervisor.stop_ambient_now()

        if parsed.autostart is not None:
            await asyncio.to_thread(set_ambient_autostart, parsed.autostart)

        _audit("ok")
        return JSONResponse(
            schema.envelope(
                schema_version=_v,
                **_ambient_status_payload(supervisor),
            )
        )
    except errors.DaemonAPIError as exc:
        _audit(exc.error_code)
        return _api_error_response(exc)
    except Exception as exc:
        _audit(errors.ERROR_CODE_INTERNAL)
        return _internal_error_response(exc, schema_version=_v, request=request)


async def timeline_day(request: Request) -> JSONResponse:
    """``POST /v0/timeline.day`` — day-scoped spans + honest blocked intervals (U3).

    Read-only day surface for the Day timeline. Returns each recording's span
    intersecting the local calendar day plus its blocked intervals split into
    ``blocked_proven`` (provable ``SCRUB_BLOCK_ACTIONS`` masking) and
    ``unverifiable`` (deleted-row coverage gaps / null-column ambiguity — the UI
    must NOT label these "blocked", R7). Recording names are fine here — this is a
    same-EUID app surface (the name-free constraint is a backfill-progress rule).

    A malformed ``date`` returns a typed 400 (``invalid_request``); a legitimate
    empty day returns ``ok:true`` with no recordings. Deliberately NOT in
    ``_ACTIVITY_PATHS`` — a read verb must not reset the idle-shutdown clock.
    """
    from pydantic import ValidationError

    from screencap import day_segments

    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        try:
            parsed = schema.TimelineDayRequest.model_validate(body)
        except ValidationError:
            return _validation_error_response(
                schema_version=schema._TIMELINE_DAY_API_VERSION,
            )
        try:
            result = await asyncio.to_thread(
                day_segments.day_segments, parsed.date, parsed.tz_offset_seconds,
            )
        except day_segments.InvalidDayRequest:
            return _validation_error_response(
                schema_version=schema._TIMELINE_DAY_API_VERSION,
            )
        # Validate the day-surface shape through the typed response model (parity
        # with every other read verb) so any drift in day_segments' output is
        # caught here rather than shipping an undocumented shape.
        recordings = [
            schema.DaySegmentRecording(**rec).model_dump()
            for rec in result["recordings"]
        ]
        return JSONResponse(
            schema.envelope(
                schema_version=schema._TIMELINE_DAY_API_VERSION,
                date=result["date"],
                recordings=recordings,
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._TIMELINE_DAY_API_VERSION,
            request=request,
        )


def _backfill_job(app: Starlette) -> Any:
    """Lazily attach the single backfill job holder to ``app.state``.

    Scoped per app instance (not module-global) so tests building fresh apps
    don't share a job. Lazy init is asyncio-safe: the ``hasattr``/assignment
    below never await, so two concurrent handlers can't interleave between the
    check and the set.
    """
    state = app.state
    if not hasattr(state, "backfill_job"):
        from screencap.daemon.backfill_job import BackfillJob

        state.backfill_job = BackfillJob(state.event_bus)
    return state.backfill_job


async def _backfill_body(request: Request) -> dict[str, Any]:
    """Read a backfill request body tolerantly.

    The three backfill verbs take no required fields, so an empty body (or no
    body at all — a bare POST/GET) is valid. ``request.json()`` raises on an
    empty payload, so we degrade to ``{}`` rather than 500-ing on a parameterless
    call.
    """
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def backfill_start(request: Request) -> JSONResponse:
    """``POST /v0/backfill.start`` — start (or resume) the content-index backfill.

    Idempotent: a ``start`` while a run is in flight returns the existing job's
    status snapshot rather than spawning a second task (one job at a time —
    avoids double OCR load + content-index lock contention). The run executes the
    U4 engine via ``asyncio.to_thread`` inside the job; progress is published on
    ``/v0/events`` as ``backfill.progress`` (privacy-safe: opaque ordinal only,
    never a recording dir name — R9). Deliberately NOT in ``_ACTIVITY_PATHS`` —
    a running backfill keeps the daemon alive via ``_daemon_is_busy``, not via
    the idle timer.
    """
    try:
        body = await _backfill_body(request)
        parsed = schema.BackfillStartRequest.model_validate(body)
        job = _backfill_job(request.app)
        kwargs: dict[str, Any] = {}
        if parsed.budget_s is not None:
            kwargs["budget_s"] = parsed.budget_s
        if parsed.max_frames_per_recording is not None:
            kwargs["max_frames_per_recording"] = parsed.max_frames_per_recording
        snapshot = job.start(**kwargs)
        return JSONResponse(
            schema.envelope(
                schema_version=schema._BACKFILL_API_VERSION,
                **snapshot.as_payload(),
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._BACKFILL_API_VERSION,
            request=request,
        )


async def backfill_status(request: Request) -> JSONResponse:
    """``GET /v0/backfill.status`` — current privacy-safe backfill snapshot.

    Read-only. Carries ONLY ``(state, done, skipped, failed, total,
    current_unit_index)`` — never a recording dir name (R9). NOT in
    ``_ACTIVITY_PATHS``: status-polling must not reset the idle timer (the busy
    predicate covers liveness while a run is active).
    """
    try:
        job = _backfill_job(request.app)
        snapshot = job.status()
        return JSONResponse(
            schema.envelope(
                schema_version=schema._BACKFILL_API_VERSION,
                **snapshot.as_payload(),
            )
        )
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._BACKFILL_API_VERSION,
            request=request,
        )


async def backfill_cancel(request: Request) -> JSONResponse:
    """``POST /v0/backfill.cancel`` — signal the in-flight run to stop.

    Sets the engine's stop flag; the run converges to ``cancelled`` (the ledger
    records partial progress, resumable). A no-op returning the current snapshot
    when no run is in flight.
    """
    try:
        body = await _backfill_body(request)
        schema.BackfillCancelRequest.model_validate(body)
        job = _backfill_job(request.app)
        snapshot = job.cancel()
        return JSONResponse(
            schema.envelope(
                schema_version=schema._BACKFILL_API_VERSION,
                **snapshot.as_payload(),
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._BACKFILL_API_VERSION,
            request=request,
        )


def _model_download_job(app: Starlette) -> Any:
    """Lazily attach the single model-download job holder to ``app.state`` (SCR-239)."""
    state = app.state
    if not hasattr(state, "model_download_job"):
        from screencap.daemon.model_download_job import ModelDownloadJob

        state.model_download_job = ModelDownloadJob(state.event_bus)
    return state.model_download_job


def _model_status_payload() -> dict[str, Any]:
    """Build the install-state snapshot for ``/v0/model.status`` (SCR-239)."""
    from screencap.models import DEFAULT_MODEL_ID
    from screencap.models.download import get_disclosed_size, is_model_installed
    from screencap.segmentation.local_model.runtime import select_runtime

    runtime = select_runtime()  # resolve the host runtime once
    return {
        "models": [
            {
                "model_id": DEFAULT_MODEL_ID,
                "size_bytes": get_disclosed_size(DEFAULT_MODEL_ID, runtime) or 0,
                "installed": is_model_installed(DEFAULT_MODEL_ID, runtime),
            }
        ]
    }


async def model_download_start(request: Request) -> JSONResponse:
    """``POST /v0/model.download.start`` — start (or return) the model download.

    Idempotent: a start while a download is in flight returns the in-flight
    snapshot. Progress is published on ``/v0/events`` as ``model.download.progress``
    (throttled, no recording context). Deliberately NOT in ``_ACTIVITY_PATHS`` — a
    running download keeps the daemon alive via ``_daemon_is_busy``.
    """
    try:
        body = await _backfill_body(request)
        parsed = schema.ModelDownloadStartRequest.model_validate(body)
        job = _model_download_job(request.app)
        snapshot = job.start(parsed.model_id)
        return JSONResponse(
            schema.envelope(schema_version=schema._MODELS_API_VERSION, **snapshot.as_payload())
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc, schema_version=schema._MODELS_API_VERSION, request=request
        )


async def model_download_status(request: Request) -> JSONResponse:
    """``GET /v0/model.download.status`` — current download snapshot (read-only)."""
    try:
        job = _model_download_job(request.app)
        snapshot = job.status()
        return JSONResponse(
            schema.envelope(schema_version=schema._MODELS_API_VERSION, **snapshot.as_payload())
        )
    except Exception as exc:
        return _internal_error_response(
            exc, schema_version=schema._MODELS_API_VERSION, request=request
        )


async def model_download_cancel(request: Request) -> JSONResponse:
    """``POST /v0/model.download.cancel`` — signal the in-flight download to stop."""
    try:
        body = await _backfill_body(request)
        schema.ModelDownloadCancelRequest.model_validate(body)
        job = _model_download_job(request.app)
        snapshot = job.cancel()
        return JSONResponse(
            schema.envelope(schema_version=schema._MODELS_API_VERSION, **snapshot.as_payload())
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc, schema_version=schema._MODELS_API_VERSION, request=request
        )


async def model_status(request: Request) -> JSONResponse:
    """``GET /v0/model.status`` — installed-model snapshot for the settings UI."""
    try:
        return JSONResponse(
            schema.envelope(
                schema_version=schema._MODELS_API_VERSION, **_model_status_payload()
            )
        )
    except Exception as exc:
        return _internal_error_response(
            exc, schema_version=schema._MODELS_API_VERSION, request=request
        )


def _graceful_refusal(kind: QuestionKind = QuestionKind.POINT) -> ChatAnswer:
    """A fail-safe refusal :class:`ChatAnswer` for a downstream miss (KTD8).

    Returned when the orchestrator/dispatch raises (retrieval backend error, empty
    index, provider fault, …) so the verb degrades to a graceful 200 envelope
    rather than a 500. Carries NO sources and an honest ``store_unavailable``
    coverage — never a leaked error string.
    """
    from screencap.segmentation.consent import ExecutionTarget

    return ChatAnswer(
        answer=(
            "I couldn't reach your recorded history to answer that right now."
        ),
        sources=[],
        coverage=CoverageDescriptor(
            state=CoverageState.STORE_UNAVAILABLE,
            note="the recall backend was unavailable for this question",
            per_stream={},
        ),
        target=ExecutionTarget.NONE,
        refusal=True,
        question_kind=kind,
    )


def _run_chat_answer(
    question: str,
    prior_turns: list[PriorTurnPointer],
    window_ms: tuple[int, int] | None,
    app: str | None,
    limit: int | None,
) -> ChatAnswer:
    """Build the evidence bundle (U3) then generate the grounded answer (U4).

    Runs off the event loop via ``asyncio.to_thread`` (retrieval + the provider
    seam are blocking). FAIL-SAFE (KTD8): any downstream exception — a retrieval
    backend fault, an empty/corrupt index, a provider error — is caught here and
    mapped to a graceful refusal :class:`ChatAnswer`, so the verb NEVER 500s on a
    downstream miss. ``answer_from_bundle`` is itself already fail-closed (it
    degrades an ordinary model/API error or an egress breach to a refusal); this
    guard covers the bundle-builder path and any unexpected raise.

    A refusal (no evidence, on-device unavailable + no cloud consent) is a normal
    :class:`ChatAnswer` with ``refusal=True`` — NOT an error. The on-device answer
    backend is gated on SCR-243 and reports unavailable today, so a real call with
    no cloud consent legitimately refuses; that is a clean success here.
    """
    try:
        bundle = build_evidence_bundle(
            question,
            prior_turns=prior_turns,
            window_ms=window_ms,
            app=app,
            limit=limit,
        )
    except Exception:
        logger.warning("chat.answer: evidence-bundle build failed; refusing", exc_info=True)
        return _graceful_refusal()
    try:
        return answer_from_bundle(bundle, question=question)
    except Exception:
        logger.warning("chat.answer: answer generation failed; refusing", exc_info=True)
        return _graceful_refusal(bundle.question_kind)


def _chat_answer_payload(result: ChatAnswer) -> dict[str, Any]:
    """Map a :class:`ChatAnswer` to the pointer-only wire payload (R2, R8).

    Sources carry ONLY ``(recording, timestamp_ms, stream)`` — no path, no image
    bytes. The typed :class:`schema.ChatAnswerResponse` (round-tripped through the
    envelope) is the R8 boundary: the shape is structurally incapable of carrying
    media.
    """
    return {
        "answer": result.answer,
        "sources": [
            {
                "recording": p.recording,
                "timestamp_ms": p.timestamp_ms,
                "stream": p.stream.value,
            }
            for p in result.sources
        ],
        "coverage": {
            "state": result.coverage.state.value,
            "note": result.coverage.note,
            "per_stream": dict(result.coverage.per_stream),
        },
        "refusal": result.refusal,
        "question_kind": result.question_kind.value,
        "target": result.target.value,
        "reason": result.reason,
    }


async def chat_answer(request: Request) -> JSONResponse:
    """``POST /v0/chat.answer`` — a grounded conversational-recall answer (U5).

    Exposes the U3 evidence-bundle builder + U4 dispatch as a FAIL-SAFE, read-only
    verb (KTD8). A valid request returns generated prose + pointer-only source
    locators + an honest coverage descriptor. Pointer-only response (R2/R8): the
    :class:`schema.ChatAnswerResponse` shape is structurally incapable of carrying a
    media path or image bytes.

    Hygiene (KTD8):

    * **Fail-safe** — a downstream miss (retrieval/provider error, empty index)
      degrades to a graceful refusal envelope, NEVER a 500. A refusal (no evidence,
      or on-device unavailable + no cloud consent) is a normal 200 with
      ``refusal=true``, not an error.
    * **Typed 4xx only for malformed INPUT** — a bad body / out-of-bounds field
      returns 400 ``invalid_request``; a traversal recording name in a prior-turn
      pointer returns 400 ``invalid_name``.
    * **Same-EUID gated** — inherited from the socket / ASGI scope like every other
      ``/v0`` verb (no new work).
    * Deliberately NOT in ``_ACTIVITY_PATHS`` — a chat turn must not reset the
      idle-shutdown clock (a cron / background caller can't pin an auto-spawned
      daemon).
    """
    from pydantic import ValidationError

    from screencap.daemon._name_validation import validate_recording_name

    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        try:
            parsed = schema.ChatAnswerRequest.model_validate(body)
        except ValidationError:
            return _validation_error_response(
                schema_version=schema._CHAT_ANSWER_API_VERSION,
            )

        # Prior-turn pointers carry a recording NAME the orchestrator re-derives
        # snippet text from — validate each (traversal-safe) up front so a bad name
        # returns a typed 4xx, never reaching the retrieval seam.
        prior_turns: list[PriorTurnPointer] = []
        for turn in parsed.prior_turns:
            validate_recording_name(turn.recording)
            prior_turns.append(
                PriorTurnPointer(
                    recording=turn.recording,
                    timestamp_ms=turn.timestamp_ms,
                    stream=turn.stream,
                )
            )

        window_ms = tuple(parsed.window_ms) if parsed.window_ms is not None else None
        # No explicit client window → resolve a natural-language time reference from
        # the question daemon-side (U3), so "yesterday" / "this morning" scope the
        # turn. An explicit client window_ms takes precedence (left as-is above).
        if window_ms is None:
            import time

            from screencap.recall.timeparse import resolve_time_window

            resolved = resolve_time_window(
                parsed.question, now_ms=int(time.time() * 1000)
            )
            if resolved is not None:
                window_ms = resolved
        limit = _clamp_limit(parsed.limit) if parsed.limit is not None else None

        result = await asyncio.to_thread(
            _run_chat_answer,
            parsed.question,
            prior_turns,
            window_ms,
            parsed.app,
            limit,
        )
        return JSONResponse(
            schema.envelope(
                schema_version=schema._CHAT_ANSWER_API_VERSION,
                **_chat_answer_payload(result),
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc,
            schema_version=schema._CHAT_ANSWER_API_VERSION,
            request=request,
        )


def _storage_recording_active() -> bool:
    """True if any process (daemon or CLI) is mid-recording (SCR-228 U3).

    Mirrors ``session_snapshot``'s pidfile invariant: an active flock alone is
    not enough — a long-lived SessionController holds the lock between
    recordings without ``recording_started_at``. Catches a non-daemon-owned
    recording that ``supervisor.acquire_migration`` (which only sees
    ``_proc``) would miss.
    """
    from screencap import pidfile

    if not pidfile.lock_is_active():
        return False
    meta = pidfile.read_lock_metadata()
    return bool(meta and meta.get("recording_started_at") is not None)


def _terminal_stage_active() -> bool:
    """True if any recording's terminal stage holds its advisory flock (SCR-228).

    A just-stopped recording's terminal stage (scrub -> upload -> sentinel) runs
    AFTER ``recording_started_at`` is cleared, and — for a CLI-initiated
    recording — out of the daemon process, so neither ``_storage_recording_active``
    nor ``supervisor.has_inflight_resume`` sees it. It holds
    ``~/.screencap/run/terminal-<name>.lock`` (``terminal_stage.terminal_lock``)
    the whole time. A non-blocking probe of those locks catches an in-flight
    finalize that an ``os.rename`` of the tree would corrupt.
    """
    import fcntl
    import glob

    from screencap.config import _DEFAULT_BASE

    run_dir = _DEFAULT_BASE / "run"
    for lock_path in glob.glob(str(run_dir / "terminal-*.lock")):
        try:
            fd = os.open(lock_path, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)  # acquired -> not held; release it
        except OSError:
            return True  # held by a live terminal stage
        finally:
            os.close(fd)
    return False


async def storage_migrate(request: Request) -> JSONResponse:
    """``POST /v0/storage.migrate`` — relocate the recordings library (SCR-228).

    Synchronous and same-volume-only: an ``os.rename`` of the recordings tree is
    atomic and O(1), so there is no background job, progress stream, or cancel.
    Mutual exclusion (a recording must not run during the move, and no recording
    may start while it runs) is enforced by ``supervisor.acquire_migration`` +
    the ``spawn`` guard under the shared operation lock; a non-daemon recording
    is caught by the pidfile check. The config flip runs inside the move via
    ``config.set_recordings_dir`` (the single commit point, which invalidates the
    daemon's in-process cache). Deliberately NOT in ``_ACTIVITY_PATHS``.
    """
    from pathlib import Path

    from screencap import config, storage_migration

    schema_version = schema._STORAGE_MIGRATE_API_VERSION
    try:
        body = await request.json()
        parsed = schema.StorageMigrateRequest.model_validate(body)
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception:
        return _validation_error_response(schema_version=schema_version)

    supervisor = request.app.state.supervisor
    try:
        await supervisor.acquire_migration(schema_version=schema_version)
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)

    try:
        if await asyncio.to_thread(_storage_recording_active):
            raise errors.StorageMigrationError(
                "recording_active",
                "Stop the current recording before moving the storage "
                "location.",
                schema_version=schema_version,
            )
        if await asyncio.to_thread(_terminal_stage_active):
            raise errors.StorageMigrationError(
                "recording_active",
                "A recording is still finishing processing. Try again once "
                "it completes.",
                schema_version=schema_version,
            )

        source = config.get_recordings_dir()
        target = Path(parsed.target).expanduser()

        result = storage_migration.validate_target(source, target)
        if not result.ok:
            raise errors.StorageMigrationError(
                result.code or "invalid_target",
                result.message or "The chosen folder can't be used.",
                schema_version=schema_version,
            )

        outcome = await asyncio.to_thread(
            storage_migration.migrate,
            source,
            target,
            config.set_recordings_dir,
        )
        if not outcome.ok:
            # A recoverable pre-commit failure (e.g. the target filled between
            # validation and the move) — surface a typed reason, not a 500.
            raise errors.StorageMigrationError(
                outcome.code or "invalid_target",
                outcome.message or "The chosen folder can't be used.",
                schema_version=schema_version,
            )
        return JSONResponse(
            schema.envelope(
                schema_version=schema_version,
                moved_from=outcome.moved_from,
                moved_to=outcome.moved_to,
            )
        )
    except errors.DaemonAPIError as exc:
        return _api_error_response(exc)
    except Exception as exc:
        return _internal_error_response(
            exc, schema_version=schema_version, request=request
        )
    finally:
        supervisor.release_migration()


def build_app() -> Starlette:
    app = Starlette(
        routes=[
            Route("/v0/daemon.info", daemon_info, methods=["GET"]),
            Route("/v0/recording.list", recording_list, methods=["GET"]),
            Route("/v0/auth.whoami", auth_whoami, methods=["GET"]),
            Route("/v0/entitlement.refresh", entitlement_refresh, methods=["POST"]),
            Route("/v0/session.snapshot", session_snapshot, methods=["GET"]),
            Route("/v0/events", events_stream, methods=["GET"]),
            Route("/v0/recording.start", recording_start, methods=["POST"]),
            Route("/v0/recording.stop", recording_stop, methods=["POST"]),
            Route("/v0/recording.mute", recording_mute, methods=["POST"]),
            Route("/v0/recording.pause", recording_pause, methods=["POST"]),
            Route("/v0/recording.resume", recording_resume, methods=["POST"]),
            Route("/v0/recording.rename", recording_rename, methods=["POST"]),
            Route("/v0/permission.request", permission_request, methods=["POST"]),
            Route("/v0/permission.cleanup_decoys", permission_cleanup_decoys, methods=["POST"]),
            Route("/v0/content.search", content_search, methods=["POST"]),
            Route("/v0/transcript.search", transcript_search, methods=["POST"]),
            Route("/v0/timeline.query", timeline_query, methods=["POST"]),
            Route("/v0/timeline.day", timeline_day, methods=["POST"]),
            Route("/v0/frame.nearest", frame_nearest, methods=["POST"]),
            Route("/v0/frame.read", frame_read, methods=["POST"]),
            Route("/v0/tasks.list", tasks_list, methods=["POST"]),
            Route("/v0/tasks.create", tasks_create, methods=["POST"]),
            Route("/v0/tasks.update", tasks_update, methods=["POST"]),
            Route("/v0/tasks.delete", tasks_delete, methods=["POST"]),
            Route("/v0/tasks.merge", tasks_merge, methods=["POST"]),
            Route("/v0/tasks.split", tasks_split, methods=["POST"]),
            Route("/v0/ambient.status", ambient_status, methods=["GET"]),
            Route("/v0/ambient.set", ambient_set, methods=["POST"]),
            Route("/v0/chat.answer", chat_answer, methods=["POST"]),
            Route("/v0/apps.list", apps_list, methods=["GET"]),
            Route("/v0/backfill.start", backfill_start, methods=["POST"]),
            Route("/v0/backfill.status", backfill_status, methods=["GET"]),
            Route("/v0/backfill.cancel", backfill_cancel, methods=["POST"]),
            Route("/v0/storage.migrate", storage_migrate, methods=["POST"]),
            Route("/v0/model.download.start", model_download_start, methods=["POST"]),
            Route("/v0/model.download.status", model_download_status, methods=["GET"]),
            Route("/v0/model.download.cancel", model_download_cancel, methods=["POST"]),
            Route("/v0/model.status", model_status, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    # The bus is app-scoped, not module-global, so tests and embedded daemon
    # instances do not share cursors or subscribers.
    app.state.event_bus = EventBus()
    return app
