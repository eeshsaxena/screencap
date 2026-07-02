"""Tests for screencap.chunk_processor — ChunkProcessor pipeline."""

from __future__ import annotations

import json
import multiprocessing
import time
from collections import defaultdict
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_terminal_run_dir(tmp_path, monkeypatch):
    """Point the per-chunk terminal-stage flock dir at a per-test tmp dir.

    SCR-125 U1 wraps the live per-chunk upload in ``terminal_lock``; without
    isolation those tests would acquire the real ``~/.screencap/run`` flock and
    could contend with a running daemon / other tests.
    """
    import screencap.terminal_stage as ts

    monkeypatch.setattr(ts, "_RUN_DIR", tmp_path / "ts-run")


@pytest.fixture
def capture_dir(recording_db):
    """Capture directory with a real engine DB schema."""
    return recording_db.db_path.parent


def test_run_loop_survives_long_idle_and_responds(capture_dir):
    """Regression: _run must survive 12s of empty queue and still process messages.

    Previously, queue.Empty (normal timeout) was caught by `except Exception`,
    which incremented consecutive_errors. After 5 timeouts (10s), the thread
    exited — killing the processor before any chunk rotation message arrived.
    """
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir,
        q,
        ack_q,
        recording_name="test",
        upload_enabled=False,
        auto_delete=False,
    )
    cp.start()

    # Wait longer than the old 10s death threshold
    time.sleep(12)

    # Thread must still be alive
    assert cp._thread is not None
    assert cp._thread.is_alive(), (
        "ChunkProcessor thread died after 12s of empty queue — "
        "queue.Empty must not be treated as an error"
    )

    # Verify it's still responsive by sending a poison pill
    q.put({"type": "poison_pill"})
    cp._thread.join(timeout=5)
    assert not cp._thread.is_alive(), "Thread should have exited after poison pill"


def test_all_chunks_uploaded_empty_returns_false(capture_dir):
    """all_chunks_uploaded() must return False when no chunks were processed."""
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir, q, ack_q, recording_name="test",
        upload_enabled=False, auto_delete=False,
    )
    assert cp.all_chunks_uploaded() is False


def test_was_force_stopped_reflects_stop_event(capture_dir):
    """was_force_stopped is False normally, True after _stop_event is set."""
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir, q, ack_q, recording_name="test",
        upload_enabled=False, auto_delete=False,
    )
    assert cp.was_force_stopped is False

    # Simulate what stop() does when the thread times out
    cp._stop_event.set()
    assert cp.was_force_stopped is True


# ---------------------------------------------------------------------------
# Cloud-intent scrubbing tests
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_capture_dir(recording_db):
    """Capture dir with a real engine DB schema including all tables."""
    return recording_db.db_path.parent


def _make_mock_pipeline():
    """Create a mock pipeline/anonymizer that detects 'John Smith' as PERSON."""
    from screencap.redaction import Anonymizer, Detection, DetectionResult

    pipeline = MagicMock()

    def mock_detect(text):
        detections = []
        idx = text.find("John Smith")
        while idx != -1:
            detections.append(Detection(
                entity_type="PERSON",
                start=idx,
                end=idx + 10,
                score=0.95,
                source="test",
            ))
            idx = text.find("John Smith", idx + 10)
        return DetectionResult(text, detections)

    pipeline.detect = mock_detect
    anonymizer = Anonymizer()
    return pipeline, anonymizer


@pytest.fixture
def cloud_processor(cloud_capture_dir):
    """ChunkProcessor with mock pipeline for cloud-intent scrubbing tests."""
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        cloud_capture_dir, q, ack_q, recording_name="test",
        upload_enabled=False, auto_delete=False,
        cloud_intent=True,
    )

    pipeline, anonymizer = _make_mock_pipeline()
    # SCR-35: the masking config now lives on the ChunkScrubber seam, not on
    # ChunkProcessor. This fixture (cloud_intent + upload_enabled=False) does not
    # init scrubbing, so rebuild the seam with the injected mock pipeline. The
    # direct scrub-function tests reach the (pipeline, anonymizer) pair off the
    # seam (cp._chunk_scrubber._pipeline / ._anonymizer) — ChunkProcessor no
    # longer carries those fields. evaluator/classifier are None here
    # (cloud_processor never inited masking), matching the prior behavior.
    from screencap.chunk_scrubber import ChunkScrubber

    cp._chunk_scrubber = ChunkScrubber(
        cloud_capture_dir,
        enabled=True,
        pipeline=pipeline,
        anonymizer=anonymizer,
        evaluator=None,
        classifier=None,
        pixel_ratio=2.0,
    )
    return cp


class TestCloudIntentGating:
    """Test that cloud-intent recordings gate media uploads."""

    def test_collect_chunk_files_includes_video_audio_for_cloud_intent(self, cloud_capture_dir):
        """Cloud-intent now includes .mp4/.flac (with placeholder frames for blocked intervals)."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"video data")
        (cloud_capture_dir / "audio_0000.flac").write_bytes(b"audio data")
        (cloud_capture_dir / "events_0000.jsonl").write_text('{"name":"click"}\n')
        (cloud_capture_dir / "chunk_0000_manifest.json").write_text('{}')

        # Cloud-intent: includes video and audio (with placeholder redaction)
        cp_cloud = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False, cloud_intent=True,
        )
        cloud_names = [f["name"] for f in cp_cloud._collect_chunk_files(0, None)]
        assert "chunk_0000.mp4" in cloud_names
        assert "audio_0000.flac" in cloud_names
        assert "events_0000.jsonl" in cloud_names
        assert "chunk_0000_manifest.json" in cloud_names

        # Non-cloud: everything
        cp_local = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False, cloud_intent=False,
        )
        local_names = [f["name"] for f in cp_local._collect_chunk_files(0, None)]
        assert "chunk_0000.mp4" in local_names
        assert "audio_0000.flac" in local_names

    def test_masked_path_switch_off_uploads_source_video(self, cloud_capture_dir):
        """SCR-125 R-SCR125-A (flag OFF, this milestone's default): the live
        per-chunk upload set's .mp4 slot resolves to the SOURCE video — the
        byte-for-byte-today guarantee."""
        from screencap.chunk_processor import ChunkProcessor

        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"rich video")
        cp = ChunkProcessor(
            cloud_capture_dir, multiprocessing.Queue(), multiprocessing.Queue(),
            recording_name="test", upload_enabled=False, auto_delete=False,
            cloud_intent=True, masked_video_upload=False,
        )
        files = cp._collect_chunk_files(0, None)
        video = next(f for f in files if f["name"] == "chunk_0000.mp4")
        assert video["path"] == cloud_capture_dir / "chunk_0000.mp4"

    def test_masked_path_switch_on_uploads_masked_copy_not_source(self, cloud_capture_dir):
        """SCR-125 R-SCR125-A (flag ON, security P0): the .mp4 slot resolves to
        the masked copy under <name>-scrubbed/masked_video/, and the rich SOURCE
        .mp4 path is NEVER in the upload set. The GCS object name is unchanged."""
        from screencap.chunk_processor import ChunkProcessor

        # Both the rich source AND a masked copy exist on disk.
        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"rich video")
        masked_dir = cloud_capture_dir.parent / f"{cloud_capture_dir.name}-scrubbed" / "masked_video"
        masked_dir.mkdir(parents=True)
        masked_video = masked_dir / "chunk_0000.mp4"
        masked_video.write_bytes(b"masked video")

        cp = ChunkProcessor(
            cloud_capture_dir, multiprocessing.Queue(), multiprocessing.Queue(),
            recording_name="test", upload_enabled=False, auto_delete=False,
            cloud_intent=True, masked_video_upload=True,
        )
        files = cp._collect_chunk_files(0, None)
        video = next(f for f in files if f["name"] == "chunk_0000.mp4")
        # The GCS object name is unchanged ...
        assert video["name"] == "chunk_0000.mp4"
        # ... but the local source is the MASKED copy, never the rich source.
        assert video["path"] == masked_video
        source = cloud_capture_dir / "chunk_0000.mp4"
        assert all(f["path"] != source for f in files), (
            "the rich SOURCE .mp4 must NEVER be in the upload set when the "
            "masked_video_upload flag is ON (R-SCR125-A)"
        )

    def test_recording_db_never_in_upload_set(self, cloud_capture_dir):
        """AE4: recording.db is never among the per-chunk uploaded files under
        either flag state (it is local-only by rule, R8)."""
        from screencap.chunk_processor import ChunkProcessor

        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"v")
        for flag in (False, True):
            cp = ChunkProcessor(
                cloud_capture_dir, multiprocessing.Queue(), multiprocessing.Queue(),
                recording_name="test", upload_enabled=False, auto_delete=False,
                cloud_intent=True, masked_video_upload=flag,
            )
            names = [f["name"] for f in cp._collect_chunk_files(0, None)]
            assert "recording.db" not in names

    def test_frozen_masked_flag_read_from_intent_not_global(self, cloud_capture_dir):
        """SCR-125 R-SCR125-A: the masked_video_upload value is read from the
        FROZEN .recording_intent, so flipping the global mid-recording does not
        change which video path the live upload ships."""
        import json as _json

        from screencap.chunk_processor import ChunkProcessor

        # Freeze ON in the intent; the global is mocked OFF.
        (cloud_capture_dir / ".recording_intent").write_text(_json.dumps({
            "version": 2, "destination": "cloud",
            "masked_video_upload": True,
        }))
        with mock.patch(
            "screencap.config.get_masked_video_upload_enabled", return_value=False,
        ):
            cp = ChunkProcessor(
                cloud_capture_dir, multiprocessing.Queue(), multiprocessing.Queue(),
                recording_name="test", upload_enabled=False, auto_delete=False,
                cloud_intent=True,  # masked_video_upload=None → resolve from intent
            )
        assert cp._masked_video_upload is True, (
            "the frozen .recording_intent value must win over the mutable global"
        )

    def test_cloud_upload_chunk_defers_on_lock_contention(self, cloud_capture_dir):
        """SCR-125 U1 lock contract: a concurrent holder of the per-recording
        terminal flock makes the live per-chunk op DEFER (leave PENDING), never
        deadlock or lose the chunk."""
        import threading

        from screencap import terminal_stage as ts
        from screencap.chunk_processor import ChunkProcessor, _UploadOutcome

        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"v")
        (cloud_capture_dir / "audio_0000.flac").write_bytes(b"a")
        (cloud_capture_dir / "events_0000.jsonl").write_text("{}\n")
        (cloud_capture_dir / "chunk_0000_manifest.json").write_text("{}")

        cp = ChunkProcessor(
            cloud_capture_dir, multiprocessing.Queue(), multiprocessing.Queue(),
            recording_name="rec-deferral", upload_enabled=True, auto_delete=False,
            cloud_intent=True, masked_video_upload=False,
        )
        held = threading.Event()
        release = threading.Event()

        def _hold():
            with ts.terminal_lock("rec-deferral"):
                held.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=_hold)
        holder.start()
        assert held.wait(timeout=5)
        try:
            # The live op cannot acquire the contended flock → DEFERRED.
            outcome = cp._cloud_upload_chunk(0, 0.0, 5.0, None)
            assert outcome is _UploadOutcome.DEFERRED
        finally:
            release.set()
            holder.join(timeout=5)

    def test_retry_backoff_releases_flock(self, cloud_capture_dir):
        """SCR-130: the per-chunk upload retry backoff must run OUTSIDE the held
        terminal flock, so a transient upload failure does not extend the
        critical section a concurrent manual upload / daemon resume waits on."""
        import threading

        from screencap import terminal_stage as ts
        from screencap.chunk_processor import ChunkProcessor, _UploadOutcome

        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"v")
        (cloud_capture_dir / "audio_0000.flac").write_bytes(b"a")
        (cloud_capture_dir / "events_0000.jsonl").write_text("{}\n")
        (cloud_capture_dir / "chunk_0000_manifest.json").write_text("{}")

        cp = ChunkProcessor(
            cloud_capture_dir, multiprocessing.Queue(), multiprocessing.Queue(),
            recording_name="rec-backoff", upload_enabled=True, auto_delete=False,
            cloud_intent=True, masked_video_upload=False,
        )

        attempts = {"n": 0}

        def _upload_side_effect(recording_name, files, capture_dir):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("transient failure")
            return True

        # The backoff fires here, between the two upload attempts. Probe from a
        # SEPARATE thread (robust whether the in-process lock is a Lock or
        # RLock): if the flock is free the probe acquires it; if it is still
        # held (the bug) the probe raises TerminalStageBusy.
        lock_free_during_backoff: list[bool] = []

        def _probe_sleep(_secs):
            result = {}

            def _probe():
                try:
                    with ts.terminal_lock("rec-backoff", non_blocking=True):
                        result["free"] = True
                except ts.TerminalStageBusy:
                    result["free"] = False

            t = threading.Thread(target=_probe)
            t.start()
            t.join(timeout=5)
            lock_free_during_backoff.append(result.get("free"))

        with (
            mock.patch(
                "screencap.chunk_processor.upload_chunk_files",
                side_effect=_upload_side_effect,
            ),
            mock.patch(
                "screencap.chunk_processor.time.sleep", side_effect=_probe_sleep,
            ),
        ):
            outcome = cp._cloud_upload_chunk(0, 0.0, 5.0, None)

        # The retry recovered (attempt 1 raised, attempt 2 succeeded) ...
        assert outcome is _UploadOutcome.UPLOADED
        assert attempts["n"] == 2
        # ... and the flock was released during the backoff sleep.
        assert lock_free_during_backoff == [True], (
            "the retry backoff must run OUTSIDE the held terminal flock (SCR-130)"
        )

    def test_retry_then_lock_contention_defers(self, cloud_capture_dir):
        """SCR-130: the retry re-acquires the flock, so a manual upload / daemon
        resume that grabs the lock DURING the backoff makes the retry attempt
        DEFER (TerminalStageBusy on re-acquire) — the chunk is left PENDING for
        the convergence pass, never marked FAILED. This release-then-contend path
        does not exist in the pre-SCR-130 single-acquisition design."""
        from screencap import terminal_stage as ts
        from screencap.chunk_processor import ChunkProcessor, _UploadOutcome

        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"v")
        (cloud_capture_dir / "audio_0000.flac").write_bytes(b"a")
        (cloud_capture_dir / "events_0000.jsonl").write_text("{}\n")
        (cloud_capture_dir / "chunk_0000_manifest.json").write_text("{}")

        cp = ChunkProcessor(
            cloud_capture_dir, multiprocessing.Queue(), multiprocessing.Queue(),
            recording_name="rec-retry-defer", upload_enabled=True,
            auto_delete=False, cloud_intent=True, masked_video_upload=False,
        )

        # Attempt 0 acquires the lock normally and the upload raises (→ backoff);
        # attempt 1's re-acquire finds the lock contended and must DEFER.
        real_terminal_lock = ts.terminal_lock
        lock_calls = {"n": 0}

        def _lock_side_effect(name, non_blocking=False):
            lock_calls["n"] += 1
            if lock_calls["n"] >= 2:
                raise ts.TerminalStageBusy("contended on retry")
            return real_terminal_lock(name, non_blocking=non_blocking)

        with (
            mock.patch(
                "screencap.chunk_processor.upload_chunk_files",
                side_effect=ConnectionError("transient failure"),
            ),
            mock.patch(
                "screencap.terminal_stage.terminal_lock",
                side_effect=_lock_side_effect,
            ),
            mock.patch("screencap.chunk_processor.time.sleep"),  # skip 5s backoff
        ):
            outcome = cp._cloud_upload_chunk(0, 0.0, 5.0, None)

        assert outcome is _UploadOutcome.DEFERRED
        assert lock_calls["n"] == 2, (
            "the retry must re-acquire the flock; contention on that second "
            "acquisition is what produces DEFERRED (SCR-130)"
        )

    def test_retry_remasks_idempotently(self, cloud_capture_dir):
        """SCR-130 idempotency invariant: with masked_video_upload ON, the retry
        re-enters the critical section and re-runs mask_chunk_for_cloud. The PR's
        safety argument rests on that re-mask being idempotent; this pins that a
        transient first attempt recovers (UPLOADED) and the masker is invoked on
        BOTH attempts."""
        from screencap.chunk_processor import ChunkProcessor, _UploadOutcome

        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"rich video")
        (cloud_capture_dir / "audio_0000.flac").write_bytes(b"a")
        (cloud_capture_dir / "events_0000.jsonl").write_text("{}\n")
        (cloud_capture_dir / "chunk_0000_manifest.json").write_text("{}")
        masked_dir = (
            cloud_capture_dir.parent
            / f"{cloud_capture_dir.name}-scrubbed" / "masked_video"
        )
        masked_dir.mkdir(parents=True)
        (masked_dir / "chunk_0000.mp4").write_bytes(b"masked video")

        cp = ChunkProcessor(
            cloud_capture_dir, multiprocessing.Queue(), multiprocessing.Queue(),
            recording_name="rec-remask", upload_enabled=True, auto_delete=False,
            cloud_intent=True, masked_video_upload=True,
        )

        attempts = {"n": 0}

        def _upload_side_effect(recording_name, files, capture_dir):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("transient failure")
            return True

        mask = mock.MagicMock(return_value=mock.MagicMock(failed=False))

        with (
            mock.patch(
                "screencap.pipeline_chunk_ops.mask_chunk_for_cloud", mask,
            ),
            mock.patch(
                "screencap.chunk_processor.upload_chunk_files",
                side_effect=_upload_side_effect,
            ),
            mock.patch("screencap.chunk_processor.time.sleep"),  # skip 5s backoff
        ):
            outcome = cp._cloud_upload_chunk(0, 0.0, 5.0, None)

        assert outcome is _UploadOutcome.UPLOADED
        assert attempts["n"] == 2
        # The masker ran on BOTH attempts — re-entering the critical section on
        # retry re-masks, and that must be idempotent (SCR-130).
        assert mask.call_count == 2

    def test_checkpoint_and_upload_db_never_uploads_raw_db(self, cloud_capture_dir):
        """U2: checkpoint_and_upload_db NEVER uploads the raw recording.db — for
        any destination — because the DB is local-only by rule (R8). It only
        WAL-checkpoints; the R8 exclusion lives at the single upload seam now.
        Asserts no network call is made (the upload was retired) for both
        cloud_intent=True and cloud_intent=False, and the DB stays on disk."""
        from unittest.mock import patch as _patch

        from screencap.chunk_processor import checkpoint_and_upload_db

        for cloud_intent in (True, False):
            with _patch("screencap.upload.request_signed_urls") as signed, \
                 _patch("screencap.chunk_processor._upload_single") as up:
                result = checkpoint_and_upload_db(
                    cloud_capture_dir, "test", cloud_intent=cloud_intent,
                )
            assert result is True
            signed.assert_not_called()  # the raw DB upload was retired
            up.assert_not_called()
            assert (cloud_capture_dir / "recording.db").exists()  # kept local

    def test_checkpoint_and_upload_db_missing_db_is_noop(self, tmp_path):
        """Legacy/migrated recording with no recording.db → clean no-op, no
        crash (the WAL-checkpoint precondition must tolerate an absent DB)."""
        from screencap.chunk_processor import checkpoint_and_upload_db

        rec = tmp_path / "legacy"
        rec.mkdir()
        assert checkpoint_and_upload_db(rec, "test", cloud_intent=False) is True

    def test_pipeline_init_failure_disables_uploads(self, cloud_capture_dir):
        """If privacy deps fail to import, uploads must be disabled (fail-closed)."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        with patch(
            "screencap.redaction.engine.create_default_pipeline",
            side_effect=ImportError("test: no privacy deps"),
        ):
            cp = ChunkProcessor(
                cloud_capture_dir, q, ack_q, recording_name="test",
                upload_enabled=True, auto_delete=False,
                cloud_intent=True,
            )
            assert cp._upload_enabled is False
            # Pipeline init failed → the scrub seam is inert (SCR-35).
            assert cp._chunk_scrubber.is_enabled is False

    def test_non_cloud_skips_pipeline_init(self, cloud_capture_dir):
        """Non-cloud recordings must not initialize the scrubbing pipeline."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=True, auto_delete=False,
            cloud_intent=False,
        )
        # No scrub/masking init → seam inert and no masking context (SCR-35).
        assert cp._chunk_scrubber.is_enabled is False
        assert cp._chunk_scrubber.has_masking_context is False


class TestCloudProcessorRequiresPiiDetection:
    """Cloud-intent ChunkProcessor must pass require_pii=True to the pipeline factory."""

    def test_cloud_processor_requires_pii_detection(self, cloud_capture_dir):
        """Exercises pipeline gating for cloud vs local intent.

        1. Cloud-intent passes require_pii=True to create_default_pipeline
        2. Pipeline failure exposes upload_warning property
        3. Non-cloud skips pipeline init entirely
        """
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        # 1. Cloud-intent: require_pii=True is passed
        with patch(
            "screencap.redaction.engine.create_default_pipeline",
        ) as mock_pipeline:
            mock_pipeline.return_value = MagicMock()
            with patch("screencap.redaction.engine.Anonymizer"):
                cp = ChunkProcessor(
                    cloud_capture_dir, q, ack_q, recording_name="test",
                    upload_enabled=True, auto_delete=False,
                    cloud_intent=True,
                )
            mock_pipeline.assert_called_once()
            _, kwargs = mock_pipeline.call_args
            assert kwargs.get("require_pii") is True

        # 2. Pipeline failure: upload_warning exposed
        with patch(
            "screencap.redaction.engine.create_default_pipeline",
            side_effect=ImportError("no PII models"),
        ):
            cp = ChunkProcessor(
                cloud_capture_dir, q, ack_q, recording_name="test",
                upload_enabled=True, auto_delete=False,
                cloud_intent=True,
            )
            assert cp._upload_enabled is False
            assert cp.upload_warning is not None
            assert "no PII models" in cp.upload_warning

        # 3. Non-cloud: pipeline not initialized, no warning
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=True, auto_delete=False,
            cloud_intent=False,
        )
        assert cp._chunk_scrubber.is_enabled is False
        assert cp.upload_warning is None


class TestSequencerStructure:
    """SCR-35: ChunkProcessor is a sequencer — manifest/scrub concerns live in
    the ChunkManifest/ChunkScrubber seams, and the old inline fields/methods
    are gone."""

    def test_extracted_concerns_removed_from_processor(self, cloud_capture_dir):
        from screencap.chunk_processor import ChunkProcessor

        cp = ChunkProcessor(
            cloud_capture_dir, multiprocessing.Queue(), multiprocessing.Queue(),
            recording_name="test", upload_enabled=False, auto_delete=False,
            cloud_intent=False,
        )
        # The two seam handles exist...
        assert cp._chunk_manifest is not None
        assert cp._chunk_scrubber is not None
        # ...and the old inline manifest/scrub fields + methods are gone.
        # _scrub_enabled / _rest_threshold / _screen_filter are passed straight
        # into the seams; the processor keeps no (trap-prone) copy.
        for removed in (
            "_segmentation_mode", "_pipeline", "_anonymizer",
            "_masking_classifier", "_masking_evaluator", "_masking_pixel_ratio",
            "_generate_manifest", "_scrub_chunk_files",
            "_scrub_enabled", "_rest_threshold", "_screen_filter",
        ):
            assert not hasattr(cp, removed), f"{removed} should be gone after SCR-35"

    def test_content_index_guard_reads_seam(self):
        """The content-index fail-closed guard sources its signal from the seam
        (R7), not a (removed) processor masking field."""
        import inspect

        from screencap.chunk_processor import ChunkProcessor

        src = inspect.getsource(ChunkProcessor._do_index_chunk_content)
        assert "_chunk_scrubber.has_masking_context" in src
        assert "_masking_classifier" not in src
        assert "_masking_evaluator" not in src


class TestInlineScrubbing:
    """Test inline scrubbing of text surfaces."""

    def test_scrub_transcripts_txt_and_json(self, cloud_capture_dir, cloud_processor):
        """Both transcript formats must have PII replaced, including segment text."""
        from screencap.scrubber import scrub_transcripts

        txt_path = cloud_capture_dir / "transcript_0000.txt"
        txt_path.write_text("Meeting with John Smith about the project")

        json_path = cloud_capture_dir / "transcript_0000.json"
        json_path.write_text(json.dumps({
            "text": "Call with John Smith",
            "segments": [
                {"start": 0, "end": 5, "text": "Call with John Smith"},
            ],
        }))

        cp = cloud_processor
        scrub_transcripts([txt_path, json_path], cp._chunk_scrubber._pipeline, cp._chunk_scrubber._anonymizer)

        # .txt
        txt_result = txt_path.read_text()
        assert "John Smith" not in txt_result
        assert "<PERSON>" in txt_result

        # .json top-level and segment
        json_data = json.loads(json_path.read_text())
        assert "John Smith" not in json_data["text"]
        assert "<PERSON>" in json_data["text"]
        assert "John Smith" not in json_data["segments"][0]["text"]

    def test_scrub_manifest_dominant_title(self, cloud_capture_dir, cloud_processor):
        """Manifest dominant_title must be scrubbed and derived_name re-derived."""
        from screencap.scrubber import scrub_manifest

        manifest_path = cloud_capture_dir / "chunk_0000_manifest.json"
        manifest_path.write_text(json.dumps({
            "chunk_index": 0,
            "tasks": [{
                "start_ts": 1000.0,
                "end_ts": 1060.0,
                "dominant_app": "com.google.Chrome",
                "dominant_title": "John Smith - Contract Review - Google Chrome",
                "derived_name": "chrome_john-smith-contract-review",
            }],
            "summary": {"primary_task": "chrome_john-smith-contract-review"},
        }))

        cp = cloud_processor
        scrub_manifest(manifest_path, cp._chunk_scrubber._pipeline, cp._chunk_scrubber._anonymizer)

        data = json.loads(manifest_path.read_text())
        task = data["tasks"][0]
        assert "John Smith" not in task["dominant_title"]
        assert "<PERSON>" in task["dominant_title"]
        assert "john-smith" not in task["derived_name"]

    def test_scrub_v2_manifest_skips(self, cloud_capture_dir, cloud_processor):
        """v2 manifests have no text fields — scrub should be a no-op."""
        from screencap.scrubber import scrub_manifest

        manifest_path = cloud_capture_dir / "chunk_0000_manifest.json"
        original = {
            "format_version": 2,
            "chunk_index": 0,
            "chunk_start": 1000.0,
            "chunk_end": 2000.0,
            "stats": {"total_events": 42, "total_window_switches": 3},
            "blocked_intervals": [],
        }
        manifest_path.write_text(json.dumps(original))

        cp = cloud_processor
        scrub_manifest(manifest_path, cp._chunk_scrubber._pipeline, cp._chunk_scrubber._anonymizer)

        # File should be unchanged
        data = json.loads(manifest_path.read_text())
        assert data == original

    def test_scrub_text_returns_sentinel_on_all_detectors_failed(
        self, cloud_capture_dir, cloud_processor,
    ):
        """scrub_text must return '<SCRUB_FAILED>' when all detectors fail."""
        from screencap.redaction import AllDetectorsFailedError
        from screencap.scrubber import scrub_text

        cloud_processor._chunk_scrubber._pipeline.detect = MagicMock(
            side_effect=AllDetectorsFailedError("all failed"),
        )

        scrubbed, _ = scrub_text("some sensitive text", cloud_processor._chunk_scrubber._pipeline, cloud_processor._chunk_scrubber._anonymizer)
        assert scrubbed == "<SCRUB_FAILED>"

    def test_scrub_chunk_files_renames_on_per_file_failure(
        self, cloud_capture_dir, cloud_processor,
    ):
        """ChunkScrubber.scrub must rename a file to .scrub_failed when events scrub raises."""
        # Write a valid events file but make the pipeline scrub raise
        events_path = cloud_capture_dir / "events_0000.jsonl"
        events_path.write_text('{"name":"click"}\n')

        with patch(
            "screencap.scrubber.scrub_events_jsonl",
            side_effect=RuntimeError("simulated scrub failure"),
        ):
            cloud_processor._chunk_scrubber.scrub(0, 1000.0, 2000.0, None)

        # Original file should be renamed, not uploaded
        assert not events_path.exists()
        assert (cloud_capture_dir / "events_0000.jsonl.scrub_failed").exists()

    def test_scrub_v2_key_type_anonymizes_pii(self, cloud_capture_dir, cloud_processor):
        """v2 format: key.type text with PII → anonymized, children key_char nulled."""
        from screencap.scrubber import scrub_events_jsonl

        events = [
            {"_meta": True, "format_version": 2},
            {
                "type": "key.type",
                "timestamp": 1000.0,
                "text": "John Smith",
                "children": [
                    {"type": "key.down", "timestamp": 1000.0, "key_char": "J"},
                    {"type": "key.up", "timestamp": 1000.01, "key_char": "J"},
                    {"type": "key.down", "timestamp": 1000.1, "key_char": "o"},
                    {"type": "key.up", "timestamp": 1000.11, "key_char": "o"},
                ],
            },
        ]

        events_path = cloud_capture_dir / "events_0000.jsonl"
        with open(events_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        cp = cloud_processor
        scrub_events_jsonl(events_path, cp._chunk_scrubber._pipeline, cp._chunk_scrubber._anonymizer)

        scrubbed = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        key_type = scrubbed[1]
        # Text is anonymized (not null) — preserves context for downstream LLM
        assert "John Smith" not in str(key_type["text"])
        assert key_type["text"] is not None
        for child in key_type["children"]:
            assert child["key_char"] is None

    def test_scrub_v2_key_type_leaves_clean(self, cloud_capture_dir, cloud_processor):
        """v2 format: key.type text without PII should be left unchanged."""
        from screencap.scrubber import scrub_events_jsonl

        events = [
            {"_meta": True, "format_version": 2},
            {
                "type": "key.type",
                "timestamp": 1000.0,
                "text": "hello world",
                "children": [],
            },
        ]

        events_path = cloud_capture_dir / "events_0000.jsonl"
        with open(events_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        cp = cloud_processor
        scrub_events_jsonl(events_path, cp._chunk_scrubber._pipeline, cp._chunk_scrubber._anonymizer)

        scrubbed = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        assert scrubbed[1]["text"] == "hello world"

    def test_scrub_v2_key_shortcut_anonymizes_pii(self, cloud_capture_dir, cloud_processor):
        """v2 format: key.shortcut with PII → text anonymized via recursive scrub."""
        from screencap.scrubber import scrub_events_jsonl

        events = [
            {"_meta": True, "format_version": 2},
            {
                "type": "key.shortcut",
                "timestamp": 1000.0,
                "text": "John Smith",
                "children": [
                    {"type": "key.down", "timestamp": 1000.0, "key_char": "J"},
                    {"type": "key.down", "timestamp": 1000.1, "key_char": "o"},
                ],
            },
        ]

        events_path = cloud_capture_dir / "events_0000.jsonl"
        with open(events_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        cp = cloud_processor
        scrub_events_jsonl(events_path, cp._chunk_scrubber._pipeline, cp._chunk_scrubber._anonymizer)

        scrubbed = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        shortcut = scrubbed[1]
        # Text is anonymized by _scrub_json_recursive (not targeted key.type handler)
        assert "John Smith" not in str(shortcut["text"])

    def test_scrub_v2_window_switch_title(self, cloud_capture_dir, cloud_processor):
        """v2 format: window.switch window_title with PII should be scrubbed."""
        from screencap.scrubber import scrub_events_jsonl

        events = [
            {"_meta": True, "format_version": 2},
            {
                "type": "window.switch",
                "timestamp": 1000.0,
                "app_name": "Chrome",
                "app_bundle_id": "com.google.Chrome",
                "window_title": "John Smith - Contract Review",
                "window_id": "1",
                "x": 0, "y": 0, "width": 800, "height": 600,
            },
        ]

        events_path = cloud_capture_dir / "events_0000.jsonl"
        with open(events_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        cp = cloud_processor
        scrub_events_jsonl(events_path, cp._chunk_scrubber._pipeline, cp._chunk_scrubber._anonymizer)

        scrubbed = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        ws = scrubbed[1]
        assert "John Smith" not in ws["window_title"]
        assert "<PERSON>" in ws["window_title"]

class TestBlockedIntervalsInManifest:
    """Tests for Phase 6: blocked intervals in chunk manifest."""

    def test_generate_manifest_passes_blocked_intervals(self, cloud_capture_dir):
        """_process_chunk → ChunkManifest gathers screen_filter intervals and
        forwards them to the task_manifest renderer (SCR-35: the gathering now
        lives in the ChunkManifest seam, so spy on the renderer it delegates to)."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        mock_filter = MagicMock()
        mock_filter.get_blocked_intervals.return_value = [
            {"start_ts": 1000.5, "end_ts": 1010.0, "reason": "app_policy"},
        ]

        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False, cloud_intent=False,
            screen_filter=mock_filter,
        )

        # Spy on the renderer ChunkManifest.produce delegates to, capturing the
        # blocked_intervals it forwards. Write a real file so downstream stages
        # see a manifest artifact.
        captured_kwargs = {}

        def spy_generate(*args, **kwargs):
            captured_kwargs.update(kwargs)
            p = cloud_capture_dir / f"chunk_{args[1]:04d}_manifest.json"
            p.write_text("{}")
            return p

        with patch("screencap.task_manifest.generate_manifest", side_effect=spy_generate), \
             patch.object(cp, "_wait_for_audio"), \
             patch.object(cp, "_transcribe", return_value=None), \
             patch.object(cp, "_trigger_flush"):
            cp._process_chunk({
                "completed_index": 0,
                "chunk_start_time": 1000.0,
                "rotation_time": 1060.0,
            })

        mock_filter.get_blocked_intervals.assert_called_once_with(1000.0, 1060.0)
        assert captured_kwargs["blocked_intervals"] == [
            {"start_ts": 1000.5, "end_ts": 1010.0, "reason": "app_policy"},
        ]

    def test_manifest_generation_failure_deletes_partial_file_and_fails_chunk(
        self, cloud_capture_dir,
    ):
        """If manifest generation raises, any partially-written manifest must
        be removed so a subsequent ``screencap upload`` doesn't ship a
        truncated JSON, and the chunk must be marked failed."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False, cloud_intent=False,
        )

        partial = cloud_capture_dir / "chunk_0000_manifest.json"

        def failing_generate(*args, **kwargs):
            # Simulate a mid-write failure that leaves a partial file on disk.
            partial.write_text('{"partial":')
            raise RuntimeError("manifest write blew up")

        with patch("screencap.task_manifest.generate_manifest", side_effect=failing_generate), \
             patch.object(cp, "_wait_for_audio"), \
             patch.object(cp, "_transcribe", return_value=None), \
             patch.object(cp, "_trigger_flush"), \
             patch.object(cp, "_export_events"):
            try:
                cp._process_chunk({
                    "completed_index": 0,
                    "chunk_start_time": 1000.0,
                    "rotation_time": 1060.0,
                })
            except RuntimeError:
                pass

        assert not partial.exists(), "partial manifest must be removed"


class TestPlaceholderFrame:
    """Tests for Phase 1: placeholder frame generation."""

    def test_make_placeholder_frame_dimensions(self):
        """Placeholder frame has correct dimensions and color."""
        from screencap.engine.recorder import _make_placeholder_frame

        frame = _make_placeholder_frame(1920, 1080)
        assert frame.size == (1920, 1080)
        assert frame.mode == "RGB"

        # Check that top-left corner is near-black (30, 30, 30)
        pixel = frame.getpixel((0, 0))
        assert pixel == (30, 30, 30)



class TestUnifiedEventExport:
    """Tests for the unified _export_events pipeline (Phase 3)."""

    @staticmethod
    def _insert_action(rdb, ts, name="click", **kwargs):
        """Insert an action_event using the shared recording_db session."""
        from screencap.engine.db import crud

        data = dict(kwargs)
        data["name"] = name
        crud.insert_action_event(rdb.session, rdb.recording, ts, data)

    @staticmethod
    def _insert_window(rdb, ts, title="Finder", bundle_id="com.apple.finder", window_id="1"):
        """Insert a window_event using the shared recording_db session."""
        from screencap.engine.db import crud

        crud.insert_window_event(rdb.session, rdb.recording, ts, {
            "title": title,
            "app_bundle_id": bundle_id,
            "window_id": window_id,
            "left": 0, "top": 0, "width": 800, "height": 600,
        })

    def test_v1_scope_guard_no_network_lines_in_chunk_jsonl(
        self, cloud_capture_dir, recording_db,
    ):
        """V1 contract: chunk JSONL contains ZERO network.* lines.

        The plan defers all JSONL emission of network events to V1.75
        alongside the cloud bucket policy + build_cloud_network_filter
        factory. V1 keeps network events DB-only -- the auto-export +
        screencap upload paths cannot distinguish local-vs-cloud intent
        at runtime, so wiring network rows into the chunk_processor
        would silently leak metadata to the cloud bucket.
        """
        from screencap.engine.db import crud
        from screencap.chunk_processor import ChunkProcessor

        crud.insert_network_event(
            recording_db.session,
            recording_db.recording,
            {
                "kind": "request",
                "flow_id": "flow-1",
                "method": "GET",
                "url": "https://example.com/api",
                "host": "example.com",
                "headers_json": json.dumps([["Host", "example.com"]]),
                "body_size": 0,
                "body_sha256": None,
                "content_type": None,
                "direction": None,
                "frame_type": None,
                "http_version": "HTTP/1.1",
                "details_json": None,
                "timestamp": 1000.5,
                "timestamp_ns": 1_000_500_000_000,
            },
        )
        crud.flush_buffers(recording_db.session)
        self._insert_action(
            recording_db, 1000.1, "click", mouse_x=1, mouse_y=2,
            mouse_button_name="left", mouse_pressed=1,
        )
        self._insert_action(
            recording_db, 1000.2, "click", mouse_x=1, mouse_y=2,
            mouse_button_name="left", mouse_pressed=0,
        )

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
        )
        cp._export_events(0, 999.0, 1001.0)

        lines = (cloud_capture_dir / "events_0000.jsonl").read_text().splitlines()
        events = [json.loads(line) for line in lines if line.strip()]
        types = [e.get("type") for e in events if not e.get("_meta")]
        assert all(not (t or "").startswith("network.") for t in types), (
            f"V1 must emit zero network.* lines; got types: {types}"
        )


    # ------------------------------------------------------------------
    # Disabled-row filter (R16) at the SELECT layer for both action and
    # window queries.
    # ------------------------------------------------------------------

    def test_disabled_action_rows_filtered_at_select(
        self, cloud_capture_dir, recording_db,
    ):
        """``action_event`` rows with ``disabled=True`` are dropped at the
        SELECT layer (R16). The disabled click never reaches the unified
        callable.
        """
        from sqlalchemy import update

        from screencap.engine.db.models import ActionEvent

        # Insert two click pairs.
        self._insert_action(recording_db, 1000.0, "click", mouse_x=10, mouse_y=10,
                            mouse_button_name="left", mouse_pressed=1)
        self._insert_action(recording_db, 1000.05, "click", mouse_x=10, mouse_y=10,
                            mouse_button_name="left", mouse_pressed=0)
        self._insert_action(recording_db, 1000.5, "click", mouse_x=200, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=1)
        self._insert_action(recording_db, 1000.55, "click", mouse_x=200, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=0)

        # Mark the second pair disabled.
        recording_db.session.execute(
            update(ActionEvent)
            .where(ActionEvent.timestamp >= 1000.5)
            .values(disabled=True)
        )
        recording_db.session.commit()

        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
        )

        cp._export_events(0, 999.0, 1001.0)

        lines = (cloud_capture_dir / "events_0000.jsonl").read_text().strip().split("\n")
        events = [json.loads(line) for line in lines[1:]]
        clicks = [e for e in events if e["type"] == "mouse.singleclick"]
        # Only the first (enabled) pair survives.
        assert len(clicks) == 1
        assert clicks[0]["x"] == 10

    def test_disabled_window_rows_filtered_when_column_present(
        self, cloud_capture_dir, recording_db,
    ):
        """When ``window_event.disabled`` exists, disabled rows are filtered
        at the SELECT layer too. The default schema does not include this
        column on ``window_event``; we add it via ALTER for the test and
        verify the chunk processor's ``has_column`` guard takes the
        filtering branch.
        """
        # Add the column to the DB so ``has_column`` returns True.
        recording_db.engine.dispose()
        recording_db.session.close()
        import sqlite3 as _sqlite

        _conn = _sqlite.connect(str(recording_db.db_path))
        _conn.execute(
            "ALTER TABLE window_event ADD COLUMN disabled BOOLEAN DEFAULT 0"
        )
        _conn.commit()

        # Insert two distinct windows; mark the second disabled.
        _conn.execute(
            "INSERT INTO window_event "
            "(recording_id, timestamp, title, app_bundle_id, window_id, "
            "left, top, width, height, disabled) "
            "VALUES (?, ?, ?, ?, ?, 0, 0, 800, 600, 0)",
            (recording_db.recording.id, 1000.0, "Finder", "com.apple.finder", "win-1"),
        )
        _conn.execute(
            "INSERT INTO window_event "
            "(recording_id, timestamp, title, app_bundle_id, window_id, "
            "left, top, width, height, disabled) "
            "VALUES (?, ?, ?, ?, ?, 0, 0, 800, 600, 1)",
            (recording_db.recording.id, 1000.5, "Chrome", "com.google.Chrome", "win-2"),
        )
        _conn.commit()
        _conn.close()

        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
        )

        cp._export_events(0, 999.0, 1001.0)

        lines = (cloud_capture_dir / "events_0000.jsonl").read_text().strip().split("\n")
        events = [json.loads(line) for line in lines[1:]]
        bundles = {e["app_bundle_id"] for e in events if e["type"] == "window.switch"}
        # Disabled Chrome row must not appear; enabled Finder must.
        assert "com.apple.finder" in bundles
        assert "com.google.Chrome" not in bundles

    def test_action_select_busy_timeout_fail_soft(
        self, cloud_capture_dir, recording_db,
    ):
        """OperationalError on the action SELECT (busy_timeout from live
        writer contention) propagates through the cleanup-on-exception
        path and is caught by the chunk-processor's outer ``_run``
        ``except Exception``. _export_events itself raises so the caller
        can mark the chunk failed; it does NOT crash the thread.

        Regression test for the new ``disabled``-row predicate not
        widening the failure surface beyond the previous fail-soft
        contract.
        """
        from contextlib import contextmanager

        from screencap.chunk_processor import ChunkProcessor
        from screencap.recording_db import OperationalError as _OpErr
        from screencap.recording_db import open_recording_db as _real_open

        # Anchor at least one row so the SELECT executes against a
        # populated table.
        self._insert_action(recording_db, 1000.0, "click", mouse_x=10, mouse_y=10,
                            mouse_button_name="left", mouse_pressed=1)

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
        )

        # Wrap the connection yielded by ``open_recording_db`` so the
        # action SELECT raises OperationalError but other queries
        # (PRAGMAs, has_column probes) still hit the real connection.
        # sqlite3.Connection is immutable, so we proxy through a tiny
        # passthrough class.
        class _ProxyConn:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, *args, **kwargs):
                if sql.startswith("SELECT * FROM action_event"):
                    raise _OpErr("database is locked")
                return self._real.execute(sql, *args, **kwargs)

            @property
            def row_factory(self):
                return self._real.row_factory

            @row_factory.setter
            def row_factory(self, value):
                self._real.row_factory = value

        @contextmanager
        def patched_open(path, **kwargs):
            with _real_open(path, **kwargs) as real:
                yield _ProxyConn(real)

        with mock.patch(
            "screencap.export.open_recording_db", patched_open,
        ):
            with pytest.raises(_OpErr):
                cp._export_events(0, 999.0, 1001.0)

        out = cloud_capture_dir / "events_0000.jsonl"
        tmp = cloud_capture_dir / "events_0000.jsonl.tmp"
        assert not out.exists()
        assert not tmp.exists()


# ---------------------------------------------------------------------------
# Upload gating integration tests — sentinel upload should be gated behind
# all_chunks_uploaded().  These tests exercise the real ChunkProcessor thread
# loop with real queues and the real _upload_chunk retry logic; only the HTTP
# layer (upload_chunk_files) is mocked.
# ---------------------------------------------------------------------------


def _create_seven_chunk_db(db_path: Path, t0: float) -> None:
    """Create a DB with events spanning 7 chunks (30s each)."""
    from screencap.engine.db import create_db, crud

    engine, Session = create_db(str(db_path))
    session = Session()
    recording = crud.insert_recording(session, {
        "timestamp": t0,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    # Insert one click pair per chunk so _export_events has something to process
    for i in range(7):
        chunk_ts = t0 + i * 30 + 5
        crud.insert_action_event(session, recording, chunk_ts, {
            "name": "click",
            "mouse_x": 100.0,
            "mouse_y": 200.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        })
        crud.insert_action_event(session, recording, chunk_ts + 0.1, {
            "name": "click",
            "mouse_x": 100.0,
            "mouse_y": 200.0,
            "mouse_button_name": "left",
            "mouse_pressed": False,
        })
    session.close()
    engine.dispose()


def _create_chunk_media_files(capture_dir: Path, n_chunks: int) -> None:
    """Create stub .mp4, .flac files for each chunk."""
    for i in range(n_chunks):
        (capture_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 1024)
        (capture_dir / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 512)


def _build_chunk_processor(
    capture_dir: Path,
) -> tuple:
    """Build a ChunkProcessor with upload enabled but side-effects mocked.

    Returns (processor, chunk_queue).  The real _upload_chunk retry logic is
    preserved — only upload_chunk_files (the HTTP layer) needs to be patched
    by the caller via @mock.patch.
    """
    from screencap.chunk_processor import ChunkProcessor

    # SCR-125 U2: the live reclaim routes through the unified retention floor,
    # which reads the FROZEN policy from .recording_intent. A real cloud
    # delete_after_upload recording freezes this at start; mirror it here so the
    # floor evicts (the floor is the single source of truth, not the
    # auto_delete bool — under a plan-tier override they can diverge).
    (capture_dir / ".recording_intent").write_text(json.dumps({
        "version": 2, "destination": "cloud",
        "retention_policy": "delete_after_upload", "retention_params": {},
        "masked_video_upload": False, "show_on_website": True,
    }))
    (capture_dir / ".recording_id").write_text("test-upload-gating")

    chunk_q = multiprocessing.Queue()
    audio_q = multiprocessing.Queue()
    cp = ChunkProcessor(
        capture_dir,
        chunk_q,
        audio_q,
        recording_name="test-upload-gating",
        upload_enabled=True,
        auto_delete=True,
        cloud_intent=False,  # skip privacy pipeline init
    )

    # Mock side effects that aren't under test
    cp._wait_for_audio = lambda *a, **kw: True
    cp._transcribe = lambda *a, **kw: None
    cp._chunk_manifest.produce = lambda *a, **kw: None

    return cp, chunk_q


def _enqueue_chunks(q, t0: float, n: int) -> None:
    """Put n chunk rotation messages into the queue."""
    for i in range(n):
        q.put({
            "type": "chunk_rotated" if i < n - 1 else "final_chunk",
            "completed_index": i,
            "chunk_start_time": t0 + i * 30,
            "rotation_time": t0 + (i + 1) * 30,
        })


class TestUploadGatingAllSucceed:
    """Scenario 1: All 7 chunks upload successfully."""

    @mock.patch("screencap.terminal_stage._chunk_confirmed_remote", return_value=True)
    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files", return_value=True)
    def test_seven_chunks_all_succeed(self, mock_upload, mock_sleep, mock_confirm, tmp_path):
        """Happy path: all chunks upload, cleanup runs, state is correct.

        SCR-125 U2: cleanup now goes through the fresh-remote-confirm floor, so
        the remote re-confirm is mocked True (in production it is a real GCS
        re-stat — the floor never deletes without it, prevention rule #3)."""
        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)
        cp, chunk_q = _build_chunk_processor(tmp_path)
        cp.start()

        _enqueue_chunks(chunk_q, t0, 7)
        cp.stop(timeout=30)

        # Core state
        assert cp.all_chunks_uploaded() is True
        n_uploaded, n_total = cp.upload_summary()
        assert n_uploaded == 7
        assert n_total == 7

        # Upload called once per chunk (no retries needed)
        assert mock_upload.call_count == 7

        # Old chunk media cleaned up (keep_recent=2: only chunks 5,6 remain)
        for i in range(5):
            assert not (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 should have been cleaned up"
            assert not (tmp_path / f"audio_{i:04d}.flac").exists(), \
                f"audio_{i:04d}.flac should have been cleaned up"
        for i in range(5, 7):
            assert (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 should still exist (recent)"
            assert (tmp_path / f"audio_{i:04d}.flac").exists(), \
                f"audio_{i:04d}.flac should still exist (recent)"

        # No retry backoff needed
        mock_sleep.assert_not_called()


class TestUploadGatingPartialFailure:
    """Scenario 2: Chunk 3 fails permanently, rest succeed."""

    @mock.patch("screencap.terminal_stage._chunk_confirmed_remote", return_value=True)
    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files")
    def test_seven_chunks_one_fails_permanently(self, mock_upload, mock_sleep, mock_confirm, tmp_path):
        """Chunk 3 fails both attempts. Retry called, files preserved for recovery."""

        def upload_side_effect(recording_name, files, capture_dir):
            for f in files:
                if "chunk_0003" in f["name"]:
                    raise ConnectionError("upload failed")
            return True

        mock_upload.side_effect = upload_side_effect

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)
        cp, chunk_q = _build_chunk_processor(tmp_path)
        cp.start()

        _enqueue_chunks(chunk_q, t0, 7)
        cp.stop(timeout=30)

        # Core state
        assert cp.all_chunks_uploaded() is False
        n_uploaded, n_total = cp.upload_summary()
        assert n_uploaded == 6
        assert n_total == 7

        # Retry: 6 successes (once each) + 2 attempts for chunk 3 = 8
        assert mock_upload.call_count == 8
        # 5s backoff sleep between chunk 3's two attempts
        mock_sleep.assert_called_once_with(5)

        # Chunk 3 media files preserved on disk (not cleaned up)
        assert (tmp_path / "chunk_0003.mp4").exists()
        assert (tmp_path / "audio_0003.flac").exists()

        # Successfully uploaded old chunks still cleaned up
        for i in [0, 1, 2]:
            assert not (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 should have been cleaned up (uploaded OK)"


class TestUploadGatingRetryRecovery:
    """Scenario 3: Chunk 3 fails first attempt, succeeds on retry."""

    @mock.patch("screencap.terminal_stage._chunk_confirmed_remote", return_value=True)
    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files")
    def test_seven_chunks_one_recovers_on_retry(self, mock_upload, mock_sleep, mock_confirm, tmp_path):
        """Chunk 3 fails first attempt, succeeds on retry. End state = happy path."""

        call_counts: dict[int, int] = defaultdict(int)

        def upload_side_effect(recording_name, files, capture_dir):
            chunk_id = None
            for f in files:
                if "chunk_0003" in f["name"]:
                    chunk_id = 3
                    break
            if chunk_id == 3:
                call_counts[3] += 1
                if call_counts[3] == 1:
                    raise ConnectionError("transient failure")
            return True

        mock_upload.side_effect = upload_side_effect

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)
        cp, chunk_q = _build_chunk_processor(tmp_path)
        cp.start()

        _enqueue_chunks(chunk_q, t0, 7)
        cp.stop(timeout=30)

        # Retry succeeded: all chunks marked as uploaded
        assert cp.all_chunks_uploaded() is True
        n_uploaded, n_total = cp.upload_summary()
        assert n_uploaded == 7
        assert n_total == 7

        # Upload called 8 times: 6 once each + chunk 3 twice
        assert mock_upload.call_count == 8
        # Backoff sleep called once (before chunk 3's retry)
        mock_sleep.assert_called_once_with(5)

        # Chunk 3 treated as success: old chunks cleaned up normally
        for i in range(5):
            assert not (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 should have been cleaned up"
        for i in range(5, 7):
            assert (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 should still exist (recent)"


class TestPrivacyFailureDataLoss:
    """Privacy pipeline init failure must not cause silent data loss.

    When cloud_intent=True and the privacy pipeline fails to initialize,
    _upload_enabled is set to False (fail-closed).  But _process_chunk must
    NOT mark those chunks as success=True — otherwise all_chunks_uploaded()
    returns True, triggering stub_recording() which deletes local media
    while nothing was uploaded to GCS.
    """

    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files", return_value=True)
    def test_privacy_failure_prevents_false_success(self, mock_upload, mock_sleep, tmp_path):
        """Cloud-intent + privacy ImportError → chunks NOT marked as success,
        media files preserved on disk."""
        from screencap.chunk_processor import ChunkProcessor

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)

        chunk_q = multiprocessing.Queue()
        audio_q = multiprocessing.Queue()

        with patch(
            "screencap.redaction.engine.create_default_pipeline",
            side_effect=ImportError("test: no privacy deps"),
        ):
            cp = ChunkProcessor(
                tmp_path, chunk_q, audio_q,
                recording_name="test-privacy-fail",
                upload_enabled=True,
                auto_delete=True,
                cloud_intent=True,
            )

        # Sanity: uploads were disabled by the ImportError
        assert cp._upload_enabled is False

        # Mock side effects not under test
        cp._wait_for_audio = lambda *a, **kw: True
        cp._transcribe = lambda *a, **kw: None
        cp._chunk_manifest.produce = lambda *a, **kw: None

        cp.start()
        _enqueue_chunks(chunk_q, t0, 7)
        cp.stop(timeout=30)

        # Core assertion: chunks must NOT be marked as success
        assert cp.all_chunks_uploaded() is False
        n_uploaded, n_total = cp.upload_summary()
        assert n_uploaded == 0
        assert n_total == 7

        # Upload was never attempted (disabled)
        mock_upload.assert_not_called()

        # ALL chunk media files must still exist — _delete_old_chunks must
        # not have run because success was False for every chunk.
        for i in range(7):
            assert (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 was deleted — data loss!"
            assert (tmp_path / f"audio_{i:04d}.flac").exists(), \
                f"audio_{i:04d}.flac was deleted — data loss!"

    def test_local_intent_unaffected(self, tmp_path):
        """Local-intent with upload_enabled=False must still mark chunks as
        success (regression guard)."""
        from screencap.chunk_processor import ChunkProcessor

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)

        chunk_q = multiprocessing.Queue()
        audio_q = multiprocessing.Queue()

        cp = ChunkProcessor(
            tmp_path, chunk_q, audio_q,
            recording_name="test-local",
            upload_enabled=False,
            auto_delete=False,
            cloud_intent=False,
        )

        cp._wait_for_audio = lambda *a, **kw: True
        cp._transcribe = lambda *a, **kw: None
        cp._chunk_manifest.produce = lambda *a, **kw: None

        cp.start()
        _enqueue_chunks(chunk_q, t0, 3)
        cp.stop(timeout=30)

        # Local-intent: all chunks should be marked as success
        assert cp.all_chunks_uploaded() is True
        n_uploaded, n_total = cp.upload_summary()
        assert n_uploaded == 3
        assert n_total == 3

    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files", return_value=True)
    def test_upload_disabled_from_caller_prevents_deletion(
        self, mock_upload, mock_sleep, tmp_path,
    ):
        """T2: upload_enabled=False from caller + auto_delete=True must NOT delete files.

        This is the --cloud --no-live-upload scenario: cloud_intent=True but
        upload_enabled=False from the caller. The constructor must force
        _auto_delete=False to prevent deletion of never-uploaded files.
        """
        from screencap.chunk_processor import ChunkProcessor

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)

        chunk_q = multiprocessing.Queue()
        audio_q = multiprocessing.Queue()

        cp = ChunkProcessor(
            tmp_path, chunk_q, audio_q,
            recording_name="test-no-live-upload",
            upload_enabled=False,
            auto_delete=True,  # caller passes True (cloud_intent && config)
            cloud_intent=True,
        )

        # Constructor must have forced _auto_delete to False
        assert cp._auto_delete is False, (
            "_auto_delete must be False when upload_enabled=False — "
            "cannot delete files that were never uploaded"
        )

        cp._wait_for_audio = lambda *a, **kw: True
        cp._transcribe = lambda *a, **kw: None
        cp._chunk_manifest.produce = lambda *a, **kw: None

        cp.start()
        _enqueue_chunks(chunk_q, t0, 7)
        cp.stop(timeout=30)

        # Upload was never attempted
        mock_upload.assert_not_called()

        # ALL media files must still exist
        for i in range(7):
            assert (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 was deleted — data loss!"
            assert (tmp_path / f"audio_{i:04d}.flac").exists(), \
                f"audio_{i:04d}.flac was deleted — data loss!"

    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files", return_value=True)
    def test_masking_classifier_failure_prevents_deletion(
        self, mock_upload, mock_sleep, tmp_path,
    ):
        """T3: Masking classifier init fails (pipeline OK) → files preserved.

        When create_default_pipeline succeeds but the masking classifier
        raises, _upload_enabled is set to False. Media files must not be
        deleted even with auto_delete=True.
        """
        from screencap.chunk_processor import ChunkProcessor

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)

        chunk_q = multiprocessing.Queue()
        audio_q = multiprocessing.Queue()

        with patch("screencap.redaction.engine.create_default_pipeline") as mock_pipeline, \
             patch("screencap.redaction.engine.Anonymizer"), \
             patch("screencap.config.get_privacy_config", side_effect=RuntimeError("masking init failed")):
            mock_pipeline.return_value = MagicMock()
            cp = ChunkProcessor(
                tmp_path, chunk_q, audio_q,
                recording_name="test-masking-fail",
                upload_enabled=True,
                auto_delete=True,
                cloud_intent=True,
            )

        # Pipeline succeeded but masking failed → uploads disabled, and the
        # seam reports no masking context (SCR-35 R7: the fail-closed signal the
        # content-index guard reads).
        assert cp._upload_enabled is False
        assert cp._chunk_scrubber.has_masking_context is False
        assert cp.upload_warning is not None
        assert cp._auto_delete is False, (
            "_auto_delete must be False when uploads are disabled due to masking failure"
        )

        cp._wait_for_audio = lambda *a, **kw: True
        cp._transcribe = lambda *a, **kw: None
        cp._chunk_manifest.produce = lambda *a, **kw: None

        cp.start()
        _enqueue_chunks(chunk_q, t0, 7)
        cp.stop(timeout=30)

        # Chunks must NOT be marked as success
        assert cp.all_chunks_uploaded() is False
        mock_upload.assert_not_called()

        # ALL media files must still exist
        for i in range(7):
            assert (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 was deleted — data loss!"



def _auth_failure_exc(kind: str) -> Exception:
    """Construct an auth-class failure the upload path may hit once the bearer
    token is threaded through ``request_signed_urls`` (U5c)."""
    import keyring.errors

    from screencap.auth import AuthError, NotEntitled, NotSignedIn

    return {
        "not_signed_in": NotSignedIn("not signed in"),
        "transient_auth": AuthError("token refresh temporarily failed"),
        "keyring_error": keyring.errors.KeyringError("keychain locked"),
        "service_error": RuntimeError("Upload service unavailable"),
        # U4: signed in but no active cloud entitlement — the client pre-check
        # raises NotEntitled inside request_signed_urls, which must route to the
        # same FAILED-chunk / media-preserved fail-closed path.
        "not_entitled": NotEntitled("Cloud upload needs an active plan."),
    }[kind]


class TestAuthFailureFailClosed:
    """U5 characterization: an auth failure on the live-upload path fails closed.

    Locks the invariant from
    ``docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md``
    BEFORE the bearer token is threaded through ``request_signed_urls`` (U5c).
    When requesting signed URLs raises any auth-class exception — NotSignedIn,
    a transient AuthError (the 503/Firebase-outage shape), a Keychain error, or
    a generic service RuntimeError — ``upload_chunk_files`` catches it and
    returns ``core_ok=False``, so the chunk lands on ``ChunkStatus.FAILED``, the
    sentinel gate (``all_chunks_uploaded``) stays False, and no local media is
    deleted. Threading auth in (U5c) must preserve this exact behavior: the auth
    failure becomes an exception *inside* ``request_signed_urls``, which this
    same machinery already routes to FAILED.
    """

    @pytest.mark.privacy
    @pytest.mark.parametrize(
        "kind",
        ["not_signed_in", "transient_auth", "keyring_error", "service_error", "not_entitled"],
    )
    @mock.patch("screencap.chunk_processor.time.sleep")
    def test_auth_failure_marks_failed_and_preserves_media(self, _mock_sleep, tmp_path, kind):
        from screencap.chunk_processor import ChunkStatus

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)
        cp, chunk_q = _build_chunk_processor(tmp_path)

        # Mock at the request_signed_urls seam — the real upload_chunk_files runs,
        # so we exercise the real exception → core_ok=False → FAILED path.
        with patch(
            "screencap.upload.request_signed_urls",
            side_effect=lambda *a, **k: (_ for _ in ()).throw(_auth_failure_exc(kind)),
        ):
            cp.start()
            _enqueue_chunks(chunk_q, t0, 7)
            cp.stop(timeout=30)

        # Sentinel gate stays closed — recorder.py never uploads the sentinel or
        # calls stub_recording() while this is False.
        assert cp.all_chunks_uploaded() is False
        assert cp._chunk_results, "chunks must have been processed (PENDING→FAILED)"
        assert all(s == ChunkStatus.FAILED for s in cp._chunk_results.values()), (
            f"every chunk must be FAILED on auth failure, got {cp._chunk_results}"
        )
        n_uploaded, n_total = cp.upload_summary()
        assert n_uploaded == 0
        assert n_total == 7

        # Point of no recovery never reached: every chunk's media preserved on
        # disk for `screencap upload` recovery.
        for i in range(7):
            assert (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 was deleted on auth failure — data loss!"
            assert (tmp_path / f"audio_{i:04d}.flac").exists(), \
                f"audio_{i:04d}.flac was deleted on auth failure — data loss!"


# ---------------------------------------------------------------------------
# _unlisted marker behaviour
# ---------------------------------------------------------------------------


class TestUnlistedMarker:
    """The _unlisted marker is non-core; a server rejection must not fail the chunk."""

    def test_collect_chunk_files_includes_unlisted_when_hidden(self, cloud_capture_dir):
        """Chunk 0 appends _unlisted when show_on_website=False."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"v")
        (cloud_capture_dir / "audio_0000.flac").write_bytes(b"a")
        (cloud_capture_dir / "events_0000.jsonl").write_text("{}\n")

        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
            show_on_website=False,
        )
        names = [f["name"] for f in cp._collect_chunk_files(0, None)]
        assert "_unlisted" in names

    def test_collect_chunk_files_omits_unlisted_when_visible(self, cloud_capture_dir):
        """Default visible recordings must not upload a marker."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"v")

        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
            show_on_website=True,
        )
        names = [f["name"] for f in cp._collect_chunk_files(0, None)]
        assert "_unlisted" not in names

    def test_collect_chunk_files_omits_unlisted_for_later_chunks(self, cloud_capture_dir):
        """Marker is only appended on chunk 0, not subsequent chunks."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        (cloud_capture_dir / "chunk_0001.mp4").write_bytes(b"v")

        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
            show_on_website=False,
        )
        names = [f["name"] for f in cp._collect_chunk_files(1, None)]
        assert "_unlisted" not in names

    def test_upload_chunk_files_unlisted_rejection_non_fatal(self, tmp_path):
        """Server omitting _unlisted from signed-url response must not fail the chunk."""
        from screencap.chunk_processor import upload_chunk_files

        chunk_path = tmp_path / "chunk_0000.mp4"
        chunk_path.write_bytes(b"v")
        marker_path = tmp_path / "_unlisted"
        marker_path.touch()

        files = [
            {"name": "chunk_0000.mp4", "path": chunk_path},
            {"name": "_unlisted", "path": marker_path},
        ]

        # Server returns a URL for the media file but silently drops _unlisted
        # (legacy behaviour — mirrors what the old filename regex did).
        def fake_request_signed_urls(recording_name, file_infos):
            return ({"chunk_0000.mp4": "https://example.com/signed"}, "gs://bucket/test/")

        with mock.patch(
            "screencap.upload.request_signed_urls",
            side_effect=fake_request_signed_urls,
        ), mock.patch(
            "screencap.chunk_processor._upload_single"
        ) as mock_upload_single:
            result = upload_chunk_files("test", files, tmp_path)

        assert result is True, (
            "Missing URL for _unlisted must not fail the chunk — it is non-core"
        )
        # Only the core media file was actually uploaded
        assert mock_upload_single.call_count == 1

    def test_upload_chunk_files_core_rejection_still_fatal(self, tmp_path):
        """A genuine core-file rejection must still fail the chunk."""
        from screencap.chunk_processor import upload_chunk_files

        chunk_path = tmp_path / "chunk_0000.mp4"
        chunk_path.write_bytes(b"v")
        files = [{"name": "chunk_0000.mp4", "path": chunk_path}]

        with mock.patch(
            "screencap.upload.request_signed_urls",
            return_value=({}, "gs://bucket/test/"),
        ):
            result = upload_chunk_files("test", files, tmp_path)

        assert result is False


# ---------------------------------------------------------------------------
# GCS reconciliation tests
# ---------------------------------------------------------------------------


class TestReconcileAgainstGcs:
    """After stop(), flip ``_chunk_results[idx]`` from ``FAILED`` to ``EMITTED``
    for chunks whose core files are all present in GCS. ``_upload_chunk()``
    returns False on any single per-file PUT exception, but the other files
    stay in the bucket — the counter must reflect that.
    """

    def _make_chunk_files(self, capture_dir: Path, idx: int) -> None:
        """Write non-empty core files for chunk `idx` on disk."""
        for name in (
            f"chunk_{idx:04d}.mp4",
            f"audio_{idx:04d}.flac",
            f"events_{idx:04d}.jsonl",
            f"chunk_{idx:04d}_manifest.json",
        ):
            (capture_dir / name).write_bytes(b"x")

    def _make_cp(self, capture_dir, *, upload_enabled=True):
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        return ChunkProcessor(
            capture_dir, q, ack_q,
            recording_name="rec",
            upload_enabled=upload_enabled,
            auto_delete=False,
        )

    def test_flips_failed_to_emitted_when_all_files_already_uploaded(self, capture_dir):
        """Server says url=None for every core file → flip FAILED to EMITTED."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[1] = ChunkStatus.FAILED
        self._make_chunk_files(capture_dir, 1)

        all_already_uploaded = {
            "chunk_0001.mp4": None,
            "audio_0001.flac": None,
            "events_0001.jsonl": None,
            "chunk_0001_manifest.json": None,
        }
        with mock.patch(
            "screencap.upload.request_signed_urls",
            return_value=(all_already_uploaded, "gs://bucket/rec/"),
        ):
            flipped = cp.reconcile_against_gcs()

        assert flipped == 1
        assert cp._chunk_results[1] == ChunkStatus.EMITTED

    def test_keeps_failed_when_one_file_missing_from_gcs(self, capture_dir):
        """Server returns a fresh URL for one file → chunk stays FAILED."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[1] = ChunkStatus.FAILED
        self._make_chunk_files(capture_dir, 1)

        partial = {
            "chunk_0001.mp4": None,
            "audio_0001.flac": None,
            "events_0001.jsonl": "https://gcs/signed-url-for-pending-upload",
            "chunk_0001_manifest.json": None,
        }
        with mock.patch(
            "screencap.upload.request_signed_urls",
            return_value=(partial, "gs://bucket/rec/"),
        ):
            flipped = cp.reconcile_against_gcs()

        assert flipped == 0
        assert cp._chunk_results[1] == ChunkStatus.FAILED

    def test_skips_already_emitted_chunks(self, capture_dir):
        """Chunks already EMITTED are not re-checked."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[0] = ChunkStatus.EMITTED
        cp._chunk_results[1] = ChunkStatus.FAILED
        self._make_chunk_files(capture_dir, 1)

        calls: list[str] = []

        def spy(recording_name, files):
            calls.append(recording_name)
            return (
                {fi.name: None for fi in files},
                "gs://bucket/rec/",
            )

        with mock.patch("screencap.upload.request_signed_urls", side_effect=spy):
            cp.reconcile_against_gcs()

        assert len(calls) == 1  # only chunk 1 queried, not chunk 0

    def test_does_not_flip_network_skipped(self, capture_dir):
        """``NETWORK_SKIPPED`` is terminal-non-EMITTED. Even though the core
        files may all be in GCS, reconcile MUST NOT relabel them as EMITTED
        — that would silently erase the network-skip signal at the gate.

        Regression guard for the prior-incident pattern routed through
        reconcile: ``s != EMITTED`` iteration would call request_signed_urls,
        every core file would come back ``url=None``, the all() check would
        pass, and the chunk would relabel ``EMITTED`` — Bug 4 shape.
        """
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[1] = ChunkStatus.NETWORK_SKIPPED
        self._make_chunk_files(capture_dir, 1)

        with mock.patch(
            "screencap.upload.request_signed_urls",
            side_effect=AssertionError("reconcile must not query NETWORK_SKIPPED"),
        ):
            flipped = cp.reconcile_against_gcs()

        assert flipped == 0
        assert cp._chunk_results[1] == ChunkStatus.NETWORK_SKIPPED

    def test_does_not_flip_network_incomplete(self, capture_dir):
        """``NETWORK_INCOMPLETE`` mirrors ``NETWORK_SKIPPED``: terminal-non-
        EMITTED, must survive reconcile unchanged."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[1] = ChunkStatus.NETWORK_INCOMPLETE
        self._make_chunk_files(capture_dir, 1)

        with mock.patch(
            "screencap.upload.request_signed_urls",
            side_effect=AssertionError("reconcile must not query NETWORK_INCOMPLETE"),
        ):
            flipped = cp.reconcile_against_gcs()

        assert flipped == 0
        assert cp._chunk_results[1] == ChunkStatus.NETWORK_INCOMPLETE

    def test_returns_zero_when_uploads_disabled(self, capture_dir):
        """Don't hit the network when uploads are disabled."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir, upload_enabled=False)
        cp._chunk_results[1] = ChunkStatus.FAILED
        self._make_chunk_files(capture_dir, 1)

        with mock.patch(
            "screencap.upload.request_signed_urls",
            side_effect=AssertionError("must not be called"),
        ):
            flipped = cp.reconcile_against_gcs()

        assert flipped == 0
        assert cp._chunk_results[1] == ChunkStatus.FAILED

    def test_skips_chunks_with_no_files_on_disk(self, capture_dir):
        """If the chunk's local files are gone, no reconciliation is possible."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[1] = ChunkStatus.FAILED
        # No files on disk for chunk 1.

        with mock.patch(
            "screencap.upload.request_signed_urls",
            side_effect=AssertionError("must not be called"),
        ):
            flipped = cp.reconcile_against_gcs()

        assert flipped == 0
        assert cp._chunk_results[1] == ChunkStatus.FAILED

    def test_swallows_request_signed_urls_errors(self, capture_dir):
        """A transient failure during reconcile must not raise — we still
        want to print the counter with the best info we have."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[1] = ChunkStatus.FAILED
        self._make_chunk_files(capture_dir, 1)

        with mock.patch(
            "screencap.upload.request_signed_urls",
            side_effect=RuntimeError("upstream down"),
        ):
            flipped = cp.reconcile_against_gcs()

        assert flipped == 0
        assert cp._chunk_results[1] == ChunkStatus.FAILED


# ---------------------------------------------------------------------------
# V1.75 ChunkStatus migration tests
# ---------------------------------------------------------------------------


class TestChunkStatusGate:
    """V1.75: ``all_chunks_uploaded()`` must compare against EMITTED only.

    Regression coverage for the four prior incidents documented in
    ``docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md``.
    Every non-EMITTED status must block the sentinel gate.
    """

    def _make_cp(self, capture_dir):
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        return ChunkProcessor(
            capture_dir, q, ack_q,
            recording_name="rec",
            upload_enabled=True,
            auto_delete=False,
        )

    def test_pending_blocks_sentinel(self, capture_dir):
        """``PENDING`` blocks the gate — survivorship-bias fix.

        Force-stop leaves a ``PENDING`` entry (eagerly set on rotation
        message receipt). Without this regression test, a future
        refactor could re-introduce Bug 2: incomplete state treated as
        complete because the entry is "truthy".
        """
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[0] = ChunkStatus.PENDING
        cp._chunk_results[1] = ChunkStatus.EMITTED
        assert cp.all_chunks_uploaded() is False

    def test_failed_blocks_sentinel(self, capture_dir):
        """Regression for prior-incident Bug 1: failed chunks must block."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[0] = ChunkStatus.FAILED
        assert cp.all_chunks_uploaded() is False

    def test_network_skipped_blocks_sentinel(self, capture_dir):
        """V1.75: ``NETWORK_SKIPPED`` is terminal-non-EMITTED.

        Per ``chunk-upload-sentinel-gating-and-data-loss.md`` fix #4:
        "disabled ≠ succeeded". A chunk whose body-encryption was
        unavailable at export time must not be conflated with an
        uploaded chunk.
        """
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[0] = ChunkStatus.EMITTED
        cp._chunk_results[1] = ChunkStatus.NETWORK_SKIPPED
        assert cp.all_chunks_uploaded() is False

    def test_network_incomplete_blocks_sentinel(self, capture_dir):
        """V1.75: ``NETWORK_INCOMPLETE`` is terminal-non-EMITTED."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[0] = ChunkStatus.EMITTED
        cp._chunk_results[1] = ChunkStatus.NETWORK_INCOMPLETE
        assert cp.all_chunks_uploaded() is False

    def test_all_emitted_passes_gate(self, capture_dir):
        """Sanity: when every chunk is ``EMITTED`` the gate passes."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._chunk_results[0] = ChunkStatus.EMITTED
        cp._chunk_results[1] = ChunkStatus.EMITTED
        cp._chunk_results[2] = ChunkStatus.EMITTED
        assert cp.all_chunks_uploaded() is True


class TestUploadSummaryCountsEmittedOnly:
    """V1.75: ``upload_summary()`` returns (n_emitted, n_total).

    Network-leg failures must NOT count toward the "uploaded" tally —
    they reach the bucket as scrub-incomplete data and the summary
    needs to reflect that for user messaging.
    """

    def test_counts_only_emitted(self, capture_dir):
        from screencap.chunk_processor import ChunkProcessor, ChunkStatus

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            capture_dir, q, ack_q,
            recording_name="rec",
            upload_enabled=True, auto_delete=False,
        )
        cp._chunk_results[0] = ChunkStatus.EMITTED
        cp._chunk_results[1] = ChunkStatus.NETWORK_SKIPPED
        cp._chunk_results[2] = ChunkStatus.FAILED
        cp._chunk_results[3] = ChunkStatus.NETWORK_INCOMPLETE

        n_emitted, n_total = cp.upload_summary()
        assert n_emitted == 1, (
            "Only EMITTED counts; NETWORK_SKIPPED, FAILED, "
            "NETWORK_INCOMPLETE all excluded from the uploaded tally."
        )
        assert n_total == 4

    def test_pending_counted_in_total_not_emitted(self, capture_dir):
        """``PENDING`` entries count toward the total (visibility) but
        never toward the uploaded count."""
        from screencap.chunk_processor import ChunkProcessor, ChunkStatus

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            capture_dir, q, ack_q,
            recording_name="rec",
            upload_enabled=True, auto_delete=False,
        )
        cp._chunk_results[0] = ChunkStatus.EMITTED
        cp._chunk_results[1] = ChunkStatus.PENDING

        n_emitted, n_total = cp.upload_summary()
        assert n_emitted == 1
        assert n_total == 2


class TestEvictThroughFloorU2:
    """SCR-125 U2: the live reclaim routes through the unified retention floor.

    The EMITTED-only / network-skipped survival invariant the old
    ``_delete_old_chunks`` enforced via the in-memory map now lives in the
    floor's candidate predicate (only ``UPLOADED`` is evictable) — exercised in
    ``tests/test_retention.py``. These tests pin the live-path wiring: the
    reclaim delegates to ``evict_recording`` with ``keep_recent=2`` /
    ``during_recording=True`` under the per-recording flock, and defers on
    contention rather than racing a concurrent terminal run (AE12).
    """

    def _make_cp(self, capture_dir):
        from screencap.chunk_processor import ChunkProcessor

        return ChunkProcessor(
            capture_dir, multiprocessing.Queue(), multiprocessing.Queue(),
            recording_name="rec-u2", upload_enabled=True, auto_delete=True,
            cloud_intent=True,
        )

    def test_live_reclaim_routes_through_floor_keep_recent_2(self, cloud_capture_dir):
        from screencap.retention import EvictionReport

        cp = self._make_cp(cloud_capture_dir)
        captured = {}

        def _fake_evict(recording_dir, **kw):
            captured.update(kw)
            captured["recording_dir"] = recording_dir
            return EvictionReport(evicted_indices=[0], bytes_freed=4096)

        with mock.patch("screencap.retention.evict_recording", _fake_evict):
            freed = cp._evict_old_chunks_through_floor(2)

        assert freed == 4096
        assert captured["keep_recent"] == 2, "the 2-most-recent window is preserved"
        assert captured["during_recording"] is True
        assert captured["recording_dir"] == cloud_capture_dir

    def test_live_reclaim_defers_on_lock_contention(self, cloud_capture_dir):
        """A concurrent terminal-stage holder makes the reclaim defer (return 0,
        evict_recording never called) — never racing the terminal run (AE12)."""
        import threading

        from screencap import terminal_stage as ts

        cp = self._make_cp(cloud_capture_dir)
        held = threading.Event()
        release = threading.Event()

        def _hold():
            with ts.terminal_lock("rec-u2"):
                held.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=_hold)
        holder.start()
        assert held.wait(timeout=5)
        try:
            with mock.patch("screencap.retention.evict_recording") as ev:
                freed = cp._evict_old_chunks_through_floor(2)
            assert freed == 0
            ev.assert_not_called()
        finally:
            release.set()
            holder.join(timeout=5)


class TestProcessChunkFinalAssignment:
    """V1.75: ``_process_chunk``'s try/finally seam routes through
    ``_pending_network_status`` and the eagerly-set ``PENDING`` entry.

    These tests stage values directly into ``_pending_network_status``
    and exercise ``_process_chunk`` without spinning up the real thread,
    so the try/finally final-assignment contract is verified in
    isolation from the queue + audio-ack plumbing.
    """

    def _make_cp(self, capture_dir):
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            capture_dir, q, ack_q,
            recording_name="rec",
            upload_enabled=True,
            auto_delete=False,
            cloud_intent=False,
        )
        # Replace side-effecting steps with no-ops so _process_chunk
        # reaches the final-assignment block without doing real work.
        cp._wait_for_audio = lambda *a, **kw: None
        cp._transcribe = lambda *a, **kw: None
        cp._trigger_flush = lambda *a, **kw: None
        cp._export_events = lambda *a, **kw: None
        cp._chunk_manifest.produce = lambda *a, **kw: None
        cp._chunk_scrubber.scrub = lambda *a, **kw: None
        cp._collect_chunk_files = lambda *a, **kw: [
            {"name": "events_0000.jsonl", "path": capture_dir / "events_0000.jsonl"},
        ]
        return cp

    def _msg(self, idx: int = 0) -> dict:
        return {
            "type": "chunk_rotated",
            "completed_index": idx,
            "chunk_start_time": 0.0,
            "rotation_time": 1.0,
        }

    def test_success_path_writes_emitted(self, capture_dir):
        """Happy path: no staging + upload succeeds → ``EMITTED``."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._upload_chunk = lambda *a, **kw: True
        cp._process_chunk(self._msg(0))
        assert cp._chunk_results[0] == ChunkStatus.EMITTED

    def test_upload_failure_writes_failed(self, capture_dir):
        """No staging + upload fails → ``FAILED``."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._upload_chunk = lambda *a, **kw: False
        cp._process_chunk(self._msg(0))
        assert cp._chunk_results[0] == ChunkStatus.FAILED

    def test_staged_network_skipped_wins_over_emitted(self, capture_dir):
        """U4's staged ``NETWORK_SKIPPED`` survives a successful upload —
        the network signal must remain visible at the sentinel gate
        even though the core files reached GCS.
        """
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._upload_chunk = lambda *a, **kw: True
        cp._pending_network_status[0] = ChunkStatus.NETWORK_SKIPPED

        cp._process_chunk(self._msg(0))

        assert cp._chunk_results[0] == ChunkStatus.NETWORK_SKIPPED
        # Staging dict is consumed on assignment so it does not grow
        # unbounded across a long recording.
        assert 0 not in cp._pending_network_status

    def test_staged_network_incomplete_wins_over_emitted(self, capture_dir):
        """U5's staged ``NETWORK_INCOMPLETE`` survives a successful upload."""
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._upload_chunk = lambda *a, **kw: True
        cp._pending_network_status[0] = ChunkStatus.NETWORK_INCOMPLETE

        cp._process_chunk(self._msg(0))
        assert cp._chunk_results[0] == ChunkStatus.NETWORK_INCOMPLETE

    def test_staged_status_survives_manifest_failure(self, capture_dir):
        """A manifest-generation re-raise must not downgrade a staged
        NETWORK_INCOMPLETE to FAILED.

        Without the consult in the outer _run handler, the staged
        status would be lost the moment _process_chunk raised — that's
        the regression the plan's outer-handler staging consult fixes.
        Here we verify it via _process_chunk directly: the finally
        block consumes staging, writes NETWORK_INCOMPLETE, then the
        exception propagates.
        """
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._pending_network_status[0] = ChunkStatus.NETWORK_INCOMPLETE

        def boom(*a, **kw):
            raise RuntimeError("manifest write blew up")

        cp._chunk_manifest.produce = boom

        # _process_chunk re-raises the manifest failure (after the
        # finally block writes the final status).
        with pytest.raises(RuntimeError):
            cp._process_chunk(self._msg(0))

        assert cp._chunk_results[0] == ChunkStatus.NETWORK_INCOMPLETE
        assert 0 not in cp._pending_network_status

    def test_early_return_on_stop_event_leaves_pending(self, capture_dir):
        """``_stop_event.set()`` mid-processing → finally block leaves
        the eagerly-set ``PENDING`` entry alone (reached_upload=False
        AND no staging). This is the Bug 2 survivorship-bias fix:
        force-stop yields a non-missing entry that the gate rejects.
        """
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)

        def trip_stop(*a, **kw):
            cp._stop_event.set()

        cp._wait_for_audio = trip_stop  # _process_chunk early-returns
        # Simulate _run's eager PENDING write that happens before
        # _process_chunk is called.
        cp._chunk_results[0] = ChunkStatus.PENDING

        cp._process_chunk(self._msg(0))

        assert cp._chunk_results[0] == ChunkStatus.PENDING

    def test_early_return_consumes_staged_network_status(self, capture_dir):
        """If U4 staged NETWORK_SKIPPED before the stop, the early-return
        path still promotes the staged value (the gate sees the network
        signal rather than a bare PENDING).
        """
        from screencap.chunk_processor import ChunkStatus

        cp = self._make_cp(capture_dir)
        cp._pending_network_status[0] = ChunkStatus.NETWORK_SKIPPED
        cp._chunk_results[0] = ChunkStatus.PENDING

        def trip_stop(*a, **kw):
            cp._stop_event.set()

        cp._wait_for_audio = trip_stop
        cp._process_chunk(self._msg(0))

        assert cp._chunk_results[0] == ChunkStatus.NETWORK_SKIPPED
        assert 0 not in cp._pending_network_status


class TestRunLoopEagerPending:
    """V1.75: ``_run`` writes ``PENDING`` on rotation receipt, BEFORE
    ``_process_chunk`` runs. This is the load-bearing seam for Bug 2.
    """

    def test_pending_set_before_process_chunk(self, capture_dir):
        """Send a rotation, intercept _process_chunk to assert the
        entry is already PENDING when processing starts.
        """
        from screencap.chunk_processor import ChunkProcessor, ChunkStatus

        chunk_q = multiprocessing.Queue()
        audio_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            capture_dir, chunk_q, audio_q,
            recording_name="rec",
            upload_enabled=False, auto_delete=False,
        )

        seen_state: dict[int, ChunkStatus] = {}

        def capture_state(msg):
            idx = msg["completed_index"]
            seen_state[idx] = cp._chunk_results.get(idx)
            # Settle the entry so the loop drains cleanly.
            cp._chunk_results[idx] = ChunkStatus.EMITTED

        cp._process_chunk = capture_state
        cp.start()
        chunk_q.put({
            "type": "chunk_rotated",
            "completed_index": 7,
            "chunk_start_time": 0.0,
            "rotation_time": 1.0,
        })
        chunk_q.put({"type": "poison_pill"})
        cp._thread.join(timeout=10)

        assert seen_state.get(7) == ChunkStatus.PENDING, (
            "_run must eagerly set PENDING before _process_chunk runs "
            "(survivorship-bias fix). Observed: " + str(seen_state)
        )

    def test_outer_handler_writes_failed_when_finally_left_pending(self, capture_dir):
        """If ``_process_chunk`` raises BEFORE its own finally block can
        write (rare — e.g. msg["completed_index"] KeyError at the top),
        the outer ``_run`` handler must fall back to FAILED so the gate
        never sees PENDING for an idx that was processed.
        """
        from screencap.chunk_processor import ChunkProcessor, ChunkStatus

        chunk_q = multiprocessing.Queue()
        audio_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            capture_dir, chunk_q, audio_q,
            recording_name="rec",
            upload_enabled=False, auto_delete=False,
        )

        def boom_before_finally(msg):
            raise RuntimeError("pre-try crash")

        cp._process_chunk = boom_before_finally
        cp.start()
        chunk_q.put({
            "type": "chunk_rotated",
            "completed_index": 3,
            "chunk_start_time": 0.0,
            "rotation_time": 1.0,
        })
        chunk_q.put({"type": "poison_pill"})
        cp._thread.join(timeout=10)

        assert cp._chunk_results.get(3) == ChunkStatus.FAILED

    def test_outer_handler_uses_staged_status_before_falling_back_to_failed(
        self, capture_dir,
    ):
        """If ``_pending_network_status[idx]`` is set when ``_process_chunk``
        raises before its own finally block, the outer ``_run`` handler must
        consume the staged value instead of falling back to ``FAILED``.

        This exercises the branch at lines 441-445 of chunk_processor.py:

            current = self._chunk_results.get(idx, ChunkStatus.PENDING)
            if current == ChunkStatus.PENDING:
                pending = self._pending_network_status.pop(idx, None)
                self._chunk_results[idx] = (
                    pending if pending is not None else ChunkStatus.FAILED
                )

        A future refactor that moves staging out of ``_process_chunk`` into
        ``_run`` would silently regress without this test.
        """
        from screencap.chunk_processor import ChunkProcessor, ChunkStatus

        chunk_q = multiprocessing.Queue()
        audio_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            capture_dir, chunk_q, audio_q,
            recording_name="rec",
            upload_enabled=False, auto_delete=False,
        )

        # Pre-stage NETWORK_INCOMPLETE so the outer handler picks it up.
        cp._pending_network_status[3] = ChunkStatus.NETWORK_INCOMPLETE

        # Replace _process_chunk with one that raises before any try/finally
        # can write to _chunk_results.  The outer _run handler must then
        # consult _pending_network_status.
        def boom_before_finally(msg):
            raise RuntimeError("boom — pre-try crash")

        cp._process_chunk = boom_before_finally
        cp.start()
        chunk_q.put({
            "type": "chunk_rotated",
            "completed_index": 3,
            "chunk_start_time": 0.0,
            "rotation_time": 1.0,
        })
        chunk_q.put({"type": "poison_pill"})
        cp._thread.join(timeout=10)

        # Outer handler must have used the staged NETWORK_INCOMPLETE rather
        # than defaulting to FAILED.
        assert cp._chunk_results.get(3) == ChunkStatus.NETWORK_INCOMPLETE, (
            "_run outer handler must consult _pending_network_status before "
            "falling back to FAILED. Observed: "
            + str(cp._chunk_results.get(3))
        )
        # Staging dict must be consumed so it does not grow unbounded.
        assert 3 not in cp._pending_network_status


class TestForceStopDominatesStatusMap:
    """V1.75: ``was_force_stopped`` still dominates the sentinel gate.

    The upstream gate predicate at engine/collaborators.py is:
        ``cp.all_chunks_uploaded() and not cp.was_force_stopped``
    Even when every entry is ``EMITTED``, ``was_force_stopped=True``
    must block sentinel upload. Restated for the enum to lock the
    contract.
    """

    def test_force_stopped_blocks_sentinel_even_when_all_emitted(self, capture_dir):
        from screencap.chunk_processor import ChunkProcessor, ChunkStatus

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            capture_dir, q, ack_q,
            recording_name="rec",
            upload_enabled=True, auto_delete=False,
        )
        cp._chunk_results[0] = ChunkStatus.EMITTED
        cp._chunk_results[1] = ChunkStatus.EMITTED

        # all_chunks_uploaded is True...
        assert cp.all_chunks_uploaded() is True
        # ...but was_force_stopped flips the upstream composite predicate.
        cp._stop_event.set()
        assert cp.was_force_stopped is True
        assert (cp.all_chunks_uploaded() and not cp.was_force_stopped) is False


class TestLedgerIsCrossProcessSourceOfTruth:
    """U7 characterization: chunk_processor mirrors settled status onto the U1
    ledger so the terminal stage (a SEPARATE process) reconstructs correct
    upload state after a crash — WITHOUT changing the in-memory gating.

    The in-memory ``_chunk_results`` stays the live gate (the existing tests
    above pin it); these pin that the on-disk ledger becomes the faithful
    cross-process replica (EMITTED -> UPLOADED, FAILED -> FAILED).
    """

    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files", return_value=True)
    def test_emitted_chunks_mirror_to_ledger_uploaded(self, mock_upload, mock_sleep, tmp_path):
        from screencap.pipeline_state import PipelineLedger, UploadState

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)
        cp, chunk_q = _build_chunk_processor(tmp_path)
        cp.start()
        _enqueue_chunks(chunk_q, t0, 3)
        cp.stop(timeout=30)

        # In-memory gating unchanged.
        assert cp.all_chunks_uploaded() is True

        # On-disk ledger reflects the same UPLOADED state (cross-process truth).
        ledger = PipelineLedger(tmp_path / "recording.db")
        states = {r.chunk_index: r.upload_state for r in ledger.all_chunks()}
        for i in range(3):
            assert states.get(i) == UploadState.UPLOADED, (
                f"chunk {i} not mirrored UPLOADED onto the ledger: {states}"
            )

    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files")
    def test_failed_chunk_mirrors_to_ledger_failed(self, mock_upload, mock_sleep, tmp_path):
        from screencap.pipeline_state import PipelineLedger, UploadState

        # Chunk 1 fails permanently; 0 and 2 succeed.
        def _upload(recording_name, files, capture_dir):
            idx = int(files[0]["name"].split("_")[1].split(".")[0])
            return idx != 1

        mock_upload.side_effect = _upload

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)
        cp, chunk_q = _build_chunk_processor(tmp_path)
        cp.start()
        _enqueue_chunks(chunk_q, t0, 3)
        cp.stop(timeout=30)

        # In-memory gate blocked by the failure (unchanged behavior).
        assert cp.all_chunks_uploaded() is False

        # The ledger records chunk 1 FAILED, not UPLOADED — "disabled/failed
        # != success" survives to disk for the terminal stage.
        ledger = PipelineLedger(tmp_path / "recording.db")
        states = {r.chunk_index: r.upload_state for r in ledger.all_chunks()}
        assert states.get(0) == UploadState.UPLOADED
        assert states.get(1) == UploadState.FAILED
        assert states.get(2) == UploadState.UPLOADED
        # Closed-set ledger gate also blocks (mirrors the in-memory gate).
        assert ledger.all_uploaded() is False
