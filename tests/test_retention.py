"""Tests for screencap.retention — the U8 universal retention & eviction unit.

TEST-FIRST on the dangerous deletes (prevention rule 4): the
"remote does NOT exist -> NOT deleted" tests and the un-evictable-floor tests
are written and asserted BEFORE the executor's happy paths, because a FALSE
DELETE is the catastrophic failure here. They live in the first two test
classes below (``TestRemoteMissingNeverDeletes``, ``TestUnEvictableFloor``).

Coverage map (plan U8 acceptance examples):
  - AE3: local-only keep-forever -> no auto-delete; size cap set later evicts.
  - AE5: long cloud recording, delete_after_upload -> a chunk's local copy
    evicted mid-recording once its upload confirms.
  - AE9: size-cap with one FAILED (un-uploaded) chunk -> never deleted even if
    it is the oldest.
  - remote-missing error path: delete_after_upload + remote not confirmed -> no
    deletion.
  - resume: an interrupted eviction (EVICT_PENDING) resumes on re-run.
  - N-days + cloud: a cloud chunk past N days but un-uploaded is NOT deleted; a
    local chunk past N days IS deleted.
  - both: rich local follows configured retention; masked cloud copy evicted
    immediately post-upload-confirm.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture — a recording dir with the U1 ledger schema + chunk media on disk.
# Mirrors tests/test_terminal_stage.py::_make_recording so eviction operates on
# a realistic on-disk + ledger pair.
# ---------------------------------------------------------------------------


def _chunk_files(rec_dir: Path, idx: int) -> list[Path]:
    return [
        rec_dir / f"chunk_{idx:04d}.mp4",
        rec_dir / f"audio_{idx:04d}.flac",
        rec_dir / f"events_{idx:04d}.jsonl",
        rec_dir / f"chunk_{idx:04d}_manifest.json",
    ]


def _make_recording(tmp_path: Path, *, n_chunks: int, name: str = "rec",
                    chunk_bytes: int = 1024 * 1024):
    """Create recording.db + ledger schema + chunk media; return (rec_dir, ledger).

    Each chunk's media totals ``chunk_bytes`` for the .mp4 (the dominant file)
    plus small sidecars, so size-cap math is predictable.
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
    session.close()
    engine.dispose()

    for i in range(n_chunks):
        (rec_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * chunk_bytes)
        (rec_dir / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 256)
        (rec_dir / f"events_{i:04d}.jsonl").write_text('{"_meta": 1}\n')
        (rec_dir / f"chunk_{i:04d}_manifest.json").write_text("{}")

    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    for i in range(n_chunks):
        ledger.seed_chunk(i)
        ledger.mark_staged(i)
    ledger.freeze_chunks_expected(n_chunks)
    return rec_dir, ledger


def _cloud_uploaded(ledger, *indices: int) -> None:
    for i in indices:
        ledger.mark_scrubbed(i)
        ledger.mark_uploaded(i)


def _local_done(ledger, *indices: int) -> None:
    for i in indices:
        ledger.mark_local_done(i)


def _present(rec_dir: Path, idx: int) -> bool:
    """True iff the chunk's primary media (.mp4) is still on disk."""
    return (rec_dir / f"chunk_{idx:04d}.mp4").exists()


def _always_true(_idx: int) -> bool:
    return True


def _always_false(_idx: int) -> bool:
    return False


# ===========================================================================
# DANGEROUS-DELETE TESTS — WRITTEN FIRST (prevention rule 4).
# Every eviction policy must refuse to delete when remote existence is not
# freshly confirmed. A false delete is the catastrophic failure.
# ===========================================================================


class TestRemoteMissingNeverDeletes:
    """For cloud/both: remote-does-NOT-exist => chunk is NOT deleted, under
    EVERY policy that could otherwise pick it (rule 3 + rule 4)."""

    @pytest.mark.parametrize("policy_name,params", [
        ("delete_after_upload", {}),
        ("delete_after_days", {"days": 0}),     # every chunk "past 0 days"
        ("size_cap", {"size_cap_mb": 1}),        # cap forces eviction pressure
    ])
    def test_cloud_chunk_not_deleted_when_remote_missing(
        self, tmp_path, policy_name, params,
    ):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=3)
        _cloud_uploaded(ledger, 0, 1, 2)
        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy(policy_name),
            params=params,
        )

        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger,
            remote_exists=_always_false,   # GCS says NOT there
        )

        # Nothing deleted; every chunk still on disk; none reached EVICTED.
        for i in range(3):
            assert _present(rec_dir, i), f"chunk {i} was deleted with remote missing"
        assert report.evicted_indices == []
        from screencap.pipeline_state import EvictState
        for row in ledger.all_chunks():
            assert row.evict_state != EvictState.EVICTED


class TestUnEvictableFloor:
    """The un-evictable floor consults the LEDGER, not file age/size. The
    currently-recording / in-flight / FAILED chunks are NEVER candidates."""

    def test_in_flight_chunk_never_candidate(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=3)
        # chunk 0 uploaded; chunk 1 still STAGED (in-flight); chunk 2 PENDING.
        _cloud_uploaded(ledger, 0)
        # chunk 1 left STAGED, chunk 2 left at seeded PENDING.
        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy.SIZE_CAP,
            params={"size_cap_mb": 1},   # heavy pressure: cap below current usage
        )

        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger, remote_exists=_always_true,
        )

        # Only the UPLOADED chunk 0 was a candidate. 1 (STAGED) + 2 (PENDING)
        # are in-flight and untouchable regardless of size pressure.
        assert _present(rec_dir, 1)
        assert _present(rec_dir, 2)
        assert 1 not in report.evicted_indices
        assert 2 not in report.evicted_indices

    def test_failed_chunk_never_evicted(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2)
        ledger.mark_failed(0, detail="scrub failed")
        _cloud_uploaded(ledger, 1)
        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy.DELETE_AFTER_UPLOAD,
            params={},
        )

        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger, remote_exists=_always_true,
        )

        assert _present(rec_dir, 0), "FAILED chunk must never be evicted"
        assert 0 not in report.evicted_indices


# ===========================================================================
# AE9 — size-cap eviction with one FAILED (un-uploaded) oldest chunk.
# ===========================================================================


class TestAE9SizeCapWithFailedChunk:
    def test_size_cap_never_deletes_failed_oldest_chunk(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        # 4 chunks @ 1 MiB each. chunk 0 (the OLDEST) is FAILED & un-uploaded.
        rec_dir, ledger = _make_recording(tmp_path, n_chunks=4, chunk_bytes=1024 * 1024)
        ledger.mark_failed(0, detail="upload failed")
        _cloud_uploaded(ledger, 1, 2, 3)
        # Cap at 1 MB: must evict down to <= 1 MB of evictable media. The
        # oldest is chunk 0 but it is FAILED -> never deleted; eviction must
        # take 1,2 (oldest evictable first) instead.
        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy.SIZE_CAP,
            params={"size_cap_mb": 1},
        )

        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger, remote_exists=_always_true,
        )

        assert _present(rec_dir, 0), "FAILED oldest chunk must survive size-cap"
        assert 0 not in report.evicted_indices
        # The cap was satisfied by evicting uploaded chunks only.
        assert set(report.evicted_indices).issubset({1, 2, 3})
        assert report.evicted_indices, "size cap should have evicted something"


# ===========================================================================
# AE3 — local-only: keep-forever default never deletes; a size cap evicts.
# ===========================================================================


class TestAE3LocalRetention:
    def test_local_keep_forever_never_evicts(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=3)
        _local_done(ledger, 0, 1, 2)
        policy = ResolvedPolicy(
            destination=Destination.LOCAL,
            retention_policy=RetentionPolicy.KEEP_FOREVER,
            params={},
        )

        report = evict_recording(rec_dir, policy=policy, ledger=ledger)

        for i in range(3):
            assert _present(rec_dir, i)
        assert report.evicted_indices == []

    def test_local_size_cap_evicts_oldest_past_cap_no_remote_precondition(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        # 4 chunks @ ~3 MiB. Cap at 7 MB -> keep the 2 newest (~6 MiB), evict
        # the 2 oldest. A whole-MiB-per-chunk size keeps the sidecar bytes well
        # within the cap's headroom so the boundary is unambiguous.
        rec_dir, ledger = _make_recording(tmp_path, n_chunks=4, chunk_bytes=3 * 1024 * 1024)
        _local_done(ledger, 0, 1, 2, 3)
        policy = ResolvedPolicy(
            destination=Destination.LOCAL,
            retention_policy=RetentionPolicy.SIZE_CAP,
            params={"size_cap_mb": 7},
        )

        # NOTE: no remote_exists passed — local eviction has NO upload/remote
        # precondition (the local path of the floor).
        report = evict_recording(rec_dir, policy=policy, ledger=ledger)

        # Oldest two evicted; newest two kept.
        assert not _present(rec_dir, 0)
        assert not _present(rec_dir, 1)
        assert _present(rec_dir, 2)
        assert _present(rec_dir, 3)
        assert set(report.evicted_indices) == {0, 1}


# ===========================================================================
# AE5 — long cloud recording, delete_after_upload, mid-recording eviction.
# ===========================================================================


class TestAE5CloudDeleteAfterUpload:
    def test_uploaded_chunk_local_copy_evicted_during_recording(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        # chunks 0,1 uploaded; chunk 2 still recording (PENDING/in-flight).
        rec_dir, ledger = _make_recording(tmp_path, n_chunks=3)
        _cloud_uploaded(ledger, 0, 1)
        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy.DELETE_AFTER_UPLOAD,
            params={},
        )

        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger,
            remote_exists=_always_true, during_recording=True,
        )

        # The uploaded chunks' local copies are gone; the recording chunk stays.
        assert not _present(rec_dir, 0)
        assert not _present(rec_dir, 1)
        assert _present(rec_dir, 2)
        assert set(report.evicted_indices) == {0, 1}
        # Ledger reflects EVICTED but upload_state stays UPLOADED (finalize gate
        # still satisfied — eviction does not relax completeness).
        from screencap.pipeline_state import EvictState, Lifecycle, UploadState
        for i in (0, 1):
            row = ledger.get_chunk(i)
            assert row.evict_state == EvictState.EVICTED
            assert row.lifecycle == Lifecycle.EVICTED
            assert row.upload_state == UploadState.UPLOADED
        assert ledger.finalize_gate_satisfied() is False  # chunk 2 not uploaded yet

    def test_keep_recent_window_preserves_most_recent_uploaded(self, tmp_path):
        """SCR-125 U2: ``keep_recent=2`` (the live-path window) keeps the 2
        most-recent UPLOADED chunks on disk and evicts the older confirmed ones
        — matching the during-recording reclaim the live path always did, but now
        through the fresh-remote-confirm floor."""
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        # 5 chunks all uploaded + remote-confirmed.
        rec_dir, ledger = _make_recording(tmp_path, n_chunks=5)
        _cloud_uploaded(ledger, 0, 1, 2, 3, 4)
        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy.DELETE_AFTER_UPLOAD,
            params={},
        )

        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger,
            remote_exists=_always_true, during_recording=True, keep_recent=2,
        )

        # The 3 oldest are evicted; the 2 most-recent stay on disk.
        assert set(report.evicted_indices) == {0, 1, 2}
        for i in (0, 1, 2):
            assert not _present(rec_dir, i)
        assert _present(rec_dir, 3)
        assert _present(rec_dir, 4)

    def test_keep_recent_zero_at_finalize_evicts_all(self, tmp_path):
        """keep_recent=0 (the finalize/terminal default) evicts EVERY uploaded
        chunk — no during-recording window at the end."""
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=3)
        _cloud_uploaded(ledger, 0, 1, 2)
        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy.DELETE_AFTER_UPLOAD,
            params={},
        )

        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger,
            remote_exists=_always_true, keep_recent=0,
        )
        assert set(report.evicted_indices) == {0, 1, 2}


# ===========================================================================
# Resume — an interrupted eviction (EVICT_PENDING) resumes on re-run.
# ===========================================================================


class TestResumeInterruptedEviction:
    def test_evict_pending_resumes_no_disk_leak(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.pipeline_state import EvictState, Lifecycle
        from screencap.retention import evict_recording

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2)
        _cloud_uploaded(ledger, 0, 1)
        # Simulate a crash AFTER begin_eviction committed EVICT_PENDING for
        # chunk 0 but BEFORE the unlink: the file is still on disk.
        ledger.begin_eviction(0, remote_exists=lambda: True)
        assert ledger.get_chunk(0).evict_state == EvictState.EVICT_PENDING
        assert _present(rec_dir, 0)  # crash mid-unlink: file still present

        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy.DELETE_AFTER_UPLOAD,
            params={},
        )
        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger, remote_exists=_always_true,
        )

        # The interrupted eviction resumed: file gone, ledger EVICTED, no leak.
        assert not _present(rec_dir, 0)
        assert ledger.get_chunk(0).lifecycle == Lifecycle.EVICTED
        assert 0 in report.evict_pending_resumed or 0 in report.evicted_indices

    def test_resume_re_confirms_remote_before_unlink(self, tmp_path):
        """An EVICT_PENDING chunk must re-confirm remote on resume; if the
        remote is now MISSING, it must NOT delete (rule 3 survives the crash)."""
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=1)
        _cloud_uploaded(ledger, 0)
        ledger.begin_eviction(0, remote_exists=lambda: True)  # crashed before unlink

        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy.DELETE_AFTER_UPLOAD,
            params={},
        )
        # Remote now reports MISSING on resume.
        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger, remote_exists=_always_false,
        )

        assert _present(rec_dir, 0), "resume must re-confirm remote, not blind-unlink"
        assert 0 not in report.evicted_indices


# ===========================================================================
# N-days + cloud vs. local — the upload precondition overrides age for cloud.
# ===========================================================================


class TestDeleteAfterDays:
    def _age_chunk(self, ledger, idx, *, age_days: float, now: float):
        """Force a chunk's ledger updated_at to be ``age_days`` old."""
        import sqlite3
        old = now - age_days * 86400.0
        conn = sqlite3.connect(str(ledger._db_path))
        try:
            conn.execute(
                "UPDATE pipeline_chunk_state SET updated_at=? WHERE chunk_index=?",
                (old, idx),
            )
            conn.commit()
        finally:
            conn.close()

    def test_cloud_chunk_past_days_but_unuploaded_not_deleted(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        now = 2_000_000.0
        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2)
        # chunk 0 FAILED (un-uploaded) but ancient; chunk 1 uploaded + ancient.
        ledger.mark_failed(0)
        _cloud_uploaded(ledger, 1)
        self._age_chunk(ledger, 0, age_days=100, now=now)
        self._age_chunk(ledger, 1, age_days=100, now=now)

        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy.DELETE_AFTER_DAYS,
            params={"days": 30},
        )
        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger,
            remote_exists=_always_true, now=now,
        )

        # The un-uploaded chunk 0 is past N days but NOT deleted (upload
        # precondition overrides age). chunk 1 is uploaded + old -> deleted.
        assert _present(rec_dir, 0), "un-uploaded cloud chunk past N days must survive"
        assert not _present(rec_dir, 1)
        assert report.evicted_indices == [1]

    def test_local_chunk_past_days_is_deleted(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        now = 2_000_000.0
        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2)
        _local_done(ledger, 0, 1)
        self._age_chunk(ledger, 0, age_days=100, now=now)   # old -> evict
        self._age_chunk(ledger, 1, age_days=1, now=now)     # fresh -> keep

        policy = ResolvedPolicy(
            destination=Destination.LOCAL,
            retention_policy=RetentionPolicy.DELETE_AFTER_DAYS,
            params={"days": 30},
        )
        report = evict_recording(rec_dir, policy=policy, ledger=ledger, now=now)

        assert not _present(rec_dir, 0)
        assert _present(rec_dir, 1)
        assert report.evicted_indices == [0]


# ===========================================================================
# both — rich local follows configured retention; masked cloud copy evicted
# immediately post-upload-confirm (a fixed rule, not the configurable policy).
# ===========================================================================


class TestBothDestination:
    def test_masked_cloud_copy_evicted_immediately_local_follows_policy(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording
        from screencap.scrubber import masked_video_dir

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2)
        _cloud_uploaded(ledger, 0, 1)

        # Materialize a masked cloud copy at <name>-scrubbed/masked_video/.
        scrubbed_dir = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        mv = masked_video_dir(scrubbed_dir)
        mv.mkdir(parents=True, exist_ok=True)
        for i in (0, 1):
            (mv / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 512)

        # both + keep_forever for the LOCAL copy.
        policy = ResolvedPolicy(
            destination=Destination.BOTH,
            retention_policy=RetentionPolicy.KEEP_FOREVER,
            params={},
        )
        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger, remote_exists=_always_true,
        )

        # Masked cloud copies evicted immediately (fixed rule); rich local kept
        # (keep_forever).
        assert not (mv / "chunk_0000.mp4").exists()
        assert not (mv / "chunk_0001.mp4").exists()
        for i in (0, 1):
            assert _present(rec_dir, i), "rich local copy must follow keep_forever"
        assert set(report.masked_copies_evicted) == {0, 1}
        assert report.evicted_indices == []

    def test_masked_copy_not_evicted_when_remote_missing(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording
        from screencap.scrubber import masked_video_dir

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=1)
        _cloud_uploaded(ledger, 0)
        scrubbed_dir = rec_dir.parent / f"{rec_dir.name}-scrubbed"
        mv = masked_video_dir(scrubbed_dir)
        mv.mkdir(parents=True, exist_ok=True)
        (mv / "chunk_0000.mp4").write_bytes(b"\x00" * 512)

        policy = ResolvedPolicy(
            destination=Destination.BOTH,
            retention_policy=RetentionPolicy.KEEP_FOREVER,
            params={},
        )
        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger, remote_exists=_always_false,
        )

        # The masked copy is a cloud artifact — never deleted without a fresh
        # remote confirm either.
        assert (mv / "chunk_0000.mp4").exists()
        assert report.masked_copies_evicted == []


# ===========================================================================
# Keep-forever cloud — uploaded chunks are NOT evicted (policy gates, floor
# permits, but keep_forever means keep the local rich copy).
# ===========================================================================


class TestKeepForeverCloud:
    def test_cloud_keep_forever_does_not_evict_uploaded_local(self, tmp_path):
        from screencap.pipeline_policy import Destination, ResolvedPolicy, RetentionPolicy
        from screencap.retention import evict_recording

        rec_dir, ledger = _make_recording(tmp_path, n_chunks=2)
        _cloud_uploaded(ledger, 0, 1)
        policy = ResolvedPolicy(
            destination=Destination.CLOUD,
            retention_policy=RetentionPolicy.KEEP_FOREVER,
            params={},
        )
        report = evict_recording(
            rec_dir, policy=policy, ledger=ledger, remote_exists=_always_true,
        )
        for i in (0, 1):
            assert _present(rec_dir, i)
        assert report.evicted_indices == []
