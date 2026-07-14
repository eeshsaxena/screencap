"""FileVault detection + surfacing (SCR-236/SCR-258 U7, KTD-11, R11/R15).

The FileVault check is a **warn-only** at-rest signal: it parses ``fdesetup
status`` text, exposes the result on ``daemon.info``, and lets ``screencap
status`` render a warning line when FileVault is Off. It NEVER blocks or refuses
recording, and the detection function is **strictly fail-open** — any parse
error, non-zero exit, timeout, or unexpected output degrades to ``unknown``
rather than raising an exception that could block daemon startup.

Every subprocess boundary is mocked; no test shells out to the real
``fdesetup`` binary.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import patch

import httpx
import pytest

pytestmark = pytest.mark.privacy


def _fake_completed(stdout: bytes = b"", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["fdesetup", "status"], returncode=returncode, stdout=stdout, stderr=b""
    )


# --------------------------------------------------------------------------- #
# Detection — happy path
# --------------------------------------------------------------------------- #


def test_status_on_parses_on() -> None:
    from screencap.container import FileVaultStatus, filevault_status

    with patch("subprocess.run", return_value=_fake_completed(b"FileVault is On.\n")) as run:
        assert filevault_status() is FileVaultStatus.ON
    assert run.called  # confirms the check went through the (mocked) subprocess


def test_status_off_parses_off() -> None:
    from screencap.container import FileVaultStatus, filevault_status

    with patch("subprocess.run", return_value=_fake_completed(b"FileVault is Off.\n")):
        assert filevault_status() is FileVaultStatus.OFF


def test_status_on_with_encryption_in_progress_still_on() -> None:
    """A mid-transition ``fdesetup`` output still resolves from its leading line."""
    from screencap.container import FileVaultStatus, filevault_status

    text = b"FileVault is On.\nEncryption in progress: Percent completed = 42\n"
    with patch("subprocess.run", return_value=_fake_completed(text)):
        assert filevault_status() is FileVaultStatus.ON


# --------------------------------------------------------------------------- #
# Detection — strictly fail-open (garbage / non-zero / timeout / empty → UNKNOWN,
# never an exception that could block daemon startup or refuse recording).
# --------------------------------------------------------------------------- #


def test_garbage_output_is_unknown_and_never_raises() -> None:
    from screencap.container import FileVaultStatus, filevault_status

    with patch("subprocess.run", return_value=_fake_completed(b"wat is this even\n")) as run:
        result = filevault_status()

    assert result is FileVaultStatus.UNKNOWN
    assert run.called  # the check ran (mocked), it did not short-circuit


def test_nonzero_exit_is_unknown() -> None:
    from screencap.container import FileVaultStatus, filevault_status

    # A non-zero exit with an otherwise-parseable body must NOT be trusted.
    with patch("subprocess.run", return_value=_fake_completed(b"FileVault is On.\n", returncode=1)):
        assert filevault_status() is FileVaultStatus.UNKNOWN


def test_empty_output_is_unknown() -> None:
    from screencap.container import FileVaultStatus, filevault_status

    with patch("subprocess.run", return_value=_fake_completed(b"")):
        assert filevault_status() is FileVaultStatus.UNKNOWN


def test_timeout_is_unknown_and_never_raises() -> None:
    from screencap.container import FileVaultStatus, filevault_status

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("fdesetup", 10.0)):
        # Must swallow the timeout and fail open, not propagate it.
        assert filevault_status() is FileVaultStatus.UNKNOWN


def test_subprocess_oserror_is_unknown_and_never_raises() -> None:
    from screencap.container import FileVaultStatus, filevault_status

    with patch("subprocess.run", side_effect=OSError("fdesetup not found")):
        assert filevault_status() is FileVaultStatus.UNKNOWN


def test_status_enum_serializes_as_plain_string() -> None:
    """The enum is ``str``-valued so it rides the daemon.info envelope directly."""
    from screencap.container import FileVaultStatus

    assert FileVaultStatus.OFF.value == "off"
    assert FileVaultStatus.ON.value == "on"
    assert FileVaultStatus.UNKNOWN.value == "unknown"


# --------------------------------------------------------------------------- #
# daemon.info carries the field.
# --------------------------------------------------------------------------- #


async def _asgi_info(filevault: str | None) -> httpx.Response:
    """GET /v0/daemon.info against a fresh app whose cached FileVault state is set.

    The lifespan-run startup probe is bypassed here (ASGITransport does not run
    lifespan), so we set ``app.state.filevault_status`` directly — the same value
    the startup probe would cache — to exercise the handler wiring.
    """
    from screencap.daemon.app import build_app

    app = build_app()
    if filevault is not None:
        app.state.filevault_status = filevault
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/v0/daemon.info")


@pytest.mark.asyncio
async def test_daemon_info_carries_off() -> None:
    from screencap.daemon import schema

    response = await _asgi_info("off")

    assert response.status_code == 200  # warn-only: OFF is a normal 200, never an error
    payload = response.json()
    assert payload["filevault"] == "off"
    # Still validates against the additive response model.
    schema.DaemonInfoResponse(**payload)


@pytest.mark.asyncio
async def test_daemon_info_on_and_unknown() -> None:
    assert (await _asgi_info("on")).json()["filevault"] == "on"
    # No cached value (older/lifespan-less path) → non-alarming "unknown".
    assert (await _asgi_info(None)).json()["filevault"] == "unknown"


# --------------------------------------------------------------------------- #
# screencap status renders the warning only when Off — and never blocks.
# --------------------------------------------------------------------------- #


def _envelope(**payload: Any) -> dict[str, Any]:
    from screencap.cli._daemon_client import SUPPORTED_API_SCHEMA_VERSION

    body = {
        "ok": True,
        "schema_version": 1,
        "daemon_version": "test",
        "api_schema_version": SUPPORTED_API_SCHEMA_VERSION,
    }
    body.update(payload)
    return body


def _idle_snapshot() -> dict[str, Any]:
    return _envelope(
        is_recording=False,
        daemon_owned=False,
        recording_name=None,
        started_at=None,
        claimant=None,
        recovering=False,
        cursor=0,
    )


def _run_status(monkeypatch, *, filevault: str, as_json: bool = False):
    """Invoke ``screencap status`` against a mock daemon reporting ``filevault``."""
    from click.testing import CliRunner

    import screencap.config
    from screencap.cli import cli
    from screencap.cli._daemon_client import DaemonHTTPClient

    # Isolate config so privacy_configured / nlp probes stay deterministic.
    monkeypatch.setattr(screencap.config, "_config_cache", None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v0/daemon.info":
            return httpx.Response(200, json=_envelope(filevault=filevault))
        return httpx.Response(200, json=_idle_snapshot())

    transport = httpx.MockTransport(handler)

    class _PatchedClient(DaemonHTTPClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("screencap.cli._daemon_client.DaemonHTTPClient", _PatchedClient)
    monkeypatch.setattr("screencap.cli._autospawn.ensure_daemon_or_spawn", lambda *a, **k: None)
    # Force the human/prose branch off by default (CliRunner is not a TTY, which
    # would otherwise auto-enable --json and suppress the rendered warning line).
    monkeypatch.setattr("screencap.cli._should_default_to_json", lambda: False)

    args = ["status", "--no-nlp-check"]
    if as_json:
        args.append("--json")
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def test_status_off_renders_warning_and_does_not_block(monkeypatch) -> None:
    result = _run_status(monkeypatch, filevault="off")

    # Warn, do not block: the command completes normally (exit 0), recording is
    # never refused — it just prints an advisory line.
    assert result.exit_code == 0
    assert "FileVault is off" in result.output
    assert "Warning" in result.output


def test_status_on_renders_no_warning(monkeypatch) -> None:
    result = _run_status(monkeypatch, filevault="on")
    assert result.exit_code == 0
    assert "FileVault" not in result.output


def test_status_unknown_renders_no_warning(monkeypatch) -> None:
    result = _run_status(monkeypatch, filevault="unknown")
    assert result.exit_code == 0
    assert "FileVault" not in result.output


def test_status_json_carries_filevault(monkeypatch) -> None:
    result = _run_status(monkeypatch, filevault="off", as_json=True)
    assert result.exit_code == 0
    payload = json.loads(result.output.strip())
    assert payload["filevault"] == "off"
    # Warn-only: the field is informational; is_recording is untouched by it.
    assert payload["is_recording"] is False
