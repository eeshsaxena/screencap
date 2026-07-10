"""U8: recording-start local-paywall gate.

With ``SCREENCAP_LOCAL_PAYWALL_ENFORCE`` on, ``recording.start`` requires an active
entitlement tier (KTD-4 lease). The gate sits after the permission check and before
``supervisor.spawn`` (typed-error-before-spawn), so a refused start never spawns an
engine worker. The gate:

* allows immediately when the lease grants an unexpired paid tier (an offline payer
  within the 72h window is never locked out — R8);
* otherwise attempts one fresh ``whoami`` + lease reconcile and re-reads the lease
  (an online payer whose lease just expired is refreshed);
* refuses with ``subscription_required`` (no spawn) only when nothing entitles.

With the flag off the path is byte-identical to today.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import entitlement_lease, errors, provenance
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


def _stub_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=None, path=None, classification=provenance.STARTED_BY_CLI
        ),
    )


def _enforce(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    import screencap.config as cfg

    monkeypatch.setattr(cfg, "get_local_paywall_enforced", lambda: on)


def _stub_whoami(monkeypatch: pytest.MonkeyPatch, info: dict[str, Any]) -> None:
    """Stub the in-process ``auth.whoami`` the gate calls on a lease miss."""
    from screencap import auth

    monkeypatch.setattr(auth, "whoami", lambda: dict(info))


async def _post_start(app, fake: _FakeSupervisor):
    app.state.supervisor = fake
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/v0/recording.start", json={"name": "demo"})


@pytest.mark.asyncio
async def test_flag_off_starts_without_touching_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off → the gate is a no-op: start proceeds even with no lease and a
    definitively-not-entitled whoami. (Byte-identical to today.)"""
    _stub_peer(monkeypatch)
    _enforce(monkeypatch, False)
    # A whoami that would clear the lease if consulted — proving the flag-off path
    # never calls it.
    called = {"whoami": 0}

    from screencap import auth

    def _whoami() -> dict[str, Any]:
        called["whoami"] += 1
        return {"signed_in": True, "tier": None}

    monkeypatch.setattr(auth, "whoami", _whoami)

    app = build_app()
    fake = _FakeSupervisor()
    response = await _post_start(app, fake)

    assert response.status_code == 200, response.text
    assert fake.spawn_calls == 1
    assert called["whoami"] == 0


@pytest.mark.asyncio
async def test_flag_on_no_lease_offline_refused_no_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on + no lease + whoami offline/stale (ambiguous, lease-preserving) →
    still no entitlement → refused with subscription_required, no spawn."""
    _stub_peer(monkeypatch)
    _enforce(monkeypatch, True)
    entitlement_lease.clear_lease()  # ensure no lease
    _stub_whoami(monkeypatch, {"signed_in": True, "stale": True, "tier": None})

    app = build_app()
    fake = _FakeSupervisor()
    response = await _post_start(app, fake)

    assert response.status_code == 402
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == errors.SUBSCRIPTION_REQUIRED
    assert fake.spawn_calls == 0


@pytest.mark.asyncio
async def test_flag_on_valid_lease_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on + an unexpired entitled lease → starts without a whoami call."""
    _stub_peer(monkeypatch)
    _enforce(monkeypatch, True)
    entitlement_lease.write_lease("local")  # 72h default TTL

    called = {"whoami": 0}
    from screencap import auth

    def _whoami() -> dict[str, Any]:
        called["whoami"] += 1
        return {"signed_in": True, "tier": None}

    monkeypatch.setattr(auth, "whoami", _whoami)

    app = build_app()
    fake = _FakeSupervisor()
    response = await _post_start(app, fake)

    assert response.status_code == 200, response.text
    assert fake.spawn_calls == 1
    # Step 1 (valid lease) short-circuits — no network whoami on the hot path.
    assert called["whoami"] == 0


@pytest.mark.asyncio
async def test_flag_on_expired_lease_reconciles_to_entitled_and_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on + expired lease but a fresh whoami resolves an entitled tier →
    reconcile refreshes the lease → start allowed."""
    _stub_peer(monkeypatch)
    _enforce(monkeypatch, True)
    # An already-expired lease: negative TTL yields expires_at in the past.
    entitlement_lease.write_lease("local", ttl_seconds=-1)
    assert entitlement_lease.lease_entitled_tier() is None
    # A definitive, entitled whoami — reconcile writes a fresh lease.
    _stub_whoami(monkeypatch, {"signed_in": True, "tier": "cloud"})

    app = build_app()
    fake = _FakeSupervisor()
    response = await _post_start(app, fake)

    assert response.status_code == 200, response.text
    assert fake.spawn_calls == 1
    # The reconcile re-armed the lease from the fresh whoami.
    assert entitlement_lease.lease_entitled_tier() == "cloud"


@pytest.mark.asyncio
async def test_flag_on_definitive_not_entitled_clears_and_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on + no lease + a definitive (not stale) not-entitled whoami →
    reconcile clears, re-read still None → refused, no spawn."""
    _stub_peer(monkeypatch)
    _enforce(monkeypatch, True)
    entitlement_lease.clear_lease()
    _stub_whoami(monkeypatch, {"signed_in": True, "tier": None})

    app = build_app()
    fake = _FakeSupervisor()
    response = await _post_start(app, fake)

    assert response.status_code == 402
    assert response.json()["error"] == errors.SUBSCRIPTION_REQUIRED
    assert fake.spawn_calls == 0
