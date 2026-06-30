"""Tests for the ``screencap backfill`` CLI thin-client (SCR-178 U6).

The three subcommands (``start`` / ``status`` / ``cancel``) are one-shot
HTTP clients of the daemon's ``backfill.*`` verbs over the UNIX socket.
These tests stub the daemon with an ``httpx.MockTransport`` (mirroring the
``stop`` command tests in ``tests/test_cli.py``) and neutralize auto-spawn,
so they exercise only the CLI translation: request shape, output rendering,
and exit codes — including the daemon-unreachable transport-failure path.
"""

import json
from unittest import mock

import httpx
import pytest
from click.testing import CliRunner

from screencap.cli import cli


def _backfill_envelope(**payload):
    """A daemon ``backfill.*`` status envelope with the privacy-safe payload."""
    body = {
        "ok": True,
        "schema_version": 1,
        "daemon_version": "test",
        "api_schema_version": 1,
        "state": "idle",
        "done": 0,
        "skipped": 0,
        "failed": 0,
        "total": 0,
        "current_unit_index": 0,
    }
    body.update(payload)
    return body


def _patch_backfill_daemon_client(handler):
    """Returns (client_patcher, autospawn_patcher) context managers.

    Mirrors ``_patch_stop_daemon_client`` in tests/test_cli.py: inject an
    ``httpx.MockTransport`` into every ``DaemonHTTPClient`` and stub
    ``ensure_daemon_or_spawn`` to a no-op (daemon "already reachable").
    """
    from screencap.cli._daemon_client import DaemonHTTPClient

    transport = httpx.MockTransport(handler)

    class _PatchedClient(DaemonHTTPClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    return (
        mock.patch("screencap.cli._daemon_client.DaemonHTTPClient", _PatchedClient),
        mock.patch("screencap.cli._autospawn.ensure_daemon_or_spawn", lambda *a, **k: None),
    )


def test_backfill_start_reports_started():
    """`backfill start` POSTs the start verb and confirms the running state."""
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json=_backfill_envelope(state="running", total=4),
        )

    client_p, autospawn_p = _patch_backfill_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["backfill", "start"])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/v0/backfill.start"
    assert "started" in result.output.lower()


def test_backfill_start_already_complete():
    """A `completed` snapshot from start renders the no-op confirmation."""

    def handler(request):
        return httpx.Response(200, json=_backfill_envelope(state="completed", total=3, done=3))

    client_p, autospawn_p = _patch_backfill_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["backfill", "start"])
    assert result.exit_code == 0, result.output
    assert "complete" in result.output.lower()


def test_backfill_status_json_emits_payload():
    """`status --json` emits the raw privacy-safe status payload as JSON."""

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v0/backfill.status"
        return httpx.Response(
            200,
            json=_backfill_envelope(
                state="running", done=2, skipped=1, failed=0, total=5, current_unit_index=3
            ),
        )

    client_p, autospawn_p = _patch_backfill_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["backfill", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["state"] == "running"
    assert payload["done"] == 2
    assert payload["skipped"] == 1
    assert payload["total"] == 5
    assert payload["current_unit_index"] == 3
    # Privacy: no recording directory name crosses the boundary.
    assert "recording" not in payload


def test_backfill_status_human_line():
    """Default (no --json) prints a human-readable progress line."""

    def handler(request):
        return httpx.Response(
            200,
            json=_backfill_envelope(state="running", done=2, skipped=1, failed=0, total=5),
        )

    client_p, autospawn_p = _patch_backfill_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p, \
            mock.patch("screencap.cli._should_default_to_json", return_value=False):
        result = runner.invoke(cli, ["backfill", "status"])
    assert result.exit_code == 0, result.output
    assert "2/5" in result.output
    assert "running" in result.output.lower()
    # Not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output.strip().splitlines()[-1])


def test_backfill_cancel_reports_state():
    """`cancel` POSTs the cancel verb and reports the resulting state."""
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json=_backfill_envelope(state="cancelled", done=2, total=5))

    client_p, autospawn_p = _patch_backfill_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["backfill", "cancel"])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/v0/backfill.cancel"
    assert "cancelled" in result.output.lower()


def test_backfill_start_daemon_unreachable_nonzero_exit():
    """Daemon unreachable → standard transport-failure message, non-zero exit."""

    def handler(request):
        raise httpx.ConnectError("socket missing")

    client_p, autospawn_p = _patch_backfill_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["backfill", "start"])
    assert result.exit_code != 0
    assert "daemon" in result.output.lower()


def test_backfill_status_daemon_unreachable_nonzero_exit():
    """`status` against a down daemon also surfaces the transport-failure exit."""

    def handler(request):
        raise httpx.ConnectError("socket missing")

    client_p, autospawn_p = _patch_backfill_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["backfill", "status", "--json"])
    assert result.exit_code != 0
    assert "daemon" in result.output.lower()


def test_backfill_cancel_daemon_unreachable_nonzero_exit():
    """`cancel` against a down daemon also surfaces the transport-failure exit."""

    def handler(request):
        raise httpx.ConnectError("socket missing")

    client_p, autospawn_p = _patch_backfill_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["backfill", "cancel"])
    assert result.exit_code != 0
    assert "daemon" in result.output.lower()
