"""U6: pre-spawn ``permission_required`` start-failure contract.

A daemon-backed recording start that lacks the Screen Recording grant must return
a typed, non-500 ``permission_required`` envelope naming the missing
permission(s) — raised *before* the engine spawns, so there is no
200-OK-then-crash and no engine worker process. Decision: hard-block on Screen
Recording only; Accessibility / Input Monitoring denials warn-and-proceed.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import errors, permission_probe, provenance
from screencap.daemon.app import build_app


class _FakeSupervisor:
    """Records whether spawn was reached (proves no engine spawn on a block)."""

    def __init__(self) -> None:
        self.spawn_calls = 0

    def is_recovering(self) -> bool:
        return False

    async def spawn(self, parsed: Any) -> dict[str, Any]:
        self.spawn_calls += 1
        return {"session_id": "s", "started_at": 1.0, "engine_pid": 123, "cursor": 0}

    async def shutdown(self) -> None:
        return None

    def current_session(self) -> None:
        return None


def _stub_probe(monkeypatch: pytest.MonkeyPatch, grants: dict[str, str]) -> None:
    monkeypatch.setattr(permission_probe, "probe_permissions", lambda: dict(grants))


def _stub_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=None, path=None, classification=provenance.STARTED_BY_CLI
        ),
    )


async def _post_start(app, fake: _FakeSupervisor):
    app.state.supervisor = fake
    async with app.router.lifespan_context(app):
        app.state.supervisor = fake  # build_app's lifespan makes a real one
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/v0/recording.start", json={"name": "demo"})


_ALL_GRANTED = {
    "screen_recording": "granted",
    "accessibility": "granted",
    "input_monitoring": "granted",
}


@pytest.mark.asyncio
async def test_screen_recording_denied_returns_typed_error_and_no_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_peer(monkeypatch)
    _stub_probe(monkeypatch, {**_ALL_GRANTED, "screen_recording": "denied"})
    app = build_app()
    fake = _FakeSupervisor()

    response = await _post_start(app, fake)

    assert response.status_code == 403  # typed 4xx, never the except-Exception 500
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == errors.PERMISSION_REQUIRED
    assert body["missing"] == ["screen_recording"]
    # No engine worker spawned → no EVENT_STARTED, no synthesized crash events.
    assert fake.spawn_calls == 0


@pytest.mark.asyncio
async def test_missing_lists_all_denied_required_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_peer(monkeypatch)
    _stub_probe(
        monkeypatch,
        {"screen_recording": "denied", "accessibility": "denied", "input_monitoring": "granted"},
    )
    app = build_app()
    fake = _FakeSupervisor()

    response = await _post_start(app, fake)

    assert response.status_code == 403
    # screen_recording first (canonical order), then any other denied required.
    assert response.json()["missing"] == ["screen_recording", "accessibility"]
    assert fake.spawn_calls == 0


@pytest.mark.asyncio
async def test_accessibility_only_denied_proceeds_to_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Decision: hard-block on Screen Recording only. Accessibility denial is
    # advisory — start must proceed.
    _stub_peer(monkeypatch)
    _stub_probe(monkeypatch, {**_ALL_GRANTED, "accessibility": "denied"})
    app = build_app()
    fake = _FakeSupervisor()

    response = await _post_start(app, fake)

    assert response.status_code == 200, response.text
    assert fake.spawn_calls == 1


@pytest.mark.asyncio
async def test_input_monitoring_only_denied_proceeds_to_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Decision: hard-block on Screen Recording only. Input Monitoring denial is
    # advisory — start must proceed.
    _stub_peer(monkeypatch)
    _stub_probe(monkeypatch, {**_ALL_GRANTED, "input_monitoring": "denied"})
    app = build_app()
    fake = _FakeSupervisor()

    response = await _post_start(app, fake)

    assert response.status_code == 200, response.text
    assert fake.spawn_calls == 1


@pytest.mark.asyncio
async def test_indeterminate_screen_recording_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Indeterminate is never "missing" — defer to the engine preflight backstop.
    _stub_peer(monkeypatch)
    _stub_probe(monkeypatch, {**_ALL_GRANTED, "screen_recording": "indeterminate"})
    app = build_app()
    fake = _FakeSupervisor()

    response = await _post_start(app, fake)

    assert response.status_code == 200, response.text
    assert fake.spawn_calls == 1


@pytest.mark.asyncio
async def test_all_granted_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_peer(monkeypatch)
    _stub_probe(monkeypatch, dict(_ALL_GRANTED))
    app = build_app()
    fake = _FakeSupervisor()

    response = await _post_start(app, fake)

    assert response.status_code == 200, response.text
    assert fake.spawn_calls == 1


@pytest.mark.asyncio
async def test_denied_to_granted_observed_without_daemon_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 at start: a denied→granted flip is observed by the next recording.start
    without restarting the daemon — proving the gate reads the fresh-subprocess
    probe, not a value frozen at daemon launch."""
    _stub_peer(monkeypatch)

    state = {"granted": False}

    def _probe() -> dict[str, str]:
        if state["granted"]:
            return dict(_ALL_GRANTED)
        return {**_ALL_GRANTED, "screen_recording": "denied"}

    monkeypatch.setattr(permission_probe, "probe_permissions", _probe)

    app = build_app()
    fake = _FakeSupervisor()
    app.state.supervisor = fake
    async with app.router.lifespan_context(app):
        app.state.supervisor = fake
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/v0/recording.start", json={"name": "demo"})
            assert first.status_code == 403
            assert fake.spawn_calls == 0

            # User grants Screen Recording. Expire the short grant cache to force
            # a fresh probe (the same daemon process — no restart). Push the
            # cache timestamp into the past so the real TTL-expiry path fires,
            # rather than reaching in to null the cache value directly.
            state["granted"] = True
            app.state._grant_cache_at = 0.0

            second = await client.post("/v0/recording.start", json={"name": "demo"})
            assert second.status_code == 200, second.text
            assert fake.spawn_calls == 1
