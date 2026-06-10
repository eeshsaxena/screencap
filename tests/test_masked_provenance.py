"""SCR-126 Fix 3: masked-video provenance + closed-set reconciliation.

These lock the stale-copy / orphan invariants that keep a stale or under-masked
``masked_video/`` copy from ever reaching the cloud upload set: provenance gates
reuse (R4), the closed-set reconcile purges orphans (H2), a coverage-FAILED chunk
leaves no stale copy, and the convergence fast path is mask-logic-version-aware
(R8).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from fractions import Fraction
from pathlib import Path
from unittest import mock

import av
import pytest
from PIL import Image

from screencap.scrubber import (
    MASK_PROVENANCE_VERSION,
    MASKED_PROVENANCE_NAME,
    is_masked_video_reusable,
    masked_provenance_version_current,
    masked_video_dir,
    purge_orphan_masked_videos,
    write_masked_provenance,
)

WIDTH, HEIGHT, FPS = 64, 48, 10


def _make_src(tmp_path: Path, *, n_chunks: int = 2, pixel_ratio: float = 2.0) -> Path:
    """A minimal source recording dir: recording.db (recording + window_geometry)
    plus n source chunk_*.mp4 files (opaque bytes — content is what's hashed)."""
    src = tmp_path / "rec"
    src.mkdir(parents=True, exist_ok=True)
    db = src / "recording.db"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, pixel_ratio REAL)")
        conn.execute("INSERT INTO recording (id, pixel_ratio) VALUES (1, ?)", (pixel_ratio,))
        conn.execute(
            "CREATE TABLE window_geometry (id INTEGER PRIMARY KEY, "
            "recording_id INTEGER, screenshot_timestamp REAL, window_list_json TEXT)"
        )
        conn.execute(
            "INSERT INTO window_geometry (recording_id, screenshot_timestamp, "
            "window_list_json) VALUES (1, 0.5, '{\"windows\": []}')"
        )
        conn.commit()
    for idx in range(n_chunks):
        (src / f"chunk_{idx:04d}.mp4").write_bytes(f"source-bytes-{idx}".encode())
    return src


def _make_masked_copies(scrubbed_dir: Path, indices: list[int]) -> None:
    md = masked_video_dir(scrubbed_dir)
    md.mkdir(parents=True, exist_ok=True)
    for idx in indices:
        (md / f"chunk_{idx:04d}.mp4").write_bytes(f"masked-{idx}".encode())


def test_provenance_roundtrip_reusable(tmp_path):
    """A fresh provenance pass over an unchanged source is reusable."""
    src = _make_src(tmp_path)
    scrubbed = tmp_path / "rec-scrubbed"
    _make_masked_copies(scrubbed, [0, 1])
    write_masked_provenance(src, scrubbed, masked_indices=[0, 1])

    assert is_masked_video_reusable(src, scrubbed, expected_indices=[0, 1])
    # The provenance record is dot-prefixed (excluded from upload).
    assert (masked_video_dir(scrubbed) / MASKED_PROVENANCE_NAME).name.startswith(".")


def test_provenance_version_mismatch_not_reusable(tmp_path):
    src = _make_src(tmp_path)
    scrubbed = tmp_path / "rec-scrubbed"
    _make_masked_copies(scrubbed, [0, 1])
    write_masked_provenance(src, scrubbed, masked_indices=[0, 1])

    # Tamper the recorded version to simulate copies from older mask logic.
    prov_path = masked_video_dir(scrubbed) / MASKED_PROVENANCE_NAME
    data = json.loads(prov_path.read_text())
    data["mask_provenance_version"] = MASK_PROVENANCE_VERSION + 1
    prov_path.write_text(json.dumps(data))

    assert not is_masked_video_reusable(src, scrubbed, expected_indices=[0, 1])
    assert not masked_provenance_version_current(scrubbed)


def test_provenance_invalidated_by_source_geometry_or_pixel_ratio_change(tmp_path):
    src = _make_src(tmp_path)
    scrubbed = tmp_path / "rec-scrubbed"
    _make_masked_copies(scrubbed, [0, 1])
    write_masked_provenance(src, scrubbed, masked_indices=[0, 1])
    assert is_masked_video_reusable(src, scrubbed, expected_indices=[0, 1])

    # (a) A source chunk's bytes change → not reusable (the input _SOURCE_HASH_GLOBS
    # omits; this is the whole reason masked-video provenance exists).
    (src / "chunk_0001.mp4").write_bytes(b"DIFFERENT source bytes")
    assert not is_masked_video_reusable(src, scrubbed, expected_indices=[0, 1])

    # (b) A geometry change → not reusable.
    src2 = _make_src(tmp_path / "g")
    scr2 = tmp_path / "g" / "rec-scrubbed"
    _make_masked_copies(scr2, [0, 1])
    write_masked_provenance(src2, scr2, masked_indices=[0, 1])
    with contextlib.closing(sqlite3.connect(str(src2 / "recording.db"))) as conn:
        conn.execute(
            "INSERT INTO window_geometry (recording_id, screenshot_timestamp, "
            "window_list_json) VALUES (1, 0.9, '{\"windows\": [1]}')"
        )
        conn.commit()
    assert not is_masked_video_reusable(src2, scr2, expected_indices=[0, 1])

    # (c) A pixel_ratio change → not reusable (every mask rect's geometry shifts).
    src3 = _make_src(tmp_path / "p", pixel_ratio=2.0)
    scr3 = tmp_path / "p" / "rec-scrubbed"
    _make_masked_copies(scr3, [0, 1])
    write_masked_provenance(src3, scr3, masked_indices=[0, 1])
    with contextlib.closing(sqlite3.connect(str(src3 / "recording.db"))) as conn:
        conn.execute("UPDATE recording SET pixel_ratio = 1.0 WHERE id = 1")
        conn.commit()
    assert not is_masked_video_reusable(src3, scr3, expected_indices=[0, 1])


def test_reuse_requires_every_expected_masked_copy_present(tmp_path):
    """An incomplete masked set (a copy missing for an expected chunk) is not
    reusable — guards against shipping a partially-masked recording."""
    src = _make_src(tmp_path, n_chunks=3)
    scrubbed = tmp_path / "rec-scrubbed"
    _make_masked_copies(scrubbed, [0, 1])  # chunk 2's copy is absent
    write_masked_provenance(src, scrubbed, masked_indices=[0, 1, 2])
    assert not is_masked_video_reusable(src, scrubbed, expected_indices=[0, 1, 2])


def test_purge_orphan_masked_videos_closed_set(tmp_path):
    """The authoritative closed-set reconcile deletes a masked copy whose source
    chunk no longer exists (orphan), and keeps the expected ones (H2)."""
    src = _make_src(tmp_path, n_chunks=2)  # sources for 0, 1
    scrubbed = tmp_path / "rec-scrubbed"
    _make_masked_copies(scrubbed, [0, 1, 7])  # chunk 7 is an orphan (no source)

    purged = purge_orphan_masked_videos(scrubbed, expected_indices={0, 1})

    md = masked_video_dir(scrubbed)
    assert purged == [7]
    assert (md / "chunk_0000.mp4").exists()
    assert (md / "chunk_0001.mp4").exists()
    assert not (md / "chunk_0007.mp4").exists()


def test_masked_convergence_ok_gate(tmp_path):
    """R8 fast-path gate: flag OFF → always ok; flag ON → ok only when provenance
    is at the current mask-logic version (else re-mask)."""
    from screencap.terminal_stage import _masked_convergence_ok

    src = _make_src(tmp_path)
    scrubbed = src.parent / f"{src.name}-scrubbed"
    _make_masked_copies(scrubbed, [0, 1])

    # Flag OFF (default): the gate is a no-op — convergence is fine.
    with mock.patch(
        "screencap.pipeline_chunk_ops.get_frozen_masked_video_upload",
        return_value=False,
    ):
        assert _masked_convergence_ok(src) is True

    # Flag ON but NO provenance yet → not ok (must fall through to produce).
    with mock.patch(
        "screencap.pipeline_chunk_ops.get_frozen_masked_video_upload",
        return_value=True,
    ):
        assert _masked_convergence_ok(src) is False

        # Flag ON + current provenance → ok.
        write_masked_provenance(src, scrubbed, masked_indices=[0, 1])
        assert _masked_convergence_ok(src) is True

        # Flag ON + stale-version provenance → not ok.
        prov_path = masked_video_dir(scrubbed) / MASKED_PROVENANCE_NAME
        data = json.loads(prov_path.read_text())
        data["mask_provenance_version"] = MASK_PROVENANCE_VERSION + 99
        prov_path.write_text(json.dumps(data))
        assert _masked_convergence_ok(src) is False


def _make_mp4(path: Path, *, n_frames: int = 10) -> None:
    container = av.open(str(path), mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=FPS)
    stream.width, stream.height, stream.pix_fmt = WIDTH, HEIGHT, "yuv420p"
    stream.codec_context.time_base = Fraction(1, FPS)
    stream.options = {"crf": "23", "preset": "ultrafast", "g": "1", "bf": "0"}
    for i in range(n_frames):
        frame = av.VideoFrame.from_image(Image.new("RGB", (WIDTH, HEIGHT), (200, 30, 30)))
        frame.pts = i
        frame.time_base = Fraction(1, FPS)
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode():
        container.mux(pkt)
    container.close()


def test_in_masker_failed_purges_stale_preexisting_copy(tmp_path):
    """A coverage-FAILED chunk must leave NO masked copy on disk — including a
    stale copy from a PRIOR successful run (mask_video_chunk's case-(b) early
    return does not touch a pre-existing output_path; the wrapper purges it)."""
    from screencap.scrubber import mask_video_chunk_for_cloud
    from screencap.video_mask import MaskOutcomeStatus

    src = tmp_path / "rec"
    src.mkdir()
    chunk = src / "chunk_0000.mp4"
    _make_mp4(chunk)
    # DB with NO window_geometry rows → coverage unprovable → FAILED.
    db = src / "recording.db"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, pixel_ratio REAL)")
        conn.execute("INSERT INTO recording (id, pixel_ratio) VALUES (1, 1.0)")
        conn.execute(
            "CREATE TABLE window_geometry (id INTEGER PRIMARY KEY, "
            "recording_id INTEGER, screenshot_timestamp REAL, window_list_json TEXT)"
        )
        conn.commit()

    scrubbed = tmp_path / "rec-scrubbed"
    md = masked_video_dir(scrubbed)
    md.mkdir(parents=True)
    stale = md / "chunk_0000.mp4"
    stale.write_bytes(b"STALE masked copy from a prior, looser run")

    outcome = mask_video_chunk_for_cloud(
        chunk, db, scrubbed, chunk_index=0,
        start_ts=0.0, end_ts=1.0, chunk_start_abs=0.0,
        pixel_ratio=1.0, enabled=True,
    )
    assert outcome is not None
    assert outcome.status is MaskOutcomeStatus.FAILED
    assert not stale.exists(), "stale masked copy survived a FAILED chunk"


def test_provenance_excluded_from_upload_enumeration(tmp_path):
    """The provenance record is dot-prefixed so list_recording_files never ships
    it (it stays local — and contains hashes, not media)."""
    from screencap.upload import list_recording_files

    src = _make_src(tmp_path)
    scrubbed = tmp_path / "rec-scrubbed"
    _make_masked_copies(scrubbed, [0])
    write_masked_provenance(src, scrubbed, masked_indices=[0])

    names = {fi.name for fi in list_recording_files(scrubbed)}
    assert not any(MASKED_PROVENANCE_NAME in n for n in names)


def test_masked_video_dirname_coupled_to_upload_gate():
    """The upload gate's masked-dir literal must stay coupled to masked_video_dir's
    actual dirname. A rename of one without the other would flip the fail-closed
    gate (best case breaks upload; worst case fails open) — this fails loudly."""
    from screencap.upload import _MASKED_VIDEO_DIRNAME

    assert masked_video_dir(Path("/x")).name == _MASKED_VIDEO_DIRNAME


def test_mask_videos_origin_uses_filename_index_for_noncontiguous_chunks(tmp_path):
    """Regression: _mask_videos must compute each chunk's absolute origin as
    base+idx*dur keyed on the FILENAME index, so a non-contiguous source set (e.g.
    chunk 1 evicted, leaving 0 and 2) masks chunk 2 at its TRUE origin — not at
    epoch 0 (which fails closed and leaves the recording stuck)."""
    from screencap.terminal_stage import CloudCopyOutcome, CloudCopyProducer

    src = _make_src(tmp_path, n_chunks=0)  # db only; we add specific chunk files
    # Non-contiguous source chunks: 0 and 2 present, 1 missing (evicted).
    (src / "chunk_0000.mp4").write_bytes(b"c0")
    (src / "chunk_0002.mp4").write_bytes(b"c2")
    # Real action events so derive_chunk_grid yields a base; rec_start=100.
    with contextlib.closing(sqlite3.connect(str(src / "recording.db"))) as conn:
        conn.execute("DROP TABLE IF EXISTS recording")
        conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)")
        conn.execute("INSERT INTO recording (id, timestamp, pixel_ratio) VALUES (1, 100.0, 1.0)")
        conn.execute("CREATE TABLE action_event (id INTEGER PRIMARY KEY, timestamp REAL)")
        conn.execute("INSERT INTO action_event (timestamp) VALUES (100.5), (130.0)")
        conn.commit()

    scrubbed = tmp_path / "rec-scrubbed"
    (masked_video_dir(scrubbed)).mkdir(parents=True)
    captured: dict[int, float] = {}

    class _Cls:
        masked, failed = True, False

    def _fake_mask(recording_dir, scrubbed_dir, idx, *, start_ts, end_ts, **kw):
        captured[idx] = start_ts
        return _Cls()

    with mock.patch(
        "screencap.pipeline_chunk_ops.get_frozen_masked_video_upload", return_value=True
    ), mock.patch(
        "screencap.pipeline_chunk_ops.mask_chunk_for_cloud", side_effect=_fake_mask
    ), mock.patch("screencap.config.get_chunk_duration", return_value=10.0), mock.patch(
        "screencap.scrubber.is_masked_video_reusable", return_value=False
    ):
        producer = CloudCopyProducer(src)
        outcome = CloudCopyOutcome(scrubbed_dir=scrubbed)
        producer._mask_videos(scrubbed, None, outcome)

    # base_ts = min(rec_start=100.0, first_event=100.5) = 100.0; chunk_dur = 10.0.
    assert captured[0] == pytest.approx(100.0)
    assert captured[2] == pytest.approx(120.0), (
        "non-contiguous chunk 2 was masked at the wrong origin (the position-vs-"
        "filename aliasing regression)"
    )


def test_mask_videos_reuse_skip_avoids_remask(tmp_path):
    """When the masked set is provably current, _mask_videos skips the re-encode
    (mask_chunk_for_cloud is never called) — and still purges orphans first."""
    from screencap.terminal_stage import CloudCopyOutcome, CloudCopyProducer

    src = _make_src(tmp_path, n_chunks=2)
    scrubbed = tmp_path / "rec-scrubbed"
    _make_masked_copies(scrubbed, [0, 1])

    called = {"n": 0}

    def _fake_mask(*a, **k):
        called["n"] += 1
        class _C:
            masked, failed = True, False
        return _C()

    with mock.patch(
        "screencap.pipeline_chunk_ops.get_frozen_masked_video_upload", return_value=True
    ), mock.patch(
        "screencap.pipeline_chunk_ops.mask_chunk_for_cloud", side_effect=_fake_mask
    ):
        # Provenance must be written under the SAME flag-ON view the reuse check
        # reads it (in production both happen with the flag ON).
        write_masked_provenance(src, scrubbed, masked_indices=[0, 1])
        producer = CloudCopyProducer(src)
        outcome = CloudCopyOutcome(scrubbed_dir=scrubbed)
        producer._mask_videos(scrubbed, None, outcome)

    assert called["n"] == 0, "re-masked despite a provably-current masked set"
    assert sorted(outcome.masked_chunks) == [0, 1]
