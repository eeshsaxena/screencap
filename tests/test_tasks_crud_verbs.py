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
