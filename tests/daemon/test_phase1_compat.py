"""Regression test: Phase 1 SwiftUI-shaped RecordingStartRequest still works.

Phase 2 U2 made ``started_by`` server-derived and soft-deprecated the
caller-supplied field. This test confirms that a SwiftUI caller sending
the full Phase 1 payload (including an explicit ``started_by``) still
receives HTTP 200 and that the daemon silently overrides the field with
its server-derived classification.

No API version bump was made — existing Phase 1 clients must keep working
unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import provenance, schema
from screencap.daemon.app import build_app


class _FakeSupervisor:
    """Minimal supervisor stand-in that captures what it receives."""

    def __init__(self) -> None:
        self.received: dict[str, Any] | None = None

    def is_recovering(self) -> bool:
        return False

    async def spawn(self, parsed):
        self.received = parsed.model_dump()
        return {
            "session_id": parsed.name or "auto-ts",
            "started_at": 1_700_000_000.0,
            "engine_pid": 99,
            "cursor": 0,
        }

    async def shutdown(self):
        return None

    def current_session(self):
        return None


# Full Encodable JSON shape sent by SwiftUI's RecorderController.swift
# in Phase 1 (all optional fields present so the test is truly a
# regression surface for field addition / removal).
_PHASE1_SWIFTUI_PAYLOAD: dict[str, Any] = {
    "name": "rec-20260514T093000",
    "started_by": "swiftui-via-daemon",  # Phase 1 caller-supplied value
    "description": None,
    "audio": True,
    "output_dir": None,
    "wifi_metrics": False,
    "app_versions": True,
    # SCR-65 removed ``force_clean`` from RecordingStartRequest; an old SwiftUI
    # client still sends it, so keep it here to prove ``extra="ignore"`` tolerates
    # the now-unknown field (the daemon must still return 200, no version bump).
    "force_clean": False,
    "capture_video": True,
    "capture_images": True,
    "capture_window_data": True,
    "verbose": False,
    "chunk_duration": None,
    "live_upload": True,
    "force_mode": None,
    "cloud_intent": False,
    "keep_local": True,
    "intent_source": "flag",
    "segmentation_mode": "llm",
    "scrub_enabled": True,
    "show_on_website": True,
    "network": False,
}


@pytest.mark.asyncio
async def test_phase1_swiftui_payload_returns_200(monkeypatch):
    """Full Phase 1 SwiftUI payload accepted without API version bump."""
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=None, path=None, classification=provenance.STARTED_BY_SWIFTUI
        ),
    )

    app = build_app()
    fake = _FakeSupervisor()
    async with app.router.lifespan_context(app):
        app.state.supervisor = fake
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v0/recording.start",
                json=_PHASE1_SWIFTUI_PAYLOAD,
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert "session_id" in payload
    assert "started_at" in payload
    assert "engine_pid" in payload
    assert "cursor" in payload


@pytest.mark.asyncio
async def test_phase1_swiftui_started_by_overridden_by_server(monkeypatch, caplog):
    """Daemon overrides caller-supplied started_by with server-derived value."""
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=None, path=None, classification=provenance.STARTED_BY_SWIFTUI
        ),
    )

    app = build_app()
    fake = _FakeSupervisor()
    with caplog.at_level(logging.DEBUG, logger="screencap.daemon.app"):
        async with app.router.lifespan_context(app):
            app.state.supervisor = fake
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post(
                    "/v0/recording.start",
                    json=_PHASE1_SWIFTUI_PAYLOAD,
                )

    assert fake.received is not None
    # Server-derived value wins over Phase 1 caller-supplied "swiftui-via-daemon"
    assert fake.received["started_by"] == provenance.STARTED_BY_SWIFTUI
    # Daemon should have logged the override at DEBUG
    assert any("ignoring caller-supplied" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_api_schema_version_still_1():
    """No API version bump from Phase 2 U2 — v1 must remain v1."""
    assert schema.API_SCHEMA_VERSION == 1
    assert schema._RECORDING_START_API_VERSION == 1
