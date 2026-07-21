"""U4 — the app-only ``/v0/day.narrative`` daemon read verb.

``day.narrative`` returns a LOCAL recording's written, evidence-bound day
narrative (R8/R9) — the prose the day view opens with, read straight off the
per-recording ``pipeline_day_narrative`` row in the local-only ``recording.db``
(never uploaded — R14). A recording that produced no narrative (a mechanical-only
/ nothing-to-name day — R9), a legacy / pre-U4 recording, or a locked/absent vault
store returns ``ok:true`` with ``narrative: null`` (never an error).

These tests pin (mirroring ``test_tasks_query`` and ``test_frame_nearest``):

* a produced recording's narrative + ``generated_at`` + ``reason`` marker serialize
  through the typed response model (verb parity check);
* a recording with no narrative row returns a null narrative (never a 500);
* a locked / absent vault store degrades to the healthy ``store_state`` envelope on
  a 200 (KTD-14), never a 500;
* a traversal recording name (``../x``) is rejected by the canonical name resolver
  (400 ``invalid_name``) — NOT pydantic type-validation alone (P2 security);
* a malformed body is a typed 400 ``invalid_request``;
* the verb is read-only — NOT in ``_ACTIVITY_PATHS`` (never resets idle-shutdown).

Privacy-marked + Vision-free (raw sqlite + the pipeline ledger, no OCR).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import schema
from screencap.daemon.app import build_app

pytestmark = pytest.mark.privacy


def _make_recording(rec_dir: Path, started: float = 1000.0) -> None:
    from screencap.pipeline_state import ensure_pipeline_state_schema

    rec_dir.mkdir(parents=True, exist_ok=True)
    db = rec_dir / "recording.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, ?, 2.0)", (started,))
        conn.commit()
    ensure_pipeline_state_schema(db)


def _write_narrative(rec_dir: Path, text: str, fingerprint: str, reason: str | None) -> None:
    from screencap.pipeline_state import PipelineLedger

    PipelineLedger(rec_dir / "recording.db").set_day_narrative(text, fingerprint, reason)


async def _asgi_post(app, path: str, body: dict):
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(path, json=body)


@pytest.mark.asyncio
async def test_verb_returns_narrative_and_validates(tmp_path, monkeypatch):
    rec = tmp_path / "rec-a"
    _make_recording(rec)
    _write_narrative(rec, "You ran payroll and triaged email.", "fp-abc", None)
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    app = build_app()
    resp = await _asgi_post(app, "/v0/day.narrative", {"recording": "rec-a"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["store_state"] == "mounted"
    # Validates through the typed response model (verb parity check).
    parsed = schema.DayNarrativeResponse(**body).model_dump()
    assert parsed["recording"] == "rec-a"
    assert parsed["narrative"] == "You ran payroll and triaged email."
    assert parsed["reason"] is None
    assert parsed["generated_at"] is not None


@pytest.mark.asyncio
async def test_verb_no_narrative_row_returns_null(tmp_path, monkeypatch):
    """A recording that produced no narrative (mechanical-only / nothing-to-name —
    R9) returns ``narrative: null`` on a 200, never an error."""
    rec = tmp_path / "rec-nada"
    _make_recording(rec)
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    app = build_app()
    resp = await _asgi_post(app, "/v0/day.narrative", {"recording": "rec-nada"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["narrative"] is None
    assert body["reason"] is None


@pytest.mark.asyncio
async def test_verb_legacy_recording_returns_null(tmp_path, monkeypatch):
    """A missing recording dir / pre-U4 recording yields a null narrative, no crash."""
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    app = build_app()
    resp = await _asgi_post(app, "/v0/day.narrative", {"recording": "rec-missing"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["narrative"] is None


@pytest.mark.asyncio
async def test_verb_locked_store_degrades_to_store_state(tmp_path, monkeypatch):
    """KTD-14: a locked vault store is a healthy serving state — null narrative +
    ``store_state``, never a 500."""
    from screencap.daemon.store_lifecycle import StoreState

    rec = tmp_path / "rec-a"
    _make_recording(rec)
    _write_narrative(rec, "You ran payroll.", "fp-abc", None)
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    app = build_app()
    app.state.store_state = StoreState.LOCKED
    resp = await _asgi_post(app, "/v0/day.narrative", {"recording": "rec-a"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["store_state"] == "locked"
    # A locked store never reads the narrative off disk, even though a row exists.
    assert body["narrative"] is None


@pytest.mark.asyncio
async def test_verb_absent_store_degrades_to_store_state(tmp_path, monkeypatch):
    from screencap.daemon.store_lifecycle import StoreState

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    app = build_app()
    app.state.store_state = StoreState.ABSENT
    resp = await _asgi_post(app, "/v0/day.narrative", {"recording": "rec-a"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["store_state"] == "absent"
    assert body["narrative"] is None


@pytest.mark.asyncio
async def test_verb_traversal_name_rejected_by_resolver(tmp_path, monkeypatch):
    """P2 security: a traversal recording name is rejected by the canonical name
    validator (400 ``invalid_name``), NOT pydantic type-validation alone."""
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    app = build_app()
    resp = await _asgi_post(app, "/v0/day.narrative", {"recording": "../x"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_name"


@pytest.mark.asyncio
async def test_verb_traversal_rejected_even_on_locked_store(tmp_path, monkeypatch):
    """The name is validated BEFORE the store-state branch, so a traversal name is
    a 400 even when the store is locked (never a silent null)."""
    from screencap.daemon.store_lifecycle import StoreState

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    app = build_app()
    app.state.store_state = StoreState.LOCKED
    resp = await _asgi_post(app, "/v0/day.narrative", {"recording": "../../etc/passwd"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_name"


@pytest.mark.asyncio
async def test_verb_malformed_body_is_typed_400(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    app = build_app()
    resp = await _asgi_post(app, "/v0/day.narrative", {"not_recording": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_verb_does_not_bump_idle_activity(tmp_path, monkeypatch):
    """day.narrative is read-only — NOT in ``_ACTIVITY_PATHS``, so a read must not
    reset the idle-shutdown clock."""
    from screencap.daemon import _idle_shutdown

    rec = tmp_path / "rec-a"
    _make_recording(rec)
    _write_narrative(rec, "You ran payroll.", "fp-abc", None)
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    app = build_app()
    _idle_shutdown.attach(app, idle_seconds=600.0)
    sentinel = 12345.0
    app.state.idle_last_activity = sentinel
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v0/day.narrative", json={"recording": "rec-a"})
    assert resp.status_code == 200
    assert app.state.idle_last_activity == sentinel
