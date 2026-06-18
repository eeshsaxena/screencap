"""SCR-125 U7 — end-to-end re-validation across the unified upload path.

These integration tests exercise the REAL ledger + flock + reconcile (only the
heavy scrub/network/HTTP is stubbed, with an injected ``remote_exists`` seam) to
prove the invariants hold across the four surfaces that share the terminal-stage
seam + ledger:

  * the live ``chunk_processor`` per-chunk upload,
  * the engine ``finalize_uploads`` convergence,
  * the manual ``screencap upload`` CLI, and
  * the daemon crash/restart resume.

All four route the destructive work through ``run_terminal_stage`` (the live path
also takes the same per-recording flock per chunk), so the assertions here are
about CONVERGENCE: the five data-loss-prevention rules and AE2/AE5/AE12 hold no
matter which surface drives a recording.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_run_dir(tmp_path, monkeypatch):
    import screencap.terminal_stage as ts

    monkeypatch.setattr(ts, "_RUN_DIR", tmp_path / "run")


def _make_recording(tmp_path: Path, *, destination: str, n_chunks: int,
                    retention: str = "keep_forever", masked: bool = False,
                    name: str = "rec") -> Path:
    """A cloud recording with the real ledger seeded STAGED + chunks_expected frozen."""
    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    rec_dir = tmp_path / name
    rec_dir.mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(session, {
        "timestamp": 1000.0, "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080, "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5, "double_click_distance_pixels": 5.0,
    })
    session.close()
    engine.dispose()
    for i in range(n_chunks):
        (rec_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 1024)
        (rec_dir / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 64)
        (rec_dir / f"events_{i:04d}.jsonl").write_text('{"_meta":1}\n')
        (rec_dir / f"chunk_{i:04d}_manifest.json").write_text("{}")
    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    for i in range(n_chunks):
        ledger.seed_chunk(i)
        ledger.mark_staged(i)
    ledger.freeze_chunks_expected(n_chunks)
    (rec_dir / ".recording_intent").write_text(json.dumps({
        "version": 2, "destination": destination,
        "retention_policy": retention, "retention_params": {},
        "masked_video_upload": masked, "show_on_website": True,
    }))
    (rec_dir / ".recording_id").write_text(name)
    return rec_dir


def _stub_cloud_seam(monkeypatch, scrubbed, *, failed_chunks=()):
    """Stub produce + upload + sentinel; the real reconcile/ledger/flock run."""
    from screencap import terminal_stage as ts
    from screencap.terminal_stage import CloudCopyOutcome

    scrubbed.mkdir(exist_ok=True)
    monkeypatch.setattr(
        ts.CloudCopyProducer, "produce",
        lambda self, **kw: CloudCopyOutcome(
            scrubbed_dir=scrubbed, failed_chunks=list(failed_chunks),
        ),
    )
    from screencap.upload import UploadResult
    import screencap.upload as up
    monkeypatch.setattr(up, "upload_recording", lambda d, **kw: UploadResult(recording=d.name))
    sentinels: list[bool] = []
    monkeypatch.setattr(
        "screencap.chunk_processor.upload_sentinel",
        lambda *a, **kw: sentinels.append(True) or True,
    )
    return sentinels


# ---------------------------------------------------------------------------
# AE12 — the shared per-recording flock serializes ALL surfaces.
# ---------------------------------------------------------------------------


def test_ae12_shared_flock_serializes_live_op_under_contention(tmp_path):
    """While a holder owns the per-recording flock (a live finalize / manual
    upload), the live chunk_processor per-chunk op DEFERS rather than racing a
    second concurrent reconcile→upload pass — proving the live path takes the
    same shared flock as the terminal-stage surfaces (AE12)."""
    import multiprocessing

    from screencap import terminal_stage as ts
    from screencap.chunk_processor import ChunkProcessor, _UploadOutcome

    rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=1, name="ae12")
    # Construct the processor BEFORE the holder takes the lock — the cloud
    # privacy-pipeline init can take several seconds, and doing it inside the
    # holder's lock-hold window would let the lock release before the live op
    # runs (the contention would never actually happen).
    cp = ChunkProcessor(
        rec_dir, multiprocessing.Queue(), multiprocessing.Queue(),
        recording_name="ae12", upload_enabled=True, auto_delete=False,
        cloud_intent=True, masked_video_upload=False,
    )
    holder_in = threading.Event()
    release = threading.Event()

    def _hold():
        with ts.terminal_lock("ae12"):
            holder_in.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=_hold)
    holder.start()
    assert holder_in.wait(timeout=5)
    try:
        # The holder owns the flock → the live per-chunk op cannot acquire it
        # (non_blocking) → it DEFERS rather than racing a second upload pass.
        assert cp._cloud_upload_chunk(0, 0.0, 5.0, None) is _UploadOutcome.DEFERRED
    finally:
        release.set()
        holder.join(timeout=5)


def test_ae12_non_blocking_resume_raises_busy_under_contention(tmp_path):
    """The daemon resume path (non_blocking) raises TerminalStageBusy — never a
    second upload pass — while another surface holds the flock."""
    from screencap import terminal_stage as ts

    rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=1, name="ae12b")
    holder_in = threading.Event()
    release = threading.Event()

    def _hold():
        with ts.terminal_lock("ae12b"):
            holder_in.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=_hold)
    holder.start()
    assert holder_in.wait(timeout=5)
    try:
        with pytest.raises(ts.TerminalStageBusy):
            ts.run_terminal_stage(rec_dir, non_blocking=True)
    finally:
        release.set()
        holder.join(timeout=5)


# ---------------------------------------------------------------------------
# AE2 — interrupt + re-run: confirmed chunks are NOT re-uploaded (no duplicate).
# ---------------------------------------------------------------------------


def test_ae2_interrupt_rerun_no_duplicate_and_converges(tmp_path, monkeypatch):
    """A half-finished run (chunks 0,1 confirmed remote, 2 not) leaves the gate
    unsatisfied (no sentinel). When chunk 2 lands, a re-run converges WITHOUT
    re-uploading 0,1 — the reconcile recognizes them; exactly-once."""
    from screencap import terminal_stage as ts
    from screencap.pipeline_state import PipelineLedger, UploadState

    rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=3, name="ae2")
    scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
    sentinels = _stub_cloud_seam(monkeypatch, scrubbed)

    # First pass: only 0,1 are confirmed in GCS.
    confirmed = {0, 1}

    def _remote_exists(idx):
        return idx in confirmed

    r1 = ts.run_terminal_stage(rec_dir, _remote_exists=_remote_exists)
    assert r1.finalize_gate_satisfied is False
    assert r1.sentinel_uploaded is False
    assert sentinels == []  # no sentinel while chunk 2 is unconfirmed

    ledger = PipelineLedger(rec_dir / "recording.db")
    st = {r.chunk_index: r.upload_state for r in ledger.all_chunks()}
    assert st[0] is UploadState.UPLOADED and st[1] is UploadState.UPLOADED
    assert st[2] is not UploadState.UPLOADED

    # Second pass (a different surface re-running): chunk 2 now lands.
    confirmed.add(2)
    r2 = ts.run_terminal_stage(rec_dir, _remote_exists=_remote_exists)

    # SCR-127: the re-validation pass now DOES re-probe the already-UPLOADED
    # chunks 0,1 (a stat, not a re-upload) to catch a stale UPLOADED. They
    # confirm present, so they are neither downgraded nor re-uploaded — the
    # exactly-once UPLOAD guarantee is unchanged: all three converge to UPLOADED,
    # the fast path emits exactly one sentinel without re-running produce/upload.
    assert r2.downgraded == 0
    st2 = {r.chunk_index: r.upload_state
           for r in PipelineLedger(rec_dir / "recording.db").all_chunks()}
    assert all(st2[i] is UploadState.UPLOADED for i in (0, 1, 2))
    assert r2.finalize_gate_satisfied is True
    assert r2.sentinel_uploaded is True
    assert sentinels == [True]  # exactly one sentinel across both passes


# ---------------------------------------------------------------------------
# R-SCR125-A — with the masked-video flag ON, NO rich video reaches the cloud
# through the live path OR the terminal stage.
# ---------------------------------------------------------------------------


def test_flag_on_no_rich_video_through_live_or_terminal(tmp_path, monkeypatch):
    """Frozen masked_video_upload ON: the live upload set's .mp4 slot is the
    masked copy (never the source), and the terminal stage masks each chunk —
    the rich source .mp4 path is never requested for upload by either surface."""
    import multiprocessing

    from screencap.chunk_processor import ChunkProcessor

    rec_dir = _make_recording(
        tmp_path, destination="cloud", n_chunks=1, masked=True, name="masked",
    )
    # A masked copy exists (as U1's mask seam would produce).
    masked_dir = rec_dir.parent / f"{rec_dir.name}-scrubbed" / "masked_video"
    masked_dir.mkdir(parents=True)
    (masked_dir / "chunk_0000.mp4").write_bytes(b"masked")

    # Live surface: the upload set's video path is the masked copy, NOT the source.
    cp = ChunkProcessor(
        rec_dir, multiprocessing.Queue(), multiprocessing.Queue(),
        recording_name="masked", upload_enabled=False, auto_delete=False,
        cloud_intent=True,  # masked_video_upload resolved from frozen intent (True)
    )
    assert cp._masked_video_upload is True
    files = cp._collect_chunk_files(0, None)
    video = next(f for f in files if f["name"] == "chunk_0000.mp4")
    assert video["path"] == masked_dir / "chunk_0000.mp4"
    source = rec_dir / "chunk_0000.mp4"
    assert all(f["path"] != source for f in files), "live path must not ship the rich source"

    # Terminal surface: _mask_videos routes through the shared seam (frozen ON).
    from screencap.terminal_stage import CloudCopyOutcome, CloudCopyProducer
    from screencap.pipeline_state import PipelineLedger

    seen: list[int] = []
    import screencap.pipeline_chunk_ops as pco
    real = pco.mask_chunk_for_cloud

    def _spy(recording_dir, scrubbed_dir, idx, **kw):
        seen.append(idx)
        return real(recording_dir, scrubbed_dir, idx, **kw)

    monkeypatch.setattr(pco, "mask_chunk_for_cloud", _spy)
    monkeypatch.setattr(
        "screencap.scrubber.mask_video_chunk_for_cloud",
        lambda *a, **kw: _masked_ok(),
    )
    # Global OFF — only the frozen intent (ON) should drive masking.
    monkeypatch.setattr("screencap.config.get_masked_video_upload_enabled", lambda: False)

    ledger = PipelineLedger(rec_dir / "recording.db")
    scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
    outcome = CloudCopyOutcome(scrubbed_dir=scrubbed)
    CloudCopyProducer(rec_dir)._mask_videos(scrubbed, ledger, outcome)
    assert seen == [0], "terminal stage masked through the shared seam"
    assert outcome.masked_chunks == [0]


def _masked_ok():
    from screencap.video_mask import MaskOutcome, MaskOutcomeStatus

    return MaskOutcome(status=MaskOutcomeStatus.MASKED, reason="t", regions_masked=1)


# ---------------------------------------------------------------------------
# Degraded path (prevention rule #5) — a FAILED chunk never gets a sentinel
# through the shared convergence point, and local media is preserved.
# ---------------------------------------------------------------------------


def test_failed_chunk_no_sentinel_preserves_media(tmp_path, monkeypatch):
    """One FAILED chunk blocks the sentinel through run_terminal_stage (the single
    convergence point all four surfaces use); local media is preserved."""
    from screencap import terminal_stage as ts

    rec_dir = _make_recording(tmp_path, destination="cloud", n_chunks=2, name="degraded")
    scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
    sentinels = _stub_cloud_seam(monkeypatch, scrubbed, failed_chunks=[1])

    # The failing chunk 1 is not yet in GCS (only an un-uploaded chunk reaches
    # masking), so the converged fast path does not fire and produce runs.
    result = ts.run_terminal_stage(rec_dir, _remote_exists=lambda i: i != 1)

    assert result.sentinel_uploaded is False
    assert sentinels == []
    assert 1 in result.failed_indices
    # Local media preserved (never stubbed/evicted under a FAILED chunk).
    assert (rec_dir / "chunk_0000.mp4").exists()
    assert (rec_dir / "chunk_0001.mp4").exists()


# ---------------------------------------------------------------------------
# AE5 / R12 — delete_after_upload evicts confirmed chunks; disk stays bounded.
# ---------------------------------------------------------------------------


def test_ae5_delete_after_upload_evicts_confirmed_through_terminal(tmp_path, monkeypatch):
    """A delete_after_upload cloud recording, converged through run_terminal_stage,
    evicts its UPLOADED chunks' local media (bounded disk) — and only after a
    fresh remote re-confirm (the floor)."""
    from screencap import terminal_stage as ts

    rec_dir = _make_recording(
        tmp_path, destination="cloud", n_chunks=2,
        retention="delete_after_upload", name="ae5",
    )
    scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
    _stub_cloud_seam(monkeypatch, scrubbed)

    result = ts.run_terminal_stage(rec_dir, _remote_exists=lambda i: True)

    assert result.sentinel_uploaded is True
    # The terminal pass evicts every UPLOADED chunk (keep_recent=0 at finalize).
    assert not (rec_dir / "chunk_0000.mp4").exists()
    assert not (rec_dir / "chunk_0001.mp4").exists()
    assert set(result.evicted) == {0, 1}
