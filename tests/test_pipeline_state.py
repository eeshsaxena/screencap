"""Characterization tests for the U1 on-disk per-chunk state ledger.

These tests are written *before* the schema (characterization-first per
the U1 plan) and port the five data-loss prevention rules from
``docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md``
into on-disk invariants:

  1. Track the full universe of expected work (closed set, no
     survivorship bias) -> ``test_closed_set_*`` / ``test_survivorship_*``.
  2. Distinguish "disabled" from "succeeded" (tri-state, only UPLOADED
     evicts) -> ``test_disabled_*``.
  3. Never delete local files without confirming remote existence now
     -> ``test_eviction_*``.
  4. Test degraded paths, not just happy paths -> crash / refused-eviction
     scenarios throughout.
  5. The completeness signal is derived from completeness evidence
     (frozen ``chunks_expected``) -> ``test_frozen_count_*`` / finalize.

The ledger is the on-disk replacement for ``chunk_processor`` 's
in-memory ``_chunk_results`` dict; later units (U2/U5/U7/U8/U9) read and
advance it.  Run with::

    PYTHONPATH=src python -m pytest tests/test_pipeline_state.py -q
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from screencap import pipeline_state as ps


# ---------------------------------------------------------------------------
# Fixtures: build a minimal recording.db the way the engine writer would,
# then let the ledger module create its own table on the existing file.
# ---------------------------------------------------------------------------

def _make_recording_db(db_path: Path) -> None:
    """Create a recording.db with a single recording row via the real engine.

    Uses the real ``create_db`` so the ``recording`` table (and the
    ``chunks_expected`` column added by U1) match production exactly.
    """
    from screencap.engine.db import create_db, crud

    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(
        session,
        {
            "timestamp": 1000.0,
            "monitor_width": 1920,
            "monitor_height": 1080,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5,
            "platform": "darwin",
            "task_description": "ledger-fixture",
        },
    )
    session.close()
    engine.dispose()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "recording.db"
    _make_recording_db(p)
    # Ensure the ledger table exists on the (already-created) DB — this is
    # the "create on an EXISTING recording.db" contract.
    ps.ensure_pipeline_state_schema(p)
    return p


@pytest.fixture
def ledger(db_path: Path) -> ps.PipelineLedger:
    return ps.PipelineLedger(db_path)


# ---------------------------------------------------------------------------
# Schema / migration on EXISTING databases (load-bearing per U1).
# ---------------------------------------------------------------------------

class TestSchemaMigration:
    def test_table_created_on_existing_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='pipeline_chunk_state'"
            ).fetchone()
            assert row is not None, "pipeline_chunk_state must exist on the DB"
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(pipeline_chunk_state)"
            ).fetchall()}
            for required in (
                "recording_id", "chunk_index", "lifecycle",
                "stages_state", "scrub_state", "upload_state", "evict_state",
            ):
                assert required in cols, f"missing column {required}"
        finally:
            conn.close()

    def test_chunks_expected_column_added_to_recording(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(recording)"
            ).fetchall()}
            assert "chunks_expected" in cols
        finally:
            conn.close()

    def test_ensure_is_idempotent(self, db_path: Path) -> None:
        # Second (and third) ensure must not raise on the existing table.
        ps.ensure_pipeline_state_schema(db_path)
        ps.ensure_pipeline_state_schema(db_path)

    def test_migrate_schema_creates_table_via_get_session(self, tmp_path: Path) -> None:
        """The broad open seam (get_session_for_path -> _migrate_schema)
        must also create the ledger table on an existing DB, so the
        terminal stage / engine writer pick it up without an explicit
        ensure call."""
        from screencap.engine.db import _migrate_schema

        p = tmp_path / "recording.db"
        _make_recording_db(p)
        # Drop the table to simulate a pre-U1 recording.db.
        conn = sqlite3.connect(str(p))
        conn.execute("DROP TABLE IF EXISTS pipeline_chunk_state")
        conn.commit()
        conn.close()

        _migrate_schema(str(p))

        conn = sqlite3.connect(str(p))
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='pipeline_chunk_state'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Seeding: closed set at rotation; never appended on completion.
# ---------------------------------------------------------------------------

class TestClosedSetSeeding:
    def test_seed_creates_one_pending_row_per_chunk(self, ledger: ps.PipelineLedger) -> None:
        for i in range(5):
            ledger.seed_chunk(i)
        rows = ledger.all_chunks()
        assert len(rows) == 5
        for r in rows:
            assert r.lifecycle == ps.Lifecycle.PENDING
            assert r.upload_state == ps.UploadState.PENDING

    def test_seed_is_idempotent_per_index(self, ledger: ps.PipelineLedger) -> None:
        ledger.seed_chunk(0)
        ledger.mark_staged(0)
        # Re-seeding the same index must NOT reset an already-advanced row.
        ledger.seed_chunk(0)
        row = ledger.get_chunk(0)
        assert row.lifecycle == ps.Lifecycle.STAGED

    def test_chunks_expected_frozen_once(self, ledger: ps.PipelineLedger) -> None:
        for i in range(5):
            ledger.seed_chunk(i)
        ledger.freeze_chunks_expected(5)
        assert ledger.chunks_expected() == 5
        # Freezing again with a different value is rejected (frozen-once).
        with pytest.raises(ps.LedgerError):
            ledger.freeze_chunks_expected(4)
        assert ledger.chunks_expected() == 5


# ---------------------------------------------------------------------------
# Happy path: PENDING -> STAGED -> (SCRUBBED -> UPLOADED | LOCAL_DONE)
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_five_chunks_advance_to_uploaded(self, ledger: ps.PipelineLedger) -> None:
        for i in range(5):
            ledger.seed_chunk(i)
        ledger.freeze_chunks_expected(5)
        for i in range(5):
            ledger.mark_staged(i)
            ledger.mark_scrubbed(i)
            ledger.mark_uploaded(i)
        for i in range(5):
            r = ledger.get_chunk(i)
            assert r.lifecycle == ps.Lifecycle.UPLOADED
            assert r.upload_state == ps.UploadState.UPLOADED
        assert ledger.all_uploaded()

    def test_local_done_path(self, ledger: ps.PipelineLedger) -> None:
        """A local-only recording stages then finishes LOCAL_DONE without
        scrub or upload — local artifacts stay rich."""
        for i in range(3):
            ledger.seed_chunk(i)
            ledger.mark_staged(i)
            ledger.mark_local_done(i)
        for i in range(3):
            r = ledger.get_chunk(i)
            assert r.lifecycle == ps.Lifecycle.LOCAL_DONE
        # LOCAL_DONE is "complete" for a local recording, but NOT uploaded.
        assert not ledger.all_uploaded()
        assert ledger.all_complete()


# ---------------------------------------------------------------------------
# Survivorship: a rotated-but-unprocessed chunk stays PENDING; closed-set
# "all uploaded" returns False.
# ---------------------------------------------------------------------------

class TestSurvivorship:
    def test_unprocessed_chunk_stays_pending(self, ledger: ps.PipelineLedger) -> None:
        for i in range(5):
            ledger.seed_chunk(i)
        # Only 4 of 5 reach upload; chunk 4 was rotated but never processed.
        for i in range(4):
            ledger.mark_staged(i)
            ledger.mark_uploaded(i)
        assert ledger.get_chunk(4).lifecycle == ps.Lifecycle.PENDING
        # Closed-set query: missing entries are IMPOSSIBLE, so a forgotten
        # chunk shows up as PENDING and blocks the all-uploaded gate.
        assert not ledger.all_uploaded()

    def test_all_uploaded_false_on_empty_ledger(self, ledger: ps.PipelineLedger) -> None:
        # No chunks seeded at all -> the writer likely failed; never True.
        assert not ledger.all_uploaded()


# ---------------------------------------------------------------------------
# Frozen count under eviction: evicting 3 of 5 leaves chunks_expected==5.
# ---------------------------------------------------------------------------

class TestFrozenCount:
    def test_eviction_does_not_shrink_expected(self, ledger: ps.PipelineLedger) -> None:
        for i in range(5):
            ledger.seed_chunk(i)
            ledger.mark_staged(i)
            ledger.mark_uploaded(i)
        ledger.freeze_chunks_expected(5)

        # Evict 3 of the 5 uploaded chunks.
        unlinked: list[int] = []
        for i in range(3):
            ledger.begin_eviction(i, remote_exists=lambda idx=i: True)
            ledger.commit_eviction(i, unlink=lambda idx=i: unlinked.append(idx))

        assert unlinked == [0, 1, 2]
        # Frozen count is stable — finalize still gates on 5, not on the
        # 2 surviving local files (no glob-based count).
        assert ledger.chunks_expected() == 5
        assert ledger.finalize_gate_satisfied()

    def test_finalize_gate_uses_frozen_count_not_glob(self, ledger: ps.PipelineLedger) -> None:
        for i in range(5):
            ledger.seed_chunk(i)
            ledger.mark_staged(i)
            ledger.mark_uploaded(i)
        ledger.freeze_chunks_expected(5)
        # All 5 terminal-uploaded -> gate satisfied even before any eviction.
        assert ledger.finalize_gate_satisfied()


# ---------------------------------------------------------------------------
# Disabled != success: uploads-off marks SKIPPED; eviction refused.
# ---------------------------------------------------------------------------

class TestDisabledIsNotSuccess:
    def test_skipped_is_distinct_from_uploaded(self, ledger: ps.PipelineLedger) -> None:
        ledger.seed_chunk(0)
        ledger.mark_staged(0)
        ledger.mark_skipped(0)
        r = ledger.get_chunk(0)
        assert r.lifecycle == ps.Lifecycle.SKIPPED
        assert r.upload_state == ps.UploadState.SKIPPED
        assert r.upload_state != ps.UploadState.UPLOADED

    def test_skipped_blocks_all_uploaded(self, ledger: ps.PipelineLedger) -> None:
        ledger.seed_chunk(0)
        ledger.mark_staged(0)
        ledger.mark_uploaded(0)
        ledger.seed_chunk(1)
        ledger.mark_staged(1)
        ledger.mark_skipped(1)
        # One SKIPPED chunk means NOT all uploaded.
        assert not ledger.all_uploaded()

    def test_eviction_refused_for_skipped(self, ledger: ps.PipelineLedger) -> None:
        ledger.seed_chunk(0)
        ledger.mark_staged(0)
        ledger.mark_skipped(0)
        with pytest.raises(ps.EvictionRefused):
            ledger.begin_eviction(0, remote_exists=lambda: True)

    def test_eviction_refused_for_failed(self, ledger: ps.PipelineLedger) -> None:
        ledger.seed_chunk(0)
        ledger.mark_staged(0)
        ledger.mark_failed(0)
        with pytest.raises(ps.EvictionRefused):
            ledger.begin_eviction(0, remote_exists=lambda: True)


# ---------------------------------------------------------------------------
# Eviction ordering + crash-safe irreversible transitions.
# ---------------------------------------------------------------------------

class TestEvictionOrdering:
    def test_evict_pending_committed_before_unlink(self, ledger: ps.PipelineLedger) -> None:
        ledger.seed_chunk(0)
        ledger.mark_staged(0)
        ledger.mark_uploaded(0)

        observed: list[str] = []

        def fake_unlink() -> None:
            # When the unlink fires, the row MUST already be EVICT_PENDING
            # on disk so a crash mid-unlink resumes from EVICT_PENDING.
            r = ledger.get_chunk(0)
            observed.append(r.evict_state.value)

        ledger.begin_eviction(0, remote_exists=lambda: True)
        assert ledger.get_chunk(0).evict_state == ps.EvictState.EVICT_PENDING
        ledger.commit_eviction(0, unlink=fake_unlink)

        assert observed == [ps.EvictState.EVICT_PENDING.value]
        assert ledger.get_chunk(0).evict_state == ps.EvictState.EVICTED

    def test_eviction_refused_when_remote_missing(self, ledger: ps.PipelineLedger) -> None:
        ledger.seed_chunk(0)
        ledger.mark_staged(0)
        ledger.mark_uploaded(0)
        # Re-confirm-remote precondition: a stale historical UPLOADED is not
        # enough; a fresh stat that comes back False refuses the deletion.
        with pytest.raises(ps.EvictionRefused):
            ledger.begin_eviction(0, remote_exists=lambda: False)
        # Row untouched — still UPLOADED, file presumed present (safe).
        assert ledger.get_chunk(0).evict_state == ps.EvictState.NONE
        assert ledger.get_chunk(0).upload_state == ps.UploadState.UPLOADED

    def test_resume_eviction_from_evict_pending(self, ledger: ps.PipelineLedger) -> None:
        ledger.seed_chunk(0)
        ledger.mark_staged(0)
        ledger.mark_uploaded(0)
        # Simulate a crash AFTER EVICT_PENDING was committed but BEFORE
        # EVICTED. begin_eviction commits EVICT_PENDING; we then drop the
        # ledger handle without committing EVICTED.
        ledger.begin_eviction(0, remote_exists=lambda: True)

        # Fresh ledger handle (process restart) finds the interrupted row.
        resumed = ps.PipelineLedger(ledger._db_path)
        pending = resumed.chunks_in_state(evict=ps.EvictState.EVICT_PENDING)
        assert [r.chunk_index for r in pending] == [0]
        # Resume: re-confirm remote, then finish unlink + EVICTED.
        unlinked: list[int] = []
        resumed.commit_eviction(0, unlink=lambda: unlinked.append(0))
        assert unlinked == [0]
        assert resumed.get_chunk(0).evict_state == ps.EvictState.EVICTED


# ---------------------------------------------------------------------------
# Crash mid-transition: a partially-written transition reads back as the
# prior committed state, never a torn intermediate state.
# ---------------------------------------------------------------------------

class TestCrashSafety:
    def test_failed_upload_leaves_prior_state(self, ledger: ps.PipelineLedger) -> None:
        ledger.seed_chunk(0)
        ledger.mark_staged(0)
        # An upload attempt that raises mid-commit must not leave a torn
        # row. mark_uploaded uses a single transaction; if the callback
        # raises BEFORE the transition is requested, the row stays STAGED.
        with pytest.raises(RuntimeError):
            ledger.mark_uploaded(
                0, on_confirm=lambda: (_ for _ in ()).throw(RuntimeError("gcs down")),
            )
        # Re-read: never a half-written UPLOADED — the prior committed
        # STAGED stands.
        r = ledger.get_chunk(0)
        assert r.lifecycle == ps.Lifecycle.STAGED
        assert r.upload_state == ps.UploadState.PENDING

    def test_uploaded_only_after_confirm(self, ledger: ps.PipelineLedger) -> None:
        ledger.seed_chunk(0)
        ledger.mark_staged(0)
        order: list[str] = []
        ledger.mark_uploaded(0, on_confirm=lambda: order.append("confirm"))
        order.append("write")
        # Confirm callback runs (and must succeed) before the UPLOADED row
        # is written.
        assert order == ["confirm", "write"]
        assert ledger.get_chunk(0).upload_state == ps.UploadState.UPLOADED


# ---------------------------------------------------------------------------
# Integration: state survives a FRESH process open of the same DB.
# ---------------------------------------------------------------------------

class TestProcessRestart:
    def test_state_persists_across_new_connection(self, db_path: Path) -> None:
        writer = ps.PipelineLedger(db_path)
        for i in range(5):
            writer.seed_chunk(i)
            writer.mark_staged(i)
            writer.mark_uploaded(i)
        writer.freeze_chunks_expected(5)

        # Fresh handle (mimics the terminal-stage process opening the same
        # recording.db after the engine writer seeded it).
        reader = ps.PipelineLedger(db_path)
        assert reader.chunks_expected() == 5
        assert reader.all_uploaded()
        assert [r.chunk_index for r in reader.all_chunks()] == [0, 1, 2, 3, 4]

    def test_reconstruct_from_disk_after_partial(self, db_path: Path) -> None:
        """The reconstruct-from-disk contract (R3/R9): a fresh process
        re-reads on-disk state and knows exactly which chunks still need
        work."""
        writer = ps.PipelineLedger(db_path)
        for i in range(5):
            writer.seed_chunk(i)
        for i in range(3):
            writer.mark_staged(i)
            writer.mark_uploaded(i)
        # chunk 3 staged but not uploaded; chunk 4 still PENDING.
        writer.mark_staged(3)

        reader = ps.PipelineLedger(db_path)
        pending = reader.chunks_needing_upload()
        assert sorted(r.chunk_index for r in pending) == [3, 4]


# ---------------------------------------------------------------------------
# Mapping from the legacy ChunkStatus enum (pinned in U1).
# ---------------------------------------------------------------------------

class TestChunkStatusMapping:
    def test_pinned_mapping(self) -> None:
        from screencap.chunk_processor import ChunkStatus

        assert ps.from_chunk_status(ChunkStatus.EMITTED) == ps.UploadState.UPLOADED
        assert ps.from_chunk_status(ChunkStatus.NETWORK_SKIPPED) == ps.UploadState.SKIPPED
        assert ps.from_chunk_status(ChunkStatus.NETWORK_INCOMPLETE) == ps.UploadState.FAILED
        assert ps.from_chunk_status(ChunkStatus.FAILED) == ps.UploadState.FAILED
        assert ps.from_chunk_status(ChunkStatus.PENDING) == ps.UploadState.PENDING

    def test_only_uploaded_is_evictable_mapping(self) -> None:
        # The mapping must keep the "only EMITTED/UPLOADED evicts" rule:
        # every other ChunkStatus maps to a non-evictable upload_state.
        from screencap.chunk_processor import ChunkStatus

        evictable = {
            s for s in ChunkStatus
            if ps.from_chunk_status(s) == ps.UploadState.UPLOADED
        }
        assert evictable == {ChunkStatus.EMITTED}
