"""HTTP-layer + ledger tests for the U7 user task CRUD verbs (SCR-214).

Drives ``tasks.create`` / ``tasks.update`` / ``tasks.delete`` / ``tasks.merge`` /
``tasks.split`` over an in-process ASGI transport (``build_app`` +
``httpx.ASGITransport``) against a real ``recording.db`` fixture in a temp
recordings dir — the same lightweight harness ``test_daemon_recording_rename.py``
uses. No engine, no subprocess: task CRUD is a post-hoc mutating surface over the
local-only ``pipeline_task_segments`` store.

Covers (per U7):
* create adds a ``source='user'`` row at a non-colliding HIGH ``task_index``;
* update marks an agent row ``edited=1`` (re-homed) and refreshes its content;
* delete removes the row (idempotent second delete);
* merge combines two segments atomically with the chosen label (span = union),
  and a simulated partial failure rolls back (no half-merge);
* split produces two segments at the point; a split_ts outside the span → 400;
* traversal recording name → 400 ``invalid_name``; malformed body / zero-length /
  out-of-range span → 400 ``invalid_request``; unknown recording → 404;
* ``_audit`` recorded on the success AND the failure path;
* a subsequent agent re-segmentation (``replace_task_segments``) preserves all
  five outcomes (user + edited-agent rows survive, no UNIQUE collision).

These exercise the local tasks store only (no capture / privacy surface), so none
are marked ``@pytest.mark.privacy``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import audit_log, errors, provenance
from screencap.daemon.app import build_app

_STARTED_AT = 1778198400.0


def _make_recording(base: Path, name: str = "rec") -> Path:
    """Create a minimal real recording dir with a migrated recording.db."""
    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import ensure_pipeline_state_schema

    recording_dir = base / name
    recording_dir.mkdir(parents=True)
    db_path = recording_dir / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(
        session,
        {
            "timestamp": _STARTED_AT,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        },
    )
    session.close()
    engine.dispose()
    ensure_pipeline_state_schema(db_path)
    return recording_dir


@pytest.fixture
def audit_log_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: target)
    return target


@pytest.fixture
def recordings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "recordings"
    d.mkdir()
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(d))
    return d


def _patch_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=4321,
            path="/usr/local/bin/screencap",
            classification=provenance.STARTED_BY_CLI,
        ),
    )


async def _post(verb: str, payload: dict):
    transport = ASGITransport(app=build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/v0/{verb}", json=payload)


def _ledger(recording_dir: Path):
    from screencap.pipeline_state import PipelineLedger

    return PipelineLedger(recording_dir / "recording.db")


def _rows_by_index(recording_dir: Path):
    return {r.task_index: r for r in _ledger(recording_dir).read_task_segments()}


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_adds_user_row_at_high_index(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap.pipeline_state import USER_TASK_INDEX_BASE

    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")

    resp = await _post(
        "tasks.create",
        {"recording": "demo", "name": "Deep work", "start_ts": 10.0, "end_ts": 20.0,
         "category": "focus"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    task = body["task"]
    assert task["task_index"] >= USER_TASK_INDEX_BASE  # disjoint high range
    assert task["name"] == "Deep work"
    assert task["start_ts"] == 10.0 and task["end_ts"] == 20.0
    assert task["category"] == "focus"

    # Persisted as a source='user' row.
    row = _rows_by_index(rec)[task["task_index"]]
    assert row.source == "user"
    assert row.name == "Deep work"

    # Audit recorded ok.
    record = json.loads(audit_log_at.read_text(encoding="utf-8").splitlines()[-1])
    assert record["verb"] == "tasks.create"
    assert record["outcome"] == "ok"
    assert record["peer_pid"] == 4321


@pytest.mark.asyncio
async def test_create_rejects_bad_spans(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _make_recording(recordings_dir, "demo")

    # zero-length, inverted, and negative-start spans all → 400 invalid_request.
    for start, end in ((5.0, 5.0), (9.0, 4.0), (-1.0, 5.0)):
        resp = await _post(
            "tasks.create",
            {"recording": "demo", "name": "x", "start_ts": start, "end_ts": end},
        )
        assert resp.status_code == 400, (start, end, resp.text)
        assert resp.json()["error"] == errors.INVALID_REQUEST


@pytest.mark.asyncio
async def test_create_empty_name_rejected(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _make_recording(recordings_dir, "demo")
    resp = await _post(
        "tasks.create",
        {"recording": "demo", "name": "   ", "start_ts": 1.0, "end_ts": 2.0},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == errors.INVALID_REQUEST


# ---------------------------------------------------------------------------
# update — marks an agent row edited=1 (re-homed) and survives replace.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_marks_agent_row_edited_and_rehomes(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap.pipeline_state import (
        USER_TASK_INDEX_BASE,
        TaskSegmentRow,
    )

    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")
    # Seed an agent row at low index 0.
    _ledger(rec).replace_task_segments(
        [TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=5.0, name="agent A")]
    )

    resp = await _post(
        "tasks.update",
        {"recording": "demo", "task_index": 0, "name": "renamed A"},
    )

    assert resp.status_code == 200, resp.text
    new_index = resp.json()["task_index"]
    assert new_index >= USER_TASK_INDEX_BASE  # re-homed out of the agent low range

    rows = _rows_by_index(rec)
    assert 0 not in rows  # the low-range slot is freed
    assert rows[new_index].source == "agent"
    assert rows[new_index].edited is True
    assert rows[new_index].name == "renamed A"


@pytest.mark.asyncio
async def test_update_missing_task_index_returns_400(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _make_recording(recordings_dir, "demo")
    resp = await _post(
        "tasks.update", {"recording": "demo", "task_index": 999, "name": "nope"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == errors.INVALID_REQUEST


# ---------------------------------------------------------------------------
# delete — idempotent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_row_idempotently(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")
    created = await _post(
        "tasks.create",
        {"recording": "demo", "name": "temp", "start_ts": 1.0, "end_ts": 2.0},
    )
    idx = created.json()["task"]["task_index"]

    first = await _post("tasks.delete", {"recording": "demo", "task_index": idx})
    assert first.status_code == 200, first.text
    assert first.json()["deleted"] is True
    assert idx not in _rows_by_index(rec)

    # Deleting again is idempotent (deleted:false, not an error).
    second = await _post("tasks.delete", {"recording": "demo", "task_index": idx})
    assert second.status_code == 200
    assert second.json()["deleted"] is False


# ---------------------------------------------------------------------------
# merge — atomic union with the chosen label; partial failure rolls back.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_combines_two_with_union_span(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap.pipeline_state import (
        USER_TASK_INDEX_BASE,
        TaskSegmentRow,
    )

    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")
    # Two adjacent agent segments.
    _ledger(rec).replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=10.0, end_ts=20.0, name="agent A"),
        TaskSegmentRow(task_index=1, start_ts=20.0, end_ts=30.0, name="agent B"),
    ])

    resp = await _post(
        "tasks.merge",
        {"recording": "demo", "task_indices": [0, 1], "name": "Combined"},
    )

    assert resp.status_code == 200, resp.text
    new_index = resp.json()["task_index"]
    assert new_index >= USER_TASK_INDEX_BASE

    rows = _rows_by_index(rec)
    # The two originals are gone; exactly one survivor remains.
    assert 0 not in rows and 1 not in rows
    assert set(rows) == {new_index}
    survivor = rows[new_index]
    assert survivor.source == "user"  # protected from the next agent pass
    assert survivor.name == "Combined"  # caller's chosen label
    assert survivor.start_ts == 10.0 and survivor.end_ts == 30.0  # union span


@pytest.mark.asyncio
async def test_merge_fewer_than_two_returns_400(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap.pipeline_state import TaskSegmentRow

    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")
    _ledger(rec).replace_task_segments(
        [TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=1.0, name="only")]
    )
    # A merge naming one distinct index → 400.
    resp = await _post(
        "tasks.merge", {"recording": "demo", "task_indices": [0, 0], "name": "x"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == errors.INVALID_REQUEST
    # A merge naming a second, non-existent index → still < 2 resolvable → 400.
    resp2 = await _post(
        "tasks.merge", {"recording": "demo", "task_indices": [0, 5], "name": "x"}
    )
    assert resp2.status_code == 400


def test_merge_partial_failure_rolls_back(
    recordings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault between the DELETE and the INSERT must leave BOTH originals — the
    whole merge is one transaction, so a partial failure never half-merges."""
    from screencap.pipeline_state import PipelineLedger, TaskSegmentRow

    rec = _make_recording(recordings_dir, "demo")
    ledger = PipelineLedger(rec / "recording.db")
    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=10.0, end_ts=20.0, name="agent A"),
        TaskSegmentRow(task_index=1, start_ts=20.0, end_ts=30.0, name="agent B"),
    ])

    # Inject a failure AFTER the DELETE (the allocator runs post-delete).
    def _boom(_conn):
        raise RuntimeError("simulated mid-merge failure")

    monkeypatch.setattr(ledger, "_next_user_task_index", _boom)

    with pytest.raises(RuntimeError):
        ledger.merge_task_segments([0, 1], name="Combined")

    # Both originals survive — the DELETE was rolled back with the failed txn.
    rows = {r.task_index: r for r in ledger.read_task_segments()}
    assert set(rows) == {0, 1}
    assert rows[0].name == "agent A"
    assert rows[1].name == "agent B"


# ---------------------------------------------------------------------------
# split — two segments at the point; split_ts outside the span → 400.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_split_produces_two_segments_at_point(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")
    created = await _post(
        "tasks.create",
        {"recording": "demo", "name": "Long task", "start_ts": 0.0, "end_ts": 10.0},
    )
    idx = created.json()["task"]["task_index"]

    resp = await _post(
        "tasks.split", {"recording": "demo", "task_index": idx, "split_ts": 4.0}
    )

    assert resp.status_code == 200, resp.text
    left_idx, right_idx = resp.json()["task_indices"]
    assert left_idx != right_idx

    rows = _rows_by_index(rec)
    # Exactly the two halves remain (the freed high index may be reallocated for
    # the left half — benign, since the response returns the authoritative pair).
    assert set(rows) == {left_idx, right_idx}
    left, right = rows[left_idx], rows[right_idx]
    assert (left.start_ts, left.end_ts) == (0.0, 4.0)
    assert (right.start_ts, right.end_ts) == (4.0, 10.0)
    # Both halves are user-owned (survive the next agent pass) and inherit the name.
    assert left.source == "user" and right.source == "user"
    assert left.name == "Long task" and right.name == "Long task"
    # The original single [0,10] span no longer exists.
    assert not any(r.end_ts - r.start_ts == 10.0 for r in rows.values())


@pytest.mark.asyncio
async def test_split_ts_outside_span_returns_400(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")
    created = await _post(
        "tasks.create",
        {"recording": "demo", "name": "T", "start_ts": 0.0, "end_ts": 10.0},
    )
    idx = created.json()["task"]["task_index"]

    # Beyond the end, and exactly on a boundary (not STRICTLY inside) → 400.
    for split_ts in (20.0, 0.0, 10.0):
        resp = await _post(
            "tasks.split",
            {"recording": "demo", "task_index": idx, "split_ts": split_ts},
        )
        assert resp.status_code == 400, split_ts
        assert resp.json()["error"] == errors.INVALID_REQUEST

    # The original is untouched by a rejected split.
    assert idx in _rows_by_index(rec)


# ---------------------------------------------------------------------------
# Boundary / error surface shared across the verbs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_traversal_recording_name_returns_invalid_name(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _make_recording(recordings_dir, "demo")
    resp = await _post(
        "tasks.create",
        {"recording": "../evil", "name": "x", "start_ts": 1.0, "end_ts": 2.0},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == errors.INVALID_NAME


@pytest.mark.asyncio
async def test_malformed_body_returns_invalid_request(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    _make_recording(recordings_dir, "demo")
    # Missing required fields (start_ts / end_ts) → pydantic ValidationError → 400.
    resp = await _post("tasks.create", {"recording": "demo", "name": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"] == errors.INVALID_REQUEST

    # A non-dict JSON body → 400 as well.
    transport = ASGITransport(app=build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp2 = await client.post("/v0/tasks.create", json=[1, 2, 3])
    assert resp2.status_code == 400
    assert resp2.json()["error"] == errors.INVALID_REQUEST


@pytest.mark.asyncio
async def test_unknown_recording_returns_404(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_peer(monkeypatch)
    # No recording dir created.
    resp = await _post(
        "tasks.create",
        {"recording": "ghost", "name": "x", "start_ts": 1.0, "end_ts": 2.0},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == errors.RECORDING_NOT_FOUND


@pytest.mark.asyncio
async def test_audit_recorded_on_failure_path(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every exit path audits — a malformed request records outcome=invalid_request."""
    _patch_peer(monkeypatch)
    _make_recording(recordings_dir, "demo")
    resp = await _post("tasks.update", {"recording": "demo", "task_index": 999})
    assert resp.status_code == 400

    record = json.loads(audit_log_at.read_text(encoding="utf-8").splitlines()[-1])
    assert record["verb"] == "tasks.update"
    assert record["outcome"] == errors.INVALID_REQUEST
    assert record["peer_pid"] == 4321


# ---------------------------------------------------------------------------
# The load-bearing invariant: a later agent re-segmentation preserves all five
# curation outcomes (create / update-edited / merge / split), no UNIQUE collision.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_resegmentation_preserves_user_outcomes(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap.pipeline_state import TaskSegmentRow

    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")

    # Seed agent rows the user will curate.
    _ledger(rec).replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=10.0, name="agent 0"),
        TaskSegmentRow(task_index=1, start_ts=10.0, end_ts=20.0, name="agent 1"),
        TaskSegmentRow(task_index=2, start_ts=20.0, end_ts=30.0, name="agent 2"),
        TaskSegmentRow(task_index=3, start_ts=30.0, end_ts=40.0, name="agent 3"),
    ])

    # create a user task
    created = await _post(
        "tasks.create",
        {"recording": "demo", "name": "user made", "start_ts": 100.0, "end_ts": 110.0},
    )
    user_idx = created.json()["task"]["task_index"]
    # edit agent 0 (re-homes + edited=1)
    edited = await _post(
        "tasks.update", {"recording": "demo", "task_index": 0, "name": "edited 0"}
    )
    edited_idx = edited.json()["task_index"]
    # merge agent 1 + 2
    merged = await _post(
        "tasks.merge",
        {"recording": "demo", "task_indices": [1, 2], "name": "merged 1+2"},
    )
    merged_idx = merged.json()["task_index"]
    # split agent 3
    split = await _post(
        "tasks.split", {"recording": "demo", "task_index": 3, "split_ts": 35.0}
    )
    split_left, split_right = split.json()["task_indices"]

    # A fresh agent re-segmentation over brand-new spans (contiguous low 0..1) —
    # must NOT raise a UNIQUE collision and must leave every curated row intact.
    _ledger(rec).replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=15.0, name="fresh 0"),
        TaskSegmentRow(task_index=1, start_ts=15.0, end_ts=40.0, name="fresh 1"),
    ])

    rows = _rows_by_index(rec)
    # All five curated outcomes survived.
    assert rows[user_idx].name == "user made" and rows[user_idx].source == "user"
    assert rows[edited_idx].name == "edited 0" and rows[edited_idx].edited is True
    assert rows[merged_idx].name == "merged 1+2"
    assert rows[split_left].name == "agent 3" and rows[split_right].name == "agent 3"
    # The fresh agent set re-populated the low range.
    fresh = sorted(
        (r for r in rows.values() if r.source == "agent" and not r.edited),
        key=lambda r: r.task_index,
    )
    assert [r.name for r in fresh] == ["fresh 0", "fresh 1"]
    assert [r.task_index for r in fresh] == [0, 1]


# ---------------------------------------------------------------------------
# update — U8 orphan guard parity with create: a span re-bound onto already-
# EVICTED footage is rejected (else mark_edited=True would PROTECT the orphan).
# ---------------------------------------------------------------------------

_NOW = 1_000_000_000.0
_DAY = 86400.0
# chunk 0's capture window (later evicted); chunk 1's window (survives).
_C0 = (_NOW - 40 * _DAY, _NOW - 40 * _DAY + 900)
_C1 = (_NOW - 39 * _DAY, _NOW - 39 * _DAY + 900)


def _make_recording_with_evicted_chunk(base: Path, name: str = "demo") -> Path:
    """Create a recording whose chunk 0 is EVICTED and chunk 1 survives.

    Two LOCAL_DONE chunks with capture-bound manifests; chunk 0 is aged past the
    30-day window and evicted (media + manifest unlinked → EVICTED), chunk 1 stays
    fresh. This gives the U8 orphan guard real eviction evidence: a span over
    ``_C0`` maps ONLY to gone footage (orphan), a span over ``_C1`` is playable.
    Mirrors the tests/test_retention_ambient.py orphan-guard fixture.
    """
    import sqlite3

    from screencap.engine.db import create_db, crud
    from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema
    from screencap.retention import evict_recording

    rec_dir = base / name
    rec_dir.mkdir(parents=True)
    db_path = rec_dir / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(session, {
        "timestamp": _NOW - 40 * _DAY,
        "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080, "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5, "double_click_distance_pixels": 5.0,
    })
    session.close()
    engine.dispose()

    for i, (cs, ce) in enumerate((_C0, _C1)):
        (rec_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * (1024 * 1024))
        (rec_dir / f"chunk_{i:04d}_manifest.json").write_text(
            json.dumps({"format_version": 2, "chunk_index": i,
                        "chunk_start": cs, "chunk_end": ce})
        )

    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    for i in range(2):
        ledger.seed_chunk(i)
        ledger.mark_staged(i)
        ledger.mark_local_done(i)
    ledger.freeze_chunks_expected(2)

    # Age chunk 0 past the 30-day cutoff (evicts); chunk 1 stays fresh (survives).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE pipeline_chunk_state SET updated_at=? WHERE chunk_index=0",
                     (_NOW - 40 * _DAY,))
        conn.execute("UPDATE pipeline_chunk_state SET updated_at=? WHERE chunk_index=1",
                     (_NOW - 1 * _DAY,))
        conn.commit()
    finally:
        conn.close()

    evict_recording(
        rec_dir,
        policy=ResolvedPolicy(
            destination=Destination.LOCAL,
            retention_policy=RetentionPolicy.DELETE_AFTER_DAYS,
            params={"days": 30},
        ),
        ledger=ledger, now=_NOW,
    )
    assert not (rec_dir / "chunk_0000.mp4").exists(), "precondition: chunk 0 evicted"
    assert (rec_dir / "chunk_0001.mp4").exists(), "precondition: chunk 1 survives"
    return rec_dir


async def _create_live_task(recording: str = "demo") -> int:
    """Create a task over the SURVIVING chunk 1 and return its task_index."""
    resp = await _post("tasks.create", {
        "recording": recording, "name": "Live task",
        "start_ts": _C1[0] + 100, "end_ts": _C1[0] + 300,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["task"]["task_index"]


@pytest.mark.asyncio
async def test_update_rebound_onto_evicted_footage_is_rejected(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-bounding a task's span onto EVICTED footage → 400, same as create.

    The deterministic update-path hole: create runs the orphan guard, update did
    not — so a task re-bound onto footage the 30-day retention rolled off persisted
    an unplayable (and, via mark_edited=True, PROTECTED) task.
    """
    _patch_peer(monkeypatch)
    rec = _make_recording_with_evicted_chunk(recordings_dir, "demo")
    idx = await _create_live_task("demo")

    resp = await _post("tasks.update", {
        "recording": "demo", "task_index": idx,
        "start_ts": _C0[0] + 100, "end_ts": _C0[0] + 300,  # onto evicted chunk 0
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == errors.INVALID_REQUEST  # SAME shape as create

    # Nothing was written: the row keeps its original (live) span.
    row = _rows_by_index(rec)[idx]
    assert (row.start_ts, row.end_ts) == (_C1[0] + 100, _C1[0] + 300)


@pytest.mark.asyncio
async def test_update_rebound_onto_live_footage_succeeds(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal re-bound onto SURVIVING footage still succeeds (guard fails open)."""
    _patch_peer(monkeypatch)
    rec = _make_recording_with_evicted_chunk(recordings_dir, "demo")
    idx = await _create_live_task("demo")

    resp = await _post("tasks.update", {
        "recording": "demo", "task_index": idx,
        "start_ts": _C1[0] + 200, "end_ts": _C1[0] + 400,  # still within live chunk 1
    })
    assert resp.status_code == 200, resp.text

    row = _rows_by_index(rec)[resp.json()["task_index"]]
    assert (row.start_ts, row.end_ts) == (_C1[0] + 200, _C1[0] + 400)


@pytest.mark.asyncio
async def test_update_name_only_is_unaffected_by_orphan_guard(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name-only update (no span bound) never triggers the orphan guard."""
    _patch_peer(monkeypatch)
    rec = _make_recording_with_evicted_chunk(recordings_dir, "demo")
    idx = await _create_live_task("demo")

    resp = await _post("tasks.update", {
        "recording": "demo", "task_index": idx, "name": "Renamed",
    })
    assert resp.status_code == 200, resp.text

    row = _rows_by_index(rec)[resp.json()["task_index"]]
    assert row.name == "Renamed"
    # Span untouched — the guard did not run, and did not alter the bounds.
    assert (row.start_ts, row.end_ts) == (_C1[0] + 100, _C1[0] + 300)


# ---------------------------------------------------------------------------
# U7 diary coherence: merge/split keep block_id + thread membership coherent.
# Privacy-marked (the composite-thread invariant is a diary-privacy-adjacent
# contract) + Vision-free — CI runs only ``pytest -m privacy``.
# ---------------------------------------------------------------------------


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_merge_same_thread_preserves_thread_and_mints_fresh_block(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merging two blocks of the SAME (recording, thread) preserves the thread;
    the merged survivor gets a coherent freshly-minted ``block_id``."""
    from screencap.pipeline_state import TaskSegmentRow

    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")
    _ledger(rec).replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=10.0, end_ts=20.0, name="A",
                       block_id="blk-a", thread_id="thr-1"),
        TaskSegmentRow(task_index=1, start_ts=20.0, end_ts=30.0, name="B",
                       block_id="blk-b", thread_id="thr-1"),
    ])

    resp = await _post(
        "tasks.merge",
        {"recording": "demo", "task_indices": [0, 1], "name": "Combined"},
    )
    assert resp.status_code == 200, resp.text
    survivor = _rows_by_index(rec)[resp.json()["task_index"]]

    # Thread preserved; block_id freshly minted (the consolidator's blk_ shape),
    # not reused from either original.
    assert survivor.thread_id == "thr-1"
    assert survivor.block_id and survivor.block_id.startswith("blk_")
    assert survivor.block_id not in {"blk-a", "blk-b"}


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_merge_mixed_thread_membership_drops_to_no_thread(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge spanning a threaded block and an un-threaded one does NOT invent a
    thread — the survivor drops to no thread (unanimity rule)."""
    from screencap.pipeline_state import TaskSegmentRow

    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")
    _ledger(rec).replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=10.0, end_ts=20.0, name="A",
                       block_id="blk-a", thread_id="thr-1"),
        TaskSegmentRow(task_index=1, start_ts=20.0, end_ts=30.0, name="B",
                       block_id="blk-b"),  # no thread
    ])

    resp = await _post(
        "tasks.merge",
        {"recording": "demo", "task_indices": [0, 1], "name": "Combined"},
    )
    assert resp.status_code == 200, resp.text
    survivor = _rows_by_index(rec)[resp.json()["task_index"]]
    assert survivor.thread_id is None  # no fabricated thread
    assert survivor.block_id and survivor.block_id.startswith("blk_")


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_split_mints_fresh_blocks_inherits_thread_leaves_neighbor(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Split assigns a FRESH ``block_id`` to each half and both inherit the
    original's thread; the neighboring block's id + thread membership are
    untouched."""
    from screencap.pipeline_state import TaskSegmentRow

    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")
    _ledger(rec).replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=10.0, name="X",
                       block_id="blk-x", thread_id="thr-1"),
        TaskSegmentRow(task_index=1, start_ts=10.0, end_ts=20.0, name="neighbor",
                       block_id="blk-n", thread_id="thr-1"),
    ])

    resp = await _post(
        "tasks.split", {"recording": "demo", "task_index": 0, "split_ts": 4.0}
    )
    assert resp.status_code == 200, resp.text
    left_idx, right_idx = resp.json()["task_indices"]
    rows = _rows_by_index(rec)
    left, right = rows[left_idx], rows[right_idx]

    # Each half: its OWN fresh block_id (neither the original's nor the other's),
    # both inheriting the split block's thread.
    assert left.block_id.startswith("blk_") and right.block_id.startswith("blk_")
    assert left.block_id != right.block_id
    assert {left.block_id, right.block_id}.isdisjoint({"blk-x", "blk-n"})
    assert left.thread_id == "thr-1" and right.thread_id == "thr-1"

    # The neighbor at index 1 was NOT rewritten — same block_id + thread.
    neighbor = rows[1]
    assert neighbor.block_id == "blk-n" and neighbor.thread_id == "thr-1"


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_audit_never_records_block_name_or_bullets(
    recordings_dir: Path, audit_log_at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create / rename / merge / split leaves NO block name (free-text label) or
    bullet text anywhere in the audit log — only verb + outcome + peer."""
    from screencap.pipeline_state import TaskSegmentRow

    _patch_peer(monkeypatch)
    rec = _make_recording(recordings_dir, "demo")
    _ledger(rec).replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=10.0, name="agent 0"),
        TaskSegmentRow(task_index=1, start_ts=10.0, end_ts=20.0, name="agent 1"),
        TaskSegmentRow(task_index=2, start_ts=20.0, end_ts=30.0, name="agent 2"),
        TaskSegmentRow(task_index=3, start_ts=30.0, end_ts=40.0, name="agent 3"),
    ])

    secret = "SECRET-PROJECT-ORION"
    # Each verb carries the sensitive label in a distinct field; disjoint indices
    # so no verb re-homes a row another verb then targets.
    assert (await _post("tasks.create", {
        "recording": "demo", "name": f"{secret}-create",
        "start_ts": 100.0, "end_ts": 110.0,
    })).status_code == 200
    assert (await _post("tasks.update", {
        "recording": "demo", "task_index": 3, "name": f"{secret}-rename",
    })).status_code == 200
    assert (await _post("tasks.merge", {
        "recording": "demo", "task_indices": [0, 1], "name": f"{secret}-merge",
    })).status_code == 200
    assert (await _post("tasks.split", {
        "recording": "demo", "task_index": 2, "split_ts": 25.0,
        "name_left": f"{secret}-left", "name_right": f"{secret}-right",
    })).status_code == 200

    log_text = audit_log_at.read_text(encoding="utf-8")
    assert secret not in log_text  # no free-text label ever accrues in the log

    records = [json.loads(line) for line in log_text.splitlines() if line.strip()]
    verbs = {r["verb"] for r in records}
    assert {"tasks.create", "tasks.update", "tasks.merge", "tasks.split"} <= verbs
    # Every one of these audited exits was a success recording peer + outcome only.
    curation = [r for r in records if r["verb"].startswith("tasks.")]
    assert curation and all(r["outcome"] == "ok" for r in curation)
    assert all(r["peer_pid"] == 4321 for r in curation)
