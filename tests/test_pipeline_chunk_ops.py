"""Tests for screencap.pipeline_chunk_ops — the SCR-125 U1 shared mask seam.

The seam both the live ``chunk_processor`` upload and the post-hoc
``terminal_stage`` convergence route through, so a given chunk masks identically
no matter which surface processes it (R1/R5/R-SCR125-A).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest


def _make_ledger(tmp_path: Path, n_chunks: int = 1):
    """Build a recording.db with the ledger schema + n seeded STAGED chunks."""
    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    rec_dir = tmp_path / "rec"
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
    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    for i in range(n_chunks):
        ledger.seed_chunk(i)
        ledger.mark_staged(i)
    return rec_dir, ledger


def _fake_outcome(status):
    from screencap.video_mask import MaskOutcome, MaskOutcomeStatus

    return MaskOutcome(status=MaskOutcomeStatus(status), reason="test")


def test_get_frozen_masked_video_upload_intent_wins_over_global(tmp_path):
    """The frozen .recording_intent value is authoritative over the mutable
    global (R-SCR125-A: a mid-recording global flip cannot change behavior)."""
    from screencap.pipeline_chunk_ops import get_frozen_masked_video_upload

    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / ".recording_intent").write_text(json.dumps({"masked_video_upload": True}))
    with mock.patch("screencap.config.get_masked_video_upload_enabled", return_value=False):
        assert get_frozen_masked_video_upload(rec) is True

    (rec / ".recording_intent").write_text(json.dumps({"masked_video_upload": False}))
    with mock.patch("screencap.config.get_masked_video_upload_enabled", return_value=True):
        assert get_frozen_masked_video_upload(rec) is False


def test_get_frozen_masked_video_upload_legacy_falls_back_to_global(tmp_path):
    """A legacy intent with no frozen field falls back to the global."""
    from screencap.pipeline_chunk_ops import get_frozen_masked_video_upload

    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / ".recording_intent").write_text(json.dumps({"destination": "cloud"}))
    with mock.patch("screencap.config.get_masked_video_upload_enabled", return_value=True):
        assert get_frozen_masked_video_upload(rec) is True


def test_mask_chunk_for_cloud_off_is_noop(tmp_path):
    """enabled=False → no masker invoked, no ledger change, empty classification
    (the capture-blocked source chunk is the cloud copy)."""
    from screencap.pipeline_chunk_ops import mask_chunk_for_cloud

    rec_dir, ledger = _make_ledger(tmp_path)
    scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
    with mock.patch("screencap.scrubber.mask_video_chunk_for_cloud") as masker:
        cls = mask_chunk_for_cloud(
            rec_dir, scrubbed, 0, start_ts=0.0, end_ts=5.0,
            enabled=False, ledger=ledger,
        )
    masker.assert_not_called()
    assert not cls.masked and not cls.failed
    from screencap.pipeline_state import Lifecycle

    assert ledger.get_chunk(0).lifecycle is Lifecycle.STAGED  # unchanged


def test_mask_chunk_for_cloud_failclosed_on_raise(tmp_path):
    """A masker exception is fail-closed: failed=True + ledger mark_failed."""
    from screencap.pipeline_chunk_ops import mask_chunk_for_cloud
    from screencap.pipeline_state import UploadState

    rec_dir, ledger = _make_ledger(tmp_path)
    scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
    with mock.patch(
        "screencap.scrubber.mask_video_chunk_for_cloud",
        side_effect=RuntimeError("boom"),
    ):
        cls = mask_chunk_for_cloud(
            rec_dir, scrubbed, 0, start_ts=0.0, end_ts=5.0,
            enabled=True, ledger=ledger,
        )
    assert cls.failed and not cls.masked
    assert ledger.get_chunk(0).upload_state is UploadState.FAILED


def test_mask_chunk_for_cloud_failed_outcome_marks_failed(tmp_path):
    """A MaskOutcomeStatus.FAILED outcome is fail-closed (no copy → FAILED)."""
    from screencap.pipeline_chunk_ops import mask_chunk_for_cloud
    from screencap.pipeline_state import UploadState

    rec_dir, ledger = _make_ledger(tmp_path)
    scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
    with mock.patch(
        "screencap.scrubber.mask_video_chunk_for_cloud",
        return_value=_fake_outcome("failed"),
    ):
        cls = mask_chunk_for_cloud(
            rec_dir, scrubbed, 0, start_ts=0.0, end_ts=5.0,
            enabled=True, ledger=ledger,
        )
    assert cls.failed and not cls.masked
    assert ledger.get_chunk(0).upload_state is UploadState.FAILED


def test_mask_chunk_for_cloud_ok_marks_scrubbed(tmp_path):
    """A MASKED outcome → masked=True + ledger mark_scrubbed; masked_path points
    at the masked_video/ copy."""
    from screencap.pipeline_chunk_ops import mask_chunk_for_cloud
    from screencap.pipeline_state import Lifecycle

    rec_dir, ledger = _make_ledger(tmp_path)
    scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
    with mock.patch(
        "screencap.scrubber.mask_video_chunk_for_cloud",
        return_value=_fake_outcome("masked"),
    ):
        cls = mask_chunk_for_cloud(
            rec_dir, scrubbed, 0, start_ts=0.0, end_ts=5.0,
            enabled=True, ledger=ledger,
        )
    assert cls.masked and not cls.failed
    assert cls.masked_path == scrubbed / "masked_video" / "chunk_0000.mp4"
    assert ledger.get_chunk(0).lifecycle is Lifecycle.SCRUBBED


def test_mask_chunk_ok_does_not_downgrade_uploaded(tmp_path):
    """The guard: an already-UPLOADED/EVICTED chunk is NOT downgraded to SCRUBBED
    (re-entry safety — else the row sticks SCRUBBED and never gates complete)."""
    from screencap.pipeline_chunk_ops import mask_chunk_for_cloud
    from screencap.pipeline_state import Lifecycle

    rec_dir, ledger = _make_ledger(tmp_path)
    ledger.mark_uploaded(0)  # a prior reconcile already confirmed it
    scrubbed = rec_dir.parent / f"{rec_dir.name}-scrubbed"
    with mock.patch(
        "screencap.scrubber.mask_video_chunk_for_cloud",
        return_value=_fake_outcome("masked"),
    ):
        mask_chunk_for_cloud(
            rec_dir, scrubbed, 0, start_ts=0.0, end_ts=5.0,
            enabled=True, ledger=ledger,
        )
    assert ledger.get_chunk(0).lifecycle is Lifecycle.UPLOADED  # not downgraded
