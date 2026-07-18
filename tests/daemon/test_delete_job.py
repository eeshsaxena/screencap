"""U8 — the ``/v0/delete.*`` range-delete verbs + the ``DeleteJob`` holder.

Range delete is IRREVERSIBLE local data destruction, so these pin the safety
contract at the daemon boundary:

* ``delete.start --dry_run`` previews (resolved set + rounded extents) and DELETES
  NOTHING; the execute call passes the confirm token back and the job deletes
  exactly it, aborting ``reconfirm_required`` when the set changed (no TOCTOU);
* a sealed / absent vault store → a TYPED error, nothing deleted (KTD-14);
* ``delete.start`` is audit-logged (peer + requested range + outcome);
* ``cancel`` stops a run cleanly;
* an execute run removes the range from ``transcript.search`` and surfaces it on
  ``timeline.day`` as a "removed by you" span (R8/AE5 backend).

Privacy-marked + Vision-free (raw sqlite + the pipeline ledger, no OCR) — CI runs
only ``pytest -m privacy``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
from httpx import ASGITransport, AsyncClient

from screencap import config
from screencap.daemon import schema
from screencap.daemon.app import build_app

# Reuse the realistic per-chunk recording fixture from the core tests.
from tests.test_range_delete import (
    _BASE,
    _chunk_bounds,
    _make_recording,
    _ms,
    _speech_token,
)

pytestmark = pytest.mark.privacy


def _rec_dir():
    return config.get_recordings_dir()


@contextlib.asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c


async def _poll_terminal(client, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get("/v0/delete.status")
        body = r.json()
        if body["state"] != "running":
            return body
        await asyncio.sleep(0.02)
    raise AssertionError("delete job did not terminate in time")


# ---------------------------------------------------------------------------
# dry_run preview — resolves + reports, DELETES NOTHING.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_preview_deletes_nothing():
    rec_dir, _ = _make_recording(_rec_dir(), n_chunks=3)
    cs, ce = _chunk_bounds(1)
    app = build_app()
    async with _client(app) as client:
        resp = await client.post(
            "/v0/delete.start",
            json={"start_ms": _ms(cs + 5), "end_ms": _ms(cs + 50), "dry_run": True},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    parsed = schema.DeletePreviewResponse(**body).model_dump()
    assert parsed["resolved"] == {"rec": [1]}
    assert parsed["total_chunks"] == 1
    plan = parsed["recordings"][0]
    assert plan["rounded_start_ms"] == _ms(cs)
    assert plan["rounded_end_ms"] == _ms(ce)
    # Nothing deleted.
    assert (rec_dir / "chunk_0001.mp4").exists()
    assert (rec_dir / "transcript_0001.txt").exists()


# ---------------------------------------------------------------------------
# execute — deletes exactly the confirmed set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_deletes_confirmed_set():
    rec_dir, _ = _make_recording(_rec_dir(), n_chunks=3)
    cs, _ = _chunk_bounds(1)
    app = build_app()
    async with _client(app) as client:
        prev = await client.post(
            "/v0/delete.start",
            json={"start_ms": _ms(cs + 5), "end_ms": _ms(cs + 50), "dry_run": True},
        )
        resolved = prev.json()["resolved"]
        start = await client.post(
            "/v0/delete.start",
            json={
                "start_ms": _ms(cs + 5), "end_ms": _ms(cs + 50),
                "dry_run": False, "resolved": resolved,
            },
        )
        assert start.status_code == 200, start.text
        final = await _poll_terminal(client)
    assert final["state"] == "completed"
    assert final["reconfirm_required"] is False
    assert final["deleted_chunks"] == 1
    assert not (rec_dir / "chunk_0001.mp4").exists()
    assert (rec_dir / "chunk_0000.mp4").exists()
    assert (rec_dir / "chunk_0002.mp4").exists()
    # Tombstone: recording.db is never deleted.
    assert (rec_dir / "recording.db").exists()


@pytest.mark.asyncio
async def test_execute_without_resolved_is_400():
    _make_recording(_rec_dir(), n_chunks=2)
    cs, _ = _chunk_bounds(0)
    app = build_app()
    async with _client(app) as client:
        resp = await client.post(
            "/v0/delete.start",
            json={"start_ms": _ms(cs), "end_ms": _ms(cs + 50), "dry_run": False},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_inverted_range_is_400():
    app = build_app()
    async with _client(app) as client:
        resp = await client.post(
            "/v0/delete.start",
            json={"start_ms": 2000, "end_ms": 1000, "dry_run": True},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# No TOCTOU — a chunk flushing between preview and confirm.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flushed_chunk_requires_reconfirm_nothing_deleted():
    import json

    from screencap.pipeline_state import PipelineLedger

    rec_dir, ledger = _make_recording(_rec_dir(), n_chunks=2)
    ledger.seed_chunk(2)  # live in-flight chunk, no manifest → excluded at preview
    cs2, ce2 = _chunk_bounds(2)
    app = build_app()
    async with _client(app) as client:
        prev = await client.post(
            "/v0/delete.start",
            json={"start_ms": _ms(_BASE + 5), "end_ms": _ms(ce2 + 100), "dry_run": True},
        )
        resolved = prev.json()["resolved"]
        assert resolved == {"rec": [0, 1]}

        # Chunk 2 flushes between preview and confirm.
        (rec_dir / "chunk_0002.mp4").write_bytes(b"\x00" * 4096)
        (rec_dir / "chunk_0002_manifest.json").write_text(
            json.dumps({"chunk_start": cs2, "chunk_end": ce2})
        )
        PipelineLedger(rec_dir / "recording.db").mark_staged(2)

        await client.post(
            "/v0/delete.start",
            json={
                "start_ms": _ms(_BASE + 5), "end_ms": _ms(ce2 + 100),
                "dry_run": False, "resolved": resolved,
            },
        )
        final = await _poll_terminal(client)
    assert final["state"] == "reconfirm_required"
    assert final["reconfirm_required"] is True
    assert final["deleted_chunks"] == 0
    # Nothing was deleted.
    for i in range(3):
        assert (rec_dir / f"chunk_{i:04d}.mp4").exists()


# ---------------------------------------------------------------------------
# Sealed / absent store — typed error, nothing deleted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sealed_store_typed_error_nothing_deleted():
    from screencap.daemon.store_lifecycle import StoreState

    rec_dir, _ = _make_recording(_rec_dir(), n_chunks=2)
    cs, _ = _chunk_bounds(0)
    app = build_app()
    app.state.store_state = StoreState.LOCKED
    async with _client(app) as client:
        resp = await client.post(
            "/v0/delete.start",
            json={
                "start_ms": _ms(cs), "end_ms": _ms(cs + 50),
                "dry_run": False, "resolved": {"rec": [0]},
            },
        )
    assert resp.status_code == 409
    assert resp.json()["error"] == "store_locked"
    assert (rec_dir / "chunk_0000.mp4").exists()  # nothing deleted


@pytest.mark.asyncio
async def test_absent_store_typed_error():
    from screencap.daemon.store_lifecycle import StoreState

    app = build_app()
    app.state.store_state = StoreState.ABSENT
    async with _client(app) as client:
        resp = await client.post(
            "/v0/delete.start",
            json={"start_ms": 1000, "end_ms": 2000, "dry_run": True},
        )
    assert resp.status_code == 409
    assert resp.json()["error"] == "store_absent"


# ---------------------------------------------------------------------------
# Audit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_start_audit_logged(tmp_path, monkeypatch):
    import json

    from screencap.daemon import audit_log

    audit_path = tmp_path / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: audit_path)

    _make_recording(_rec_dir(), n_chunks=2)
    cs, _ = _chunk_bounds(0)
    app = build_app()
    async with _client(app) as client:
        await client.post(
            "/v0/delete.start",
            json={"start_ms": _ms(cs), "end_ms": _ms(cs + 50), "dry_run": True},
        )
    lines = [json.loads(ln) for ln in audit_path.read_text().splitlines() if ln.strip()]
    entries = [e for e in lines if e.get("verb") == "delete.start"]
    assert entries, "delete.start was not audit-logged"
    entry = entries[-1]
    assert entry["outcome"] == "ok"
    assert entry["start_ms"] == _ms(cs)
    assert entry["end_ms"] == _ms(cs + 50)
    assert entry["mode"] == "dry_run"


# ---------------------------------------------------------------------------
# Integration — deleted range gone from transcript.search + timeline.day.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcript_search_empty_after_delete():
    from screencap.daemon.app import _run_transcript_search

    rec_dir, _ = _make_recording(_rec_dir(), n_chunks=3, with_scrubbed=True)
    token = _speech_token(1)
    # Guard against a vacuous pass: the token IS searchable before the delete.
    assert _run_transcript_search(token, None, 10)

    cs, _ = _chunk_bounds(1)
    app = build_app()
    async with _client(app) as client:
        prev = await client.post(
            "/v0/delete.start",
            json={"start_ms": _ms(cs + 5), "end_ms": _ms(cs + 50), "dry_run": True},
        )
        await client.post(
            "/v0/delete.start",
            json={
                "start_ms": _ms(cs + 5), "end_ms": _ms(cs + 50),
                "dry_run": False, "resolved": prev.json()["resolved"],
            },
        )
        await _poll_terminal(client)
    # Deleted speech is gone from BOTH the source + the scrubbed sibling.
    assert _run_transcript_search(token, None, 10) == []


@pytest.mark.asyncio
async def test_timeline_day_reports_removed_by_you():
    # _BASE (1000 s) falls on 1970-01-01 UTC.
    rec_dir, _ = _make_recording(_rec_dir(), n_chunks=3)
    cs, ce = _chunk_bounds(0)
    app = build_app()
    async with _client(app) as client:
        prev = await client.post(
            "/v0/delete.start",
            json={"start_ms": _ms(cs + 5), "end_ms": _ms(cs + 50), "dry_run": True},
        )
        await client.post(
            "/v0/delete.start",
            json={
                "start_ms": _ms(cs + 5), "end_ms": _ms(cs + 50),
                "dry_run": False, "resolved": prev.json()["resolved"],
            },
        )
        await _poll_terminal(client)
        day = await client.post(
            "/v0/timeline.day", json={"date": "1970-01-01", "tz_offset_seconds": 0}
        )
    body = day.json()
    parsed = schema.TimelineDayResponse(**body).model_dump()
    rec = next(r for r in parsed["recordings"] if r["name"] == "rec")
    deleted = rec["deleted"]
    assert deleted, "expected a 'removed by you' span on the day timeline"
    assert deleted[0]["start_ms"] == _ms(cs)
    assert deleted[0]["end_ms"] == _ms(ce)


# ---------------------------------------------------------------------------
# Cancel — the DeleteJob stops cleanly at a recording boundary.
# ---------------------------------------------------------------------------


class _FakeBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_cancel_mid_job_stops_cleanly(monkeypatch):
    import threading

    from screencap import range_delete
    from screencap.daemon.delete_job import DeleteJob
    from screencap.range_delete import DeleteReport

    loop_started = threading.Event()

    def _blocking_execute(start_ms, end_ms, resolved, *, stop_event=None, progress_cb=None, **kw):
        # Signal we're in-flight, then block until cancelled — a delete that
        # spans enough recordings to be cancellable mid-run.
        loop_started.set()
        stop_event.wait(timeout=5.0)
        return DeleteReport()  # cancelled before deleting anything

    monkeypatch.setattr(range_delete, "execute_delete", _blocking_execute)

    job = DeleteJob(_FakeBus())
    job.start(1000, 2000, {"recA": [0], "recB": [0]})
    # Wait until the worker thread is in-flight, then cancel.
    for _ in range(200):
        if loop_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert loop_started.is_set()
    job.cancel()
    for _ in range(200):
        if not job.is_running():
            break
        await asyncio.sleep(0.01)
    snap = job.status()
    assert snap.state == "cancelled"
    assert snap.deleted_chunks == 0
