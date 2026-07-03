"""U2 (prototype UI): the recording.start ``audio`` flag — engine threading and
the effective-audio response echo.

The ``audio`` request field already threads to the engine (session.py →
start_recording); U2 adds the response *echo* so U6/U7 can reflect what the
engine actually did, and the app treats a missing/mismatched echo as audio-on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import provenance, schema
from screencap.daemon.app import build_app
from screencap.daemon.supervisor import build_engine_worker_args


class _FakeSupervisor:
    """Minimal supervisor stand-in — the handler adds the audio echo post-spawn,
    so the fake need not know about audio at all."""

    def __init__(self) -> None:
        self.received: dict[str, Any] | None = None

    def is_recovering(self) -> bool:
        return False

    async def spawn(self, parsed):
        self.received = parsed.model_dump()
        return {"session_id": "s", "started_at": 1.0, "engine_pid": 123, "cursor": 0}

    async def shutdown(self):
        return None

    def current_session(self):
        return None


@pytest.fixture(autouse=True)
def _fake_peer(monkeypatch):
    # Avoid the real peer-socket read; also make the grant probe report granted so
    # recording.start reaches the supervisor rather than a permission block.
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=None, path=None, classification=provenance.STARTED_BY_SWIFTUI
        ),
    )


async def _start(app, body: dict) -> dict:
    fake = _FakeSupervisor()
    async with app.router.lifespan_context(app):
        app.state.supervisor = fake
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/v0/recording.start", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --- Engine threading: audio=False spawns the engine without audio ---


def test_worker_args_thread_audio_false_to_engine():
    """The engine worker reads ``args["audio"]``; a False request threads False
    (the argv/config effect that disables capture)."""
    req = schema.RecordingStartRequest(name="demo", audio=False)
    args = build_engine_worker_args(req, name="demo", capture_dir=Path("/tmp/demo"))
    assert args["audio"] is False


def test_worker_args_thread_audio_true_to_engine():
    req = schema.RecordingStartRequest(name="demo", audio=True)
    args = build_engine_worker_args(req, name="demo", capture_dir=Path("/tmp/demo"))
    assert args["audio"] is True


# --- Response echo ---


@pytest.mark.asyncio
async def test_start_echoes_audio_false():
    body = await _start(build_app(), {"name": "demo", "audio": False})
    assert body["audio"] is False


@pytest.mark.asyncio
async def test_start_echoes_audio_true():
    body = await _start(build_app(), {"name": "demo", "audio": True})
    assert body["audio"] is True


@pytest.mark.asyncio
async def test_start_echoes_audio_on_by_default_when_unspecified():
    """No audio field → the daemon resolves the audio-on default so the app has a
    definite value (matching the mic row's default-on state). `get_audio_default()`
    defaults True."""
    body = await _start(build_app(), {"name": "demo"})
    assert body["audio"] is True


@pytest.mark.asyncio
async def test_start_echoes_configured_audio_default_when_unspecified(monkeypatch):
    """An unspecified request echoes the ENGINE's actual resolution
    (`config.get_audio_default()`), not a hardcoded True — so a user who set
    `audio_default=false` sees the mic reported off, matching what the engine records."""
    from screencap import config

    monkeypatch.setattr(config, "get_audio_default", lambda: False)
    body = await _start(build_app(), {"name": "demo"})
    assert body["audio"] is False


@pytest.mark.asyncio
async def test_api_schema_version_unchanged():
    """U2 is additive; the API schema version stays at 1."""
    assert schema.API_SCHEMA_VERSION == 1
    assert schema._RECORDING_START_API_VERSION == 1
