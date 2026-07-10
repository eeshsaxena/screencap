"""U14: bounded last-known-good entitlement lease + daemon re-mint wiring (KTD-4).

Covers the lease store/read/expiry/clear + injected-time semantics that the U8/U9
gates depend on, and the two daemon write touchpoints: the regular ``auth.whoami``
verb reconcile and the ``entitlement.refresh`` re-mint verb (which must force
``get_id_token(force_refresh=True)`` and rewrite the lease).
"""

from __future__ import annotations

import stat
from typing import Any

import httpx
import pytest

from screencap.daemon import entitlement_lease as el
from screencap.daemon import schema

pytestmark = pytest.mark.privacy


# --------------------------------------------------------------------------
# Lease store: write / read / expiry / clear (injected time)
# --------------------------------------------------------------------------


def test_write_lease_persists_tier_and_expiry() -> None:
    lease = el.write_lease("cloud", ttl_seconds=100, now=1_000.0)
    assert lease.tier == "cloud"
    assert lease.expires_at == 1_100.0

    read_back = el.read_lease()
    assert read_back is not None
    assert read_back.tier == "cloud"
    assert read_back.expires_at == 1_100.0


def test_lease_entitled_tier_within_and_past_window() -> None:
    el.write_lease("local", ttl_seconds=100, now=1_000.0)

    # Within the window: the cached tier is grace-allowed.
    assert el.lease_entitled_tier(now=1_050.0) == "local"
    assert el.is_entitled(now=1_050.0) is True

    # Exactly at expiry and past it: no longer entitled.
    assert el.lease_entitled_tier(now=1_100.0) is None
    assert el.is_entitled(now=1_100.0) is False
    assert el.lease_entitled_tier(now=5_000.0) is None
    assert el.is_entitled(now=5_000.0) is False


def test_default_ttl_is_72h() -> None:
    assert el.DEFAULT_LEASE_TTL_SECONDS == 72 * 60 * 60
    lease = el.write_lease("cloud", now=0.0)
    assert lease.expires_at == 72 * 60 * 60


def test_read_lease_missing_returns_none() -> None:
    assert el.read_lease() is None
    assert el.lease_entitled_tier(now=0.0) is None
    assert el.is_entitled(now=0.0) is False


def test_read_lease_malformed_returns_none() -> None:
    # Write a fresh lease, then corrupt the file — read must fail safe to None,
    # never raise, so a garbled lease degrades to "no valid lease" (gate blocks).
    el.write_lease("cloud", ttl_seconds=100, now=1_000.0)
    path = el._lease_path()
    path.write_text("{ this is not json")
    assert el.read_lease() is None


def test_clear_lease_removes_it() -> None:
    el.write_lease("cloud", ttl_seconds=100, now=1_000.0)
    assert el.read_lease() is not None
    el.clear_lease()
    assert el.read_lease() is None
    # Idempotent: clearing an already-absent lease is a no-op, never raises.
    el.clear_lease()


def test_tier_none_lease_is_not_entitled() -> None:
    # A signed-in-but-no-paid-tier lease (tier=None) is a valid-but-not-entitled
    # record: within the window it still yields None (not-entitled), not a crash.
    el.write_lease(None, ttl_seconds=100, now=1_000.0)
    lease = el.read_lease()
    assert lease is not None and lease.tier is None
    assert el.lease_entitled_tier(now=1_050.0) is None
    assert el.is_entitled(now=1_050.0) is False


def test_lease_file_is_0600() -> None:
    el.write_lease("cloud", ttl_seconds=100, now=1_000.0)
    mode = stat.S_IMODE(el._lease_path().stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_lease_dir_is_0700() -> None:
    el.write_lease("cloud", ttl_seconds=100, now=1_000.0)
    run_dir = el._lease_path().parent
    mode = stat.S_IMODE(run_dir.stat().st_mode)
    assert mode == 0o700, oct(mode)


# --------------------------------------------------------------------------
# reconcile_from_whoami policy (the single write rule, KTD-4)
# --------------------------------------------------------------------------


def test_reconcile_success_with_tier_writes_lease() -> None:
    el.reconcile_from_whoami({"signed_in": True, "tier": "cloud"})
    lease = el.read_lease()
    assert lease is not None and lease.tier == "cloud"


def test_reconcile_success_no_tier_clears_lease() -> None:
    # A prior lease exists; a definitive signed-in-but-no-paid-tier clears it now.
    el.write_lease("cloud", ttl_seconds=10_000, now=1_000.0)
    el.reconcile_from_whoami({"signed_in": True})  # no tier, no stale
    assert el.read_lease() is None


def test_reconcile_stale_preserves_lease() -> None:
    # An offline/stale whoami must NEVER touch the lease — an offline payer keeps
    # grace within the window; a whoami hiccup never locks anyone out.
    el.write_lease("cloud", ttl_seconds=10_000, now=1_000.0)
    el.reconcile_from_whoami({"signed_in": True, "stale": True, "tier": None})
    lease = el.read_lease()
    assert lease is not None and lease.tier == "cloud"


def test_reconcile_signed_out_preserves_lease() -> None:
    # A bare signed_in:false with no stale flag is ambiguous → preserve, never a
    # definitive clear (KTD-4: a whoami hiccup must not lock out a payer).
    el.write_lease("cloud", ttl_seconds=10_000, now=1_000.0)
    el.reconcile_from_whoami({"signed_in": False})
    lease = el.read_lease()
    assert lease is not None and lease.tier == "cloud"


# --------------------------------------------------------------------------
# Daemon wiring: /v0/auth.whoami reconcile + /v0/entitlement.refresh re-mint
# --------------------------------------------------------------------------


async def _asgi_post(path: str, body: dict[str, Any]) -> httpx.Response:
    from screencap.daemon.app import build_app

    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body)


async def _asgi_get(path: str) -> httpx.Response:
    from screencap.daemon.app import build_app

    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_whoami_verb_writes_lease_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful /v0/auth.whoami re-arms the lease with the reported tier."""
    from screencap import auth

    monkeypatch.setattr(
        auth, "whoami",
        lambda: {"signed_in": True, "uid": "u", "email": "a@b.com", "tier": "local"},
    )
    response = await _asgi_get("/v0/auth.whoami")
    assert response.status_code == 200
    lease = el.read_lease()
    assert lease is not None and lease.tier == "local"


@pytest.mark.asyncio
async def test_whoami_verb_stale_preserves_existing_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline (stale) /v0/auth.whoami must not disturb a valid lease."""
    from screencap import auth

    el.write_lease("cloud", ttl_seconds=10_000, now=1_000.0)
    monkeypatch.setattr(
        auth, "whoami",
        lambda: {"signed_in": True, "stale": True, "tier": None},
    )
    response = await _asgi_get("/v0/auth.whoami")
    assert response.status_code == 200
    lease = el.read_lease()
    assert lease is not None and lease.tier == "cloud"


@pytest.mark.asyncio
async def test_entitlement_refresh_forces_remint_and_rewrites_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/v0/entitlement.refresh forces get_id_token(force_refresh=True) then rewrites
    the lease from the freshly-materialized whoami — the post-checkout un-gate."""
    from screencap import auth

    calls: list[bool] = []

    def _fake_get_id_token(force_refresh: bool = False) -> str:
        calls.append(force_refresh)
        return "tok"

    # After the re-mint, whoami reports the just-converted tier.
    monkeypatch.setattr(auth, "get_id_token", _fake_get_id_token)
    monkeypatch.setattr(
        auth, "whoami",
        lambda: {"signed_in": True, "uid": "u", "email": "a@b.com", "tier": "cloud"},
    )

    response = await _asgi_post("/v0/entitlement.refresh", {})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["schema_version"] == schema._ENTITLEMENT_REFRESH_API_VERSION
    assert payload["tier"] == "cloud"

    # The force path was taken (the whole point — re-mint, don't serve the cached
    # pre-conversion token).
    assert calls == [True], calls
    # And the lease was rewritten with the just-granted tier.
    lease = el.read_lease()
    assert lease is not None and lease.tier == "cloud"


@pytest.mark.asyncio
async def test_entitlement_refresh_offline_preserves_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-mint that fails offline (whoami reports stale) preserves the lease — a
    spurious/offline call never locks out a within-window payer."""
    from screencap import auth
    from screencap.auth import AuthError

    el.write_lease("cloud", ttl_seconds=10_000, now=1_000.0)

    def _boom(force_refresh: bool = False) -> str:
        raise AuthError("offline")

    monkeypatch.setattr(auth, "get_id_token", _boom)
    monkeypatch.setattr(
        auth, "whoami",
        lambda: {"signed_in": True, "stale": True, "tier": None},
    )

    response = await _asgi_post("/v0/entitlement.refresh", {})
    assert response.status_code == 200
    assert response.json()["stale"] is True
    # Preserved — no spurious clear on an offline re-mint.
    lease = el.read_lease()
    assert lease is not None and lease.tier == "cloud"


@pytest.mark.asyncio
async def test_entitlement_refresh_confirmed_not_entitled_clears_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-mint that confirms not-entitled (signed in, no tier) clears the lease
    immediately — a lapsed user is gated without waiting out the window."""
    from screencap import auth

    el.write_lease("cloud", ttl_seconds=10_000, now=1_000.0)
    monkeypatch.setattr(auth, "get_id_token", lambda force_refresh=False: "tok")
    monkeypatch.setattr(
        auth, "whoami",
        lambda: {"signed_in": True, "uid": "u", "email": "a@b.com"},  # no tier
    )

    response = await _asgi_post("/v0/entitlement.refresh", {})
    assert response.status_code == 200
    assert el.read_lease() is None
