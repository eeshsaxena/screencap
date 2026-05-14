"""Unit tests for the daemon idle-shutdown watchdog."""

from __future__ import annotations

import asyncio
import time

import pytest

from screencap.daemon._idle_shutdown import _daemon_is_busy, run_watchdog


class _FakeBus:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    def subscriber_count(self) -> int:
        return self._count


class _FakeSupervisor:
    def __init__(self, session=None) -> None:
        self._session = session

    def current_session(self):
        return self._session


class _AppShim:
    """Mimics ``app.state.*`` attribute access without dragging Starlette."""

    def __init__(self) -> None:
        self.state = type("S", (), {})()


@pytest.mark.asyncio
async def test_watchdog_fires_when_idle_with_no_recording_no_subscribers():
    app = _AppShim()
    app.state.idle_shutdown_seconds = 0.05
    app.state.idle_last_activity = time.monotonic() - 1.0  # already idle
    app.state.event_bus = _FakeBus(count=0)
    app.state.supervisor = _FakeSupervisor(session=None)

    fired = asyncio.Event()

    def request_shutdown() -> None:
        fired.set()

    await asyncio.wait_for(
        run_watchdog(app, request_shutdown=request_shutdown, poll_interval_s=0.01),
        timeout=1.0,
    )
    assert fired.is_set()


@pytest.mark.asyncio
async def test_watchdog_defers_while_subscribers_present():
    app = _AppShim()
    app.state.idle_shutdown_seconds = 0.05
    app.state.idle_last_activity = time.monotonic() - 1.0
    app.state.event_bus = _FakeBus(count=1)
    app.state.supervisor = _FakeSupervisor(session=None)

    fired = asyncio.Event()

    task = asyncio.create_task(
        run_watchdog(
            app,
            request_shutdown=lambda: fired.set(),
            poll_interval_s=0.01,
        )
    )
    await asyncio.sleep(0.1)
    # Should NOT have fired — subscriber count is 1.
    assert not fired.is_set()
    task.cancel()
    # ``run_watchdog`` traps CancelledError on the sleep and returns
    # cleanly, so awaiting the task surfaces no exception.
    await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_watchdog_defers_during_active_recording():
    app = _AppShim()
    app.state.idle_shutdown_seconds = 0.05
    app.state.idle_last_activity = time.monotonic() - 1.0
    app.state.event_bus = _FakeBus(count=0)
    app.state.supervisor = _FakeSupervisor(session={"session_id": "x"})

    fired = asyncio.Event()
    task = asyncio.create_task(
        run_watchdog(
            app,
            request_shutdown=lambda: fired.set(),
            poll_interval_s=0.01,
        )
    )
    await asyncio.sleep(0.1)
    assert not fired.is_set()
    task.cancel()
    # ``run_watchdog`` traps CancelledError on the sleep and returns
    # cleanly, so awaiting the task surfaces no exception.
    await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_watchdog_does_nothing_when_idle_seconds_zero():
    app = _AppShim()
    app.state.idle_shutdown_seconds = 0.0
    app.state.idle_last_activity = time.monotonic() - 100.0

    fired = asyncio.Event()
    await asyncio.wait_for(
        run_watchdog(
            app,
            request_shutdown=lambda: fired.set(),
            poll_interval_s=0.01,
        ),
        timeout=0.2,
    )
    assert not fired.is_set()


def test_daemon_is_busy_with_subscriber():
    app = _AppShim()
    app.state.event_bus = _FakeBus(count=2)
    app.state.supervisor = _FakeSupervisor(session=None)
    assert _daemon_is_busy(app) is True


def test_daemon_is_busy_with_active_recording():
    app = _AppShim()
    app.state.event_bus = _FakeBus(count=0)
    app.state.supervisor = _FakeSupervisor(session={"session_id": "x"})
    assert _daemon_is_busy(app) is True


def test_daemon_is_idle_when_nothing_active():
    app = _AppShim()
    app.state.event_bus = _FakeBus(count=0)
    app.state.supervisor = _FakeSupervisor(session=None)
    assert _daemon_is_busy(app) is False
