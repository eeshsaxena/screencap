"""End-to-end tests for ``screencap start`` as a daemon HTTP client.

Phase 2 U1.6 collapsed the in-process engine spawn into a thin client of
``POST /v0/recording.start`` plus an NDJSON event stream. These tests
verify the exit-code translation contract:

- terminal ``recording_finalized`` (clean)   → exit 0
- daemon ``lock_contended`` envelope          → exit 2
- streamed ``permission_lost`` event          → exit 3
- terminal ``recording_finalized(disk_full)`` → exit 4

The auto-spawn fallback is stubbed (``ensure_daemon_or_spawn`` returns
silently); auto-spawn coverage lives in tests/cli/test_autospawn.py.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest import mock

import httpx
import pytest
from click.testing import CliRunner


def _envelope(**payload: Any) -> dict[str, Any]:
    body = {
        "ok": True,
        "schema_version": 1,
        "daemon_version": "test",
        "api_schema_version": 1,
    }
    body.update(payload)
    return body


def _ndjson(events: list[dict[str, Any]]) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def _safe_stderr(result: Any) -> str:
    """Return CliRunner stderr, tolerating Click versions that fold it into
    ``output`` (accessing ``.stderr`` then raises ValueError)."""
    try:
        return result.stderr or ""
    except ValueError:
        return ""


@pytest.fixture
def stub_daemon(monkeypatch):
    """Builds a daemon-client harness whose POST/GET behavior is scripted.

    Usage:
        with stub_daemon(start_response=..., events=[...]) as harness:
            harness.invoke(["start", "--name", "x"])
    """

    class _Harness:
        def __init__(self):
            self.start_status: int = 200
            self.start_response: dict[str, Any] = _envelope(
                session_id="s1", started_at=time.time(), engine_pid=1234, cursor=1
            )
            self.events: list[dict[str, Any]] = [
                {
                    "type": "recording_finalized",
                    "schema_version": 1,
                    "ts": time.time(),
                    "cursor": 2,
                    "name": "x",
                    "duration_seconds": 0.0,
                    "force_stopped": False,
                    "disk_full": False,
                }
            ]
            self.stop_status: int = 200
            self.stop_response: dict[str, Any] = _envelope(
                stopped=True, final_state="stopped"
            )
            self.captured_start: dict[str, Any] = {}

        def _handler(self, request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v0/recording.start" and request.method == "POST":
                self.captured_start = json.loads(request.content)
                return httpx.Response(self.start_status, json=self.start_response)
            if path == "/v0/events" and request.method == "GET":
                return httpx.Response(200, content=_ndjson(self.events))
            if path == "/v0/recording.stop" and request.method == "POST":
                return httpx.Response(self.stop_status, json=self.stop_response)
            if path == "/v0/session.snapshot":
                return httpx.Response(200, json=_envelope(is_recording=False, cursor=0))
            return httpx.Response(404, json={"ok": False, "error": "not_found"})

        def invoke(self, args, **kwargs):
            from screencap.cli import cli
            from screencap.cli._daemon_client import DaemonHTTPClient

            transport = httpx.MockTransport(self._handler)

            class _PatchedClient(DaemonHTTPClient):
                def __init__(self, *a, **kw):
                    kw["transport"] = transport
                    super().__init__(*a, **kw)

            monkeypatch.setattr(
                "screencap.cli._daemon_client.DaemonHTTPClient", _PatchedClient
            )
            monkeypatch.setattr(
                "screencap.cli._autospawn.ensure_daemon_or_spawn",
                lambda *a, **k: None,
            )
            # The start command surrounds itself with first-run prompts;
            # neutralize them so CliRunner does not block on stdin.
            monkeypatch.setattr(
                "screencap.cli._maybe_prompt_privacy_setup", lambda **kw: None
            )
            monkeypatch.setattr(
                "screencap.cli._maybe_prompt_matrix_acknowledgement", lambda: None
            )
            monkeypatch.setattr(
                "screencap.cli._stdin_is_tty", lambda: False
            )
            runner = CliRunner()
            return runner.invoke(cli, args, catch_exceptions=False, **kwargs)

    return _Harness()


def test_start_clean_finalize_exits_zero(stub_daemon):
    result = stub_daemon.invoke(["start", "--name", "demo", "--no-audio", "--local"])
    assert result.exit_code == 0, result.output
    # The CLI proxies daemon events to stderr as line-buffered JSON.
    assert "recording_finalized" in (result.output or "")
    # POST payload carries the resolved options.
    assert stub_daemon.captured_start["name"] == "demo"
    # ``audio`` is normalized to False via ``--no-audio``; the cloud_intent
    # flag is False under ``--local``.
    assert stub_daemon.captured_start["audio"] is False
    assert stub_daemon.captured_start["cloud_intent"] is False


def test_start_lock_contended_returns_exit_2(stub_daemon):
    stub_daemon.start_status = 409
    stub_daemon.start_response = {
        "ok": False,
        "error": "lock_contended",
        "schema_version": 1,
        "api_schema_version": 1,
        "daemon_version": "test",
        "owner": {"pid": 9999, "claimant": "swiftui"},
    }
    result = stub_daemon.invoke(["start", "--name", "demo", "--local"])
    assert result.exit_code == 2, result.output


def test_start_permission_lost_exits_three(stub_daemon):
    stub_daemon.events = [
        {
            "type": "started",
            "schema_version": 1,
            "ts": time.time(),
            "cursor": 1,
        },
        {
            "type": "permission_lost",
            "schema_version": 1,
            "ts": time.time(),
            "cursor": 2,
        },
        {
            "type": "recording_finalized",
            "schema_version": 1,
            "ts": time.time(),
            "cursor": 3,
            "name": "demo",
            "duration_seconds": 1.0,
            "force_stopped": True,
            "disk_full": False,
        },
    ]
    result = stub_daemon.invoke(["start", "--name", "demo", "--local"])
    assert result.exit_code == 3, result.output


def test_start_permission_required_envelope_names_permissions_and_exits_three(stub_daemon):
    """SCR-142: the daemon's pre-spawn permission gate rejects the start with a
    ``permission_required`` envelope carrying the full ``missing`` list. The CLI
    must NOT collapse this into a generic exit 1 (which loses the permission
    identity) — it re-emits a structured ``permission_required`` stderr event
    naming the exact permissions and exits 3, so the CLI-fallback SwiftUI shell
    can route into the same precise grant flow the daemon transport already uses.
    """
    stub_daemon.start_status = 403
    stub_daemon.start_response = {
        "ok": False,
        "error": "permission_required",
        "schema_version": 1,
        "api_schema_version": 1,
        "daemon_version": "test",
        "missing": ["screen_recording", "accessibility"],
    }
    result = stub_daemon.invoke(["start", "--name", "demo", "--local"])

    assert result.exit_code == 3, result.output

    # The structured event lands on stderr (terminal users still get a
    # human-readable echo; the JSON line is for the SwiftUI / agent parser).
    captured = result.output + (_safe_stderr(result))
    events = [
        json.loads(line)
        for line in captured.splitlines()
        if line.strip().startswith("{")
    ]
    perm_events = [e for e in events if e.get("type") == "permission_required"]
    assert perm_events, f"no permission_required event emitted; captured={captured!r}"
    assert perm_events[0]["missing"] == ["screen_recording", "accessibility"]


def test_start_disk_full_exits_four(stub_daemon):
    stub_daemon.events = [
        {
            "type": "started",
            "schema_version": 1,
            "ts": time.time(),
            "cursor": 1,
        },
        {
            "type": "recording_finalized",
            "schema_version": 1,
            "ts": time.time(),
            "cursor": 2,
            "name": "demo",
            "duration_seconds": 5.0,
            "force_stopped": True,
            "disk_full": True,
        },
    ]
    result = stub_daemon.invoke(["start", "--name", "demo", "--local"])
    assert result.exit_code == 4, result.output


def test_start_payload_carries_flags(stub_daemon):
    stub_daemon.invoke([
        "start",
        "--name", "demo",
        "--no-video",
        "--no-images",
        "--no-window-data",
        "--no-wifi-metrics",
        "--no-app-versions",
        "--local",
    ])
    payload = stub_daemon.captured_start
    assert payload["name"] == "demo"
    assert payload["capture_video"] is False
    assert payload["capture_images"] is False
    assert payload["capture_window_data"] is False
    assert payload["wifi_metrics"] is False
    assert payload["app_versions"] is False
    # SCR-65: the dropped ``--force``/``force_clean`` plumbing leaves no
    # residue in the start payload.
    assert "force_clean" not in payload


def test_start_force_flag_removed(stub_daemon):
    """SCR-65: ``start --force`` was a silent no-op (dead since SCR-53), so the
    flag is removed. Click now rejects it instead of accepting a parameter that
    nothing consumes."""
    result = stub_daemon.invoke(["start", "--name", "demo", "--force", "--local"])
    assert result.exit_code == 2, result.output
    assert "no such option" in result.output.lower()


def test_start_daemon_error_returns_exit_1(stub_daemon):
    stub_daemon.start_status = 500
    stub_daemon.start_response = {
        "ok": False,
        "error": "internal",
        "schema_version": 1,
        "api_schema_version": 1,
        "daemon_version": "test",
    }
    result = stub_daemon.invoke(["start", "--name", "demo", "--local"])
    assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# Cloud NLP-model gate (SCR-52)
#
# ``screencap start --cloud`` refuses to start when the NLP models needed to
# scrub cloud-bound PII are not cached. The gate is owned by the CLI
# (src/screencap/cli/__init__.py) and fires *before* the daemon handoff — there
# is no daemon-side ``models_not_cached`` envelope, so the regression surface is
# the client boundary, not the request handler. These replace the skipped
# ``test_cloud_start_nlp_model_gate`` in tests/test_cli.py, ported to the
# Phase 2 daemon-client architecture. ``stub_daemon`` runs non-interactively
# (``_stdin_is_tty`` → False), so the gate takes its hard-fail branch.
# ---------------------------------------------------------------------------


def test_cloud_start_blocked_when_models_missing(stub_daemon):
    """Non-interactive cloud start with no cached models: exit 1, an actionable
    ``screencap setup`` message, and the daemon is never contacted — the gate
    fires before ``POST /v0/recording.start``."""
    with mock.patch(
        "screencap.redaction.are_nlp_models_cached", return_value=False
    ):
        result = stub_daemon.invoke(["start", "--name", "demo", "--cloud"])
    assert result.exit_code == 1, result.output
    # Pin the exact remediation command. Normalize whitespace first: rich wraps
    # the message at the 80-col default, splitting ``screencap`` from ``setup``
    # across a newline, so a raw-substring match on the full command would fail.
    assert "screencap setup --scan" in " ".join(result.output.split())
    # No payload reached the daemon: the gate short-circuits the handoff.
    assert stub_daemon.captured_start == {}


def test_cloud_start_proceeds_when_models_cached(stub_daemon):
    """Cached models let the cloud start through: the daemon is POSTed with
    ``cloud_intent=True`` and the clean finalize maps to exit 0. Guards against
    a gate that over-blocks every cloud recording."""
    with mock.patch(
        "screencap.redaction.are_nlp_models_cached", return_value=True
    ):
        result = stub_daemon.invoke(["start", "--name", "demo", "--cloud"])
    assert result.exit_code == 0, result.output
    assert stub_daemon.captured_start["cloud_intent"] is True


def test_local_start_skips_model_probe(stub_daemon):
    """The gate is cloud-scoped: a ``--local`` start never consults the model
    cache and proceeds straight to the daemon with ``cloud_intent=False``."""
    with mock.patch("screencap.redaction.are_nlp_models_cached") as probe:
        result = stub_daemon.invoke(["start", "--name", "demo", "--local"])
    assert result.exit_code == 0, result.output
    probe.assert_not_called()
    assert stub_daemon.captured_start["cloud_intent"] is False
