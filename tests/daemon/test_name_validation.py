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


def test_unicode_letters_rejected_under_ascii_safelist():
    """Non-ASCII characters are rejected by the strict ASCII safelist."""
    with pytest.raises(errors.InvalidNameError):
        validate_recording_name("récap-café")


def test_ascii_safelist_accepted():
    """Characters in the ASCII safelist pass validation."""
    assert validate_recording_name("My_Recording 2026-05-14") is not None
    assert validate_recording_name("rec.final-v2") is not None


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
        "trailing.",
        "   ",
        "rec\x1b[1mname",  # ANSI escape
        "line\nnewline",   # embedded newline
        "récap-café",      # non-ASCII letters
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
    without echoing crafted bytes from the input.

    Under the strict ASCII safelist, control characters and path separators
    are both caught by the same ``_SAFE_CHARS_RE`` check, so both craft
    inputs land in the same rejection branch.
    """
    crafted = "\x07evil"
    with pytest.raises(errors.InvalidNameError) as exc_info:
        validate_recording_name(crafted)
    reason = exc_info.value.reason
    # Reason describes the safelist constraint, not the crafted bytes.
    assert "ASCII" in reason
    # Crafted control bytes must not appear verbatim in the reason.
    assert "\x07" not in reason

    # Also verify a path-separator is caught by the same branch.
    crafted_slash = "evil/name"
    with pytest.raises(errors.InvalidNameError) as exc_info2:
        validate_recording_name(crafted_slash)
    reason2 = exc_info2.value.reason
    assert "ASCII" in reason2
    # The crafted name must not appear verbatim in the reason.
    assert "evil/name" not in reason2


# ----- handler-level gate -----


@pytest.mark.asyncio
async def test_handler_rejects_traversal_name_with_invalid_name_envelope(monkeypatch):
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=None, path=None, classification=provenance.STARTED_BY_CLI
        ),
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
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=None, path=None, classification=provenance.STARTED_BY_CLI
        ),
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
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=None, path=None, classification=provenance.STARTED_BY_CLI
        ),
    )

    app = build_app()
    async with app.router.lifespan_context(app):
        app.state.supervisor = _NoopSupervisor()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/v0/recording.start", json={})
    assert response.status_code == 200, response.text
