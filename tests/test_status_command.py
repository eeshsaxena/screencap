"""Tests for ``screencap status`` as a daemon HTTP client (Phase 2 U1.4).

The command's contract:
- Always exits 0.
- Reports ``is_recording=false`` when the daemon is unreachable.
- Translates the daemon's ``/v0/session.snapshot`` envelope into the
  status payload, preserving the per-recording timestamp + elapsed
  semantics SwiftUI's poll loop depended on in Phase 1.
- Surfaces a kickstart hint to stderr when a LaunchAgent is installed
  but unreachable (so an operator notices launchd / daemon drift).
- Always probes privacy + nlp_models_cached config flags regardless of
  daemon reachability — those are local, daemon-independent signals.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest import mock

import httpx
import pytest
from click.testing import CliRunner

from screencap.cli import cli
from screencap.cli._daemon_client import (
    SUPPORTED_API_SCHEMA_VERSION,
    DaemonHTTPClient,
)


def _run_status_json():
    runner = CliRunner()
    return runner.invoke(cli, ["status", "--json"], catch_exceptions=False)


def _envelope(**payload: Any) -> dict[str, Any]:
    body = {
        "ok": True,
        "schema_version": 1,
        "daemon_version": "test",
        "api_schema_version": SUPPORTED_API_SCHEMA_VERSION,
    }
    body.update(payload)
    return body


def _idle_snapshot(**extra) -> dict[str, Any]:
    return _envelope(
        is_recording=False,
        daemon_owned=False,
        recording_name=None,
        started_at=None,
        claimant=None,
        recovering=False,
        cursor=0,
        **extra,
    )


def _recording_snapshot(*, name: str, started_at: float, **extra) -> dict[str, Any]:
    return _envelope(
        is_recording=True,
        daemon_owned=True,
        recording_name=name,
        started_at=started_at,
        claimant="daemon",
        recovering=False,
        cursor=1,
        **extra,
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect config to tmp_path so privacy_configured starts False."""
    import screencap.config

    monkeypatch.setattr(screencap.config, "_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(screencap.config, "_DEFAULT_BASE", tmp_path)
    monkeypatch.setattr(screencap.config, "_config_cache", None)


def _patch_client(monkeypatch, handler) -> None:
    """Replace ``DaemonHTTPClient`` with one wrapping ``MockTransport``."""

    transport = httpx.MockTransport(handler)

    class _PatchedClient(DaemonHTTPClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("screencap.cli._daemon_client.DaemonHTTPClient", _PatchedClient)
    # ``ensure_daemon_or_spawn`` runs first — pretend the daemon is up.
    monkeypatch.setattr("screencap.cli._autospawn.ensure_daemon_or_spawn", lambda *a, **k: None)


def test_idle_daemon_reports_not_recording(monkeypatch):
    def handler(request):
        # ``status`` reads session.snapshot for recording state and, since
        # SCR-236, daemon.info for the warn-only FileVault field.
        assert request.url.path in ("/v0/session.snapshot", "/v0/daemon.info")
        return httpx.Response(200, json=_idle_snapshot())

    _patch_client(monkeypatch, handler)
    result = _run_status_json()
    assert result.exit_code == 0
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["is_recording"] is False
    assert payload["daemon_reachable"] is True
    assert payload["started_at"] is None
    assert payload["elapsed"] is None
    assert payload["recording_name"] is None
    assert payload["claimant"] is None


def test_active_recording_translates_snapshot(monkeypatch):
    started = time.time() - 5.0

    def handler(request):
        return httpx.Response(
            200, json=_recording_snapshot(name="demo", started_at=started)
        )

    _patch_client(monkeypatch, handler)
    result = _run_status_json()
    payload = json.loads(result.output.strip())
    assert payload["is_recording"] is True
    assert payload["recording_name"] == "demo"
    assert payload["claimant"] == "daemon"
    assert payload["daemon_reachable"] is True
    assert 4.0 <= payload["elapsed"] < 30.0
    assert payload["capture_dir"].endswith("/recordings/demo")


def test_daemon_unreachable_returns_not_recording(monkeypatch):
    monkeypatch.setattr("screencap.cli._autospawn.ensure_daemon_or_spawn", lambda *a, **k: None)

    def handler(request):
        raise httpx.ConnectError("socket missing")

    transport = httpx.MockTransport(handler)

    class _PatchedClient(DaemonHTTPClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("screencap.cli._daemon_client.DaemonHTTPClient", _PatchedClient)

    result = _run_status_json()
    assert result.exit_code == 0
    payload = json.loads(result.output.strip())
    assert payload["is_recording"] is False
    assert payload["daemon_reachable"] is False
    # No traceback; no daemon error spam.
    assert "Traceback" not in (result.stderr or "")


def test_launchagent_running_but_socket_dead_surfaces_kickstart_hint(monkeypatch):
    from screencap.cli._autospawn import LaunchAgentNotRunningError

    def fake_ensure(*_args, **_kwargs):
        raise LaunchAgentNotRunningError(LaunchAgentNotRunningError.KICKSTART_HINT)

    monkeypatch.setattr("screencap.cli._autospawn.ensure_daemon_or_spawn", fake_ensure)

    def handler(request):
        raise httpx.ConnectError("socket missing")

    transport = httpx.MockTransport(handler)

    class _PatchedClient(DaemonHTTPClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("screencap.cli._daemon_client.DaemonHTTPClient", _PatchedClient)

    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--json"], catch_exceptions=False)
    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip())
    assert payload["is_recording"] is False
    # Click 8.3+ separates stderr from stdout by default; in earlier
    # versions stderr-on-stdout merging would land the hint in stdout.
    combined = (result.stderr or "") + (result.stdout or "")
    assert "kickstart" in combined


def test_no_nlp_check_skips_probe(monkeypatch):
    """``--no-nlp-check`` reports ``nlp_models_cached=null`` and skips the probe."""

    def handler(request):
        return httpx.Response(200, json=_idle_snapshot())

    _patch_client(monkeypatch, handler)
    runner = CliRunner()
    with mock.patch("screencap.redaction.engine.are_nlp_models_cached") as m:
        result = runner.invoke(
            cli, ["status", "--json", "--no-nlp-check"], catch_exceptions=False
        )
    payload = json.loads(result.output.strip())
    assert payload["nlp_models_cached"] is None
    m.assert_not_called()


def test_status_envelope_carries_schema_version(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=_idle_snapshot())

    _patch_client(monkeypatch, handler)
    payload = json.loads(_run_status_json().output.strip())
    assert payload["schema_version"] == 1


def test_status_includes_privacy_configured_flag(monkeypatch, tmp_path):
    import screencap.config

    def handler(request):
        return httpx.Response(200, json=_idle_snapshot())

    _patch_client(monkeypatch, handler)

    # No config file → False
    payload = json.loads(_run_status_json().output.strip())
    assert payload["privacy_configured"] is False

    # Write a [privacy] section + invalidate cache → True
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[privacy]\nmode = "internal"\n')
    screencap.config._config_cache = None
    payload = json.loads(_run_status_json().output.strip())
    assert payload["privacy_configured"] is True


def test_status_includes_nlp_models_cached_flag(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=_idle_snapshot())

    _patch_client(monkeypatch, handler)
    payload = json.loads(_run_status_json().output.strip())
    assert "nlp_models_cached" in payload
    assert isinstance(payload["nlp_models_cached"], bool)


def test_status_human_path_escapes_daemon_markup(monkeypatch):
    """SCR-169: the human-path ``status`` output interpolates the
    daemon-controlled ``recording_name`` and ``claimant`` into Rich-markup
    strings. A value carrying an unbalanced ``[/]`` makes Rich raise
    ``MarkupError`` and crash the command; a balanced ``[x]`` run is silently
    stripped. The dynamic segments must be escaped so brackets render verbatim
    and never reach the parser.

    Force the human path — CliRunner stdout is non-TTY, which would otherwise
    auto-select the unaffected --json path."""
    from rich.errors import MarkupError

    import screencap.cli as cli_mod

    snapshot = _recording_snapshot(name="rec[/]ord", started_at=time.time() - 5.0)
    snapshot["claimant"] = "cl[/]aim"  # override the helper's default "daemon"

    def handler(request):
        return httpx.Response(200, json=snapshot)

    _patch_client(monkeypatch, handler)
    monkeypatch.setattr(cli_mod, "_should_default_to_json", lambda: False)
    result = CliRunner().invoke(cli, ["status"], catch_exceptions=False)

    assert not isinstance(result.exception, MarkupError), result.exception
    assert result.exit_code == 0
    # Escaped, so the literal brackets survive instead of crashing/stripping.
    assert "rec[/]ord" in result.output
    assert "cl[/]aim" in result.output


def test_daemon_api_error_surfaces_and_reports_not_recording(monkeypatch):
    def handler(request):
        return httpx.Response(
            500,
            json={
                "ok": False,
                "error": "internal",
                "schema_version": 1,
                "api_schema_version": SUPPORTED_API_SCHEMA_VERSION,
                "daemon_version": "test",
            },
        )

    _patch_client(monkeypatch, handler)
    result = _run_status_json()
    assert result.exit_code == 0
    # The error message is emitted to stderr; the JSON payload is the
    # last line of stdout.
    json_line = result.output.strip().splitlines()[-1]
    payload = json.loads(json_line)
    # Daemon was reachable for the HTTP call but returned an error;
    # status falls back to "not recording" without raising.
    assert payload["is_recording"] is False
