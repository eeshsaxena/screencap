"""U9: recall/search local-paywall gate (5 verbs).

With ``SCREENCAP_LOCAL_PAYWALL_ENFORCE`` on, the five recall verbs
(``content.search`` / ``transcript.search`` / ``timeline.query`` / ``frame.nearest``
/ ``apps.list``) require an unexpired entitled KTD-4 lease. Unlike U8 the recall
gate reads the lease ONLY (no per-search ``whoami`` — the lease is kept fresh by the
whoami verb and U8's start path; a per-search network call would be too chatty).

Browse verbs (``recording.list`` / ``timeline.day`` / ``tasks.list``) never gate — a
lapsed user keeps browsing and exporting their own data (R8). With the flag off the
recall path is unchanged.
"""

from __future__ import annotations

import httpx
import pytest

from screencap.daemon import entitlement_lease, errors

# The five gated recall verbs, keyed by (method, path, body).
_RECALL_POST = [
    ("/v0/content.search", {"query": "x"}),
    ("/v0/transcript.search", {"query": "x"}),
    ("/v0/timeline.query", {}),
    ("/v0/frame.nearest", {"recording": "rec", "timestamp_ms": 0}),
    ("/v0/frame.read", {"recording": "rec", "stem": "0"}),
]
_RECALL_GET = ["/v0/apps.list"]

# Browse GET verbs that must NEVER gate.
_BROWSE_GET = ["/v0/recording.list"]
# Browse POST verbs that must NEVER gate (tasks.list is a POST taking a recording).
_BROWSE_POST = [("/v0/tasks.list", {"recording": "nonexistent"})]


def _enforce(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    import screencap.config as cfg

    monkeypatch.setattr(cfg, "get_local_paywall_enforced", lambda: on)


async def _asgi_get(path: str) -> httpx.Response:
    from screencap.daemon.app import build_app

    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def _asgi_post(path: str, body: dict) -> httpx.Response:
    from screencap.daemon.app import build_app

    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", _RECALL_POST)
async def test_recall_post_gated_when_no_lease(
    path: str, body: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enforce(monkeypatch, True)
    entitlement_lease.clear_lease()
    response = await _asgi_post(path, body)
    assert response.status_code == 402, response.text
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"] == errors.SUBSCRIPTION_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _RECALL_GET)
async def test_recall_get_gated_when_no_lease(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enforce(monkeypatch, True)
    entitlement_lease.clear_lease()
    response = await _asgi_get(path)
    assert response.status_code == 402, response.text
    assert response.json()["error"] == errors.SUBSCRIPTION_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", _RECALL_POST)
async def test_recall_post_allowed_with_valid_lease(
    path: str, body: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enforce(monkeypatch, True)
    entitlement_lease.write_lease("local")
    response = await _asgi_post(path, body)
    # Not gated: the verb answers its own contract (200 ok:true with empty results
    # since no recordings are seeded), never the 402 subscription error.
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _RECALL_GET)
async def test_recall_get_allowed_with_valid_lease(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enforce(monkeypatch, True)
    entitlement_lease.write_lease("cloud")
    response = await _asgi_get(path)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", _RECALL_POST)
async def test_recall_post_ungated_when_flag_off(
    path: str, body: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enforce(monkeypatch, False)
    entitlement_lease.clear_lease()  # no lease, but flag off → open
    response = await _asgi_post(path, body)
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _RECALL_GET)
async def test_recall_get_ungated_when_flag_off(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enforce(monkeypatch, False)
    entitlement_lease.clear_lease()
    response = await _asgi_get(path)
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _BROWSE_GET)
async def test_browse_get_verbs_never_gate(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """recording.list succeeds even with the flag on and no lease — a lapsed user
    keeps browsing + exporting their own data (R8)."""
    _enforce(monkeypatch, True)
    entitlement_lease.clear_lease()
    response = await _asgi_get(path)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", _BROWSE_POST)
async def test_browse_post_verbs_never_gate(
    path: str, body: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tasks.list (a browse POST) succeeds even with the flag on and no lease."""
    _enforce(monkeypatch, True)
    entitlement_lease.clear_lease()
    response = await _asgi_post(path, body)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
