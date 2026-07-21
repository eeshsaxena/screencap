"""U6 — the ``/v0/diary.search`` daemon read verb.

``diary.search`` returns ranked day-diary BLOCK snippets + POINTERS over the
``diary_fts`` table inside the global ``content_index.db`` (R5/KTD-8): a fuzzy topic
memory finds a block by its name + topic bullets across months of history, landing on
its ``(recording, block_id)`` pointer + ms span (AE2). Mirrors ``content.search``'s
recall posture; the NARRATIVE is never indexed, so it can never surface here.

These tests pin (mirroring ``test_day_narrative`` / ``content.search``):

* a real diary hit serializes through the typed response model (verb parity);
* an absent index returns ``not_indexed`` + empty hits (never a 500);
* a locked vault store degrades to the healthy ``store_state`` envelope on a 200
  (KTD-14), never a 500;
* a traversal recording filter (``../x``) is rejected (400 ``invalid_name``);
* a malformed body is a typed 400 ``invalid_request``;
* the verb is read-only — NOT in ``_ACTIVITY_PATHS`` (never resets idle-shutdown).

Privacy-marked + Vision-free (raw sqlite + the pipeline ledger / FTS, no OCR).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import screencap.content_index as ci
from screencap.daemon import schema
from screencap.daemon.app import build_app

pytestmark = pytest.mark.privacy


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the global content-index path at a per-test temp dir (never the real
    ``~/.screencap`` store) and disable the local paywall so the recall gate is a
    no-op regardless of the dev's ``config.toml``."""
    store = tmp_path / "content_index.db"
    monkeypatch.setattr(ci, "default_index_path", lambda: store)
    monkeypatch.setenv("SCREENCAP_LOCAL_PAYWALL_ENFORCE", "0")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    return store


def _seed_and_index(rec_dir: Path, recording: str) -> None:
    from screencap.content_index import write_recording_diary
    from screencap.pipeline_state import (
        PipelineLedger,
        TaskSegmentRow,
        ensure_pipeline_state_schema,
    )

    rec_dir.mkdir(parents=True, exist_ok=True)
    db = rec_dir / "recording.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, 1000.0, 2.0)")
        conn.commit()
    ensure_pipeline_state_schema(db)
    PipelineLedger(db).replace_task_segments([
        TaskSegmentRow(
            task_index=0,
            start_ts=10.0,
            end_ts=120.0,
            name="Kick technique review",
            metadata=json.dumps({"bullets": ["spinning back kick chamber"]}),
            block_id="blk-1",
        )
    ])
    write_recording_diary(recording, db)


async def _asgi_post(app, path: str, body: dict):
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(path, json=body)


@pytest.mark.asyncio
async def test_verb_returns_hits_and_validates(tmp_path):
    _seed_and_index(tmp_path / "rec-a", "rec-a")

    app = build_app()
    resp = await _asgi_post(app, "/v0/diary.search", {"query": "spinning"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["store_state"] == "mounted"
    assert body["index_state"] == "ok"
    # Validates through the typed response model (verb parity check).
    parsed = schema.DiarySearchResponse(**body).model_dump()
    assert len(parsed["hits"]) == 1
    hit = parsed["hits"][0]
    assert hit["recording"] == "rec-a"
    assert hit["block_id"] == "blk-1"
    assert hit["start_ms"] == 10_000 and hit["end_ms"] == 120_000
    assert "spinning" in hit["snippet"].lower()


@pytest.mark.asyncio
async def test_verb_no_index_returns_not_indexed(tmp_path):
    """No diary index yet (no consolidated block) → ``not_indexed`` + empty hits,
    never a 500 and never an empty store spawned."""
    app = build_app()
    resp = await _asgi_post(app, "/v0/diary.search", {"query": "anything"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["hits"] == []
    assert body["index_state"] == "not_indexed"
    assert not (tmp_path / "content_index.db").exists()


@pytest.mark.asyncio
async def test_verb_locked_store_degrades_to_store_state(tmp_path):
    """KTD-14: a locked vault store is a healthy serving state — empty hits +
    ``store_state``, never a 500."""
    from screencap.daemon.store_lifecycle import StoreState

    _seed_and_index(tmp_path / "rec-a", "rec-a")
    app = build_app()
    app.state.store_state = StoreState.LOCKED
    resp = await _asgi_post(app, "/v0/diary.search", {"query": "spinning"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["store_state"] == "locked"
    assert body["hits"] == []
    assert body["index_state"] == "store_unavailable"


@pytest.mark.asyncio
async def test_verb_traversal_recording_filter_rejected(tmp_path):
    app = build_app()
    resp = await _asgi_post(
        app, "/v0/diary.search", {"query": "x", "recording": "../x"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_name"


@pytest.mark.asyncio
async def test_verb_malformed_body_is_typed_400(tmp_path):
    app = build_app()
    resp = await _asgi_post(app, "/v0/diary.search", {"not_query": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_verb_does_not_bump_idle_activity(tmp_path):
    """diary.search is read-only — NOT in ``_ACTIVITY_PATHS``, so a read must not
    reset the idle-shutdown clock."""
    from screencap.daemon import _idle_shutdown

    _seed_and_index(tmp_path / "rec-a", "rec-a")
    app = build_app()
    _idle_shutdown.attach(app, idle_seconds=600.0)
    sentinel = 12345.0
    app.state.idle_last_activity = sentinel
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v0/diary.search", json={"query": "spinning"})
    assert resp.status_code == 200
    assert app.state.idle_last_activity == sentinel
