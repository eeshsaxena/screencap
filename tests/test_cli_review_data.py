"""Tests for the ``screencap review-data`` CLI subcommand and the
``screencap.review`` orchestration module (plan SCR-97, U4).

The SwiftUI shell calls this command before opening the review window to
prepare the recording for native playback. After U4 all video processing
runs in-process via PyAV (``screencap.engine.video``) — no ffmpeg/ffprobe
on PATH — so most tests build *real* tiny videos and exercise the actual
concat → probe → remediate pipeline. The engine primitives themselves are
unit-tested in ``tests/engine/test_video.py``; here we cover the
orchestration: envelope shape, the R9 "can't process this video" failure
state, nullable metadata serialization, and events.jsonl auto-export.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner
from PIL import Image

from screencap.cli import cli
from screencap.engine import utils
from screencap.engine.video import VideoWriter, read_pixel_format
from screencap.review import (
    REVIEW_SCHEMA_VERSION,
    ReviewPrepareError,
    prepare_review_data,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _init_timestamp():
    """VideoWriter relies on the engine timestamp system being initialized."""
    utils.set_start_time(time.time())


def _write_video(
    path: Path,
    color: tuple[int, int, int] = (200, 0, 0),
    *,
    pix_fmt: str | None = None,
    n_frames: int = 5,
    fps: int = 24,
    size: int = 64,
) -> None:
    """Write a real solid-color H.264 video via VideoWriter.

    Default ``pix_fmt`` (None) is the recorder's yuv444p — the format AVKit
    rejects and the pipeline must remediate. Pass ``"yuv420p"`` for an
    already-AVKit-safe source.
    """
    base = time.time()
    writer = VideoWriter(str(path), width=size, height=size, fps=fps, pix_fmt=pix_fmt)
    for i in range(n_frames):
        writer.write_frame(Image.new("RGB", (size, size), color=color), base + i / fps)
    writer.close()


def _make_recording(root: Path, name: str, *, with_db: bool = True) -> Path:
    """Create a recording dir under ``root`` (optionally with a timing DB)."""
    rec_dir = root / name
    rec_dir.mkdir()
    if with_db:
        conn = sqlite3.connect(rec_dir / "recording.db")
        conn.execute("CREATE TABLE recording (timestamp REAL)")
        conn.execute("INSERT INTO recording VALUES (1716800000.0)")
        conn.commit()
        conn.close()
    return rec_dir


@pytest.fixture
def recordings_root(tmp_path, monkeypatch):
    """A recordings root wired so ``resolve_recording_dir`` finds it."""
    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setattr("screencap.config.get_recordings_dir", lambda: root)
    return root


# ---------------------------------------------------------------------------
# prepare_review_data — real PyAV pipeline (no ffmpeg)
# ---------------------------------------------------------------------------


def test_yuv444p_source_remediated_without_ffmpeg(recordings_root, monkeypatch):
    """Covers AE1 (remediation half), R1/R5: a yuv444p recording is remediated
    fully in-process — proven by stripping PATH so no ffmpeg/ffprobe is reachable."""
    monkeypatch.setenv("PATH", "")
    rec_dir = _make_recording(recordings_root, "rec-444")
    _write_video(rec_dir / "video.mp4")  # yuv444p (recorder default)
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    envelope = prepare_review_data("rec-444")

    assert envelope["ok"] is True
    assert envelope["video_pixfmt_remediated"] is True
    assert envelope["video_path"].endswith("/.video_review.mp4")
    # The advertised playable path is a real, AVKit-safe yuv420p file.
    assert read_pixel_format(Path(envelope["video_path"])) == "yuv420p"
    # The original lossless video.mp4 is untouched (R6).
    assert read_pixel_format(rec_dir / "video.mp4") == "yuv444p"


def test_chunked_yuv444p_full_pipeline_without_ffmpeg(recordings_root, monkeypatch):
    """Covers AE1 (full chain) + integration: chunk-only yuv444p recording with
    PATH stripped → in-process concat → remediate → playable yuv420p envelope."""
    monkeypatch.setenv("PATH", "")
    rec_dir = _make_recording(recordings_root, "rec-chunks")
    # Two chunks, no merged video.mp4 — forces the concat path.
    _write_video(rec_dir / "chunk_0001.mp4", (200, 0, 0))
    _write_video(rec_dir / "chunk_0002.mp4", (0, 200, 0))
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    envelope = prepare_review_data("rec-chunks")

    assert (rec_dir / "video.mp4").exists(), "concat must produce video.mp4"
    assert envelope["video_pixfmt_remediated"] is True
    assert envelope["video_path"].endswith("/.video_review.mp4")
    assert read_pixel_format(Path(envelope["video_path"])) == "yuv420p"


def test_yuv420p_source_not_remediated(recordings_root):
    """Covers AE2: an already-AVKit-safe recording is served as-is, no artifact."""
    rec_dir = _make_recording(recordings_root, "rec-420")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    envelope = prepare_review_data("rec-420")

    assert envelope["ok"] is True
    assert envelope["video_pixfmt_remediated"] is False
    assert envelope["video_path"].endswith("/video.mp4")
    assert not (rec_dir / ".video_review.mp4").exists()
    assert envelope["started_at"] == pytest.approx(1716800000.0)


def test_idempotent_second_run_does_no_reencode(recordings_root):
    """Covers AE3: a second invocation reuses the artifact and re-runs nothing."""
    rec_dir = _make_recording(recordings_root, "rec-idem")
    _write_video(rec_dir / "video.mp4")  # yuv444p
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    first = prepare_review_data("rec-idem")
    # Tamper with the artifact; a second re-encode would overwrite it.
    Path(first["video_path"]).write_bytes(b"SENTINEL")
    second = prepare_review_data("rec-idem")

    assert second == first, "second run must return an identical envelope"
    assert Path(second["video_path"]).read_bytes() == b"SENTINEL", (
        "re-encode ran on the idempotent second call"
    )


# ---------------------------------------------------------------------------
# prepare_review_data — R9 structural failure state + distinct error flavors
# ---------------------------------------------------------------------------


def test_undecodable_video_raises_cant_process(recordings_root):
    """Covers AE5/R9: a genuinely corrupt source surfaces the structural
    "can't process this video" failure — never a missing-binary message."""
    rec_dir = _make_recording(recordings_root, "rec-bad")
    (rec_dir / "video.mp4").write_bytes(b"not a real mp4")

    with pytest.raises(ReviewPrepareError, match="can't process this video") as exc:
        prepare_review_data("rec-bad")
    assert "ffmpeg" not in str(exc.value).lower()


def test_corrupt_chunks_concat_failure_raises_cant_process(recordings_root):
    """A chunk PyAV cannot concat propagates through fail_loud=True into the R9
    failure state (the HTML viewer would swallow it; review-data must not)."""
    rec_dir = _make_recording(recordings_root, "rec-badchunks")
    (rec_dir / "chunk_0001.mp4").write_bytes(b"garbage one")
    (rec_dir / "chunk_0002.mp4").write_bytes(b"garbage two")

    with pytest.raises(ReviewPrepareError, match="can't process this video") as exc:
        prepare_review_data("rec-badchunks")
    assert "ffmpeg" not in str(exc.value).lower()


def test_invalid_name_traversal_is_distinct_error(recordings_root):
    """A path-traversal name is rejected with an error structurally distinct
    from the decode-failure state (no "can't process this video")."""
    with pytest.raises(ReviewPrepareError) as exc:
        prepare_review_data("../escaping")
    assert "can't process this video" not in str(exc.value)


def test_missing_recording_is_distinct_error(recordings_root):
    with pytest.raises(ReviewPrepareError, match="not found") as exc:
        prepare_review_data("does-not-exist")
    assert "can't process this video" not in str(exc.value)


# ---------------------------------------------------------------------------
# prepare_review_data — orchestration (events export, nullable metadata)
# ---------------------------------------------------------------------------


def test_auto_exports_missing_events_filtered(recordings_root):
    """Missing events.jsonl is auto-exported with exclude_moves=True so the
    timeline pane only sees discrete events."""
    rec_dir = _make_recording(recordings_root, "rec-noev")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    assert not (rec_dir / "events.jsonl").exists()

    def fake_export(rec_dir, output_path, exclude_moves, metadata, **kwargs):
        Path(output_path).write_text(json.dumps(metadata) + "\n")
        return 0

    with mock.patch(
        "screencap.exporter.export_recording", side_effect=fake_export
    ) as ex:
        envelope = prepare_review_data("rec-noev")

    assert (rec_dir / "events.jsonl").exists()
    assert envelope["events_path"].endswith("/events.jsonl")
    ex.assert_called_once()
    assert ex.call_args.kwargs["exclude_moves"] is True


def test_nullable_metadata_serialized_as_json_null(recordings_root):
    """A playable recording with no action events (``_read_recording_meta``
    returns None) still yields ok=True with started_at/duration_seconds as
    JSON null — the Swift side decodes them as Double?."""
    rec_dir = _make_recording(recordings_root, "rec-nometa")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    with mock.patch(
        "screencap.catalog._read_recording_meta", return_value=(None, None)
    ):
        envelope = prepare_review_data("rec-nometa")

    assert envelope["ok"] is True
    assert envelope["started_at"] is None
    assert envelope["duration_seconds"] is None
    # Serialized as JSON null, not omitted and not 0.
    serialized = json.loads(json.dumps(envelope))
    assert serialized["started_at"] is None
    assert serialized["duration_seconds"] is None


# ---------------------------------------------------------------------------
# CLI command (`screencap review-data`)
# ---------------------------------------------------------------------------


def test_cli_emits_json_envelope(recordings_root):
    rec_dir = _make_recording(recordings_root, "rec-cli")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    result = CliRunner().invoke(cli, ["review-data", "--json", "rec-cli"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["schema_version"] == REVIEW_SCHEMA_VERSION
    assert payload["video_path"].endswith("/video.mp4")
    assert payload["events_path"].endswith("/events.jsonl")
    assert payload["video_pixfmt_remediated"] is False


def test_cli_cant_process_emits_error_envelope(recordings_root):
    """R9/AE5 at the CLI boundary: corrupt source → ok=false, non-zero exit,
    "can't process this video", and not a missing-binary message."""
    rec_dir = _make_recording(recordings_root, "rec-cli-bad")
    (rec_dir / "video.mp4").write_bytes(b"not a real mp4")

    result = CliRunner().invoke(cli, ["review-data", "--json", "rec-cli-bad"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["schema_version"] == REVIEW_SCHEMA_VERSION
    assert "can't process this video" in payload["error"]
    assert "ffmpeg" not in payload["error"].lower()


def test_cli_invalid_name_emits_error_envelope(recordings_root):
    result = CliRunner().invoke(cli, ["review-data", "--json", "../escape"])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["schema_version"] == REVIEW_SCHEMA_VERSION
    assert payload["error"]
    assert "can't process this video" not in payload["error"]


def test_cli_chunked_recording_stdout_is_clean_json(recordings_root):
    """A multi-chunk recording triggers _ensure_single_video's concat-progress
    prints. Those must go to stderr, leaving stdout as a single parseable JSON
    envelope — otherwise the SwiftUI shell (which parses stdout) breaks on
    every chunked recording, the central case review-data exists to serve."""
    rec_dir = _make_recording(recordings_root, "rec-cli-chunks")
    _write_video(rec_dir / "chunk_0001.mp4", (200, 0, 0))
    _write_video(rec_dir / "chunk_0002.mp4", (0, 200, 0))
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    result = CliRunner().invoke(cli, ["review-data", "--json", "rec-cli-chunks"])

    assert result.exit_code == 0, result.output
    # result.stdout is the stdout stream alone (what the SwiftUI subprocess
    # reads); the concat-progress lines must land on stderr, not here, so this
    # parses as a single JSON envelope. (result.output combines both streams.)
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["video_pixfmt_remediated"] is True
    assert payload["video_path"].endswith("/.video_review.mp4")
    # The progress noise is present, but isolated on stderr.
    assert "Concatenating" in result.stderr


# ---------------------------------------------------------------------------
# Failure-flavor coverage (every failure → clean envelope, structurally distinct)
# ---------------------------------------------------------------------------


def test_no_video_recording_is_distinct_error(recordings_root):
    """A recording with neither video.mp4 nor chunks yields a distinct
    'no video to review' error — not 'can't process this video', not 'not found'."""
    _make_recording(recordings_root, "rec-novideo")  # DB only, no video, no chunks

    with pytest.raises(ReviewPrepareError, match="no video to review") as exc:
        prepare_review_data("rec-novideo")
    msg = str(exc.value)
    assert "can't process this video" not in msg
    assert "not found" not in msg.lower()


def test_events_export_failure_becomes_clean_error(recordings_root):
    """A playable video but a missing recording.db makes the events auto-export
    raise ExportError; it must surface as a ReviewPrepareError (clean envelope),
    not a raw traceback escaping the command's failure contract."""
    rec_dir = _make_recording(recordings_root, "rec-nodb", with_db=False)
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    assert not (rec_dir / "events.jsonl").exists()

    with pytest.raises(ReviewPrepareError, match="could not prepare review events"):
        prepare_review_data("rec-nodb")


def test_concat_oserror_becomes_cant_process(recordings_root, monkeypatch):
    """An OSError from the concat step (PyAV av.error.OSError, or an
    os.replace/mux write failure) is caught and surfaced as the structural
    'can't process this video' state — the catch must include OSError, not just
    RuntimeError/ValueError, or it would escape as a raw traceback."""
    rec_dir = _make_recording(recordings_root, "rec-oserr")
    _write_video(rec_dir / "chunk_0001.mp4", (200, 0, 0))
    _write_video(rec_dir / "chunk_0002.mp4", (0, 200, 0))
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("screencap.engine.video.concat_video_chunks", boom)
    with pytest.raises(ReviewPrepareError, match="can't process this video"):
        prepare_review_data("rec-oserr")
