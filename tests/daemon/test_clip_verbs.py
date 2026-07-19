"""U10 — the ``/v0/clip.*`` verbs (create / list / delete) over the vault store.

Wraps the in-vault clips store (``tests/test_clips.py`` pins the store itself) at
the daemon boundary:

* ``clip.create`` writes a catalog entry (``creator`` provenance + honesty flag)
  and a playable mp4 — and FAILS CLOSED (envelope ``ok:false`` + ``policy_purged``)
  over a policy-purged range, writing nothing;
* ``clip.list`` is a READ verb: a sealed / absent vault returns empty + a degraded
  ``store_state`` (never a 500), and the catalog survives a daemon restart;
* ``clip.create`` on a sealed store is a TYPED error (409), nothing created;
* ``clip.delete`` removes the mp4 + entry and is audit-logged.

Privacy-marked + Vision-free (raw sqlite + injected exporter, no OCR / PyAV).
"""

from __future__ import annotations

import contextlib

import pytest
from httpx import ASGITransport, AsyncClient

from screencap import config
from screencap.daemon.app import build_app

# Reuse the realistic per-chunk recording fixture from the core tests.
from tests.test_clips import _fake_export, _seed_purge_span
from tests.test_range_delete import _chunk_bounds, _make_recording, _ms

pytestmark = pytest.mark.privacy


def _rec_dir():
    return config.get_recordings_dir()


@pytest.fixture(autouse=True)
def _stub_exporter(monkeypatch):
    """Route the real PyAV trim through the byte-writing stub (Vision-free)."""
    import screencap.clips as clips

    monkeypatch.setattr(clips, "_export_clip", _fake_export)


@contextlib.asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c


# ---------------------------------------------------------------------------
# create.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clip_create_writes_entry_and_file():
    _make_recording(_rec_dir(), n_chunks=2)
    cs, _ = _chunk_bounds(0)
    app = build_app()
    async with _client(app) as client:
        resp = await client.post(
            "/v0/clip.create",
            json={
                "recording": "rec",
                "start_ms": _ms(cs + 5),
                "end_ms": _ms(cs + 50),
                "creator": "ui",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["reason"] is None
    clip = body["clip"]
    assert clip["source_recording"] == "rec"
    assert clip["creator"] == "ui"
    assert clip["honesty_flags"]["clip_video_capture_blocked_only"] is True
    mp4 = _rec_dir() / ".clips" / f"{clip['id']}.mp4"
    assert mp4.is_file() and mp4.stat().st_size > 0


@pytest.mark.asyncio
async def test_clip_create_over_policy_purge_fails_closed():
    rec_dir, _ = _make_recording(_rec_dir(), n_chunks=2)
    cs, ce = _chunk_bounds(0)
    _seed_purge_span(rec_dir / "recording.db", cs, ce, origin="policy")
    app = build_app()
    async with _client(app) as client:
        resp = await client.post(
            "/v0/clip.create",
            json={"recording": "rec", "start_ms": _ms(cs + 5), "end_ms": _ms(cs + 50)},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == "policy_purged"
    assert body["clip"] is None
    assert not any((_rec_dir() / ".clips").glob("*.mp4")) if (_rec_dir() / ".clips").exists() else True


@pytest.mark.asyncio
async def test_clip_create_invalid_range_400():
    _make_recording(_rec_dir(), n_chunks=1)
    app = build_app()
    async with _client(app) as client:
        resp = await client.post(
            "/v0/clip.create",
            json={"recording": "rec", "start_ms": 2000, "end_ms": 1000},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# list.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clip_list_returns_created_and_survives_restart():
    _make_recording(_rec_dir(), n_chunks=1)
    cs, _ = _chunk_bounds(0)
    app = build_app()
    async with _client(app) as client:
        created = (
            await client.post(
                "/v0/clip.create",
                json={"recording": "rec", "start_ms": _ms(cs + 1), "end_ms": _ms(cs + 20)},
            )
        ).json()
        listed = (await client.get("/v0/clip.list")).json()
    assert listed["ok"] is True
    assert [c["id"] for c in listed["clips"]] == [created["clip"]["id"]]

    # A FRESH app (daemon restart) re-reads the persisted catalog from disk.
    app2 = build_app()
    async with _client(app2) as client:
        again = (await client.get("/v0/clip.list")).json()
    assert [c["id"] for c in again["clips"]] == [created["clip"]["id"]]


@pytest.mark.asyncio
async def test_clip_list_sealed_store_degrades():
    from screencap.daemon.store_lifecycle import StoreState

    app = build_app()
    app.state.store_state = StoreState.LOCKED
    async with _client(app) as client:
        resp = await client.get("/v0/clip.list")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["clips"] == []
    assert body["store_state"] == "locked"


# ---------------------------------------------------------------------------
# sealed-store mutation → typed error, nothing created.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clip_create_sealed_store_typed_error():
    from screencap.daemon.store_lifecycle import StoreState

    _make_recording(_rec_dir(), n_chunks=1)
    cs, _ = _chunk_bounds(0)
    app = build_app()
    app.state.store_state = StoreState.LOCKED
    async with _client(app) as client:
        resp = await client.post(
            "/v0/clip.create",
            json={"recording": "rec", "start_ms": _ms(cs + 1), "end_ms": _ms(cs + 20)},
        )
    assert resp.status_code == 409
    assert resp.json()["error"] == "store_locked"
    assert not (_rec_dir() / ".clips").exists() or list((_rec_dir() / ".clips").glob("*.mp4")) == []


@pytest.mark.asyncio
async def test_clip_delete_absent_store_typed_error():
    from screencap.daemon.store_lifecycle import StoreState

    app = build_app()
    app.state.store_state = StoreState.ABSENT
    async with _client(app) as client:
        resp = await client.post("/v0/clip.delete", json={"clip_id": "deadbeef"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "store_absent"


# ---------------------------------------------------------------------------
# delete + audit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clip_delete_removes_and_audits(tmp_path, monkeypatch):
    import json

    from screencap.daemon import audit_log

    audit_path = tmp_path / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: audit_path)

    _make_recording(_rec_dir(), n_chunks=1)
    cs, _ = _chunk_bounds(0)
    app = build_app()
    async with _client(app) as client:
        created = (
            await client.post(
                "/v0/clip.create",
                json={"recording": "rec", "start_ms": _ms(cs + 1), "end_ms": _ms(cs + 20)},
            )
        ).json()
        clip_id = created["clip"]["id"]
        mp4 = _rec_dir() / ".clips" / f"{clip_id}.mp4"
        assert mp4.is_file()
        resp = await client.post("/v0/clip.delete", json={"clip_id": clip_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True
    assert not mp4.exists()

    lines = [json.loads(ln) for ln in audit_path.read_text().splitlines() if ln.strip()]
    entries = [e for e in lines if e.get("verb") == "clip.delete"]
    assert entries, "clip.delete was not audit-logged"
    assert entries[-1]["outcome"] == "ok"


@pytest.mark.asyncio
async def test_clip_verbs_registered():
    """The three verbs are wired into the router (documents the surface)."""
    app = build_app()
    paths = {r.path for r in app.routes}
    assert {"/v0/clip.create", "/v0/clip.list", "/v0/clip.delete"} <= paths


def test_clip_verbs_not_in_activity_paths():
    """The read verb (and the store mutations) must not pin an auto-spawned daemon
    open the way a live recording does — clip.list is a poll, not a session."""
    from screencap.daemon._idle_shutdown import _ACTIVITY_PATHS

    assert "/v0/clip.list" not in _ACTIVITY_PATHS
