"""Idle-shutdown watchdog for CLI-auto-spawned daemons.

Phase 2 U1 introduces an F3 auto-spawn fallback: when the CLI is invoked
without a LaunchAgent installed (e.g., headless install via Homebrew),
``screencap`` spawns ``screencap serve --idle-shutdown=<secs>``
in the background. Without an upper bound on its lifetime, those
ephemeral daemons would accumulate after every cron-driven
``screencap status`` invocation.

This module supplies the watchdog. LaunchAgent-managed daemons keep
``run all day`` semantics; the watchdog is only attached when ``serve``
is invoked with ``--idle-shutdown=<secs>`` (auto-spawn passes 600).

Activity sources counted against the idle window:
- Mutating HTTP requests: ``recording.start`` and ``recording.stop``
  (middleware bumps the timestamp on the request START).
  Read-only poll routes like ``session.snapshot`` are excluded so
  that cron-driven ``screencap status`` calls do not keep the daemon
  alive permanently.
- ``event_bus.subscriber_count() > 0`` — a held subscription is a
  live conversation even between request boundaries.
- ``supervisor.has_active_session()`` — an in-flight recording must
  never be torn down by the watchdog.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Poll cadence for the watchdog. Five seconds keeps the worst-case
# extra-uptime bounded without producing measurable CPU load.
_POLL_INTERVAL_S = 5.0


# Paths that count as "activity" for idle-shutdown purposes.
# Read-only polling routes (``session.snapshot``, ``recording.list``,
# ``daemon.info``) do NOT count — a cron-driven ``screencap status``
# should not prevent the auto-spawned daemon from shutting down.
# The events subscription stream keeps the daemon busy via the
# ``subscriber_count() > 0`` check in ``_daemon_is_busy``, so it does
# not need to be listed here.
_ACTIVITY_PATHS = frozenset(
    {
        "/v0/recording.start",
        "/v0/recording.stop",
    }
)


class _ActivityMiddleware(BaseHTTPMiddleware):
    """Bump ``app.state.idle_last_activity`` on mutating requests only.

    Read-only polling routes such as ``GET /v0/session.snapshot`` are
    explicitly excluded so that cron-driven ``screencap status`` calls do
    not keep an auto-spawned daemon alive indefinitely.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in _ACTIVITY_PATHS:
            request.app.state.idle_last_activity = time.monotonic()
        return await call_next(request)


def attach(app: Starlette, idle_seconds: float) -> None:
    """Attach idle-tracking middleware to the daemon app.

    Call once during ``serve`` setup. The watchdog task itself is
    started by :func:`run_watchdog` from within the running event loop
    so it inherits the loop's cancellation semantics.
    """
    app.state.idle_last_activity = time.monotonic()
    app.state.idle_shutdown_seconds = float(idle_seconds)
    app.add_middleware(_ActivityMiddleware)


async def run_watchdog(
    app: Starlette,
    *,
    request_shutdown: Callable[[], None],
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> None:
    """Run the idle-shutdown loop until cancelled or the daemon exits.

    ``request_shutdown`` is the cooperative shutdown hook (the same one
    SIGTERM / SIGINT call through). The watchdog never kills the
    process directly; it asks the server to drain and exit.
    """
    idle_seconds = float(getattr(app.state, "idle_shutdown_seconds", 0.0) or 0.0)
    if idle_seconds <= 0.0:
        return

    while True:
        try:
            await asyncio.sleep(poll_interval_s)
        except asyncio.CancelledError:
            return

        now = time.monotonic()
        last = float(getattr(app.state, "idle_last_activity", now))
        elapsed = now - last
        if elapsed < idle_seconds:
            continue

        if _daemon_is_busy(app):
            # Holding a subscription or running a recording defers the
            # idle deadline without restarting it; once activity quiesces
            # the next poll re-checks the gap from ``idle_last_activity``.
            continue

        logger.info(
            "idle-shutdown firing after %.1fs without activity (limit=%.1fs)",
            elapsed,
            idle_seconds,
        )
        request_shutdown()
        return


def _daemon_is_busy(app: Starlette) -> bool:
    bus = getattr(app.state, "event_bus", None)
    if bus is not None and bus.subscriber_count() > 0:
        return True
    supervisor = getattr(app.state, "supervisor", None)
    if supervisor is not None and _has_active_session(supervisor):
        return True
    # SCR-125 U6: a crash/restart terminal-stage resume is in flight. After the
    # engine exits, ``current_session()`` is None, so without this the
    # auto-spawned daemon could idle-exit mid-upload.
    if supervisor is not None and _has_inflight_resume(supervisor):
        return True
    # SCR-228: a storage-location migration holds the daemon. /v0/storage.migrate
    # is deliberately NOT in _ACTIVITY_PATHS, and its handler yields the loop
    # across an asyncio.to_thread move, so this busy check is what stops an
    # auto-spawned daemon idle-exiting mid-migration.
    if supervisor is not None and _is_migrating(supervisor):
        return True
    # SCR-258 U9 (KTD-15): an in-flight ``storage.lock`` OPERATION pins the
    # watchdog so an auto-spawned daemon cannot idle-exit mid stop→quiesce→detach.
    # The sealed STEADY state is deliberately NOT pinned here (a locked daemon
    # idle-exits normally; the next start re-enters sealed serving).
    if supervisor is not None and _is_lock_in_flight(supervisor):
        return True
    # SCR-178 U5: a content-index backfill run is in flight. ``backfill.*`` is
    # deliberately NOT in ``_ACTIVITY_PATHS`` (status-polling must not reset the
    # idle timer), so the only thing keeping the daemon alive across a long
    # OCR run is this busy predicate.
    job = getattr(app.state, "backfill_job", None)
    if job is not None and _backfill_running(job):
        return True
    # SCR-239: a model download is in flight. Like ``backfill.*``, the
    # ``model.download.*`` verbs are NOT in ``_ACTIVITY_PATHS``, so this busy
    # predicate is what keeps the auto-spawned daemon alive across a ~2 GB fetch.
    dl_job = getattr(app.state, "model_download_job", None)
    if dl_job is not None and _backfill_running(dl_job):  # same is_running() shape
        return True
    # SCR-258 U6: an upgrade migration is in flight. ``storage.encrypt.*`` is NOT
    # in ``_ACTIVITY_PATHS`` (status-polling must not reset the idle timer), so
    # this busy predicate is what keeps the auto-spawned daemon alive across a
    # long copy/verify run.
    encrypt_job = getattr(app.state, "encrypt_job", None)
    if encrypt_job is not None and _backfill_running(encrypt_job):  # same shape
        return True
    return False


def _backfill_running(job: object) -> bool:
    getter = getattr(job, "is_running", None)
    if getter is None:
        return False
    try:
        return bool(getter())
    except Exception:  # noqa: BLE001 — watchdog stays robust against test doubles
        return False


def _has_inflight_resume(supervisor: object) -> bool:
    getter = getattr(supervisor, "has_inflight_resume", None)
    if getter is None:
        return False
    try:
        return bool(getter())
    except Exception:  # noqa: BLE001 — watchdog stays robust against test doubles
        return False


def _is_migrating(supervisor: object) -> bool:
    getter = getattr(supervisor, "is_migrating", None)
    if getter is None:
        return False
    try:
        return bool(getter())
    except Exception:  # noqa: BLE001 — watchdog stays robust against test doubles
        return False


def _is_lock_in_flight(supervisor: object) -> bool:
    getter = getattr(supervisor, "is_lock_in_flight", None)
    if getter is None:
        return False
    try:
        return bool(getter())
    except Exception:  # noqa: BLE001 — watchdog stays robust against test doubles
        return False


def _has_active_session(supervisor: object) -> bool:
    # ``Supervisor`` exposes ``current_session()``; an in-flight
    # daemon-owned recording returns a non-empty mapping. Catching the
    # broad surface here keeps the watchdog robust against test doubles
    # that don't implement the full interface.
    getter = getattr(supervisor, "current_session", None)
    if getter is None:
        return False
    try:
        session = getter()
    except Exception:  # noqa: BLE001
        return False
    return bool(session)
