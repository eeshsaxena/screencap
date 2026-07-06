"""U7 — FileVault detection and surfacing (SCR-236, KTD-11 / R11).

FileVault status is checked once per daemon start via ``fdesetup status``,
served on ``/v0/daemon.info``, and shown by ``screencap status`` — warn-only:
a FileVault-off machine is surfaced but recording is never blocked, and a
broken check fails open to "unknown" without ever raising.

Privacy-marked + Vision-free so the CI privacy lane runs it.
"""

from __future__ import annotations

import httpx
import pytest

from screencap import container
from screencap.daemon import app as daemon_app
from screencap.daemon import permission_probe

pytestmark = pytest.mark.privacy


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# container.filevault_status() parsing — fail-open, never raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proc,expected",
    [
        (_Proc(0, "FileVault is On.\n"), "on"),
        (_Proc(0, "FileVault is Off.\n"), "off"),
        (_Proc(0, "Deferred enablement appears to be active.\n"), "unknown"),
        (_Proc(1, "", "some error"), "unknown"),
        (_Proc(0, ""), "unknown"),
    ],
)
def test_filevault_status_parsing(monkeypatch, proc, expected):
    monkeypatch.setattr(container.subprocess, "run", lambda *a, **k: proc)
    assert container.filevault_status() == expected


def test_filevault_status_fails_open_on_oserror(monkeypatch):
    def boom(*a, **k):
        raise OSError("fdesetup missing")

    monkeypatch.setattr(container.subprocess, "run", boom)
    assert container.filevault_status() == "unknown"  # never raises


def test_filevault_status_fails_open_on_timeout(monkeypatch):
    import subprocess as _sp

    def slow(*a, **k):
        raise _sp.TimeoutExpired(cmd="fdesetup", timeout=10)

    monkeypatch.setattr(container.subprocess, "run", slow)
    assert container.filevault_status() == "unknown"


# ---------------------------------------------------------------------------
# daemon _filevault_status() — cached, log-once, warn-only
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_filevault_cache(monkeypatch):
    monkeypatch.setattr(daemon_app, "_filevault_cache", None)
    yield


def test_daemon_filevault_cached_and_checked_once(monkeypatch, reset_filevault_cache):
    calls = []

    def fake_status():
        calls.append(1)
        return "on"

    monkeypatch.setattr(container, "filevault_status", fake_status)
    assert daemon_app._filevault_status() == "on"
    assert daemon_app._filevault_status() == "on"
    assert len(calls) == 1  # checked once per daemon process, then cached


def test_daemon_filevault_off_logs_warning(monkeypatch, reset_filevault_cache, caplog):
    monkeypatch.setattr(container, "filevault_status", lambda: "off")
    import logging

    with caplog.at_level(logging.WARNING, logger="screencap.daemon.app"):
        assert daemon_app._filevault_status() == "off"
    assert any("FileVault is OFF" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# AE4: daemon.info carries the field; off never blocks (warn-only)
# ---------------------------------------------------------------------------


async def _daemon_info(monkeypatch, fv: str) -> dict:
    # This top-level test doesn't inherit tests/daemon/conftest's autouse
    # probe stub, so stub the TCC probe here to avoid the real subprocess.
    monkeypatch.setattr(
        permission_probe,
        "probe_permissions",
        lambda *a, **k: {
            "screen_recording": "granted",
            "accessibility": "granted",
            "input_monitoring": "granted",
        },
    )
    monkeypatch.setattr(container, "filevault_status", lambda: fv)
    monkeypatch.setattr(daemon_app, "_filevault_cache", None)
    transport = httpx.ASGITransport(app=daemon_app.build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v0/daemon.info")
    assert resp.status_code == 200  # off never turns readiness into an error
    return resp.json()


async def test_daemon_info_reports_filevault_off(monkeypatch):
    payload = await _daemon_info(monkeypatch, "off")
    assert payload["filevault"] == "off"


async def test_daemon_info_reports_filevault_on(monkeypatch):
    payload = await _daemon_info(monkeypatch, "on")
    assert payload["filevault"] == "on"


# ---------------------------------------------------------------------------
# AE4: screencap status renders the warning when off
# ---------------------------------------------------------------------------


def test_status_cli_renders_filevault_warning(monkeypatch):
    """``screencap status`` prints the FileVault warning when the daemon
    reports "off", and recording state is otherwise unaffected."""
    from click.testing import CliRunner

    from screencap import cli as cli_module
    from screencap.cli import _autospawn, _daemon_client, cli

    monkeypatch.setattr(_autospawn, "ensure_daemon_or_spawn", lambda **k: None)
    # Force the human-readable path (CliRunner's stdout is not a TTY, so
    # --json would otherwise auto-default on).
    monkeypatch.setattr(cli_module, "_should_default_to_json", lambda: False)

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def snapshot(self):
            return {"is_recording": False}

        def info(self):
            return {"filevault": "off"}

    monkeypatch.setattr(_daemon_client, "DaemonHTTPClient", lambda *a, **k: _FakeClient())
    result = CliRunner().invoke(cli, ["status", "--no-nlp-check"])
    assert result.exit_code == 0
    assert "FileVault is off" in result.output
