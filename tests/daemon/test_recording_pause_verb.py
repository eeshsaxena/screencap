"""HTTP-layer + confirmed-state tests for recording.pause / recording.resume
(SCR-214 U4).

Mirrors the recording.mute verb tests: a real daemon subprocess, a fake engine
spawned via SCREENCAP_DAEMON_ENGINE_COMMAND that reads its stdin control channel
and emits the CONFIRMED recording_paused / recording_resumed event on each
set_paused command, and an httpx client over the AF_UNIX socket. So it exercises
the whole chain end-to-end — verb -> supervisor.set_paused -> engine stdin ->
confirmed event -> pump -> session state -> snapshot.

Pause is distinct from mute: it stops the WHOLE capture surface. The daemon layer
tested here is transport + confirmed-state bookkeeping; the capture gap itself
(no frames, no audio) is covered by tests/test_recording_pause.py (AE1).
"""

from __future__ import annotations

import asyncio
import json
import os
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

# --- unit: the pump is the sole writer of confirmed pause state --------------


def test_observe_event_writes_confirmed_pause_state() -> None:
    sup = Supervisor(EventBus(), reconcile_on_init=False)
    sup._session_state = {"recording_name": "x"}  # type: ignore[assignment]

    sup._observe_event({"type": _stderr_events.EVENT_RECORDING_PAUSED})
    assert sup.current_session()["paused"] is True

    sup._observe_event({"type": _stderr_events.EVENT_RECORDING_RESUMED})
    assert sup.current_session()["paused"] is False


def test_observe_event_pause_is_noop_without_session() -> None:
    sup = Supervisor(EventBus(), reconcile_on_init=False)
    sup._observe_event({"type": _stderr_events.EVENT_RECORDING_PAUSED})
    assert sup.current_session() is None


# --- fake engine that confirms pause ----------------------------------------


@pytest.fixture
def pause_confirming_engine_script(tmp_path: Path) -> Path:
    """A fake engine that, on each set_paused command, emits the CONFIRMED
    recording_paused / recording_resumed event (so the U4 forward is observable
    end-to-end) and, if SCREENCAP_PAUSE_LOG is set, appends the received command
    so a test can assert exactly what the daemon forwarded. Also confirms
    set_muted (harmless) so it can double as a regression engine."""
    script = tmp_path / "pause_confirming_engine.py"
    script.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import base64
            import json
            import os
            import signal
            import sys
            import threading
            import time

            args = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
            name = args.get("name") or "fake"
            log_path = os.environ.get("SCREENCAP_PAUSE_LOG")


            def emit(event_type, **payload):
                sys.stderr.write(
                    json.dumps({"type": event_type, "schema_version": 1,
                                "ts": time.time(), **payload}) + "\\n")
                sys.stderr.flush()


            def read_stdin():
                for line in sys.stdin:
                    line = line.strip()
                    if not line:
                        continue
                    cmd = json.loads(line)
                    if cmd.get("type") == "set_paused":
                        if log_path:
                            with open(log_path, "a") as fh:
                                fh.write(json.dumps(cmd) + "\\n")
                        emit("recording_paused" if cmd["paused"]
                             else "recording_resumed", paused=cmd["paused"])
                    elif cmd.get("type") == "set_muted":
                        emit("audio_muted" if cmd["muted"] else "audio_unmuted",
                             muted=cmd["muted"])


            def handle_term(_s, _f):
                emit("recording_finalized", name=name, duration_seconds=0.2,
                     force_stopped=False, disk_full=False)
                raise SystemExit(0)


            signal.signal(signal.SIGTERM, handle_term)
            threading.Thread(target=read_stdin, daemon=True).start()
            emit("started", claimant="daemon")
            while True:
                time.sleep(0.05)
            """
        ),
        encoding="utf-8",
    )
    return script


def _serve(tmp_path: Path, engine_script: Path, pause_log: Path | None = None):
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
    if pause_log is not None:
        env["SCREENCAP_PAUSE_LOG"] = str(pause_log)
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


async def _snapshot_paused(client: httpx.AsyncClient) -> bool:
    snap = (await client.get("/v0/session.snapshot")).json()
    return snap.get("paused", False)


# --- integration ------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_forwards_to_engine_and_echoes(
    tmp_path: Path, pause_confirming_engine_script: Path
) -> None:
    pause_log = tmp_path / "pause_commands.log"
    proc, socket_path = _serve(tmp_path, pause_confirming_engine_script, pause_log)
    try:
        async with _client(socket_path) as client:
            start = await client.post(
                "/v0/recording.start",
                json={"name": "demo", "output_dir": str(tmp_path / "demo")},
            )
            assert start.status_code == 200

            resp = await client.post("/v0/recording.pause", json={"paused": True})
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert body["paused"] is True  # echoes REQUESTED state
            assert isinstance(body["cursor"], int)

            # recording.resume always resumes regardless of the body's `paused`.
            resp2 = await client.post("/v0/recording.resume", json={"paused": True})
            assert resp2.status_code == 200
            assert resp2.json()["paused"] is False

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not pause_log.exists():
            await asyncio.sleep(0.05)
        assert pause_log.exists(), "engine never received the set_paused command"
        logged = [json.loads(line) for line in pause_log.read_text().splitlines()]
        assert {"type": "set_paused", "paused": True} in logged
        assert {"type": "set_paused", "paused": False} in logged
    finally:
        _stop(proc)


@pytest.mark.asyncio
async def test_confirmed_pause_event_updates_snapshot(
    tmp_path: Path, pause_confirming_engine_script: Path
) -> None:
    # The snapshot's `paused` is driven by the engine's confirmed event, NOT the
    # verb's echo — the daemon never sets pause state on the request itself.
    proc, socket_path = _serve(tmp_path, pause_confirming_engine_script)
    try:
        async with _client(socket_path) as client:
            await client.post(
                "/v0/recording.start",
                json={"name": "demo", "output_dir": str(tmp_path / "demo")},
            )
            assert await _snapshot_paused(client) is False  # running at start

            await client.post("/v0/recording.pause", json={"paused": True})
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and not await _snapshot_paused(client):
                await asyncio.sleep(0.05)
            assert await _snapshot_paused(client) is True

            await client.post("/v0/recording.resume", json={"paused": False})
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and await _snapshot_paused(client):
                await asyncio.sleep(0.05)
            assert await _snapshot_paused(client) is False
    finally:
        _stop(proc)


@pytest.mark.asyncio
async def test_pause_when_not_recording_returns_not_recording(
    tmp_path: Path, pause_confirming_engine_script: Path
) -> None:
    proc, socket_path = _serve(tmp_path, pause_confirming_engine_script)
    try:
        async with _client(socket_path) as client:
            resp = await client.post("/v0/recording.pause", json={"paused": True})
            assert resp.status_code == 409
            assert resp.json()["error"] == "not_recording"
            # resume when not recording is likewise a typed 409.
            resp2 = await client.post("/v0/recording.resume", json={"paused": False})
            assert resp2.status_code == 409
    finally:
        _stop(proc)


@pytest.mark.asyncio
async def test_pause_rejects_malformed_body(
    tmp_path: Path, pause_confirming_engine_script: Path
) -> None:
    proc, socket_path = _serve(tmp_path, pause_confirming_engine_script)
    try:
        async with _client(socket_path) as client:
            await client.post(
                "/v0/recording.start",
                json={"name": "demo", "output_dir": str(tmp_path / "demo")},
            )
            # Missing required `paused` field -> validation error, not a crash.
            resp = await client.post("/v0/recording.pause", json={})
            assert resp.status_code >= 400
            assert resp.json()["ok"] is False
    finally:
        _stop(proc)


@pytest.mark.asyncio
async def test_start_stop_without_pause_is_unchanged(
    tmp_path: Path, pause_confirming_engine_script: Path
) -> None:
    """Covers R16. The explicit (non-ambient, never-paused) start/stop flow over
    the shared capture path is unaffected by the pause edits: a plain start ->
    stop cycle still succeeds and the snapshot never reports paused."""
    proc, socket_path = _serve(tmp_path, pause_confirming_engine_script)
    try:
        async with _client(socket_path) as client:
            start = await client.post(
                "/v0/recording.start",
                json={"name": "demo", "output_dir": str(tmp_path / "demo")},
            )
            assert start.status_code == 200
            assert await _snapshot_paused(client) is False  # never paused

            stop = await client.post("/v0/recording.stop", json={})
            assert stop.status_code == 200
            assert stop.json()["ok"] is True
    finally:
        _stop(proc)
