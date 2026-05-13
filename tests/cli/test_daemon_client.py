"""Contract tests for the CLI's ``DaemonHTTPClient``.

Covers the happy paths and error envelopes the live-state command
bodies will rely on in U1.4-U1.6. Uses ``httpx.MockTransport`` so the
tests are runnable on any platform (the production transport pins to
AF_UNIX which only works on macOS in our deployment).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from screencap.cli._daemon_client import (
    SUPPORTED_API_SCHEMA_VERSION,
    DaemonAPIError,
    DaemonHTTPClient,
    DaemonUnreachableError,
    SchemaMismatchError,
)


def _envelope(**payload: Any) -> dict[str, Any]:
    """Match the daemon's standard envelope shape from ``schema.envelope``."""
    body = {
        "ok": True,
        "schema_version": 1,
        "daemon_version": "test",
        "api_schema_version": SUPPORTED_API_SCHEMA_VERSION,
    }
    body.update(payload)
    return body


def _mock_client(handler) -> DaemonHTTPClient:
    return DaemonHTTPClient(transport=httpx.MockTransport(handler))


def test_info_returns_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v0/daemon.info"
        return httpx.Response(200, json=_envelope(build="test", started_at=1.0))

    with _mock_client(handler) as client:
        result = client.info()
        assert result["ok"] is True
        assert result["build"] == "test"


def test_snapshot_passes_through():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v0/session.snapshot"
        return httpx.Response(
            200,
            json=_envelope(is_recording=False, daemon_owned=False, cursor=42),
        )

    with _mock_client(handler) as client:
        snap = client.snapshot()
        assert snap["is_recording"] is False
        assert snap["cursor"] == 42


def test_list_recordings_passes_through():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v0/recording.list"
        return httpx.Response(200, json=_envelope(recordings=[{"name": "x"}]))

    with _mock_client(handler) as client:
        result = client.list_recordings()
        assert result["recordings"] == [{"name": "x"}]


def test_start_posts_request_body():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v0/recording.start"
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json=_envelope(
                session_id="abc",
                started_at=1.0,
                engine_pid=999,
                cursor=7,
            ),
        )

    with _mock_client(handler) as client:
        result = client.start(name="demo", audio=False)
        assert result["session_id"] == "abc"
        assert captured == {"name": "demo", "audio": False}


def test_stop_serializes_force_flag():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_envelope(stopped=True, final_state="idle"))

    with _mock_client(handler) as client:
        result = client.stop(force=True)
        assert result["stopped"] is True
        assert captured == {"force": True}


def test_api_error_raises_with_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "ok": False,
                "error": "lock_contended",
                "schema_version": 1,
                "api_schema_version": SUPPORTED_API_SCHEMA_VERSION,
                "daemon_version": "test",
                "owner": {"pid": 123, "claimant": "swiftui"},
            },
        )

    with _mock_client(handler) as client:
        with pytest.raises(DaemonAPIError) as exc_info:
            client.start(name="demo")
        assert exc_info.value.status_code == 409
        assert exc_info.value.envelope["error"] == "lock_contended"
        assert exc_info.value.envelope["owner"]["claimant"] == "swiftui"


def test_schema_mismatch_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "schema_version": 1,
                "api_schema_version": SUPPORTED_API_SCHEMA_VERSION + 100,
                "daemon_version": "future",
            },
        )

    with _mock_client(handler) as client:
        with pytest.raises(SchemaMismatchError) as exc_info:
            client.info()
        assert exc_info.value.daemon_version == SUPPORTED_API_SCHEMA_VERSION + 100


def test_connect_error_surfaces_as_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("socket missing")

    with _mock_client(handler) as client:
        with pytest.raises(DaemonUnreachableError):
            client.info()


def test_non_json_body_surfaces_as_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>oops</html>")

    with _mock_client(handler) as client:
        with pytest.raises(DaemonUnreachableError):
            client.info()


def test_events_streams_ndjson_lines():
    body = (
        json.dumps({"type": "subscribed", "cursor": 0}) + "\n"
        + json.dumps({"type": "started", "cursor": 1}) + "\n"
        + json.dumps({"type": "recording_finalized", "cursor": 2}) + "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v0/events"
        assert request.url.params.get("since") == "5"
        return httpx.Response(200, content=body.encode("utf-8"))

    with _mock_client(handler) as client:
        with client.events(since=5) as stream:
            events = list(stream)
    assert [e["type"] for e in events] == ["subscribed", "started", "recording_finalized"]


def test_events_propagates_cursor_unknown_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            410,
            json={
                "ok": False,
                "error": "cursor_unknown",
                "schema_version": 1,
                "api_schema_version": SUPPORTED_API_SCHEMA_VERSION,
                "daemon_version": "test",
                "requested_cursor": 999,
                "daemon_cursor": 12,
                "oldest_retained_cursor": 5,
            },
        )

    with _mock_client(handler) as client:
        with pytest.raises(DaemonAPIError) as exc_info:
            with client.events(since=999) as stream:
                list(stream)
        assert exc_info.value.envelope["error"] == "cursor_unknown"
        assert exc_info.value.envelope["oldest_retained_cursor"] == 5


def test_events_skips_malformed_lines():
    body = (
        json.dumps({"type": "subscribed", "cursor": 0}) + "\n"
        + "not-json\n"
        + json.dumps({"type": "started", "cursor": 1}) + "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode("utf-8"))

    with _mock_client(handler) as client:
        with client.events() as stream:
            events = list(stream)
    assert [e["type"] for e in events] == ["subscribed", "started"]
