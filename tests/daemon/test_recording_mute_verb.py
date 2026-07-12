"""HTTP-layer tests for the recording.mute daemon verb (SCR-218 U4).

These drive the full path: a real daemon subprocess, a fake engine spawned via
SCREENCAP_DAEMON_ENGINE_COMMAND that reads its stdin control channel, and an
httpx client over the AF_UNIX socket. So they also end-to-end exercise U1 —
Supervisor.send_command writing to the engine's stdin PIPE.
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

from .conftest import short_socket_path, wait_for_socket


@pytest.fixture
def stdin_aware_engine_script(tmp_path: Path) -> Path:
    """A fake engine that reads its stdin control channel and records every
    set_muted command it receives to a log file, then emits the confirmed
    audio_muted/audio_unmuted event (so the U1->U4 forward is observable)."""
    script = tmp_path / "stdin_engine.py"
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
            log_path = os.environ["SCREENCAP_MUTE_LOG"]


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
                    if cmd.get("type") == "set_muted":
                        with open(log_path, "a") as fh:
                            fh.write(json.dumps(cmd) + "\\n")
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


def _daemon_env(tmp_path: Path, engine_script: Path, mute_log: Path) -> dict[str, str]:
    import os

    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "HOME": str(tmp_path / "home"),
        "SCREENCAP_RECORDINGS_DIR": str(tmp_path),
        "SCREENCAP_PERMISSION_PROBE_FAKE": "granted",
        "SCREENCAP_LOCAL_PAYWALL_ENFORCE": "0",
        "SCREENCAP_MUTE_LOG": str(mute_log),
        "SCREENCAP_DAEMON_ENGINE_COMMAND": json.dumps(
            [sys.executable, str(engine_script), "{encoded_args}"]
        ),
        "SCREENCAP_DAEMON_POLL_INTERVAL": "0.05",
        "SCREENCAP_DAEMON_STARTUP_TIMEOUT": "2",
        "SCREENCAP_DAEMON_STOP_TIMEOUT": "2",
        "SCREENCAP_DAEMON_RECONCILE_GRACE": "0.2",
    }


def _serve(tmp_path: Path, engine_script: Path, mute_log: Path):
    socket_path = short_socket_path(tmp_path)
    env = _daemon_env(tmp_path, engine_script, mute_log)
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


@pytest.mark.asyncio
async def test_recording_mute_forwards_to_engine_and_echoes(
    tmp_path: Path, stdin_aware_engine_script: Path
) -> None:
    mute_log = tmp_path / "mute_commands.log"
    proc, socket_path = _serve(tmp_path, stdin_aware_engine_script, mute_log)
    try:
        async with _client(socket_path) as client:
            start = await client.post(
                "/v0/recording.start",
                json={"name": "demo", "output_dir": str(tmp_path / "demo")},
            )
            assert start.status_code == 200

            resp = await client.post("/v0/recording.mute", json={"muted": True})
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert body["muted"] is True
            assert isinstance(body["cursor"], int)

        # The engine received the forwarded command over its stdin channel.
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not mute_log.exists():
            await asyncio.sleep(0.05)
        assert mute_log.exists(), "engine never received the set_muted command"
        logged = [json.loads(line) for line in mute_log.read_text().splitlines()]
        assert {"type": "set_muted", "muted": True} in logged
    finally:
        _stop(proc)


@pytest.mark.asyncio
async def test_recording_mute_when_not_recording_returns_not_recording(
    tmp_path: Path, stdin_aware_engine_script: Path
) -> None:
    mute_log = tmp_path / "mute_commands.log"
    proc, socket_path = _serve(tmp_path, stdin_aware_engine_script, mute_log)
    try:
        async with _client(socket_path) as client:
            resp = await client.post("/v0/recording.mute", json={"muted": True})
            assert resp.status_code == 409
            assert resp.json()["error"] == "not_recording"
    finally:
        _stop(proc)


@pytest.mark.asyncio
async def test_recording_mute_rejects_malformed_body(
    tmp_path: Path, stdin_aware_engine_script: Path
) -> None:
    mute_log = tmp_path / "mute_commands.log"
    proc, socket_path = _serve(tmp_path, stdin_aware_engine_script, mute_log)
    try:
        async with _client(socket_path) as client:
            await client.post(
                "/v0/recording.start",
                json={"name": "demo", "output_dir": str(tmp_path / "demo")},
            )
            # Missing required `muted` field -> validation error, not a crash.
            resp = await client.post("/v0/recording.mute", json={})
            assert resp.status_code >= 400
            assert resp.json()["ok"] is False
    finally:
        _stop(proc)
