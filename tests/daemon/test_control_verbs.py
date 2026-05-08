"""HTTP-layer tests for daemon recording control verbs over AF_UNIX sockets."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from contextlib import contextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator

import httpx
import pytest

from screencap import _stderr_events
from screencap.daemon import errors, schema

from .conftest import short_socket_path, wait_for_socket


@pytest.fixture
def fake_engine_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_engine.py"
    script.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import base64
            import json
            import os
            import signal
            import sys
            import time

            args = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
            name = args.get("name") or "fake"


            def emit(event_type: str, **payload) -> None:
                sys.stderr.write(
                    json.dumps(
                        {
                            "type": event_type,
                            "schema_version": 1,
                            "ts": time.time(),
                            **payload,
                        }
                    )
                    + "\\n"
                )
                sys.stderr.flush()


            def handle_term(_signum, _frame) -> None:
                emit(
                    "recording_finalized",
                    name=name,
                    duration_seconds=0.2,
                    force_stopped=False,
                    disk_full=False,
                )
                raise SystemExit(0)


            signal.signal(signal.SIGTERM, handle_term)
            emit("started", claimant="daemon")
            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    return script


@pytest.fixture
def cli_lock_holder_script(tmp_path: Path) -> Path:
    script = tmp_path / "cli_lock_holder.py"
    script.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import json
            import signal
            import sys
            import time
            from pathlib import Path

            from screencap import pidfile

            capture = Path(sys.argv[1])
            pidfile.claim_lock(capture, claimant="cli")
            meta = pidfile.read_lock_metadata()
            sys.stdout.write(json.dumps(meta) + "\\n")
            sys.stdout.flush()

            def raise_system_exit():
                raise SystemExit(0)


            signal.signal(signal.SIGTERM, lambda _s, _f: raise_system_exit())
            while True:
                time.sleep(0.5)
            """
        ),
        encoding="utf-8",
    )
    return script


def _daemon_env(tmp_path: Path, fake_engine_script: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "HOME": str(tmp_path / "home"),
        "SCREENCAP_RECORDINGS_DIR": str(tmp_path / "recordings"),
        "SCREENCAP_DAEMON_ENGINE_COMMAND": json.dumps(
            [sys.executable, str(fake_engine_script), "{encoded_args}"]
        ),
        "SCREENCAP_DAEMON_POLL_INTERVAL": "0.05",
        "SCREENCAP_DAEMON_STARTUP_TIMEOUT": "2",
        "SCREENCAP_DAEMON_STOP_TIMEOUT": "2",
        "SCREENCAP_DAEMON_RECONCILE_GRACE": "0.2",
    }


@contextmanager
def _serve(
    tmp_path: Path,
    fake_engine_script: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> Iterator[tuple[subprocess.Popen[bytes], Path, dict[str, str]]]:
    socket_path = short_socket_path(tmp_path)
    env = _daemon_env(tmp_path, fake_engine_script)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "screencap.cli",
            "--no-update-check",
            "serve",
            "--socket",
            str(socket_path),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for_socket(socket_path, proc=proc)
    try:
        yield proc, socket_path, env
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _client(socket_path: Path) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    return httpx.AsyncClient(transport=transport, base_url="http://screencap")


async def _read_line(lines: AsyncIterator[str], *, timeout: float = 4.0) -> dict:
    line = await asyncio.wait_for(anext(lines), timeout=timeout)
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line.strip())


async def _read_until_type(
    lines: AsyncIterator[str],
    event_type: str,
    *,
    timeout: float = 4.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"timed out waiting for {event_type}"
        event = await _read_line(lines, timeout=remaining)
        if event.get("type") == event_type:
            return event


def _lock_file(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".screencap" / "run" / "recording.lock"


@contextmanager
def _cli_holder(
    env: dict[str, str],
    script: Path,
    capture_dir: Path,
) -> Iterator[tuple[subprocess.Popen[bytes], dict]]:
    proc = subprocess.Popen(
        [sys.executable, str(script), str(capture_dir)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    meta = json.loads(proc.stdout.readline().decode("utf-8"))
    try:
        yield proc, meta
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)


@pytest.mark.asyncio
async def test_recording_start_happy_path_publishes_started(
    tmp_path: Path,
    fake_engine_script: Path,
) -> None:
    with _serve(tmp_path, fake_engine_script) as (_proc, socket_path, _env):
        async with _client(socket_path) as stream_client, _client(socket_path) as client:
            async with stream_client.stream("GET", "/v0/events") as response:
                lines = response.aiter_lines()
                assert (await _read_line(lines))["type"] == "subscribed"

                result = await client.post(
                    "/v0/recording.start",
                    json={"name": "demo", "output_dir": str(tmp_path / "demo")},
                )

                assert result.status_code == 200
                payload = result.json()
                assert payload["ok"] is True
                assert payload["schema_version"] == schema._RECORDING_START_API_VERSION
                assert payload["session_id"] == "demo"
                assert isinstance(payload["engine_pid"], int)
                event = await _read_until_type(lines, _stderr_events.EVENT_STARTED)
                assert event["claimant"] == "daemon"


@pytest.mark.asyncio
async def test_recording_stop_happy_path_finalizes_and_releases_lock(
    tmp_path: Path,
    fake_engine_script: Path,
) -> None:
    with _serve(tmp_path, fake_engine_script) as (_proc, socket_path, env):
        async with _client(socket_path) as stream_client, _client(socket_path) as client:
            await client.post(
                "/v0/recording.start",
                json={"name": "demo", "output_dir": str(tmp_path / "demo")},
            )
            async with stream_client.stream("GET", "/v0/events") as response:
                lines = response.aiter_lines()
                await _read_line(lines)

                stopped = await client.post("/v0/recording.stop", json={})

                assert stopped.status_code == 200
                assert stopped.json()["final_state"] == "stopped"
                finalized = await _read_until_type(
                    lines, _stderr_events.EVENT_RECORDING_FINALIZED
                )
                assert finalized["force_stopped"] is False
        assert not _lock_file(env).exists() or not json.loads(_lock_file(env).read_text()).get(
            "recording_started_at"
        )


@pytest.mark.asyncio
async def test_recording_start_while_cli_lock_held_returns_lock_contended(
    tmp_path: Path,
    fake_engine_script: Path,
    cli_lock_holder_script: Path,
) -> None:
    env = _daemon_env(tmp_path, fake_engine_script)
    with _cli_holder(env, cli_lock_holder_script, tmp_path / "cli-rec") as (_holder, meta):
        with _serve(tmp_path, fake_engine_script) as (_proc, socket_path, _env):
            async with _client(socket_path) as client:
                result = await client.post("/v0/recording.start", json={"name": "demo"})

    assert result.status_code == 409
    payload = result.json()
    assert payload["ok"] is False
    assert payload["error"] == errors.LOCK_CONTENDED
    assert payload["owner"] == meta


@pytest.mark.asyncio
async def test_recording_stop_cli_lock_without_force_returns_not_owned(
    tmp_path: Path,
    fake_engine_script: Path,
    cli_lock_holder_script: Path,
) -> None:
    env = _daemon_env(tmp_path, fake_engine_script)
    with _cli_holder(env, cli_lock_holder_script, tmp_path / "cli-rec"):
        with _serve(tmp_path, fake_engine_script) as (_proc, socket_path, _env):
            async with _client(socket_path) as client:
                result = await client.post("/v0/recording.stop", json={"force": False})

    assert result.status_code == 409
    payload = result.json()
    assert payload["error"] == errors.NOT_OWNED_BY_DAEMON
    assert payload["claimant"] == "cli"


@pytest.mark.asyncio
async def test_recording_stop_cli_lock_force_requires_cas_fields(
    tmp_path: Path,
    fake_engine_script: Path,
    cli_lock_holder_script: Path,
) -> None:
    env = _daemon_env(tmp_path, fake_engine_script)
    with _cli_holder(env, cli_lock_holder_script, tmp_path / "cli-rec"):
        with _serve(tmp_path, fake_engine_script) as (_proc, socket_path, _env):
            async with _client(socket_path) as client:
                result = await client.post("/v0/recording.stop", json={"force": True})

    payload = result.json()
    assert result.status_code == 409
    assert payload["error"] == errors.NOT_OWNED_BY_DAEMON
    assert "expected_claimant_pid" in payload["hint"]


@pytest.mark.asyncio
async def test_recording_stop_cli_lock_force_mismatch_is_rejected(
    tmp_path: Path,
    fake_engine_script: Path,
    cli_lock_holder_script: Path,
) -> None:
    env = _daemon_env(tmp_path, fake_engine_script)
    with _cli_holder(env, cli_lock_holder_script, tmp_path / "cli-rec"):
        with _serve(tmp_path, fake_engine_script) as (_proc, socket_path, _env):
            async with _client(socket_path) as client:
                result = await client.post(
                    "/v0/recording.stop",
                    json={
                        "force": True,
                        "expected_claimant_pid": 1,
                        "expected_started_at": 2.0,
                    },
                )

    payload = result.json()
    assert result.status_code == 409
    assert payload["error"] == errors.FORCE_MISMATCH
    assert "claimant_pid" not in payload
    assert "started_at" not in payload


@pytest.mark.asyncio
async def test_recording_stop_cli_lock_force_matching_cas_terminates_holder(
    tmp_path: Path,
    fake_engine_script: Path,
    cli_lock_holder_script: Path,
) -> None:
    env = _daemon_env(tmp_path, fake_engine_script)
    with _cli_holder(env, cli_lock_holder_script, tmp_path / "cli-rec") as (holder, meta):
        with _serve(tmp_path, fake_engine_script) as (_proc, socket_path, _env):
            async with _client(socket_path) as client:
                result = await client.post(
                    "/v0/recording.stop",
                    json={
                        "force": True,
                        "expected_claimant_pid": meta["pid"],
                        "expected_started_at": meta["started_at"],
                    },
                )

        holder.wait(timeout=3)

    assert result.status_code == 200
    assert result.json()["stopped"] is True
    assert holder.poll() is not None


@pytest.mark.asyncio
async def test_recording_start_during_reconciliation_returns_503(
    tmp_path: Path,
    fake_engine_script: Path,
) -> None:
    env = _daemon_env(tmp_path, fake_engine_script)
    env["SCREENCAP_DAEMON_RECONCILE_GRACE"] = "1.0"
    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal,time;"
                "signal.signal(signal.SIGTERM, lambda s,f: time.sleep(2));"
                "time.sleep(60)"
            ),
        ]
    )
    lock_file = _lock_file(env)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(
        json.dumps(
            {
                "claimant": "daemon",
                "pid": 999999,
                "engine_pid": stubborn.pid,
                "started_at": 1778198400.0,
                "recording_started_at": 1778198401.0,
                "recording_name": "orphan",
            }
        ),
        encoding="utf-8",
    )
    try:
        with _serve(
            tmp_path,
            fake_engine_script,
            extra_env={"SCREENCAP_DAEMON_RECONCILE_GRACE": "1.0"},
        ) as (_proc, socket_path, _env):
            async with _client(socket_path) as client:
                result = await client.post("/v0/recording.start", json={"name": "demo"})
    finally:
        if stubborn.poll() is None:
            stubborn.kill()
            stubborn.wait(timeout=3)

    assert result.status_code == 503
    assert result.json()["error"] == errors.RECONCILING


@pytest.mark.asyncio
async def test_engine_crash_mid_recording_publishes_terminal_events(
    tmp_path: Path,
    fake_engine_script: Path,
) -> None:
    with _serve(tmp_path, fake_engine_script) as (_proc, socket_path, env):
        async with _client(socket_path) as stream_client, _client(socket_path) as client:
            async with stream_client.stream("GET", "/v0/events") as response:
                lines = response.aiter_lines()
                await _read_line(lines)
                started = await client.post(
                    "/v0/recording.start",
                    json={"name": "demo", "output_dir": str(tmp_path / "demo")},
                )
                engine_pid = started.json()["engine_pid"]
                await _read_until_type(lines, _stderr_events.EVENT_STARTED)

                os.kill(engine_pid, signal.SIGKILL)

                crashed = await _read_until_type(
                    lines, _stderr_events.EVENT_ENGINE_CRASHED, timeout=4.0
                )
                finalized = await _read_until_type(
                    lines, _stderr_events.EVENT_RECORDING_FINALIZED, timeout=4.0
                )
                assert crashed["exit_code"] < 0
                assert finalized["force_stopped"] is True
        assert not _lock_file(env).exists() or not json.loads(_lock_file(env).read_text()).get(
            "recording_started_at"
        )


@pytest.mark.asyncio
async def test_snapshot_during_active_daemon_recording_includes_engine_state(
    tmp_path: Path,
    fake_engine_script: Path,
) -> None:
    with _serve(tmp_path, fake_engine_script) as (_proc, socket_path, _env):
        async with _client(socket_path) as client:
            started = await client.post(
                "/v0/recording.start",
                json={"name": "demo", "output_dir": str(tmp_path / "demo")},
            )

            snapshot = (await client.get("/v0/session.snapshot")).json()

    assert snapshot["is_recording"] is True
    assert snapshot["daemon_owned"] is True
    assert snapshot["recovering"] is False
    assert snapshot["engine_pid"] == started.json()["engine_pid"]


@pytest.mark.asyncio
async def test_sigterm_shutdown_drains_recording_finalized_before_stream_closes(
    tmp_path: Path,
    fake_engine_script: Path,
) -> None:
    with _serve(tmp_path, fake_engine_script) as (proc, socket_path, env):
        async with _client(socket_path) as stream_client, _client(socket_path) as client:
            async with stream_client.stream("GET", "/v0/events") as response:
                lines = response.aiter_lines()
                await _read_line(lines)
                await client.post(
                    "/v0/recording.start",
                    json={"name": "demo", "output_dir": str(tmp_path / "demo")},
                )
                await _read_until_type(lines, _stderr_events.EVENT_STARTED)

                proc.send_signal(signal.SIGTERM)

                finalized = await _read_until_type(
                    lines, _stderr_events.EVENT_RECORDING_FINALIZED, timeout=5.0
                )
                close = await _read_until_type(lines, "_close", timeout=5.0)
                assert finalized["force_stopped"] is False
                assert close["reason"] == "shutdown"

        proc.wait(timeout=5)
    assert not _lock_file(env).exists() or not json.loads(_lock_file(env).read_text()).get(
        "recording_started_at"
    )
