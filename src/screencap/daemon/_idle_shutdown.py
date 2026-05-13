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
- Any HTTP request handled by the app (middleware bumps the
  timestamp on the request START so long-poll subscriptions count).
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


class _ActivityMiddleware(BaseHTTPMiddleware):
    """Bump ``app.state.idle_last_activity`` on every request entry."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
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
