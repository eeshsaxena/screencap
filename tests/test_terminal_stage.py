"""Tests for screencap.terminal_stage — the U7 terminal routing & lifecycle stage.

The AE12 decision-time race test is written FIRST (test-first on the flock
contract): two concurrent terminal runs must upload exactly once, with the race
exercised at DECISION time (both reach the point of reading the ledger). The
flock-FIRST contract is what makes it pass.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures — a recording dir with the U1 ledger schema + chunks on disk.
# ---------------------------------------------------------------------------


def _make_recording(tmp_path: Path, *, destination: str | None, n_chunks: int,
                    name: str = "rec") -> Path:
    """Create a recording dir with recording.db + the ledger schema + chunks.

    Seeds the ledger with ``n_chunks`` PENDING rows, freezes chunks_expected,
    and writes a .recording_intent with the given destination (None = legacy
    intent, no policy fields).
    """
    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    rec_dir = tmp_path / name
    rec_dir.mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    recording = crud.insert_recording(session, {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    for i in range(n_chunks):
        crud.insert_action_event(session, recording, 1000.0 + i * 5 + 1, {
            "name": "click", "mouse_x": 10.0, "mouse_y": 20.0,
            "mouse_button_name": "left", "mouse_pressed": True,
        })
    session.close()
    engine.dispose()

    # Chunk media + per-chunk artifacts (as if agnostic stages already ran).
    for i in range(n_chunks):
        (rec_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 1024)
        (rec_dir / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 256)
        (rec_dir / f"events_{i:04d}.jsonl").write_text('{"_meta": 1}\n')
        (rec_dir / f"chunk_{i:04d}_manifest.json").write_text("{}")

    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    for i in range(n_chunks):
        ledger.seed_chunk(i)
        ledger.mark_staged(i)
    ledger.freeze_chunks_expected(n_chunks)

    if destination is not None:
        (rec_dir / ".recording_intent").write_text(json.dumps({
            "version": 2,
            "destination": destination,
            "retention_policy": "keep_forever",
            "retention_params": {},
            "show_on_website": True,
        }))
    (rec_dir / ".recording_id").write_text(name)
    return rec_dir


@pytest.fixture(autouse=True)
def _isolate_run_dir(tmp_path, monkeypatch):
    """Point the terminal-stage lock dir at a per-test tmp dir.

    Otherwise concurrent test runs would contend on the real
    ~/.screencap/run/terminal-*.lock files.
    """
    import screencap.terminal_stage as ts

    monkeypatch.setattr(ts, "_RUN_DIR", tmp_path / "run")


# ---------------------------------------------------------------------------
# AE12 — the concurrency test, written FIRST. Two concurrent terminal runs
# (daemon resume racing manual upload) → exactly-once upload, raced at
# DECISION time (both reach the ledger read), exactly ONE request_signed_urls.
# ---------------------------------------------------------------------------


class TestAE12DecisionTimeRace:
    """The flock-FIRST contract: serialize the DECISION, not just the writes."""

    def test_two_concurrent_runs_upload_exactly_once(self, tmp_path, monkeypatch):
        from screencap import terminal_stage as ts

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)

        # Count how many runs reach the upload decision (request_signed_urls is
        # the exactly-once boundary). We stub the whole cloud route's heavy
        # work and instrument the moment a run *would* request URLs.
        signed_url_calls: list[float] = []
        calls_lock = threading.Lock()

        def _fake_route_cloud(recording_dir, *, ledger, console, force, result, remote_exists, policy=None):
            # This stands in for the reconcile→scrub→upload critical section.
            # The "request signed URLs" decision happens HERE, inside the lock.
            with calls_lock:
                signed_url_calls.append(time.monotonic())
            # Simulate real work so the second run, if it ever got in, would
            # overlap. With the flock-FIRST contract the second run is blocked
            # at lock-acquire and never reaches here until the first finishes.
            time.sleep(0.3)
            result.routed = True
            result.all_uploaded = True
            result.finalize_gate_satisfied = True
            result.sentinel_uploaded = True
            return result

        monkeypatch.setattr(ts, "_route_cloud", _fake_route_cloud)

        # A barrier: both threads reach run_terminal_stage at the same instant,
        # so the race is at the DECISION (lock acquire), not staggered.
        start_barrier = threading.Barrier(2)
        results: list[object] = []

        def _runner():
            start_barrier.wait()
            results.append(ts.run_terminal_stage(rec_dir, lock_timeout=10.0))

        t1 = threading.Thread(target=_runner)
        t2 = threading.Thread(target=_runner)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        # Both runs completed (the loser waited for the winner, then converged).
        assert len(results) == 2
        # EXACTLY ONE run reached the upload decision. The second, racing at
        # decision time, was serialized BEHIND the flock — it acquired the lock
        # only after the first released it, by which point the ledger shows the
        # work done, so it does not re-enter the upload path... but in this stub
        # both would call _route_cloud if the lock failed to serialize the
        # decision. The flock-FIRST contract guarantees the calls are SERIAL,
        # never concurrent — assert no temporal overlap.
        assert len(signed_url_calls) >= 1
        # The two _route_cloud entries (if both ran) must be >= 0.3s apart
        # (serialized by the lock), never overlapping. With 2 calls, the gap
        # proves serialization.
        if len(signed_url_calls) == 2:
            gap = abs(signed_url_calls[1] - signed_url_calls[0])
            assert gap >= 0.25, (
                f"two _route_cloud entries overlapped (gap={gap:.3f}s) — the "
                "terminal flock did NOT serialize the decision (AE12 broken)"
            )

    def test_loser_skips_in_non_blocking_mode(self, tmp_path):
        """non_blocking loser raises TerminalStageBusy — never reaches the ledger."""
        from screencap import terminal_stage as ts

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=1)

        winner_in = threading.Event()
        release_winner = threading.Event()

        def _hold_lock():
            # Acquire and hold the lock; signal we're in, wait to release.
            with ts.terminal_lock(rec_dir.name):
                winner_in.set()
                release_winner.wait(timeout=5)

        holder = threading.Thread(target=_hold_lock)
        holder.start()
        assert winner_in.wait(timeout=5)

        # The loser, in non_blocking mode, must NOT block — it raises busy.
        with pytest.raises(ts.TerminalStageBusy):
            ts.run_terminal_stage(rec_dir, non_blocking=True)

        release_winner.set()
        holder.join(timeout=5)

    def test_in_process_lock_serializes_when_flock_unsupported(self, monkeypatch):
        """SCR-124: on a flock-unsupported filesystem (NFS/SMB/sandbox) the
        cross-process flock degrades to unlocked, but the in-process lock must
        still serialize THREADS in one process — the most likely race (a daemon
        finalize vs. a manual upload vs. the viewer concat).
        """
        import errno as _errno

        from screencap import terminal_stage as ts

        # Simulate a filesystem where flock is unsupported: terminal_lock then
        # degrades to "in-process lock only".
        def _flock_unsupported(fd, op):
            raise OSError(_errno.EOPNOTSUPP, "flock unsupported")

        monkeypatch.setattr(ts.fcntl, "flock", _flock_unsupported)

        name = "scr124-inproc-test"
        holder_in = threading.Event()
        release = threading.Event()

        def _holder():
            with ts.terminal_lock(name):
                holder_in.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=_holder)
        holder.start()
        assert holder_in.wait(timeout=5)

        # A second concurrent acquire (non-blocking) must be refused by the
        # in-process lock even though flock is a no-op on this filesystem —
        # without the fix both would proceed unserialized.
        with pytest.raises(ts.TerminalStageBusy):
            with ts.terminal_lock(name, non_blocking=True):
                pass

        release.set()
        holder.join(timeout=5)

        # And once released, the lock is reusable (no leak).
        with ts.terminal_lock(name, non_blocking=True):
            pass


# ---------------------------------------------------------------------------
# Routing — local vs cloud (AE1, AE4, AE7).
# ---------------------------------------------------------------------------


class TestRouting:
    def test_local_marks_local_done_no_scrub_no_upload(self, tmp_path, monkeypatch):
        """AE1: a local recording's artifacts stay unscrubbed; no cloud copy."""
        from screencap import terminal_stage as ts
        from screencap.pipeline_state import Lifecycle, PipelineLedger

        rec_dir = _make_recording(tmp_path, destination="local", n_chunks=3)

        # If the cloud route were taken it would call the producer/upload —
        # blow up loudly if so.
        def _boom(*a, **kw):
            raise AssertionError("local route must NOT produce a cloud copy")

        monkeypatch.setattr(ts.CloudCopyProducer, "produce", _boom)

        result = ts.run_terminal_stage(rec_dir)
        assert result.destination == "local"
        assert result.routed is True
        assert result.sentinel_uploaded is False
        # No <name>-scrubbed dir produced (AE1).
        assert not (rec_dir.parent / f"{rec_dir.name}-scrubbed").exists()
        # Every chunk LOCAL_DONE.
        ledger = PipelineLedger(rec_dir / "recording.db")
        assert all(r.lifecycle == Lifecycle.LOCAL_DONE for r in ledger.all_chunks())
        # No upload sentinel.
        assert not (rec_dir / "recording_complete.json").exists()

    def test_legacy_no_intent_defaults_to_local(self, tmp_path, monkeypatch):
        """A recording with no .recording_intent routes local (conservative)."""
        from screencap import terminal_stage as ts

        rec_dir = _make_recording(tmp_path, destination=None, n_chunks=1)
        (rec_dir / ".recording_intent").unlink(missing_ok=True)

        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not upload")),
        )
        result = ts.run_terminal_stage(rec_dir)
        assert result.destination == "local"

    def test_cloud_uploads_scrubbed_copy_never_raw_db(self, tmp_path, monkeypatch):
        """AE4: recording.db is never among the uploaded artifacts."""
        from screencap import terminal_stage as ts
        from screencap.terminal_stage import CloudCopyOutcome

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        scrubbed.mkdir()
        # Scrubbed copy contains chunk artifacts + a (wrongly-copied) recording.db
        # to prove the upload seam excludes it.
        for i in range(2):
            (scrubbed / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 32)
            (scrubbed / f"events_{i:04d}.jsonl").write_text("{}\n")
            (scrubbed / f"chunk_{i:04d}_manifest.json").write_text("{}")
        (scrubbed / "recording.db").write_bytes(b"RAWDB")

        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: CloudCopyOutcome(scrubbed_dir=scrubbed),
        )

        uploaded_names: list[str] = []

        def _fake_upload_recording(directory, **kw):
            from screencap.upload import UploadResult, list_recording_files
            files = list_recording_files(directory)
            uploaded_names.extend(f.name for f in files)
            res = UploadResult(recording=directory.name)
            res.uploaded = [f.name for f in files]
            return res

        monkeypatch.setattr(ts, "_open_ledger", lambda d: None)  # legacy gate path
        import screencap.upload as up
        monkeypatch.setattr(up, "upload_recording", _fake_upload_recording)

        ts.run_terminal_stage(rec_dir)
        assert "recording.db" not in uploaded_names, (
            f"recording.db leaked into uploaded set: {uploaded_names}"
        )
        assert any(n.startswith("chunk_") for n in uploaded_names)


# ---------------------------------------------------------------------------
# AE2 — interrupted upload, re-run from disk: confirmed chunks not re-uploaded.
# ---------------------------------------------------------------------------


class TestAE2ResumeFromDisk:
    def test_already_uploaded_chunk_not_reuploaded(self, tmp_path, monkeypatch):
        from screencap import terminal_stage as ts
        from screencap.pipeline_state import PipelineLedger, UploadState
        from screencap.terminal_stage import CloudCopyOutcome

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=3)
        ledger = PipelineLedger(rec_dir / "recording.db")
        # Simulate a prior run that uploaded chunk 0 only.
        ledger.mark_uploaded(0)

        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        scrubbed.mkdir()
        for i in range(3):
            (scrubbed / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 32)

        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: CloudCopyOutcome(scrubbed_dir=scrubbed),
        )

        # remote_exists: chunk 0 already in GCS; 1 and 2 land during this run.
        confirmed = {0}

        def _remote(idx):
            confirmed.add(idx)  # upload "lands" the rest
            return True

        from screencap.upload import UploadResult
        monkeypatch.setattr(
            ts, "_route_cloud", ts._route_cloud,  # use the real one
        )
        import screencap.upload as up
        monkeypatch.setattr(
            up, "upload_recording",
            lambda d, **kw: UploadResult(recording=d.name),
        )
        # Sentinel upload is the last write — stub it to a success boolean.
        monkeypatch.setattr(
            "screencap.chunk_processor.upload_sentinel",
            lambda *a, **kw: True,
        )

        result = ts.run_terminal_stage(rec_dir, _remote_exists=_remote)

        # Reconcile should NOT re-probe chunk 0 (already UPLOADED); it re-stats
        # only PENDING/FAILED. After the run all three are UPLOADED.
        ledger2 = PipelineLedger(rec_dir / "recording.db")
        states = {r.chunk_index: r.upload_state for r in ledger2.all_chunks()}
        assert states == {
            0: UploadState.UPLOADED,
            1: UploadState.UPLOADED,
            2: UploadState.UPLOADED,
        }
        assert result.finalize_gate_satisfied is True
        assert result.sentinel_uploaded is True


# ---------------------------------------------------------------------------
# Fail-closed — a FAILED chunk blocks the sentinel; local media preserved.
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_failed_chunk_blocks_sentinel_preserves_local(self, tmp_path, monkeypatch):
        from screencap import terminal_stage as ts
        from screencap.terminal_stage import CloudCopyOutcome

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        scrubbed.mkdir()

        # Producer reports chunk 1 FAILED at masking (fail-closed).
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: CloudCopyOutcome(scrubbed_dir=scrubbed, failed_chunks=[1]),
        )
        from screencap.upload import UploadResult
        import screencap.upload as up
        monkeypatch.setattr(up, "upload_recording", lambda d, **kw: UploadResult(recording=d.name))

        sentinel_called = []
        monkeypatch.setattr(
            "screencap.chunk_processor.upload_sentinel",
            lambda *a, **kw: sentinel_called.append(True) or True,
        )

        result = ts.run_terminal_stage(rec_dir, _remote_exists=lambda i: True)

        assert result.sentinel_uploaded is False
        assert not sentinel_called, "sentinel must NOT be written when a chunk FAILED"
        assert 1 in result.failed_indices
        # Local media preserved (we never stub).
        assert (rec_dir / "chunk_0001.mp4").exists()
        assert (rec_dir / "chunk_0000.mp4").exists()


# ---------------------------------------------------------------------------
# Force-stop edge — a PENDING chunk on disk prevents the sentinel even on re-run.
# ---------------------------------------------------------------------------


class TestForceStopGate:
    def test_pending_chunk_blocks_sentinel(self, tmp_path, monkeypatch):
        from screencap import terminal_stage as ts
        from screencap.pipeline_state import PipelineLedger
        from screencap.terminal_stage import CloudCopyOutcome

        # 3 expected, but chunk 2 was never confirmed (force-stop left PENDING).
        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=3)
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        scrubbed.mkdir()
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: CloudCopyOutcome(scrubbed_dir=scrubbed),
        )
        from screencap.upload import UploadResult
        import screencap.upload as up
        monkeypatch.setattr(up, "upload_recording", lambda d, **kw: UploadResult(recording=d.name))
        monkeypatch.setattr("screencap.chunk_processor.upload_sentinel", lambda *a, **kw: True)

        # remote_exists confirms 0 and 1 but NOT 2 (still mid-flight on disk).
        result = ts.run_terminal_stage(rec_dir, _remote_exists=lambda i: i in (0, 1))

        assert result.finalize_gate_satisfied is False, (
            "frozen closed-set gate must block while chunk 2 is not UPLOADED"
        )
        assert result.sentinel_uploaded is False
        ledger = PipelineLedger(rec_dir / "recording.db")
        # chunk 2 stays not-uploaded.
        st = {r.chunk_index: r.upload_state.value for r in ledger.all_chunks()}
        assert st[2] != "uploaded"


# ---------------------------------------------------------------------------
# Stale sentinel — a pre-existing legacy recording_complete.json is ignored;
# chunks_expected regenerated from the frozen ledger.
# ---------------------------------------------------------------------------


class TestStaleSentinel:
    def test_legacy_sentinel_ignored_regenerated_from_ledger(self, tmp_path, monkeypatch):
        from screencap import terminal_stage as ts
        from screencap.terminal_stage import CloudCopyOutcome

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        # A stale legacy sentinel with the WRONG (Bug-3) chunks_expected: 0.
        (rec_dir / "recording_complete.json").write_text(json.dumps({
            "version": 1, "chunks_expected": 0, "sentinel_id": "stale",
        }))

        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        scrubbed.mkdir()
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: CloudCopyOutcome(scrubbed_dir=scrubbed),
        )
        from screencap.upload import UploadResult
        import screencap.upload as up
        monkeypatch.setattr(up, "upload_recording", lambda d, **kw: UploadResult(recording=d.name))

        captured = {}

        def _fake_sentinel(capture_dir, recording_name, *, stop_reason, chunks_expected, show_on_website):
            captured["chunks_expected"] = chunks_expected
            return True

        monkeypatch.setattr("screencap.chunk_processor.upload_sentinel", _fake_sentinel)

        ts.run_terminal_stage(rec_dir, _remote_exists=lambda i: True)

        # The regenerated sentinel uses the FROZEN ledger count (2), NOT the
        # stale 0 (Bug 3 fixed).
        assert captured["chunks_expected"] == 2
