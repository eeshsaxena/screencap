"""Tests for screencap.range_delete — the U8 irreversible range-delete core.

TEST-FIRST on irreversible data destruction (mirrors tests/test_retention.py's
discipline). Every failure path must delete NOTHING or report exactly what it
deleted; recording.db is NEVER deleted (tombstone rule). The scenarios below
cover the plan's U8 test list:

  - chunk rounding + reported extent
  - transcript flat files (transcript_<idx>.* AND bare transcript.txt) no longer
    contain in-range text after delete (fixtures seeded so the assertion can't
    pass vacuously)
  - a seeded <name>-scrubbed sibling gets the range purged
  - deleting a PENDING chunk in a cloud recording -> USER_DELETED -> terminal
    stage converges without a retry loop / stuck sentinel
  - a chunk flushing between preview and confirm is not deleted without re-confirm
  - crash between transaction and unlink -> reconciliation completes the unlink
  - live chunk excluded and reported
  - multi-recording range
  - kept-task span deleted when user-requested
  - legacy NULL-origin purged_interval row classifies as policy
  - recording.db is preserved (tombstone)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.privacy

_BASE = 1000.0          # unix seconds of chunk 0 start
_DUR = 900.0            # chunk duration (default 900 s)


# ---------------------------------------------------------------------------
# Fixture — a recording dir with the ledger, chunk media + manifests carrying
# chunk_start/chunk_end, screenshots (flat + DB rows), window/action events, and
# per-chunk + whole-recording transcripts. Optionally a <name>-scrubbed sibling.
# ---------------------------------------------------------------------------


def _chunk_bounds(idx: int) -> tuple[float, float]:
    return _BASE + idx * _DUR, _BASE + (idx + 1) * _DUR


def _speech_token(idx: int) -> str:
    return f"chunkspeech{idx:04d}xyzzy"


def _make_recording(
    tmp_path: Path,
    *,
    n_chunks: int,
    name: str = "rec",
    destination: str = "local",
    with_scrubbed: bool = False,
    started_at: float = _BASE,
):
    """Create a realistic per-chunk recording; return (rec_dir, ledger)."""
    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    rec_dir = tmp_path / name
    (rec_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(session, {
        "timestamp": started_at,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    session.close()
    engine.dispose()

    # .recording_intent so retention / skip_intervals can resolve the policy.
    (rec_dir / ".recording_intent").write_text(
        json.dumps({"version": 2, "destination": destination, "privacy_mode": "public"})
    )
    (rec_dir / ".recording_id").write_text(name)

    conn = sqlite3.connect(str(db_path))
    try:
        for i in range(n_chunks):
            cs, ce = _chunk_bounds(i)
            (rec_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 4096)
            (rec_dir / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 256)
            (rec_dir / f"events_{i:04d}.jsonl").write_text('{"_meta": 1}\n')
            (rec_dir / f"chunk_{i:04d}_manifest.json").write_text(
                json.dumps({"chunk_start": cs, "chunk_end": ce})
            )
            # Per-chunk transcript flat files carrying a UNIQUE searchable token.
            (rec_dir / f"transcript_{i:04d}.txt").write_text(
                f"chunk {i} speech {_speech_token(i)}\n", encoding="utf-8"
            )
            (rec_dir / f"transcript_{i:04d}.json").write_text(
                json.dumps({"text": _speech_token(i), "segments": []}), encoding="utf-8"
            )
            # One window_event + action_event + screenshot mid-chunk.
            mid = cs + 10.0
            conn.execute(
                "INSERT INTO window_event (timestamp, app_bundle_id, app_name, title) "
                "VALUES (?, ?, ?, ?)",
                (cs, "com.example.app", "Example", f"win {i}"),
            )
            conn.execute(
                "INSERT INTO action_event (timestamp, window_event_timestamp, name) "
                "VALUES (?, ?, ?)",
                (mid, cs, "click"),
            )
            rel = f"screenshots/{mid:.6f}.jpg"
            (rec_dir / rel).write_bytes(b"\xff" * 128)
            conn.execute(
                "INSERT INTO screenshot (timestamp, image_path) VALUES (?, ?)",
                (mid, rel),
            )
        conn.commit()
    finally:
        conn.close()

    # Bare whole-recording transcript.txt = concat of every chunk's speech.
    bare = "\n".join(
        f"chunk {i} speech {_speech_token(i)}" for i in range(n_chunks)
    )
    (rec_dir / "transcript.txt").write_text(bare + "\n", encoding="utf-8")

    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    for i in range(n_chunks):
        ledger.seed_chunk(i)
        ledger.mark_staged(i)
    ledger.freeze_chunks_expected(n_chunks)

    if with_scrubbed:
        scrubbed = rec_dir.parent / f"{name}-scrubbed"
        (scrubbed / "screenshots").mkdir(parents=True, exist_ok=True)
        for i in range(n_chunks):
            cs, _ = _chunk_bounds(i)
            (scrubbed / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 4096)
            (scrubbed / f"transcript_{i:04d}.txt").write_text(
                f"chunk {i} speech {_speech_token(i)}\n", encoding="utf-8"
            )
            mid = cs + 10.0
            (scrubbed / f"screenshots/{mid:.6f}.jpg").write_bytes(b"\xff" * 128)
        (scrubbed / "transcript.txt").write_text(bare + "\n", encoding="utf-8")

    return rec_dir, ledger


def _ms(sec: float) -> int:
    return int(round(sec * 1000))


def _chunk_present(rec_dir: Path, idx: int) -> bool:
    return (rec_dir / f"chunk_{idx:04d}.mp4").exists()


def _transcript_files_present(rec_dir: Path, idx: int) -> bool:
    return (rec_dir / f"transcript_{idx:04d}.txt").exists()


# ===========================================================================
# Resolution / dry-run preview.
# ===========================================================================


class TestResolveRange:
    def test_rounds_to_chunk_bounds_and_reports_extent(self, tmp_path):
        from screencap.range_delete import resolve_range

        rec_dir, _ = _make_recording(tmp_path, n_chunks=3)
        # A range strictly inside chunk 1 → rounds out to chunk 1's whole window.
        cs, ce = _chunk_bounds(1)
        preview = resolve_range(
            _ms(cs + 100), _ms(cs + 200), recordings_dir=tmp_path,
        )
        assert len(preview.recordings) == 1
        plan = preview.recordings[0]
        assert plan.chunk_indices == [1]
        assert plan.rounded_start_ms == _ms(cs)
        assert plan.rounded_end_ms == _ms(ce)

    def test_multi_chunk_range(self, tmp_path):
        from screencap.range_delete import resolve_range

        _make_recording(tmp_path, n_chunks=4)
        c1s, _ = _chunk_bounds(1)
        _, c2e = _chunk_bounds(2)
        preview = resolve_range(_ms(c1s + 5), _ms(c2e - 5), recordings_dir=tmp_path)
        assert preview.recordings[0].chunk_indices == [1, 2]

    def test_multi_recording_range(self, tmp_path):
        from screencap.range_delete import resolve_range

        _make_recording(tmp_path, n_chunks=2, name="recA")
        _make_recording(tmp_path, n_chunks=2, name="recB")
        # A range covering chunk 0 of every recording (they share the base grid).
        cs, ce = _chunk_bounds(0)
        preview = resolve_range(_ms(cs + 1), _ms(ce - 1), recordings_dir=tmp_path)
        names = {p.recording for p in preview.recordings}
        assert names == {"recA", "recB"}

    def test_live_chunk_excluded_and_reported(self, tmp_path):
        from screencap.range_delete import resolve_range

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2)
        # Simulate a live in-flight chunk: seeded (PENDING) with NO manifest.
        ledger.seed_chunk(2)
        cs, ce = _chunk_bounds(2)
        # A range that reaches into the live chunk's wall-clock window.
        preview = resolve_range(_ms(_BASE + 5), _ms(ce + 100), recordings_dir=tmp_path)
        plan = preview.recordings[0]
        assert 2 not in plan.chunk_indices
        assert 2 in plan.excluded_live_chunks

    def test_dry_run_deletes_nothing(self, tmp_path):
        from screencap.range_delete import resolve_range

        rec_dir, _ = _make_recording(tmp_path, n_chunks=3)
        cs, _ = _chunk_bounds(1)
        resolve_range(_ms(cs), _ms(cs + 10), recordings_dir=tmp_path)
        for i in range(3):
            assert _chunk_present(rec_dir, i)
            assert _transcript_files_present(rec_dir, i)


# ===========================================================================
# Execution — the irreversible deletes.
# ===========================================================================


class TestExecuteDelete:
    def test_deletes_covered_chunk_artifacts(self, tmp_path):
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=3)
        cs, _ = _chunk_bounds(1)
        preview = resolve_range(_ms(cs + 5), _ms(cs + 50), recordings_dir=tmp_path)
        report = execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        assert not report.reconfirm_required
        # Chunk 1 gone; 0 and 2 preserved.
        assert not _chunk_present(rec_dir, 1)
        assert _chunk_present(rec_dir, 0)
        assert _chunk_present(rec_dir, 2)
        # Manifest, audio, events, transcripts for chunk 1 gone.
        assert not (rec_dir / "audio_0001.flac").exists()
        assert not (rec_dir / "chunk_0001_manifest.json").exists()
        assert not (rec_dir / "transcript_0001.txt").exists()
        assert not (rec_dir / "transcript_0001.json").exists()

    def test_recording_db_never_deleted(self, tmp_path):
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, _ = _make_recording(tmp_path, n_chunks=2)
        cs, ce = _chunk_bounds(0)
        preview = resolve_range(_ms(cs), _ms(ce), recordings_dir=tmp_path)
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        assert (rec_dir / "recording.db").exists()

    def test_transcripts_no_longer_contain_in_range_text(self, tmp_path):
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, _ = _make_recording(tmp_path, n_chunks=3)
        token = _speech_token(1)
        # Precondition (guards vacuous pass): the token IS present before delete.
        assert token in (rec_dir / "transcript_0001.txt").read_text()
        assert token in (rec_dir / "transcript.txt").read_text()

        cs, _ = _chunk_bounds(1)
        preview = resolve_range(_ms(cs + 5), _ms(cs + 50), recordings_dir=tmp_path)
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        # Per-chunk file gone; the bare whole-recording transcript regenerated
        # from surviving chunks no longer carries the deleted chunk's speech.
        assert not (rec_dir / "transcript_0001.txt").exists()
        bare = (rec_dir / "transcript.txt").read_text()
        assert token not in bare
        # Surviving chunks' speech is retained.
        assert _speech_token(0) in bare
        assert _speech_token(2) in bare

    def test_scrubbed_sibling_range_purged(self, tmp_path):
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, _ = _make_recording(tmp_path, n_chunks=3, with_scrubbed=True)
        scrubbed = tmp_path / "rec-scrubbed"
        token = _speech_token(1)
        assert token in (scrubbed / "transcript_0001.txt").read_text()

        cs, _ = _chunk_bounds(1)
        preview = resolve_range(_ms(cs + 5), _ms(cs + 50), recordings_dir=tmp_path)
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        assert not (scrubbed / "chunk_0001.mp4").exists()
        assert not (scrubbed / "transcript_0001.txt").exists()
        assert token not in (scrubbed / "transcript.txt").read_text()

    def test_screenshots_in_range_unlinked(self, tmp_path):
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, _ = _make_recording(tmp_path, n_chunks=2)
        cs, _ = _chunk_bounds(0)
        shot = rec_dir / "screenshots" / f"{cs + 10.0:.6f}.jpg"
        assert shot.exists()
        preview = resolve_range(_ms(cs), _ms(cs + 50), recordings_dir=tmp_path)
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        assert not shot.exists()

    def test_event_rows_deleted_in_range(self, tmp_path):
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, _ = _make_recording(tmp_path, n_chunks=2)
        cs, _ = _chunk_bounds(0)
        preview = resolve_range(_ms(cs), _ms(cs + 50), recordings_dir=tmp_path)
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        conn = sqlite3.connect(str(rec_dir / "recording.db"))
        try:
            we = conn.execute(
                "SELECT COUNT(*) FROM window_event WHERE timestamp >= ? AND timestamp < ?",
                _chunk_bounds(0),
            ).fetchone()[0]
            ss = conn.execute(
                "SELECT COUNT(*) FROM screenshot WHERE timestamp >= ? AND timestamp < ?",
                _chunk_bounds(0),
            ).fetchone()[0]
        finally:
            conn.close()
        assert we == 0
        assert ss == 0

    def test_ledger_chunk_becomes_user_deleted(self, tmp_path):
        from screencap.pipeline_state import Lifecycle, PipelineLedger
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, _ = _make_recording(tmp_path, n_chunks=2)
        cs, _ = _chunk_bounds(0)
        preview = resolve_range(_ms(cs), _ms(cs + 50), recordings_dir=tmp_path)
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        row = PipelineLedger(rec_dir / "recording.db").get_chunk(0)
        assert row.lifecycle == Lifecycle.USER_DELETED

    def test_kept_task_span_deleted_when_user_requested(self, tmp_path):
        from screencap.pipeline_state import TaskSegmentRow
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2)
        cs, ce = _chunk_bounds(0)
        # A KEPT (user) task pinned over chunk 0 — retention would protect it.
        ledger.insert_task_segment(
            TaskSegmentRow(task_index=0, start_ts=cs + 1, end_ts=ce - 1, name="kept")
        )
        preview = resolve_range(_ms(cs), _ms(cs + 50), recordings_dir=tmp_path)
        # User delete OVERRIDES kept-task protection (explicit intent wins).
        assert preview.recordings[0].chunk_indices == [0]
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        assert not _chunk_present(rec_dir, 0)


# ===========================================================================
# TOCTOU — a chunk flushing between preview and confirm.
# ===========================================================================


class TestNoTOCTOU:
    def test_flushed_live_chunk_requires_reconfirm(self, tmp_path):
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2)
        # Live chunk 2: seeded, no manifest → excluded at preview.
        ledger.seed_chunk(2)
        cs2, ce2 = _chunk_bounds(2)
        preview = resolve_range(_ms(_BASE + 5), _ms(ce2 + 100), recordings_dir=tmp_path)
        assert 2 not in preview.recordings[0].chunk_indices

        # Between preview and confirm, chunk 2 flushes: manifest + media appear.
        (rec_dir / "chunk_0002.mp4").write_bytes(b"\x00" * 4096)
        (rec_dir / "chunk_0002_manifest.json").write_text(
            json.dumps({"chunk_start": cs2, "chunk_end": ce2})
        )
        ledger.mark_staged(2)

        report = execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        # The resolved set changed (chunk 2 now overlaps) → abort, delete nothing.
        assert report.reconfirm_required
        assert _chunk_present(rec_dir, 2)
        # Nothing from the original confirmed set was deleted either.
        assert _chunk_present(rec_dir, 0)
        assert _chunk_present(rec_dir, 1)


# ===========================================================================
# Crash reconciliation — a crash between the transaction and the unlink.
# ===========================================================================


class TestReconciliation:
    def test_reconcile_completes_outstanding_unlink(self, tmp_path):
        from screencap import range_delete

        rec_dir, _ = _make_recording(tmp_path, n_chunks=2)
        cs, ce = _chunk_bounds(0)
        # Run ONLY the transaction phase (simulate a crash before unlink): the
        # ledger row is USER_DELETED + purged_interval origin='user' committed,
        # but chunk 0's media / transcripts are still on disk.
        range_delete._commit_delete_transaction(
            rec_dir, [0], cs, ce,
        )
        assert _chunk_present(rec_dir, 0)  # bytes still present (crash)

        # Reconciliation on restart completes the unlink under the flock.
        range_delete.reconcile_user_deletes(recordings_dir=tmp_path)
        assert not _chunk_present(rec_dir, 0)
        assert not (rec_dir / "transcript_0000.txt").exists()


# ===========================================================================
# Legacy NULL-origin purged_interval classifies as policy (both readers).
# ===========================================================================


class TestUserDeletedConvergence:
    """Deleting a not-yet-uploaded chunk in a CLOUD recording must not strand the
    completeness sentinel: USER_DELETED is satisfied-by-deletion, the stage runner
    skips it, and the terminal stage never re-uploads it (no retry loop)."""

    def test_pending_chunk_delete_satisfies_sentinel_gate(self, tmp_path):
        from screencap.pipeline_state import Lifecycle, PipelineLedger
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2, destination="cloud")
        # Chunk 0 UPLOADED, chunk 1 left PENDING (never uploaded) — a cloud
        # recording that would STRAND the sentinel forever without USER_DELETED.
        ledger.mark_scrubbed(0)
        ledger.mark_uploaded(0)
        assert not ledger.finalize_gate_satisfied()  # chunk 1 PENDING blocks it

        cs, _ = _chunk_bounds(1)
        preview = resolve_range(_ms(cs + 5), _ms(cs + 50), recordings_dir=tmp_path)
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        led = PipelineLedger(rec_dir / "recording.db")
        row1 = led.get_chunk(1)
        assert row1.lifecycle == Lifecycle.USER_DELETED
        # The gate is now satisfied — chunk 0 UPLOADED + chunk 1 satisfied-by-deletion.
        assert led.finalize_gate_satisfied()
        assert led.all_complete()

    def test_terminal_stage_skips_user_deleted_upload(self, tmp_path):
        from screencap.range_delete import execute_delete, resolve_range
        from screencap.terminal_stage import _mark_uploaded_chunks, _media_upload_indices

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2, destination="cloud")
        ledger.mark_scrubbed(0)
        ledger.mark_uploaded(0)
        cs, _ = _chunk_bounds(1)
        preview = resolve_range(_ms(cs + 5), _ms(cs + 50), recordings_dir=tmp_path)
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        from screencap.pipeline_state import PipelineLedger

        led = PipelineLedger(rec_dir / "recording.db")
        # The deleted chunk 1 is never a media-upload candidate...
        assert 1 not in _media_upload_indices(led, set())
        # ...and a mark-uploaded pass (remote "present") never marks it uploaded
        # (its media is gone) — so there is no retry loop against a deleted chunk.
        _mark_uploaded_chunks(
            rec_dir, led, failed_chunks=set(), remote_exists=lambda _idx: True,
        )
        from screencap.pipeline_state import Lifecycle

        assert led.get_chunk(1).lifecycle == Lifecycle.USER_DELETED

    def test_stage_runner_skips_user_deleted(self, tmp_path):
        from screencap.pipeline_stages import PipelineStageRunner
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2)
        cs, _ = _chunk_bounds(0)
        preview = resolve_range(_ms(cs), _ms(cs + 50), recordings_dir=tmp_path)
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        from screencap.pipeline_state import PipelineLedger

        led = PipelineLedger(rec_dir / "recording.db")
        calls: list[int] = []

        def _transcribe(idx):
            calls.append(idx)
            return None

        runner = PipelineStageRunner(
            rec_dir,
            transcribe=_transcribe,
            export_events=lambda i, s, e: rec_dir / f"events_{i:04d}.jsonl",
            manifest=lambda i, s, e: rec_dir / f"chunk_{i:04d}_manifest.json",
            ledger=led,
        )
        cs0, _ = _chunk_bounds(0)
        assert runner.run_chunk(0, cs0, cs0 + _DUR) is None  # USER_DELETED → skipped
        assert calls == []  # no step invoked for the deleted chunk


class TestOriginClassification:
    def test_legacy_null_origin_is_policy(self, tmp_path):
        from screencap.backfill.skip_intervals import (
            RETROACTIVE_PURGE,
            _read_purged_intervals,
        )

        rec_dir, _ = _make_recording(tmp_path, n_chunks=1)
        db = rec_dir / "recording.db"
        conn = sqlite3.connect(str(db))
        try:
            # A pre-migration purged_interval table WITHOUT the origin column.
            conn.execute(
                "CREATE TABLE purged_interval (id INTEGER PRIMARY KEY, "
                "start_ts REAL NOT NULL, end_ts REAL, disabled_at REAL)"
            )
            conn.execute(
                "INSERT INTO purged_interval (start_ts, end_ts, disabled_at) "
                "VALUES (?, ?, ?)",
                (_BASE + 100, _BASE + 200, None),
            )
            conn.commit()
        finally:
            conn.close()
        intervals = _read_purged_intervals(db)
        assert len(intervals) == 1
        # NULL/absent origin → policy (RETROACTIVE_PURGE), never user-deleted.
        assert intervals[0].reason == RETROACTIVE_PURGE

    def test_user_origin_classifies_distinctly(self, tmp_path):
        from screencap.backfill.skip_intervals import (
            USER_RANGE_DELETE,
            _read_purged_intervals,
        )
        from screencap.range_delete import execute_delete, resolve_range

        rec_dir, _ = _make_recording(tmp_path, n_chunks=2)
        cs, _ = _chunk_bounds(0)
        preview = resolve_range(_ms(cs), _ms(cs + 50), recordings_dir=tmp_path)
        execute_delete(
            preview.start_ms, preview.end_ms, preview.resolved_map(),
            recordings_dir=tmp_path,
        )
        intervals = _read_purged_intervals(rec_dir / "recording.db")
        reasons = {iv.reason for iv in intervals}
        assert USER_RANGE_DELETE in reasons
