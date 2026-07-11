"""Mute state on the events stream + session snapshot (SCR-218 U5).

The engine emits audio_muted/audio_unmuted only after the mic stream actually
toggles; the supervisor's stderr pump is the SOLE writer of session mute state,
which the daemon-owned snapshot overlay then surfaces. These tests cover the
observe-event writer in isolation and the full loop end-to-end (verb -> engine
stdin -> confirmed event -> pump -> session state -> snapshot).
"""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from screencap import _stderr_events
from screencap.daemon.event_bus import EventBus
from screencap.daemon.supervisor import Supervisor

from .conftest import short_socket_path, wait_for_socket


# --- unit: the pump is the sole writer of confirmed mute state ---------------


def test_observe_event_writes_confirmed_mute_state() -> None:
    sup = Supervisor(EventBus(), reconcile_on_init=False)
    sup._session_state = {"recording_name": "x"}  # type: ignore[assignment]

    sup._observe_event({"type": _stderr_events.EVENT_AUDIO_MUTED})
    assert sup.current_session()["muted"] is True

    sup._observe_event({"type": _stderr_events.EVENT_AUDIO_UNMUTED})
    assert sup.current_session()["muted"] is False


def test_observe_event_mute_is_noop_without_session() -> None:
    sup = Supervisor(EventBus(), reconcile_on_init=False)
    # No active session -> no crash, nothing to write.
    sup._observe_event({"type": _stderr_events.EVENT_AUDIO_MUTED})
    assert sup.current_session() is None


# --- integration: full confirmed-state loop ---------------------------------


@pytest.fixture
def confirming_engine_script(tmp_path: Path) -> Path:
    script = tmp_path / "confirming_engine.py"
    script.write_text(
        textwrap.dedent(
            """
            import base64, json, signal, sys, threading, time
            args = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
            name = args.get("name") or "fake"

            def emit(t, **p):
                sys.stderr.write(json.dumps({"type": t, "schema_version": 1,
                                             "ts": time.time(), **p}) + "\\n")
                sys.stderr.flush()

            def read_stdin():
                for line in sys.stdin:
                    line = line.strip()
                    if not line:
                        continue
                    cmd = json.loads(line)
                    if cmd.get("type") == "set_muted":
                        # Confirmed event fires only after the "real toggle".
                        emit("audio_muted" if cmd["muted"] else "audio_unmuted",
                             muted=cmd["muted"])

            def term(_s, _f):
                emit("recording_finalized", name=name, duration_seconds=0.2,
                     force_stopped=False, disk_full=False)
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, term)
            threading.Thread(target=read_stdin, daemon=True).start()
            emit("started", claimant="daemon")
            while True:
                time.sleep(0.05)
            """
        ),
        encoding="utf-8",
    )
    return script


def _serve(tmp_path: Path, engine_script: Path):
    import os

    socket_path = short_socket_path(tmp_path)
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "HOME": str(tmp_path / "home"),
        "SCREENCAP_RECORDINGS_DIR": str(tmp_path),
        "SCREENCAP_PERMISSION_PROBE_FAKE": "granted",
        "SCREENCAP_LOCAL_PAYWALL_ENFORCE": "0",
        "SCREENCAP_DAEMON_ENGINE_COMMAND": json.dumps(
            [sys.executable, str(engine_script), "{encoded_args}"]
        ),
        "SCREENCAP_DAEMON_POLL_INTERVAL": "0.05",
        "SCREENCAP_DAEMON_STARTUP_TIMEOUT": "2",
        "SCREENCAP_DAEMON_STOP_TIMEOUT": "2",
        "SCREENCAP_DAEMON_RECONCILE_GRACE": "0.2",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "screencap.cli", "--no-update-check", "serve",
         "--socket", str(socket_path)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    wait_for_socket(socket_path, proc=proc)
    return proc, socket_path


def _client(socket_path: Path) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    return httpx.AsyncClient(transport=transport, base_url="http://screencap")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


async def _snapshot_muted(client: httpx.AsyncClient) -> bool:
    snap = (await client.get("/v0/session.snapshot")).json()
    return snap.get("muted", False)


@pytest.mark.asyncio
async def test_confirmed_mute_event_updates_snapshot(
    tmp_path: Path, confirming_engine_script: Path
) -> None:
    proc, socket_path = _serve(tmp_path, confirming_engine_script)
    try:
        async with _client(socket_path) as client:
            await client.post(
                "/v0/recording.start",
                json={"name": "demo", "output_dir": str(tmp_path / "demo")},
            )
            assert await _snapshot_muted(client) is False  # unmuted at start

            await client.post("/v0/recording.mute", json={"muted": True})
            # Snapshot flips only after the engine's confirmed audio_muted event
            # is pumped into session state.
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and not await _snapshot_muted(client):
                await asyncio.sleep(0.05)
            assert await _snapshot_muted(client) is True

            await client.post("/v0/recording.mute", json={"muted": False})
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and await _snapshot_muted(client):
                await asyncio.sleep(0.05)
            assert await _snapshot_muted(client) is False
    finally:
        _stop(proc)


@pytest.mark.asyncio
async def test_audio_muted_event_reaches_bus_subscriber(
    tmp_path: Path, confirming_engine_script: Path
) -> None:
    proc, socket_path = _serve(tmp_path, confirming_engine_script)
    try:
        async with _client(socket_path) as stream_client, _client(socket_path) as client:
            async with stream_client.stream("GET", "/v0/events") as response:
                lines = response.aiter_lines()
                assert json.loads(await asyncio.wait_for(anext(lines), timeout=4))[
                    "type"
                ] == "subscribed"
                await client.post(
                    "/v0/recording.start",
                    json={"name": "demo", "output_dir": str(tmp_path / "demo")},
                )
                await client.post("/v0/recording.mute", json={"muted": True})

                deadline = time.monotonic() + 4.0
                seen = None
                while time.monotonic() < deadline:
                    line = await asyncio.wait_for(anext(lines), timeout=4)
                    evt = json.loads(line)
                    if evt.get("type") == "audio_muted":
                        seen = evt
                        break
                assert seen is not None and seen["muted"] is True
    finally:
        _stop(proc)
