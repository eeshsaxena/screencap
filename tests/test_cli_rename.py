"""Tests for the ``screencap rename`` CLI command (editable titles U4).

Exercises the command as a thin client of ``POST /v0/recording.rename`` by
patching ``DaemonHTTPClient`` with an ``httpx.MockTransport`` (same seam the
``stop`` tests in ``test_cli.py`` use) and stubbing daemon auto-spawn. Covers a
successful rename, both clear affordances (empty TITLE and ``--clear``), and the
error path — a daemon error envelope must exit non-zero AND surface to the user
(never a silent failure; see docs/solutions/integration-issues/
cli-json-envelope-nonzero-exit-discards-stdout).
"""

import json
from unittest import mock

from click.testing import CliRunner

from screencap.cli import cli


def _patch_rename_daemon_client(handler):
    """Return (client_patcher, autospawn_patcher) mirroring test_cli.py."""
    import httpx

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


def _rename_ok_envelope(**payload):
    body = {
        "ok": True,
        "schema_version": 1,
        "daemon_version": "test",
        "api_schema_version": 1,
    }
    body.update(payload)
    return body


def test_rename_sets_title_and_succeeds():
    """``rename <rec> "New Title"`` POSTs the selector + title and reports ok."""
    import httpx

    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_rename_ok_envelope(
                title="New Title", title_is_user_set=True, cursor=7
            ),
        )

    client_p, autospawn_p = _patch_rename_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["rename", "rec-abc", "New Title", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/v0/recording.rename"
    assert captured["body"] == {"recording_id": "rec-abc", "title": "New Title"}
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["recording"] == "rec-abc"
    assert payload["title"] == "New Title"
    assert payload["title_is_user_set"] is True


def test_rename_accepts_directory_name_as_selector():
    """The selector passes through verbatim — a directory name is as valid as a
    recording_id (the daemon resolves either)."""
    import httpx

    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_rename_ok_envelope(title="Demo", title_is_user_set=True, cursor=1),
        )

    client_p, autospawn_p = _patch_rename_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(
            cli, ["rename", "2026-07-12_15-14-00", "Demo", "--json"]
        )

    assert result.exit_code == 0, result.output
    assert captured["body"]["recording_id"] == "2026-07-12_15-14-00"


def test_rename_empty_title_clears():
    """An empty TITLE argument sends ``""`` — the daemon reverts to its default."""
    import httpx

    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_rename_ok_envelope(
                title="Jul 12 2026, 3:14 PM", title_is_user_set=False, cursor=8
            ),
        )

    client_p, autospawn_p = _patch_rename_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["rename", "rec-abc", "", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["body"] == {"recording_id": "rec-abc", "title": ""}
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["title_is_user_set"] is False


def test_rename_clear_flag_sends_empty_title():
    """``--clear`` (no positional TITLE) also sends ``""``."""
    import httpx

    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_rename_ok_envelope(
                title="Jul 12 2026, 3:14 PM", title_is_user_set=False, cursor=9
            ),
        )

    client_p, autospawn_p = _patch_rename_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["rename", "rec-abc", "--clear", "--json"])

    assert result.exit_code == 0, result.output
    assert captured["body"] == {"recording_id": "rec-abc", "title": ""}


def test_rename_clear_and_title_conflict_is_rejected_without_daemon_call():
    """``--clear`` plus a non-empty TITLE is a usage error — exits non-zero and
    never reaches the daemon."""
    import httpx

    called = {"hit": False}

    def handler(request):
        called["hit"] = True
        return httpx.Response(200, json=_rename_ok_envelope())

    client_p, autospawn_p = _patch_rename_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["rename", "rec-abc", "Nope", "--clear"])

    assert result.exit_code != 0
    assert called["hit"] is False
    assert "clear" in result.output.lower()


def test_rename_unknown_recording_exits_nonzero_and_surfaces_error():
    """A daemon error envelope (unknown recording) must exit non-zero AND reach
    the user on stderr — never a silent failure (the envelope-on-nonzero-exit
    learning: don't swallow the error)."""
    import httpx

    def handler(request):
        return httpx.Response(
            404,
            json={
                "ok": False,
                "error": "recording_not_found",
                "schema_version": 1,
                "api_schema_version": 1,
                "daemon_version": "test",
            },
        )

    client_p, autospawn_p = _patch_rename_daemon_client(handler)
    runner = CliRunner()
    with (
        client_p,
        autospawn_p,
        mock.patch("screencap.cli._should_default_to_json", return_value=False),
    ):
        result = runner.invoke(cli, ["rename", "does-not-exist", "New Title"])

    assert result.exit_code != 0
    # Surfaced to the user (stderr), not swallowed.
    assert "recording_not_found" in result.stderr
    assert "recording_not_found" in result.output


def test_rename_error_json_envelope_carries_error_field():
    """In --json mode the error still exits non-zero and the stdout envelope
    carries ``ok:false`` + the daemon error code for the macOS consumer."""
    import httpx

    def handler(request):
        return httpx.Response(
            404,
            json={
                "ok": False,
                "error": "recording_not_found",
                "schema_version": 1,
                "api_schema_version": 1,
                "daemon_version": "test",
            },
        )

    client_p, autospawn_p = _patch_rename_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["rename", "does-not-exist", "New Title", "--json"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "recording_not_found"


def test_rename_help_mentions_command():
    """``rename --help`` documents the command, its selector, and clearing."""
    result = CliRunner().invoke(cli, ["rename", "--help"])
    assert result.exit_code == 0
    assert "rename" in result.output.lower()
    assert "RECORDING" in result.output
    assert "TITLE" in result.output
    assert "--clear" in result.output


def test_rename_listed_in_top_level_help():
    """The command is registered on the top-level group."""
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "rename" in result.output
