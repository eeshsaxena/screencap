"""Tests for Supervisor.send_command — the daemon-side writer of the
engine stdin control channel (SCR-218 U1).

send_command writes one NDJSON command line to the running engine's stdin. It
must be tolerant of a dead engine and a broken pipe (return False, never raise),
so a mute command that races a stop can never crash the daemon.
"""

from __future__ import annotations

import json

import pytest

from screencap.daemon.event_bus import EventBus
from screencap.daemon.supervisor import Supervisor


class _FakeProc:
    def __init__(self, *, alive: bool = True, raise_on_write: Exception | None = None) -> None:
        self._alive = alive
        self._raise = raise_on_write
        self.lines: list[str] = []

    @property
    def pid(self) -> int:
        return 4321

    def is_alive(self) -> bool:
        return self._alive

    def send_line(self, line: str) -> None:
        if self._raise is not None:
            raise self._raise
        self.lines.append(line)


def _supervisor() -> Supervisor:
    return Supervisor(EventBus(), reconcile_on_init=False)


@pytest.mark.asyncio
async def test_send_command_writes_json_line_to_live_engine() -> None:
    sup = _supervisor()
    proc = _FakeProc(alive=True)
    sup._proc = proc  # type: ignore[assignment]

    ok = await sup.send_command({"type": "set_muted", "muted": True})

    assert ok is True
    assert len(proc.lines) == 1
    assert proc.lines[0].endswith("\n")
    assert json.loads(proc.lines[0]) == {"type": "set_muted", "muted": True}


@pytest.mark.asyncio
async def test_send_command_returns_false_when_no_engine() -> None:
    sup = _supervisor()
    # No _proc set (no live recording).
    ok = await sup.send_command({"type": "set_muted", "muted": True})
    assert ok is False


@pytest.mark.asyncio
async def test_send_command_returns_false_when_engine_dead() -> None:
    sup = _supervisor()
    sup._proc = _FakeProc(alive=False)  # type: ignore[assignment]

    ok = await sup.send_command({"type": "set_muted", "muted": False})
    assert ok is False


@pytest.mark.asyncio
async def test_send_command_returns_false_on_broken_pipe() -> None:
    sup = _supervisor()
    proc = _FakeProc(alive=True, raise_on_write=BrokenPipeError())
    sup._proc = proc  # type: ignore[assignment]

    # A mute racing engine teardown must degrade to False, never raise.
    ok = await sup.send_command({"type": "set_muted", "muted": True})
    assert ok is False


# --- SCR-214 U4: set_paused pass-through (mirrors set_muted) ------------------


@pytest.mark.asyncio
async def test_set_paused_forwards_command_to_live_engine() -> None:
    sup = _supervisor()
    proc = _FakeProc(alive=True)
    sup._proc = proc  # type: ignore[assignment]

    assert await sup.set_paused(True) is True
    assert json.loads(proc.lines[0]) == {"type": "set_paused", "paused": True}
    assert await sup.set_paused(False) is True
    assert json.loads(proc.lines[1]) == {"type": "set_paused", "paused": False}


@pytest.mark.asyncio
async def test_set_paused_returns_false_when_no_engine() -> None:
    sup = _supervisor()
    # No live recording -> a pause that races a stop degrades to False, no raise.
    assert await sup.set_paused(True) is False
