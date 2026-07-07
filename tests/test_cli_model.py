"""Tests for the ``screencap model`` CLI thin-client (U6, SCR-239).

The subcommands (``download`` / ``status`` / ``cancel``) are one-shot HTTP
clients of the daemon's ``model.download.*`` / ``model.status`` verbs. These stub
the daemon with an ``httpx.MockTransport`` and neutralize auto-spawn, exercising
only the CLI translation: request shape, output rendering, and exit codes.
"""

import json
from unittest import mock

import httpx
from click.testing import CliRunner

from screencap.cli import cli


def _dl_envelope(**payload):
    body = {
        "ok": True, "schema_version": 1, "daemon_version": "test",
        "api_schema_version": 1, "state": "idle", "model_id": None,
        "bytes_done": 0, "bytes_total": 0, "reason": None,
    }
    body.update(payload)
    return body


def _status_envelope(models):
    return {
        "ok": True, "schema_version": 1, "daemon_version": "test",
        "api_schema_version": 1, "models": models,
    }


def _patch_daemon(handler):
    from screencap.cli._daemon_client import DaemonHTTPClient

    transport = httpx.MockTransport(handler)

    class _Patched(DaemonHTTPClient):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)

    return (
        mock.patch("screencap.cli._daemon_client.DaemonHTTPClient", _Patched),
        mock.patch("screencap.cli._autospawn.ensure_daemon_or_spawn", lambda *a, **k: None),
    )


def test_model_download_starts():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json=_dl_envelope(state="downloading", bytes_total=100))

    client_p, spawn_p = _patch_daemon(handler)
    with client_p, spawn_p:
        result = CliRunner().invoke(cli, ["model", "download"])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/v0/model.download.start"
    assert "started" in result.output.lower()


def test_model_download_failed_exits_nonzero():
    def handler(request):
        return httpx.Response(200, json=_dl_envelope(state="failed", reason="sha256-mismatch:m"))

    client_p, spawn_p = _patch_daemon(handler)
    with client_p, spawn_p:
        result = CliRunner().invoke(cli, ["model", "download"])
    assert result.exit_code == 1
    assert "failed" in result.output.lower()


def test_model_status_json():
    def handler(request):
        if request.url.path == "/v0/model.download.status":
            return httpx.Response(200, json=_dl_envelope(state="installed"))
        return httpx.Response(200, json=_status_envelope(
            [{"model_id": "qwen2.5-3b-instruct", "size_bytes": 2_000_000_000, "installed": True}]
        ))

    client_p, spawn_p = _patch_daemon(handler)
    with client_p, spawn_p:
        result = CliRunner().invoke(cli, ["model", "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["download"]["state"] == "installed"
    assert payload["installed"]["models"][0]["installed"] is True


def test_model_status_human():
    def handler(request):
        if request.url.path == "/v0/model.download.status":
            return httpx.Response(200, json=_dl_envelope(state="idle"))
        return httpx.Response(200, json=_status_envelope(
            [{"model_id": "qwen2.5-3b-instruct", "size_bytes": 2_000_000_000, "installed": False}]
        ))

    client_p, spawn_p = _patch_daemon(handler)
    # Force the human path (CliRunner's captured stdout is not a TTY, which would
    # otherwise auto-default to --json).
    json_default_p = mock.patch(
        "screencap.cli._should_default_to_json", lambda: False
    )
    with client_p, spawn_p, json_default_p:
        result = CliRunner().invoke(cli, ["model", "status"])
    assert result.exit_code == 0, result.output
    assert "qwen2.5-3b-instruct" in result.output
    assert "not installed" in result.output


def test_model_cancel():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        return httpx.Response(200, json=_dl_envelope(state="cancelled"))

    client_p, spawn_p = _patch_daemon(handler)
    with client_p, spawn_p:
        result = CliRunner().invoke(cli, ["model", "cancel"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/v0/model.download.cancel"
    assert "cancelled" in result.output.lower()
