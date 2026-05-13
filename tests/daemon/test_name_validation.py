"""Tests for the canonical recording-name validator (Phase 2 U2.3).

Two layers of coverage:

1. The pure validator (``validate_recording_name``) — exhaustive
   rejection cases against the per-character / per-segment / length
   rules.
2. The handler-level gate — ``POST /v0/recording.start`` with a
   forbidden name returns the ``invalid_name`` envelope with HTTP 400.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import errors, provenance
from screencap.daemon._name_validation import validate_recording_name
from screencap.daemon.app import build_app


class _NoopSupervisor:
    def is_recovering(self) -> bool:
        return False

    async def spawn(self, parsed):
        return {
            "session_id": parsed.name or "auto",
            "started_at": 1.0,
            "engine_pid": 0,
            "cursor": 0,
        }

    async def shutdown(self):
        return None

    def current_session(self):
        return None


# ----- pure-validator cases -----


def test_simple_name_accepted():
    assert validate_recording_name("demo") == "demo"


def test_name_with_hyphens_and_digits_accepted():
    assert validate_recording_name("rec-2026-05-13T093000-abc") is not None


def test_unicode_letters_accepted():
    """Non-ASCII letters are fine — only path-dangerous chars are rejected."""
    assert validate_recording_name("récap-café") is not None


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "..",
        ".",
        ".hidden",
        "a/b",
        "a\\b",
        "/abs",
        "\\abs",
        "name\x00with-nul",
        "\x01start-with-control",
    ],
)
def test_rejected_paths_raise_invalid_name(name):
    with pytest.raises(errors.InvalidNameError):
        validate_recording_name(name)


def test_empty_name_raises():
    with pytest.raises(errors.InvalidNameError):
        validate_recording_name("")


def test_overlong_name_raises():
    with pytest.raises(errors.InvalidNameError):
        validate_recording_name("x" * 256)


def test_max_length_accepted():
    name = "x" * 255
    assert validate_recording_name(name) == name


def test_non_string_raises():
    with pytest.raises(errors.InvalidNameError):
        validate_recording_name(42)
    with pytest.raises(errors.InvalidNameError):
        validate_recording_name(None)


def test_reason_does_not_echo_unsafe_bytes():
    """An InvalidNameError ``reason`` should describe the violation
    without echoing crafted bytes from the input."""
    crafted = "evil\x07name"
    try:
        validate_recording_name(crafted)
    except errors.InvalidNameError as exc:
        # Reason mentions the violation type, not the crafted bytes.
        # (The crafted bytes start with a printable char so the
        # control-char branch above doesn't fire; this one trips the
        # length-echo check.)
        assert "name must not contain NUL" in exc.reason or "control" in exc.reason or "length" in exc.reason
        # Crafted control bytes never appear in the reason.
        assert "\x07" not in exc.reason


# ----- handler-level gate -----


@pytest.mark.asyncio
async def test_handler_rejects_traversal_name_with_invalid_name_envelope(monkeypatch):
    monkeypatch.setattr(
        provenance,
        "derive_started_by_from_asgi_scope",
        lambda _scope: provenance.STARTED_BY_CLI,
    )

    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _NoopSupervisor()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                "/v0/recording.start",
                json={"name": "../../../escape"},
            )
    assert response.status_code == 400, response.text
    payload: dict[str, Any] = response.json()
    assert payload["ok"] is False
    assert payload["error"] == "invalid_name"
    assert "reason" in payload


@pytest.mark.asyncio
async def test_handler_accepts_well_formed_name(monkeypatch):
    monkeypatch.setattr(
        provenance,
        "derive_started_by_from_asgi_scope",
        lambda _scope: provenance.STARTED_BY_CLI,
    )

    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _NoopSupervisor()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                "/v0/recording.start",
                json={"name": "well-formed-demo-2026"},
            )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_handler_with_no_name_is_accepted(monkeypatch):
    """``name`` is optional; the daemon synthesizes a timestamp when missing."""
    monkeypatch.setattr(
        provenance,
        "derive_started_by_from_asgi_scope",
        lambda _scope: provenance.STARTED_BY_CLI,
    )

    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _NoopSupervisor()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/v0/recording.start", json={})
    assert response.status_code == 200, response.text
