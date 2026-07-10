"""Tests for the ``screencap storage migrate`` CLI thin-client (SCR-228 U5).

Stubs the daemon with an ``httpx.MockTransport`` (mirroring the backfill CLI
tests) and neutralizes auto-spawn, so they exercise only the CLI translation:
request shape, path absolutization, output rendering, and exit codes.
"""

import json
from unittest import mock

import httpx
from click.testing import CliRunner

from screencap.cli import cli


def _ok_envelope(**payload):
    body = {
        "ok": True,
        "schema_version": 1,
        "daemon_version": "test",
        "api_schema_version": 1,
    }
    body.update(payload)
    return body


def _refusal_envelope(reason, message):
    return {
        "ok": False,
        "schema_version": 1,
        "daemon_version": "test",
        "api_schema_version": 1,
        "error": "storage_migration_failed",
        "reason": reason,
        "message": message,
    }


def _patch_daemon(handler):
    from screencap.cli._daemon_client import DaemonHTTPClient

    transport = httpx.MockTransport(handler)

    class _PatchedClient(DaemonHTTPClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    return (
        mock.patch("screencap.cli._daemon_client.DaemonHTTPClient", _PatchedClient),
        mock.patch(
            "screencap.cli._autospawn.ensure_daemon_or_spawn", lambda *a, **k: None
        ),
    )


def test_migrate_posts_verb_and_absolute_target():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json=_ok_envelope(moved_from="/old", moved_to="/new/recs")
        )

    client_p, autospawn_p = _patch_daemon(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        # A relative path must be sent to the daemon as an absolute path.
        result = runner.invoke(cli, ["storage", "migrate", "some/rel/dir"])

    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/v0/storage.migrate"
    assert captured["body"]["target"].startswith("/")
    assert captured["body"]["target"].endswith("some/rel/dir")


def test_migrate_success_json_default_under_non_tty():
    def handler(request):
        return httpx.Response(
            200, json=_ok_envelope(moved_from="/old", moved_to="/new/recs")
        )

    client_p, autospawn_p = _patch_daemon(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["storage", "migrate", "/new/recs"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["moved_to"] == "/new/recs"


def test_migrate_human_output_on_success(monkeypatch):
    monkeypatch.setattr(
        "screencap.cli._should_default_to_json", lambda: False
    )

    def handler(request):
        return httpx.Response(
            200, json=_ok_envelope(moved_from="/old", moved_to="/new/recs")
        )

    client_p, autospawn_p = _patch_daemon(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["storage", "migrate", "/new/recs"])

    assert result.exit_code == 0, result.output
    assert "moved to" in result.output.lower()
    assert "/new/recs" in result.output


def test_migrate_refusal_exits_nonzero_with_reason():
    def handler(request):
        return httpx.Response(
            409,
            json=_refusal_envelope(
                "cross_volume", "Pick a folder on the same disk."
            ),
        )

    client_p, autospawn_p = _patch_daemon(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["storage", "migrate", "/other/volume"])

    assert result.exit_code == 1
    payload = json.loads(result.output)  # --json default under non-TTY
    assert payload["ok"] is False
    assert payload["reason"] == "cross_volume"


def test_migrate_refusal_human_message(monkeypatch):
    monkeypatch.setattr(
        "screencap.cli._should_default_to_json", lambda: False
    )

    def handler(request):
        return httpx.Response(
            409,
            json=_refusal_envelope(
                "cloud_synced", "That folder is synced to iCloud."
            ),
        )

    client_p, autospawn_p = _patch_daemon(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["storage", "migrate", "/synced/dir"])

    assert result.exit_code == 1
    assert "synced to icloud" in result.output.lower()
