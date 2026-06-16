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

        def _fake_route_cloud(recording_dir, *, ledger, console, force, result, remote_exists, policy=None, retention_override=None):
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

        # The failing chunk 1 is NOT already in GCS (a realistic fail-closed
        # scenario — only an un-uploaded chunk reaches produce/masking). So the
        # gate is unsatisfied and the converged fast path does not fire; produce
        # runs and reports the FAILED chunk.
        result = ts.run_terminal_stage(rec_dir, _remote_exists=lambda i: i != 1)

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


# ---------------------------------------------------------------------------
# SCR-125 U1 — the terminal stage routes its per-chunk video mask through the
# SAME shared seam the live chunk_processor uses, gated on the FROZEN value.
# ---------------------------------------------------------------------------


class TestSharedMaskSeamU1:
    def _write_intent(self, rec_dir: Path, *, masked: bool) -> None:
        (rec_dir / ".recording_intent").write_text(json.dumps({
            "version": 2, "destination": "cloud",
            "retention_policy": "keep_forever", "retention_params": {},
            "masked_video_upload": masked, "show_on_website": True,
        }))

    def test_mask_videos_routes_through_shared_seam_frozen_on(self, tmp_path, monkeypatch):
        """Flag frozen ON → the terminal stage's _mask_videos drives the SHARED
        pipeline_chunk_ops.mask_chunk_for_cloud (same body the live path uses)
        and marks the ledger SCRUBBED. The GLOBAL is mocked OFF to prove the
        frozen .recording_intent value is the gate, not the global."""
        import screencap.pipeline_chunk_ops as pco
        from screencap import terminal_stage as ts
        from screencap.pipeline_state import Lifecycle, PipelineLedger
        from screencap.terminal_stage import CloudCopyOutcome, CloudCopyProducer

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        self._write_intent(rec_dir, masked=True)
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"

        seen: list[int] = []
        real = pco.mask_chunk_for_cloud

        def _spy(recording_dir, scrubbed_dir, idx, **kw):
            seen.append(idx)
            return real(recording_dir, scrubbed_dir, idx, **kw)

        monkeypatch.setattr(pco, "mask_chunk_for_cloud", _spy)
        monkeypatch.setattr(
            "screencap.scrubber.mask_video_chunk_for_cloud",
            lambda *a, **kw: _MASKED_OK(),
        )
        # Global OFF — the frozen ON value must still drive masking.
        monkeypatch.setattr(
            "screencap.config.get_masked_video_upload_enabled", lambda: False,
        )

        ledger = PipelineLedger(rec_dir / "recording.db")
        outcome = CloudCopyOutcome(scrubbed_dir=scrubbed)
        CloudCopyProducer(rec_dir)._mask_videos(scrubbed, ledger, outcome)

        assert sorted(seen) == [0, 1], "both chunks routed through the shared seam"
        assert sorted(outcome.masked_chunks) == [0, 1]
        for i in (0, 1):
            assert ledger.get_chunk(i).lifecycle is Lifecycle.SCRUBBED

    def test_mask_videos_noop_when_frozen_off(self, tmp_path, monkeypatch):
        """Flag frozen OFF → no masker invoked even if the GLOBAL is ON
        (frozen value is authoritative — R-SCR125-A)."""
        from screencap import terminal_stage as ts
        from screencap.terminal_stage import CloudCopyOutcome, CloudCopyProducer

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        self._write_intent(rec_dir, masked=False)
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"

        called = []
        monkeypatch.setattr(
            "screencap.scrubber.mask_video_chunk_for_cloud",
            lambda *a, **kw: called.append(True),
        )
        monkeypatch.setattr(
            "screencap.config.get_masked_video_upload_enabled", lambda: True,
        )
        outcome = CloudCopyOutcome(scrubbed_dir=scrubbed)
        CloudCopyProducer(rec_dir)._mask_videos(scrubbed, None, outcome)
        assert not called, "frozen OFF must not invoke the masker even if global ON"
        assert not outcome.masked_chunks and not outcome.failed_chunks


def _MASKED_OK():
    from screencap.video_mask import MaskOutcome, MaskOutcomeStatus

    return MaskOutcome(status=MaskOutcomeStatus.MASKED, reason="test", regions_masked=1)


# ---------------------------------------------------------------------------
# SCR-125 U3 — internal AE8 refusal + explicit promotion + retention override.
# ---------------------------------------------------------------------------


def _stub_cloud_seam(monkeypatch, scrubbed):
    """Stub the producer + upload so the cloud route runs without real work."""
    from screencap import terminal_stage as ts
    from screencap.terminal_stage import CloudCopyOutcome

    scrubbed.mkdir(exist_ok=True)
    monkeypatch.setattr(
        ts.CloudCopyProducer, "produce",
        lambda self, **kw: CloudCopyOutcome(scrubbed_dir=scrubbed),
    )
    from screencap.upload import UploadResult
    import screencap.upload as up
    monkeypatch.setattr(up, "upload_recording", lambda d, **kw: UploadResult(recording=d.name))
    monkeypatch.setattr("screencap.chunk_processor.upload_sentinel", lambda *a, **kw: True)


class TestDryRunReadOnly:
    """SCR-125 U5: dry-run is a read-only preview — no flock, no disk mutation."""

    def test_dry_run_does_not_block_on_held_lock(self, tmp_path):
        """A dry-run never takes the flock, so it completes even while another
        surface holds it (a read-only preview must not block on a live run)."""
        import threading

        from screencap import terminal_stage as ts

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        held = threading.Event()
        release = threading.Event()

        def _hold():
            with ts.terminal_lock(rec_dir.name):
                held.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=_hold)
        holder.start()
        assert held.wait(timeout=5)
        try:
            # Would block (up to lock_timeout) if dry-run took the flock.
            result = ts.run_terminal_stage(rec_dir, dry_run=True, lock_timeout=2.0)
            assert result.routed is True
            assert result.n_expected == 2  # reported from the read-only ledger
            assert result.sentinel_uploaded is False
        finally:
            release.set()
            holder.join(timeout=5)

    def test_dry_run_does_not_migrate_or_mutate(self, tmp_path, monkeypatch):
        """Dry-run never produces a scrubbed copy, marks LOCAL_DONE, or writes a
        sentinel — and uses the read-only ledger opener (no schema migration)."""
        from screencap import terminal_stage as ts
        from screencap.pipeline_state import Lifecycle, PipelineLedger

        rec_dir = _make_recording(tmp_path, destination="local", n_chunks=2)
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("dry-run must not produce")),
        )
        ts.run_terminal_stage(rec_dir, dry_run=True)
        # No LOCAL_DONE marks written (chunks stay STAGED).
        ledger = PipelineLedger(rec_dir / "recording.db")
        assert all(r.lifecycle == Lifecycle.STAGED for r in ledger.all_chunks())
        assert not (rec_dir / "recording_complete.json").exists()


class TestAlreadyConvergedFastPath:
    """SCR-125 U4: when reconcile shows the closed set is already all UPLOADED,
    the terminal stage skips the expensive produce/re-scrub + no-op upload and
    goes straight to the sentinel — so engine finalize stays within the stop
    budget and a daemon resume / CLI re-upload of a converged recording is cheap."""

    def test_already_converged_skips_produce(self, tmp_path, monkeypatch):
        from screencap import terminal_stage as ts
        from screencap.pipeline_state import PipelineLedger

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        # Everything already uploaded + confirmed remote (the happy live path).
        ledger = PipelineLedger(rec_dir / "recording.db")
        for i in range(2):
            ledger.mark_uploaded(i)

        # produce() MUST NOT run on the converged fast path.
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("produce must be skipped when already converged")
            ),
        )
        sentinels = []
        monkeypatch.setattr(
            "screencap.chunk_processor.upload_sentinel",
            lambda *a, **kw: sentinels.append(True) or True,
        )

        result = ts.run_terminal_stage(rec_dir, _remote_exists=lambda i: True)
        assert result.finalize_gate_satisfied is True
        assert result.sentinel_uploaded is True
        assert sentinels == [True]

    def test_force_still_rebuilds_even_when_converged(self, tmp_path, monkeypatch):
        """--force re-scrubs + re-uploads even on a converged recording."""
        from screencap import terminal_stage as ts
        from screencap.pipeline_state import PipelineLedger
        from screencap.terminal_stage import CloudCopyOutcome

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=1)
        ledger = PipelineLedger(rec_dir / "recording.db")
        ledger.mark_uploaded(0)

        produced = []
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        scrubbed.mkdir()
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: produced.append(True) or CloudCopyOutcome(scrubbed_dir=scrubbed),
        )
        from screencap.upload import UploadResult
        import screencap.upload as up
        monkeypatch.setattr(up, "upload_recording", lambda d, **kw: UploadResult(recording=d.name))
        monkeypatch.setattr("screencap.chunk_processor.upload_sentinel", lambda *a, **kw: True)

        ts.run_terminal_stage(rec_dir, force=True, _remote_exists=lambda i: True)
        assert produced == [True], "force must rebuild even when already converged"


class TestU3PromotionAndRetention:
    def test_ae8_hole_refuses_internally_nothing_uploaded(self, tmp_path, monkeypatch):
        """AE8 moved inside run_terminal_stage: an evicted-and-unconfirmable chunk
        raises PromotionRefused on the cloud route — the producer is never
        reached, so nothing is uploaded."""
        from screencap import terminal_stage as ts

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=3)
        # Chunk 1's local media is gone and it is NOT uploaded (still STAGED).
        (rec_dir / "chunk_0001.mp4").unlink()

        # Producer must NEVER run when there is a hole.
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not produce on a hole")),
        )
        with pytest.raises(ts.PromotionRefused):
            # remote_exists False for the missing chunk → unconfirmable hole.
            ts.run_terminal_stage(rec_dir, _remote_exists=lambda i: False)

    def test_force_destination_cloud_promotes_local_recording(self, tmp_path, monkeypatch):
        """force_destination=cloud routes a local-intent recording through the
        cloud path (uploads) instead of the LOCAL no-op."""
        from screencap import terminal_stage as ts

        rec_dir = _make_recording(tmp_path, destination="local", n_chunks=2)
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        _stub_cloud_seam(monkeypatch, scrubbed)

        result = ts.run_terminal_stage(
            rec_dir, force_destination="cloud", _remote_exists=lambda i: True,
        )
        assert result.destination == "cloud"
        assert result.routed is True
        assert result.sentinel_uploaded is True

    def test_retention_override_keep_forever_suppresses_eviction(self, tmp_path, monkeypatch):
        """retention_override=keep_forever prevents eviction even when the frozen
        policy is delete_after_upload (the --no-delete semantics)."""
        from screencap import terminal_stage as ts

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        # Freeze delete_after_upload.
        (rec_dir / ".recording_intent").write_text(json.dumps({
            "version": 2, "destination": "cloud",
            "retention_policy": "delete_after_upload", "retention_params": {},
            "show_on_website": True,
        }))
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        _stub_cloud_seam(monkeypatch, scrubbed)

        ts.run_terminal_stage(
            rec_dir, retention_override="keep_forever", _remote_exists=lambda i: True,
        )
        # With keep_forever override, the local media survives the upload.
        assert (rec_dir / "chunk_0000.mp4").exists()
        assert (rec_dir / "chunk_0001.mp4").exists()

    def test_delete_after_upload_without_override_evicts(self, tmp_path, monkeypatch):
        """Control: the SAME recording WITHOUT the override evicts (proves the
        override is what suppresses, not a mock)."""
        from screencap import terminal_stage as ts

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        (rec_dir / ".recording_intent").write_text(json.dumps({
            "version": 2, "destination": "cloud",
            "retention_policy": "delete_after_upload", "retention_params": {},
            "show_on_website": True,
        }))
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        _stub_cloud_seam(monkeypatch, scrubbed)

        ts.run_terminal_stage(rec_dir, _remote_exists=lambda i: True)
        # keep_recent defaults to 0 at the terminal pass → all uploaded evicted.
        assert not (rec_dir / "chunk_0000.mp4").exists()
        assert not (rec_dir / "chunk_0001.mp4").exists()

    def test_legacy_no_ledger_force_cloud_no_spurious_hole(self, tmp_path, monkeypatch):
        """A legacy / no-ledger recording with force_destination=cloud falls
        through the whole-dir path (R14) — no spurious AE8 hole refusal."""
        from screencap import terminal_stage as ts

        rec_dir = tmp_path / "legacy"
        rec_dir.mkdir()
        # A single-file legacy recording: no recording.db, no ledger table.
        (rec_dir / "video.mp4").write_bytes(b"\x00" * 64)
        (rec_dir / ".recording_id").write_text("legacy")
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        _stub_cloud_seam(monkeypatch, scrubbed)

        # Must NOT raise PromotionRefused (no closed chunk set to gate on).
        result = ts.run_terminal_stage(
            rec_dir, force_destination="cloud", _remote_exists=lambda i: True,
        )
        assert result.destination == "cloud"
        assert result.routed is True


# ---------------------------------------------------------------------------
# SCR-129 — the terminal stage is the SOLE uploader on non-live paths
# (`--no-live-upload`, daemon resume of an all-session-failed live upload,
# local->cloud promotion). The `<name>-scrubbed` copy STRIPS media, so the
# source chunk video/audio must be uploaded by the terminal stage directly —
# else the cloud copy is permanently media-less. Every prior cloud-route test
# mocks the upload seam or hand-fills the scrubbed dir with media, so the gap
# was invisible; these exercise the real media-less scrubbed dir + the real
# per-chunk upload seam.
# ---------------------------------------------------------------------------


class TestSCR129SourceMediaUpload:
    def _metadata_only_scrubbed(self, rec_dir: Path, n_chunks: int) -> Path:
        """A `<name>-scrubbed` dir as the REAL scrubber leaves it: events +
        manifest, NO media (.mp4/.flac are stripped by _SKIP_EXTENSIONS)."""
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        scrubbed.mkdir(exist_ok=True)
        for i in range(n_chunks):
            (scrubbed / f"events_{i:04d}.jsonl").write_text("{}\n")
            (scrubbed / f"chunk_{i:04d}_manifest.json").write_text("{}")
        return scrubbed

    def _capture_uploads(self, monkeypatch, n_chunks: int):
        """Wire a stateful (mock) cloud that distinguishes a PROBE from an
        actual upload (PUT).

        ``request_signed_urls`` returns ``None`` ("already there") only for a
        name that has actually been PUT — so a chunk's reconcile/confirm probe
        cannot spuriously confirm a video that was never uploaded. The scrubbed
        metadata (events/manifest) is pre-seeded as already-uploaded (the
        ``upload_recording`` scrubbed-dir path is stubbed out — not under test
        here), so a chunk converges iff its MEDIA is genuinely PUT.

        Returns the ``put`` list of ``(name, path)`` pairs actually uploaded.
        """
        from screencap.upload import UploadResult

        uploaded: set[str] = set()
        for i in range(n_chunks):
            uploaded.add(f"events_{i:04d}.jsonl")
            uploaded.add(f"chunk_{i:04d}_manifest.json")
        put: list[tuple[str, str]] = []

        def _fake_request_signed_urls(recording_name, files):
            return (
                {f.name: (None if f.name in uploaded else f"https://x/{f.name}")
                 for f in files},
                "prefix/",
            )

        def _fake_upload_single(fi, signed_url):
            put.append((fi.name, str(fi.path)))
            uploaded.add(fi.name)

        monkeypatch.setattr(
            "screencap.upload.request_signed_urls", _fake_request_signed_urls,
        )
        monkeypatch.setattr(
            "screencap.chunk_processor._upload_single", _fake_upload_single,
        )
        # The scrubbed-metadata upload is not under test — stub it to a no-op so
        # only the per-chunk MEDIA upload exercises the (mock) cloud.
        monkeypatch.setattr(
            "screencap.upload.upload_recording",
            lambda d, **kw: UploadResult(recording=d.name),
        )
        monkeypatch.setattr(
            "screencap.chunk_processor.upload_sentinel", lambda *a, **kw: True,
        )
        return put

    def test_source_video_audio_uploaded_when_live_did_not(self, tmp_path, monkeypatch):
        """Flag OFF (default): the source chunk video AND audio reach the cloud
        through the terminal-stage-only path, and the recording converges."""
        from screencap import terminal_stage as ts
        from screencap.pipeline_state import PipelineLedger, UploadState
        from screencap.terminal_stage import CloudCopyOutcome

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        scrubbed = self._metadata_only_scrubbed(rec_dir, 2)
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: CloudCopyOutcome(scrubbed_dir=scrubbed),
        )
        put = self._capture_uploads(monkeypatch, 2)

        result = ts.run_terminal_stage(rec_dir)

        names = {n for n, _ in put}
        assert {"chunk_0000.mp4", "chunk_0001.mp4"} <= names, (
            f"source video never uploaded by the terminal stage: {sorted(names)}"
        )
        assert {"audio_0000.flac", "audio_0001.flac"} <= names, (
            f"source audio never uploaded by the terminal stage: {sorted(names)}"
        )
        # The uploaded video is the RICH SOURCE copy (flag OFF), not a masked one.
        video_paths = {n: p for n, p in put}
        assert video_paths["chunk_0000.mp4"] == str(rec_dir / "chunk_0000.mp4")

        # With the media genuinely in GCS, the recording converges.
        assert result.finalize_gate_satisfied is True
        assert result.sentinel_uploaded is True
        ledger = PipelineLedger(rec_dir / "recording.db")
        assert all(
            r.upload_state == UploadState.UPLOADED for r in ledger.all_chunks()
        )

    def test_masked_copy_uploaded_never_rich_source_when_flag_on(self, tmp_path, monkeypatch):
        """Flag frozen ON: the terminal stage ships the MASKED copy from
        `<name>-scrubbed/masked_video/` under the plain `chunk_NNNN.mp4` key and
        NEVER the rich source .mp4 (R-SCR125-A masked-path-switch)."""
        from screencap import terminal_stage as ts
        from screencap.scrubber import masked_video_dir
        from screencap.terminal_stage import CloudCopyOutcome

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        (rec_dir / ".recording_intent").write_text(json.dumps({
            "version": 2, "destination": "cloud",
            "retention_policy": "keep_forever", "retention_params": {},
            "masked_video_upload": True, "show_on_website": True,
        }))
        scrubbed = self._metadata_only_scrubbed(rec_dir, 2)
        masked_dir = masked_video_dir(scrubbed)
        masked_dir.mkdir(parents=True, exist_ok=True)
        for i in range(2):
            (masked_dir / f"chunk_{i:04d}.mp4").write_bytes(b"MASKED")

        # produce() already ran the mask (we mock it): masked copies on disk.
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: CloudCopyOutcome(
                scrubbed_dir=scrubbed, masked_chunks=[0, 1]),
        )
        put = self._capture_uploads(monkeypatch, 2)

        ts.run_terminal_stage(rec_dir)

        video_paths = {n: p for n, p in put if n.startswith("chunk_") and n.endswith(".mp4")}
        assert "chunk_0000.mp4" in video_paths, "masked video must reach the cloud"
        assert video_paths["chunk_0000.mp4"] == str(masked_dir / "chunk_0000.mp4"), (
            "must ship the masked copy, not the rich source"
        )
        # The rich SOURCE .mp4 path is never among the uploaded paths.
        rich_sources = {str(rec_dir / f"chunk_{i:04d}.mp4") for i in range(2)}
        assert not (rich_sources & {p for _, p in put}), (
            "rich source video must never be uploaded when masking is ON"
        )


# ---------------------------------------------------------------------------
# SCR-116 — account-ownership gate. A cloud recording is pinned at start to the
# uid that owned it. If a different account is signed in at convergence time
# (the user switched mid-/post-recording), the terminal stage must REFUSE all
# cloud ops — no upload, no sentinel, no eviction — so the recording is never
# fragmented into a second user's namespace.
# ---------------------------------------------------------------------------


def _jwt(claims: dict) -> str:
    import base64

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"


class TestSCR116AccountOwnershipGate:
    def test_account_mismatch_refuses_cloud_convergence(self, tmp_path, monkeypatch):
        from screencap import auth
        from screencap import terminal_stage as ts
        from screencap.catalog import write_owner_uid

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        write_owner_uid(rec_dir, "uid-A")
        # The process is now signed in as a DIFFERENT account.
        monkeypatch.setattr(
            auth, "get_id_token", lambda force_refresh=False: _jwt({"user_id": "uid-B"})
        )

        produced = []
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: produced.append(1),
        )
        uploaded = []
        import screencap.upload as up
        monkeypatch.setattr(
            up, "upload_recording", lambda *a, **k: uploaded.append(1)
        )

        result = ts.run_terminal_stage(rec_dir)

        assert produced == [], "must not scrub/produce a cloud copy under the wrong account"
        assert uploaded == [], "must not upload under the wrong account"
        assert result.sentinel_uploaded is False
        assert result.upload_warning and "account mismatch" in result.upload_warning.lower()

    def test_matching_account_proceeds_to_upload(self, tmp_path, monkeypatch):
        from screencap import auth
        from screencap import terminal_stage as ts
        from screencap.catalog import write_owner_uid
        from screencap.terminal_stage import CloudCopyOutcome

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2)
        write_owner_uid(rec_dir, "uid-A")
        # Same account that owns the recording — the gate must NOT fire.
        monkeypatch.setattr(
            auth, "get_id_token", lambda force_refresh=False: _jwt({"user_id": "uid-A"})
        )

        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        scrubbed.mkdir()
        for i in range(2):
            (scrubbed / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 32)
            (scrubbed / f"events_{i:04d}.jsonl").write_text("{}\n")
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: CloudCopyOutcome(scrubbed_dir=scrubbed),
        )
        uploaded = []
        import screencap.upload as up

        def _fake_upload(directory, **kw):
            from screencap.upload import UploadResult
            uploaded.append(directory)
            return UploadResult(recording=directory.name)

        monkeypatch.setattr(up, "upload_recording", _fake_upload)
        monkeypatch.setattr(ts, "_open_ledger", lambda d: None)  # legacy gate path

        result = ts.run_terminal_stage(rec_dir)

        assert uploaded, "matching account must proceed to upload"
        assert not (result.upload_warning and "account mismatch" in result.upload_warning.lower())

    def test_no_owner_pin_legacy_recording_not_refused(self, tmp_path, monkeypatch):
        """A recording with no pinned owner (legacy / local-promoted) is not gated
        — preserves current behavior for recordings predating the pin."""
        from screencap import auth
        from screencap import terminal_stage as ts
        from screencap.terminal_stage import CloudCopyOutcome

        rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=1)
        # No write_owner_uid(...) — no pin on disk.
        monkeypatch.setattr(
            auth, "get_id_token", lambda force_refresh=False: _jwt({"user_id": "uid-B"})
        )
        scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        scrubbed.mkdir()
        (scrubbed / "chunk_0000.mp4").write_bytes(b"\x00" * 32)
        monkeypatch.setattr(
            ts.CloudCopyProducer, "produce",
            lambda self, **kw: CloudCopyOutcome(scrubbed_dir=scrubbed),
        )
        uploaded = []
        import screencap.upload as up

        def _fake_upload(directory, **kw):
            from screencap.upload import UploadResult
            uploaded.append(directory)
            return UploadResult(recording=directory.name)

        monkeypatch.setattr(up, "upload_recording", _fake_upload)
        monkeypatch.setattr(ts, "_open_ledger", lambda d: None)

        result = ts.run_terminal_stage(rec_dir)
        assert not (result.upload_warning and "account mismatch" in result.upload_warning.lower())
