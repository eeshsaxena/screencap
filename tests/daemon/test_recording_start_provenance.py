"""Integration tests for the recording.start handler's server-derived
``started_by`` (Phase 2 U2.2).

These tests exercise the wire-level shape: a caller posts a body with
or without ``started_by``, the handler classifies the peer (we monkey-
patch the classifier so we don't need a real UNIX socket), and the
supervisor sees the derived value instead of the caller's.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import provenance
from screencap.daemon.app import build_app


class _FakeSupervisor:
    """Minimal supervisor stand-in capturing the ``started_by`` it received."""

    def __init__(self) -> None:
        self.received: dict[str, Any] | None = None

    def is_recovering(self) -> bool:
        return False

    async def spawn(self, parsed):
        self.received = parsed.model_dump()
        return {
            "session_id": "s",
            "started_at": 1.0,
            "engine_pid": 123,
            "cursor": 0,
        }

    async def shutdown(self):
        return None

    def current_session(self):
        return None


@pytest.mark.asyncio
async def test_caller_supplied_started_by_is_overridden_by_server_derived(monkeypatch):
    monkeypatch.setattr(
        provenance,
        "derive_started_by_from_asgi_scope",
        lambda _scope: provenance.STARTED_BY_CLI,
    )

    app = build_app()
    fake = _FakeSupervisor()
    app.state.supervisor = fake
    async with app.router.lifespan_context(app):
        # Replace supervisor again post-lifespan (build_app's lifespan
        # constructs a real one).
        app.state.supervisor = fake
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v0/recording.start",
                json={"name": "demo", "started_by": "lies"},
            )
    assert response.status_code == 200, response.text
    assert fake.received is not None
    assert fake.received["started_by"] == provenance.STARTED_BY_CLI


@pytest.mark.asyncio
async def test_no_started_by_supplied_still_uses_server_derived(monkeypatch):
    monkeypatch.setattr(
        provenance,
        "derive_started_by_from_asgi_scope",
        lambda _scope: provenance.STARTED_BY_SWIFTUI,
    )

    app = build_app()
    fake = _FakeSupervisor()
    async with app.router.lifespan_context(app):
        app.state.supervisor = fake
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v0/recording.start", json={"name": "demo"})
    assert response.status_code == 200, response.text
    assert fake.received["started_by"] == provenance.STARTED_BY_SWIFTUI


@pytest.mark.asyncio
async def test_unknown_classifier_result_lands_on_metadata(monkeypatch):
    monkeypatch.setattr(
        provenance,
        "derive_started_by_from_asgi_scope",
        lambda _scope: provenance.STARTED_BY_UNKNOWN,
    )
    app = build_app()
    fake = _FakeSupervisor()
    async with app.router.lifespan_context(app):
        app.state.supervisor = fake
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/v0/recording.start",
                json={"name": "demo", "started_by": "anything"},
            )
    assert fake.received["started_by"] == provenance.STARTED_BY_UNKNOWN


@pytest.mark.asyncio
async def test_api_schema_version_unchanged():
    """U2 is additive; the API schema version must stay at 1."""
    from screencap.daemon import schema

    assert schema.API_SCHEMA_VERSION == 1
    assert schema._RECORDING_START_API_VERSION == 1
