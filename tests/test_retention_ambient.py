"""Ambient retention window + kept-task-span protection (SCR-214 U8).

Covers plan U8 (R9/R13/R14, KTD5):

* AE4 — ambient chunks older than the 30-day window evict, EXCEPT a chunk
  covered by a KEPT (user-curated) task span, which is retained even when older.
  The span → chunk mapping uses per-chunk CAPTURE-time bounds (the chunk
  manifest's ``chunk_start`` / ``chunk_end``), NEVER the ledger ``updated_at`` (a
  state-transition time): the isolating test bumps ``updated_at`` off the capture
  window and proves the kept chunk still survives.
* The user-curated-only semantic — an agent-only span (``source='agent' AND
  edited=0``) does NOT pin its footage (it evicts), so the agent labelling the
  active day can't defeat retention.
* Whole-chunk granularity — a chunk PARTIALLY under a kept span is kept whole.
* Ambient resolves to ``DELETE_AFTER_DAYS`` (default 30, user-configurable), and
  the window is FROZEN per recording — a changed default affects FUTURE ambient
  days only.
* Non-ambient ``keep_forever`` recordings still no-op (regression).
* The ``tasks.create`` orphan guard — creating a task over already-EVICTED
  footage returns 400 and persists nothing.

Retention runs over the capture-time enforcement boundary (which chunks survive),
so the capture-path-shaped assertions are marked ``@pytest.mark.privacy`` to ride
the CI privacy lane; they are Vision-free.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_NOW = 1_000_000_000.0
_DAY = 86400.0


# ---------------------------------------------------------------------------
# Fixture — an ambient recording dir with the ledger schema, LOCAL_DONE chunks,
# and per-chunk manifests carrying real capture-time bounds (chunk_start/end).
# Mirrors tests/test_retention.py::_make_recording but WRITES the manifests with
# chunk_start/chunk_end so the capture-time mapping has something to read.
# ---------------------------------------------------------------------------


def _make_ambient_recording(
    tmp_path: Path, *, windows: list[tuple[float, float]], name: str = "ambient-20260101",
):
    """Create recording.db + ledger + LOCAL_DONE chunks with capture-bound manifests.

    ``windows[i]`` is chunk ``i``'s ``(chunk_start, chunk_end)`` capture window,
    written into ``chunk_000i_manifest.json``. Returns ``(rec_dir, ledger)``.
    """
    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    rec_dir = tmp_path / name
    rec_dir.mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(session, {
        "timestamp": _NOW - 40 * _DAY,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    session.close()
    engine.dispose()

    for i, (cs, ce) in enumerate(windows):
        (rec_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * (1024 * 1024))
        (rec_dir / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 256)
        (rec_dir / f"events_{i:04d}.jsonl").write_text('{"_meta": 1}\n')
        (rec_dir / f"chunk_{i:04d}_manifest.json").write_text(
            json.dumps({"format_version": 2, "chunk_index": i,
                        "chunk_start": cs, "chunk_end": ce})
        )

    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    for i in range(len(windows)):
        ledger.seed_chunk(i)
        ledger.mark_staged(i)
        ledger.mark_local_done(i)
    ledger.freeze_chunks_expected(len(windows))
    return rec_dir, ledger


def _age_chunk(ledger, idx: int, *, updated_at: float) -> None:
    """Force chunk ``idx``'s ledger ``updated_at`` (the age signal) to a value."""
    import sqlite3

    conn = sqlite3.connect(str(ledger._db_path))
    try:
        conn.execute(
            "UPDATE pipeline_chunk_state SET updated_at=? WHERE chunk_index=?",
            (updated_at, idx),
        )
        conn.commit()
    finally:
        conn.close()


def _add_user_task(ledger, start: float, end: float, name: str = "Kept task") -> int:
    """Insert a KEPT (source='user') task span; returns its task_index."""
    from screencap.pipeline_state import TaskSegmentRow

    return ledger.insert_task_segment(
        TaskSegmentRow(task_index=0, start_ts=start, end_ts=end, name=name)
    )


def _add_agent_task(ledger, start: float, end: float, name: str = "Agent task") -> None:
    """Insert an ephemeral agent span (source='agent', edited=0) — does NOT pin."""
    from screencap.pipeline_state import TASK_SOURCE_AGENT, TaskSegmentRow

    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=start, end_ts=end, name=name,
                       source=TASK_SOURCE_AGENT, edited=False)
    ])


def _present(rec_dir: Path, idx: int) -> bool:
    return (rec_dir / f"chunk_{idx:04d}.mp4").exists()


def _local_delete_after_days(days: int = 30):
    from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy

    return ResolvedPolicy(
        destination=Destination.LOCAL,
        retention_policy=RetentionPolicy.DELETE_AFTER_DAYS,
        params={"days": days},
    )


# ===========================================================================
# AE4 — 30-day roll-off, but a KEPT task retains its chunk, mapped via
# CAPTURE-time bounds (never updated_at).
# ===========================================================================


class TestKeptTaskProtectsChunk:
    @pytest.mark.privacy
    def test_kept_task_chunk_retained_via_capture_bounds_not_updated_at(self, tmp_path):
        """A kept task over chunk 0 retains it even though it is age-old.

        The isolation: chunk 0's ``updated_at`` is bumped to a DIFFERENT time than
        its capture window (still past the 30-day cutoff, so it IS an age
        candidate). The kept task lives in the CAPTURE window, not near
        ``updated_at`` — so a protector that (wrongly) mapped spans via
        ``updated_at`` would fail to protect chunk 0 and evict it. Correct
        capture-time mapping keeps it.
        """
        from screencap.retention import evict_recording

        # chunk 0: capture window 40 days old; chunk 1: 39 days old. Both candidates.
        c0 = (_NOW - 40 * _DAY, _NOW - 40 * _DAY + 900)
        c1 = (_NOW - 39 * _DAY, _NOW - 39 * _DAY + 900)
        rec_dir, ledger = _make_ambient_recording(tmp_path, windows=[c0, c1])

        # updated_at deliberately OFF the capture window (31 days old — a later
        # re-transition), still past the 30d cutoff so age-selection picks it.
        _age_chunk(ledger, 0, updated_at=_NOW - 31 * _DAY)
        _age_chunk(ledger, 1, updated_at=_NOW - 39 * _DAY)

        # Kept USER task inside chunk 0's CAPTURE window (not near updated_at).
        _add_user_task(ledger, c0[0] + 100, c0[0] + 300)

        report = evict_recording(
            rec_dir, policy=_local_delete_after_days(30), ledger=ledger, now=_NOW,
        )

        assert _present(rec_dir, 0), "kept-task chunk must survive (capture-time mapping)"
        assert not _present(rec_dir, 1), "unprotected old chunk must evict"
        assert report.evicted_indices == [1]

    @pytest.mark.privacy
    def test_capture_bounds_read_from_manifest_not_updated_at(self, tmp_path):
        """``chunk_capture_bounds`` returns the manifest window, independent of updated_at."""
        from screencap.retention import chunk_capture_bounds

        c0 = (_NOW - 40 * _DAY, _NOW - 40 * _DAY + 900)
        rec_dir, ledger = _make_ambient_recording(tmp_path, windows=[c0])
        _age_chunk(ledger, 0, updated_at=_NOW)  # updated_at == now, far from capture

        assert chunk_capture_bounds(rec_dir, 0) == c0
        # A missing / evicted manifest reads as unknown (None).
        assert chunk_capture_bounds(rec_dir, 5) is None


# ===========================================================================
# The user-curated-only semantic — an agent-only span does NOT pin footage.
# ===========================================================================


class TestAgentSpanDoesNotProtect:
    @pytest.mark.privacy
    def test_agent_only_task_chunk_evicts(self, tmp_path):
        from screencap.retention import evict_recording

        c0 = (_NOW - 40 * _DAY, _NOW - 40 * _DAY + 900)
        rec_dir, ledger = _make_ambient_recording(tmp_path, windows=[c0])
        _age_chunk(ledger, 0, updated_at=_NOW - 40 * _DAY)

        # Agent (ephemeral, edited=0) span over chunk 0 — must NOT pin it.
        _add_agent_task(ledger, c0[0] + 100, c0[0] + 300)

        report = evict_recording(
            rec_dir, policy=_local_delete_after_days(30), ledger=ledger, now=_NOW,
        )

        assert not _present(rec_dir, 0), "agent-only span must NOT protect its chunk"
        assert report.evicted_indices == [0]


# ===========================================================================
# Whole-chunk granularity — a chunk partially under a kept span is kept whole.
# ===========================================================================


class TestPartialOverlapKeptWhole:
    @pytest.mark.privacy
    def test_chunk_partially_overlapping_kept_span_is_kept_whole(self, tmp_path):
        from screencap.retention import evict_recording

        c0 = (_NOW - 40 * _DAY, _NOW - 40 * _DAY + 900)
        c1 = (_NOW - 39 * _DAY, _NOW - 39 * _DAY + 900)
        rec_dir, ledger = _make_ambient_recording(tmp_path, windows=[c0, c1])
        _age_chunk(ledger, 0, updated_at=_NOW - 40 * _DAY)
        _age_chunk(ledger, 1, updated_at=_NOW - 39 * _DAY)

        # Kept span touches only the last 100s of chunk 0 (partial overlap).
        _add_user_task(ledger, c0[1] - 100, c0[1] + 50)

        report = evict_recording(
            rec_dir, policy=_local_delete_after_days(30), ledger=ledger, now=_NOW,
        )

        assert _present(rec_dir, 0), "partial overlap must keep the whole chunk"
        assert not _present(rec_dir, 1)
        assert report.evicted_indices == [1]


# ===========================================================================
# Ambient default resolution + the FROZEN-per-recording window (future-only).
# ===========================================================================


class TestAmbientRetentionResolution:
    def test_ambient_resolves_to_delete_after_days_30_by_default(self, monkeypatch):
        from screencap.pipeline_policy import Destination, RetentionPolicy, resolve_policy

        monkeypatch.delenv("SCREENCAP_AMBIENT_RETENTION_DAYS", raising=False)
        monkeypatch.delenv("SCREENCAP_RETENTION_POLICY", raising=False)
        resolved = resolve_policy(destination="local", ambient=True)

        assert resolved.destination is Destination.LOCAL
        assert resolved.retention_policy is RetentionPolicy.DELETE_AFTER_DAYS
        assert resolved.params == {"days": 30}

    def test_non_ambient_keeps_keep_forever_default(self, monkeypatch):
        from screencap.pipeline_policy import RetentionPolicy, resolve_policy

        monkeypatch.delenv("SCREENCAP_RETENTION_POLICY", raising=False)
        monkeypatch.delenv("SCREENCAP_AUTO_DELETE", raising=False)
        resolved = resolve_policy(destination="local", ambient=False)

        assert resolved.retention_policy is RetentionPolicy.KEEP_FOREVER

    def test_ambient_window_is_user_configurable(self, monkeypatch):
        from screencap.pipeline_policy import resolve_policy

        monkeypatch.setenv("SCREENCAP_AMBIENT_RETENTION_DAYS", "60")
        resolved = resolve_policy(destination="local", ambient=True)
        assert resolved.params == {"days": 60}

    def test_changed_default_affects_future_days_only_frozen_per_recording(
        self, monkeypatch
    ):
        """A day's frozen policy keeps its window even after the default changes.

        The resolved policy is serialized into ``.recording_intent`` at start;
        reconstructing it (``from_dict``) never re-reads config, so a later default
        change cannot retroactively re-window an existing ambient day.
        """
        from screencap.pipeline_policy import ResolvedPolicy, resolve_policy

        monkeypatch.setenv("SCREENCAP_AMBIENT_RETENTION_DAYS", "30")
        frozen = resolve_policy(destination="local", ambient=True).to_dict()
        assert frozen["retention_params"] == {"days": 30}

        # The operator later widens the default for FUTURE days.
        monkeypatch.setenv("SCREENCAP_AMBIENT_RETENTION_DAYS", "90")
        # The already-frozen day is unaffected — reconstructed straight from disk.
        reloaded = ResolvedPolicy.from_dict(frozen)
        assert reloaded is not None
        assert reloaded.params == {"days": 30}
        # A NEW ambient day picks up the widened window.
        assert resolve_policy(destination="local", ambient=True).params == {"days": 90}

    @pytest.mark.privacy
    def test_resolved_ambient_policy_drives_eviction_end_to_end(self, tmp_path, monkeypatch):
        """The default-resolved ambient policy evicts a >30-day-old chunk."""
        from screencap.retention import evict_recording

        monkeypatch.delenv("SCREENCAP_AMBIENT_RETENTION_DAYS", raising=False)
        from screencap.pipeline_policy import resolve_policy

        c0 = (_NOW - 40 * _DAY, _NOW - 40 * _DAY + 900)
        rec_dir, ledger = _make_ambient_recording(tmp_path, windows=[c0])
        _age_chunk(ledger, 0, updated_at=_NOW - 40 * _DAY)

        policy = resolve_policy(destination="local", ambient=True)
        report = evict_recording(rec_dir, policy=policy, ledger=ledger, now=_NOW)

        assert not _present(rec_dir, 0)
        assert report.evicted_indices == [0]


# ===========================================================================
# Regression — non-ambient keep_forever recordings still no-op.
# ===========================================================================


class TestNonAmbientKeepForeverNoOp:
    def test_keep_forever_evicts_nothing_even_when_old(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        c0 = (_NOW - 40 * _DAY, _NOW - 40 * _DAY + 900)
        c1 = (_NOW - 39 * _DAY, _NOW - 39 * _DAY + 900)
        rec_dir, ledger = _make_ambient_recording(tmp_path, windows=[c0, c1], name="rec")
        _age_chunk(ledger, 0, updated_at=_NOW - 40 * _DAY)
        _age_chunk(ledger, 1, updated_at=_NOW - 39 * _DAY)

        policy = ResolvedPolicy(
            destination=Destination.LOCAL,
            retention_policy=RetentionPolicy.KEEP_FOREVER,
            params={},
        )
        report = evict_recording(rec_dir, policy=policy, ledger=ledger, now=_NOW)

        assert _present(rec_dir, 0) and _present(rec_dir, 1)
        assert report.evicted_indices == []


# ===========================================================================
# tasks.create orphan guard — a task over already-EVICTED footage → 400.
# ===========================================================================


def _patch_peer(monkeypatch) -> None:
    from screencap.daemon import provenance

    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: provenance.PeerDescriptor(
            pid=4321, path="/usr/local/bin/screencap",
            classification=provenance.STARTED_BY_CLI,
        ),
    )


async def _post(verb: str, payload: dict):
    from httpx import ASGITransport, AsyncClient

    from screencap.daemon.app import build_app

    transport = ASGITransport(app=build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/v0/{verb}", json=payload)


class TestTasksCreateOrphanGuard:
    @pytest.mark.asyncio
    async def test_create_over_evicted_footage_is_rejected(self, tmp_path, monkeypatch):
        from screencap.daemon import audit_log

        monkeypatch.setattr(audit_log, "_audit_log_path", lambda: tmp_path / "audit.log")
        _patch_peer(monkeypatch)
        recs = tmp_path / "recordings"
        recs.mkdir()
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recs))

        # chunk 0 capture window [.., ..]; chunk 1 later. Both LOCAL_DONE.
        c0 = (_NOW - 40 * _DAY, _NOW - 40 * _DAY + 900)
        c1 = (_NOW - 39 * _DAY, _NOW - 39 * _DAY + 900)
        rec_dir, ledger = _make_ambient_recording(recs, windows=[c0, c1], name="demo")
        _age_chunk(ledger, 0, updated_at=_NOW - 40 * _DAY)  # old → evicts
        _age_chunk(ledger, 1, updated_at=_NOW - 1 * _DAY)   # fresh → survives

        # Actually evict chunk 0 (unlinks media + manifest, sets EVICTED).
        from screencap.retention import evict_recording

        evict_recording(
            rec_dir, policy=_local_delete_after_days(30), ledger=ledger, now=_NOW,
        )
        assert not _present(rec_dir, 0), "precondition: chunk 0 evicted"
        assert _present(rec_dir, 1), "precondition: chunk 1 survives"

        # Create a task over chunk 0's (now-evicted) window → orphan → 400.
        resp = await _post("tasks.create", {
            "recording": "demo", "name": "Over gone footage",
            "start_ts": c0[0] + 100, "end_ts": c0[0] + 300,
        })
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_request"

        # Nothing persisted.
        from screencap.pipeline_state import PipelineLedger

        assert PipelineLedger(rec_dir / "recording.db").read_task_segments() == []

    @pytest.mark.asyncio
    async def test_create_over_surviving_footage_succeeds(self, tmp_path, monkeypatch):
        from screencap.daemon import audit_log

        monkeypatch.setattr(audit_log, "_audit_log_path", lambda: tmp_path / "audit.log")
        _patch_peer(monkeypatch)
        recs = tmp_path / "recordings"
        recs.mkdir()
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recs))

        c0 = (_NOW - 40 * _DAY, _NOW - 40 * _DAY + 900)
        c1 = (_NOW - 39 * _DAY, _NOW - 39 * _DAY + 900)
        rec_dir, ledger = _make_ambient_recording(recs, windows=[c0, c1], name="demo")
        _age_chunk(ledger, 0, updated_at=_NOW - 40 * _DAY)  # old → evicts
        _age_chunk(ledger, 1, updated_at=_NOW - 1 * _DAY)   # fresh → survives
        from screencap.retention import evict_recording

        evict_recording(
            rec_dir, policy=_local_delete_after_days(30), ledger=ledger, now=_NOW,
        )

        # A task over the SURVIVING chunk 1 is allowed.
        resp = await _post("tasks.create", {
            "recording": "demo", "name": "Over live footage",
            "start_ts": c1[0] + 100, "end_ts": c1[0] + 300,
        })
        assert resp.status_code == 200

        from screencap.pipeline_state import PipelineLedger

        rows = PipelineLedger(rec_dir / "recording.db").read_task_segments()
        assert len(rows) == 1 and rows[0].source == "user"
