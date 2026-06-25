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


def _make_fake_scrub(root: Path):
    """A fast stand-in for ``scrub_recording``.

    Mirrors the real entry point's observable contract — copies the source to
    ``<name>-scrubbed`` with media/derived files filtered out (so events*.jsonl
    and screenshots/ survive), returns a ``ScrubResult`` pointing at it — without
    loading the heavy NER pipeline. Tests that need genuine redaction mark
    themselves ``real_scrub`` to bypass this stub.
    """
    import shutil as _sh

    def _fake(name, pii_engine=None, *, cloud_bound_recovery=False, _already_locked=False):
        from screencap.scrubber import _SKIP_EXTENSIONS, _SKIP_FILES, ScrubResult

        src = root / name
        dst = root / f"{name}-scrubbed"
        if dst.exists():
            _sh.rmtree(dst)

        def _ignore(_d, entries):
            return {
                e for e in entries
                if e in _SKIP_FILES or Path(e).suffix in _SKIP_EXTENSIONS
            }

        _sh.copytree(src, dst, ignore=_ignore)
        return ScrubResult(output_dir=dst)

    return _fake


@pytest.fixture
def recordings_root(tmp_path, monkeypatch, request):
    """A recordings root wired so ``resolve_recording_dir`` finds it.

    Also points the scrubber's ``get_recordings_dir`` at the test root (it binds
    its own reference, so the config patch alone would let real scrubs escape to
    ``~/.screencap``) and, unless the test is marked ``real_scrub``, swaps in the
    fast fake scrub + a no-op chunk recovery so the video/envelope tests don't
    pay the NER cost.
    """
    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setattr("screencap.config.get_recordings_dir", lambda: root)
    monkeypatch.setattr("screencap.scrubber.get_recordings_dir", lambda: root)
    if not request.node.get_closest_marker("real_scrub"):
        monkeypatch.setattr("screencap.scrubber.scrub_recording", _make_fake_scrub(root))
        monkeypatch.setattr(
            "screencap.recovery._recover_chunk_metadata",
            lambda *a, **k: None,
        )
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


def test_auto_exports_missing_events_with_upload_config(recordings_root):
    """Missing events.jsonl is auto-exported into the source dir with the
    canonical *upload* config (``exclude_moves=False``) — not the old
    discrete-only config — so the scrubbed copy the review reads is the exact
    event set ``screencap upload`` would ship (reviewed == uploaded, R3)."""
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

    assert (rec_dir / "events.jsonl").exists(), "canonical export lands in the source dir"
    # The envelope points at the scrubbed copy's events file, not the source.
    assert envelope["events_path"].endswith("/events.jsonl")
    assert "-scrubbed/" in envelope["events_path"]
    ex.assert_called_once()
    assert ex.call_args.kwargs["exclude_moves"] is False


def test_nullable_metadata_serialized_as_json_null(recordings_root):
    """A playable recording with no action events (``_read_recording_meta``
    returns None timing, timing_status="ok") still yields ok=True with
    started_at/duration_seconds as JSON null — the Swift side decodes them as
    Double?. timing_error is False because the DB read itself succeeded."""
    rec_dir = _make_recording(recordings_root, "rec-nometa")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    with mock.patch(
        "screencap.catalog._read_recording_meta",
        return_value=(None, None, "ok"),
    ):
        envelope = prepare_review_data("rec-nometa")

    assert envelope["ok"] is True
    assert envelope["started_at"] is None
    assert envelope["duration_seconds"] is None
    # Benign event-free null is NOT a read failure.
    assert envelope["timing_status"] == "ok"
    assert envelope["timing_error"] is False
    # Serialized as JSON null, not omitted and not 0.
    serialized = json.loads(json.dumps(envelope))
    assert serialized["started_at"] is None
    assert serialized["duration_seconds"] is None
    assert serialized["timing_status"] == "ok"
    assert serialized["timing_error"] is False


def test_timing_status_corrupt_when_db_read_fails(recordings_root):
    """SCR-107/SCR-166: a swallowed DB-read failure (corrupt/unreadable
    recording.db) yields ok=True + null timing with timing_status="corrupt" (and
    the back-compat timing_error=True), so the Swift consumer can surface a
    "metadata couldn't be read" advisory instead of presenting a corrupted DB
    as a clean, event-free review."""
    rec_dir = _make_recording(recordings_root, "rec-badmeta")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    with mock.patch(
        "screencap.catalog._read_recording_meta",
        return_value=(None, None, "corrupt"),
    ):
        envelope = prepare_review_data("rec-badmeta")

    # Still playable — the video renders; only the timeline is unavailable.
    assert envelope["ok"] is True
    assert envelope["started_at"] is None
    assert envelope["duration_seconds"] is None
    assert envelope["timing_status"] == "corrupt"
    # Back-compat boolean stays True for the non-"ok" state.
    assert envelope["timing_error"] is True
    serialized = json.loads(json.dumps(envelope))
    assert serialized["timing_status"] == "corrupt"
    assert serialized["timing_error"] is True


def test_timing_status_locked_is_distinct_from_corrupt(recordings_root):
    """SCR-166: a transient lock yields timing_status="locked" — distinct from
    "corrupt" — so the consumer can say "temporarily unavailable" instead of a
    corruption-flavored advisory. The back-compat boolean is still True."""
    rec_dir = _make_recording(recordings_root, "rec-locked")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    with mock.patch(
        "screencap.catalog._read_recording_meta",
        return_value=(None, None, "locked"),
    ):
        envelope = prepare_review_data("rec-locked")

    assert envelope["ok"] is True
    assert envelope["timing_status"] == "locked"
    # A lock is non-"ok", so the legacy boolean still flags the advisory.
    assert envelope["timing_error"] is True


def test_timing_status_real_corrupt_db(recordings_root):
    """SCR-166 end-to-end: a real corrupt recording.db (non-sqlite bytes) yields
    timing_status="corrupt" in the envelope without mocking _read_recording_meta.

    The review stays playable (ok=True), started_at/duration_seconds are null,
    and timing_status is "corrupt" (not "locked") — a real unreadable DB is
    corruption, not contention.
    """
    rec_dir = _make_recording(recordings_root, "rec-realcorrupt", with_db=False)
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")
    (rec_dir / "recording.db").write_bytes(b"not a sqlite database")

    envelope = prepare_review_data("rec-realcorrupt")

    assert envelope["ok"] is True
    assert envelope["started_at"] is None
    assert envelope["timing_status"] == "corrupt"
    assert envelope["timing_error"] is True


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
    assert "timing_error" in payload
    assert payload["timing_error"] is False
    assert payload["timing_status"] == "ok"


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


def test_cli_human_error_escapes_rich_markup():
    """SCR-117: the human-readable error sink interpolates exception text into a
    Rich-markup string. When that text carries markup metacharacters — most
    dangerously an unbalanced ``[/]`` — Rich raises ``MarkupError``, which the
    surrounding ``except ReviewPrepareError`` does not catch, so it propagates
    and crashes the command with a traceback instead of the intended exit-1.

    Patch ``_should_default_to_json`` to force the human path (CliRunner's
    stdout is non-TTY, which would otherwise auto-select the unaffected --json
    path). The dynamic segment must be escaped, so the brackets survive verbatim
    rather than being parsed as markup (and stripped, or crashing)."""
    from rich.errors import MarkupError

    with mock.patch("screencap.cli._should_default_to_json", return_value=False), \
         mock.patch(
             "screencap.review.prepare_review_data",
             side_effect=ReviewPrepareError("token=[/] in [secret] failed"),
         ):
        result = CliRunner().invoke(cli, ["review-data", "some-rec"])

    # Core regression: markup metacharacters in error text must not crash.
    assert not isinstance(result.exception, MarkupError), result.exception
    assert result.exit_code == 1
    # Escaped, so the literal brackets are preserved instead of stripped.
    assert "[/]" in result.output
    assert "[secret]" in result.output


def test_cli_human_success_escapes_rich_markup():
    """SCR-169: the human-readable *success* path interpolates the user-chosen
    recording name and the envelope's file paths (which embed that name) into
    Rich-markup strings. SCR-117 only covered the error path. A name carrying an
    unbalanced ``[/]`` makes Rich raise ``MarkupError`` and crash the command;
    a balanced ``[x]`` run is silently stripped. The dynamic segments must be
    escaped so the brackets survive verbatim."""
    from rich.errors import MarkupError

    envelope = {
        "ok": True,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "video_path": "/recs/we[/]ird/video.mp4",
        "events_path": "/recs/we[/]ird/events.jsonl",
    }
    with mock.patch("screencap.cli._should_default_to_json", return_value=False), \
         mock.patch(
             "screencap.review.prepare_review_data",
             return_value=envelope,
         ):
        result = CliRunner().invoke(cli, ["review-data", "we[/]ird"])

    assert not isinstance(result.exception, MarkupError), result.exception
    assert result.exit_code == 0
    # Escaped, so the literal brackets survive instead of crashing/stripping.
    assert "we[/]ird" in result.output
    assert "/recs/we[/]ird/video.mp4" in result.output


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


# ---------------------------------------------------------------------------
# U10: chunks-are-the-record; derived video.mp4 + eviction-race hardening (R2)
# ---------------------------------------------------------------------------


def test_chunked_review_derives_video_and_chunks_remain_the_record(recordings_root):
    """R2 happy path: a chunked recording's review DERIVES ``video.mp4`` on
    demand from the chunk set; the chunk set itself remains the record (every
    ``chunk_*.mp4`` survives the concat — the merged file is additive)."""
    rec_dir = _make_recording(recordings_root, "rec-derive")
    _write_video(rec_dir / "chunk_0001.mp4", (200, 0, 0))
    _write_video(rec_dir / "chunk_0002.mp4", (0, 200, 0))
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")
    assert not (rec_dir / "video.mp4").exists()

    envelope = prepare_review_data("rec-derive")

    assert envelope["ok"] is True
    # video.mp4 was derived on demand...
    assert (rec_dir / "video.mp4").exists()
    # ...and the chunk set (the record) is intact.
    assert sorted(p.name for p in rec_dir.glob("chunk_*.mp4")) == [
        "chunk_0001.mp4", "chunk_0002.mp4",
    ]


def test_ensure_single_video_takes_terminal_lock_for_multichunk(recordings_root):
    """The multi-chunk concat is serialized against U8 eviction by taking the
    per-recording ``terminal_lock`` (plan: "serialize via the ledger/flock").
    Spy on the lock to prove it is acquired around the concat."""
    from screencap.viewer import _ensure_single_video

    rec_dir = _make_recording(recordings_root, "rec-lock")
    _write_video(rec_dir / "chunk_0001.mp4", (200, 0, 0))
    _write_video(rec_dir / "chunk_0002.mp4", (0, 200, 0))

    taken: list[str] = []
    import contextlib

    import screencap.terminal_stage as ts

    real_lock = ts.terminal_lock

    @contextlib.contextmanager
    def spy_lock(name, **kwargs):
        taken.append(name)
        with real_lock(name, **kwargs):
            yield

    with mock.patch("screencap.terminal_stage.terminal_lock", spy_lock):
        _ensure_single_video(rec_dir)

    assert taken == ["rec-lock"], "the per-recording terminal lock must be taken"
    assert (rec_dir / "video.mp4").exists()


def test_concat_tolerates_chunk_evicted_while_waiting_for_lock(recordings_root):
    """Race/regression: if eviction removes chunks before the concat acquires
    the lock, the post-lock re-glob sees the surviving set and NEVER leaves a
    half-written ``video.mp4`` masquerading as complete. Here all-but-one chunk
    is evicted while we 'hold' the lock; the survivor is symlinked, no partial
    mux is produced."""
    from screencap.viewer import _ensure_single_video

    rec_dir = _make_recording(recordings_root, "rec-evicted-mid")
    _write_video(rec_dir / "chunk_0001.mp4", (200, 0, 0))
    _write_video(rec_dir / "chunk_0002.mp4", (0, 200, 0))

    import contextlib

    @contextlib.contextmanager
    def evicting_lock(name, **kwargs):
        # Simulate U8 eviction having run just before/while we hold the lock:
        # one chunk's local media is reclaimed. The concat re-globs INSIDE the
        # lock, so it must act on the survivor set, not the stale pre-lock list.
        (rec_dir / "chunk_0001.mp4").unlink()
        yield

    with mock.patch("screencap.terminal_stage.terminal_lock", evicting_lock):
        _ensure_single_video(rec_dir, fail_loud=True)

    # The lone survivor is symlinked — never a partial multi-chunk mux.
    vid = rec_dir / "video.mp4"
    assert vid.exists()
    assert vid.is_symlink(), "single survivor is symlinked, not concatenated"
    assert vid.resolve().name == "chunk_0002.mp4"
    # No stale concat temp left behind.
    assert not list(rec_dir.glob(".video.mp4.*.tmp"))


def test_concat_busy_lock_warns_in_viewer_path(recordings_root):
    """A contended lock (terminal stage mid-eviction) makes the best-effort
    viewer path warn-and-continue rather than hang or leave a partial file —
    no ``video.mp4`` is produced and no exception escapes."""
    from screencap.terminal_stage import TerminalStageBusy
    from screencap.viewer import _ensure_single_video

    rec_dir = _make_recording(recordings_root, "rec-busy")
    _write_video(rec_dir / "chunk_0001.mp4", (200, 0, 0))
    _write_video(rec_dir / "chunk_0002.mp4", (0, 200, 0))

    import contextlib

    @contextlib.contextmanager
    def busy_lock(name, **kwargs):
        raise TerminalStageBusy("held by terminal stage")
        yield  # pragma: no cover

    with mock.patch("screencap.terminal_stage.terminal_lock", busy_lock):
        # Best-effort path: no raise.
        _ensure_single_video(rec_dir, fail_loud=False)
    assert not (rec_dir / "video.mp4").exists(), "no partial video on a busy lock"


def test_concat_busy_lock_propagates_in_review_path(recordings_root):
    """The ``review-data`` path (``fail_loud=True``) propagates a contended lock
    as a clean failure (TerminalStageBusy is a RuntimeError, caught by
    prepare_review_data into the R9 'can't process this video' envelope) — never
    a partial ``video.mp4`` treated as complete."""
    from screencap.terminal_stage import TerminalStageBusy
    from screencap.viewer import _ensure_single_video

    rec_dir = _make_recording(recordings_root, "rec-busy-loud")
    _write_video(rec_dir / "chunk_0001.mp4", (200, 0, 0))
    _write_video(rec_dir / "chunk_0002.mp4", (0, 200, 0))

    import contextlib

    @contextlib.contextmanager
    def busy_lock(name, **kwargs):
        raise TerminalStageBusy("held by terminal stage")
        yield  # pragma: no cover

    with mock.patch("screencap.terminal_stage.terminal_lock", busy_lock):
        with pytest.raises(TerminalStageBusy):
            _ensure_single_video(rec_dir, fail_loud=True)
    assert not (rec_dir / "video.mp4").exists()


def test_legacy_single_file_renders_without_concat_or_lock(recordings_root):
    """R14 regression: a legacy single-file recording (real ``video.mp4``, no
    chunks) renders through the existing path untouched — the no-chunks early
    return never reaches the concat or the terminal lock."""
    from screencap.viewer import _ensure_single_video

    rec_dir = _make_recording(recordings_root, "rec-legacy")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    original = (rec_dir / "video.mp4").read_bytes()

    # The lock must NOT be taken for a legacy recording (no chunks).
    def fail_if_locked(*a, **k):  # pragma: no cover
        raise AssertionError("terminal_lock must not be taken for a legacy recording")

    with mock.patch("screencap.terminal_stage.terminal_lock", fail_if_locked):
        _ensure_single_video(rec_dir)

    assert (rec_dir / "video.mp4").read_bytes() == original, "legacy video untouched"
    assert not (rec_dir / "video.mp4").is_symlink()


# ---------------------------------------------------------------------------
# U2: scrub-before-review orchestration (R1/R2/R5/R7/R9/R11)
# ---------------------------------------------------------------------------


def _write_screenshot(rec_dir: Path, ts: float) -> Path:
    shot_dir = rec_dir / "screenshots"
    shot_dir.mkdir(exist_ok=True)
    p = shot_dir / f"{ts}.jpg"
    Image.new("RGB", (8, 8), color=(10, 20, 30)).save(p)
    return p


def test_event_and_screenshot_paths_resolve_under_scrubbed_dir(recordings_root):
    """Happy path: the envelope's event + screenshot paths live under
    ``<name>-scrubbed`` (what uploads), while the video stays at the local
    original (navigation aid, never uploaded)."""
    rec_dir = _make_recording(recordings_root, "rec-paths")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(
        json.dumps({"_meta": True}) + "\n"
        + json.dumps({"timestamp": 1.0, "type": "key.type", "text": "hi"}) + "\n"
    )
    _write_screenshot(rec_dir, 1.0)

    envelope = prepare_review_data("rec-paths")

    assert envelope["ok"] is True
    assert "/rec-paths-scrubbed/" in envelope["events_path"]
    assert envelope["events_paths"], "the scrubbed event file set must be exposed"
    assert all("/rec-paths-scrubbed/" in p for p in envelope["events_paths"])
    assert envelope["screenshots"], "scrubbed screenshots must be exposed"
    assert all("/rec-paths-scrubbed/screenshots/" in p for p in envelope["screenshots"])
    # The video is the LOCAL original, not under the scrubbed dir.
    assert envelope["video_path"].endswith("/rec-paths/video.mp4")
    assert "-scrubbed" not in envelope["video_path"]


def test_traversal_name_rejected_before_any_scrub(recordings_root):
    """A ``../`` name is rejected before the envelope is built — scrub never
    runs, so no path is emitted from an out-of-tree location."""
    with mock.patch("screencap.scrubber.scrub_recording") as scrub_spy:
        with pytest.raises(ReviewPrepareError):
            prepare_review_data("../escape")
    scrub_spy.assert_not_called()


def test_total_scrub_failure_raises_and_leaves_no_sentinel(recordings_root):
    """A total scrub failure is a structural preparation failure
    (``ReviewPrepareError``) and leaves no reusable scrubbed dir — no
    ``.scrub_complete`` sentinel that a later upload could trust."""
    rec_dir = _make_recording(recordings_root, "rec-scrubfail")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    with mock.patch(
        "screencap.scrubber.scrub_recording",
        side_effect=RuntimeError("scrub blew up"),
    ):
        with pytest.raises(ReviewPrepareError):
            prepare_review_data("rec-scrubfail")

    sentinels = list(recordings_root.glob("**/.scrub_complete"))
    assert sentinels == [], "a failed scrub must not leave a completion sentinel"


def test_recovery_runs_cloud_bound_before_scrub(recordings_root):
    """``review-data`` replicates the upload loop's load-bearing ordering:
    ``_recover_chunk_metadata(cloud_bound=True)`` runs *before* scrub, so the
    prepared dir is upload-equivalent (pointer suppression applies)."""
    rec_dir = _make_recording(recordings_root, "rec-order")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    calls: list = []
    fake_scrub = _make_fake_scrub(recordings_root)

    def spy_recover(*_a, **k):
        calls.append(("recover", k.get("cloud_bound")))

    def spy_scrub(name, pii_engine=None, *, cloud_bound_recovery=False, _already_locked=False):
        calls.append(("scrub", cloud_bound_recovery))
        return fake_scrub(name)

    with mock.patch(
        "screencap.recovery._recover_chunk_metadata", side_effect=spy_recover
    ), mock.patch(
        "screencap.scrubber.scrub_recording", side_effect=spy_scrub
    ):
        prepare_review_data("rec-order")

    # Recovery runs first with cloud_bound=True; the scrub is then told recovery
    # ran (cloud_bound_recovery=True) so the sentinel marks the dir reusable.
    assert calls == [("recover", True), ("scrub", True)], calls


def test_chunked_review_uses_per_chunk_event_set(recordings_root):
    """For a chunked recording the reviewed event source is the per-chunk
    ``events_*.jsonl`` set (what upload ships), not a freshly combined file."""
    rec_dir = _make_recording(recordings_root, "rec-chunked")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    for idx in (0, 1):
        (rec_dir / f"events_{idx:04d}.jsonl").write_text(
            json.dumps({"_meta": True}) + "\n"
        )

    envelope = prepare_review_data("rec-chunked")

    names = sorted(Path(p).name for p in envelope["events_paths"])
    assert names == ["events_0000.jsonl", "events_0001.jsonl"]
    assert all("/rec-chunked-scrubbed/" in p for p in envelope["events_paths"])
    # No combined events.jsonl is conjured — the set matches what ships.
    assert "events.jsonl" not in names


def test_empty_screenshots_returns_empty_set(recordings_root):
    """A recording with an empty ``screenshots/`` returns an explicit empty
    screenshot set, not an error."""
    rec_dir = _make_recording(recordings_root, "rec-noshots")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")
    (rec_dir / "screenshots").mkdir()

    envelope = prepare_review_data("rec-noshots")

    assert envelope["ok"] is True
    assert envelope["screenshots"] == []


@pytest.mark.privacy
@pytest.mark.real_scrub
def test_raw_event_value_absent_from_reviewed_events(recordings_root):
    """Covers AE1: a value present in the raw events is absent from the events
    the envelope points to (the review reads the scrubbed copy)."""
    rec_dir = _make_recording(recordings_root, "rec-redact")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    secret = "alice@contoso.example"
    (rec_dir / "events.jsonl").write_text(
        json.dumps({"_meta": True, "screencap_version": "0.1.0"}) + "\n"
        + json.dumps({"timestamp": 1.0, "type": "key.type",
                      "text": f"email {secret}", "children": []}) + "\n"
    )

    envelope = prepare_review_data("rec-redact")

    reviewed = Path(envelope["events_path"]).read_text()
    assert secret not in reviewed, "the reviewed events must be the scrubbed copy"
    # And the raw original is untouched (R11 — Cancel/decline leaves it intact).
    assert secret in (rec_dir / "events.jsonl").read_text()


@pytest.mark.privacy
@pytest.mark.real_scrub
def test_cli_real_scrub_stdout_stays_clean_json(recordings_root):
    """The genuine scrubber prints its summary to a stdout-bound Rich console;
    review-data must redirect that to stderr so the SwiftUI shell still reads a
    single parseable JSON envelope from stdout (the whole point of the command)."""
    rec_dir = _make_recording(recordings_root, "rec-cli-scrub")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(
        json.dumps({"_meta": True, "screencap_version": "0.1.0"}) + "\n"
        + json.dumps({"timestamp": 1.0, "type": "key.type",
                      "text": "email bob@contoso.example", "children": []}) + "\n"
    )

    result = CliRunner().invoke(cli, ["review-data", "--json", "rec-cli-scrub"])

    assert result.exit_code == 0, result.output
    # If the scrub summary leaked onto stdout, this parse would fail.
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert "Scrub complete" not in result.stdout


# ---------------------------------------------------------------------------
# U3: enriched, versioned envelope (R5/R8/R9/R13/R14/R15)
# ---------------------------------------------------------------------------


def test_envelope_schema_version_is_bumped(recordings_root):
    rec_dir = _make_recording(recordings_root, "rec-ver")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    envelope = prepare_review_data("rec-ver")

    assert envelope["schema_version"] == REVIEW_SCHEMA_VERSION
    assert REVIEW_SCHEMA_VERSION >= 3, "SCR-166's timing_status addition must bump the version"


def test_zero_redactions_serialize_empty_collections_not_null(recordings_root):
    """Pinning (mirrors test_nullable_metadata_serialized_as_json_null): a
    recording with no redactions still yields ok:true with empty — not missing
    — redaction collections, JSON-round-trippable."""
    rec_dir = _make_recording(recordings_root, "rec-noredact")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    envelope = prepare_review_data("rec-noredact")

    assert envelope["ok"] is True
    red = envelope["redaction"]
    assert red["summary"] == {}
    assert red["markers"] == []
    assert red["blocked_intervals"] == []
    assert red["fail_closed"] == []
    # Round-trips as JSON (no inf, no non-serializable Counter leaking).
    serialized = json.loads(json.dumps(envelope))
    assert serialized["redaction"]["summary"] == {}


def test_coverage_facts_present_and_structured(recordings_root):
    """Covers AE4: coverage carries the R9 facts as structured booleans the UI
    renders copy from (video/audio local-only, transcript scrubbed, screenshots
    uploaded, allowed-app on-screen PII the operator's to verify)."""
    rec_dir = _make_recording(recordings_root, "rec-cov")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    envelope = prepare_review_data("rec-cov")

    cov = envelope["coverage"]
    assert cov["video_local_only"] is True
    assert cov["audio_local_only"] is True
    assert cov["allowed_app_screenshot_pii_manual_review"] is True
    assert set(cov) == {
        "video_local_only",
        "audio_local_only",
        "transcript_uploaded_scrubbed",
        "screenshots_uploaded",
        "allowed_app_screenshot_pii_manual_review",
    }


def test_coverage_reflects_transcript_presence(recordings_root):
    rec_dir = _make_recording(recordings_root, "rec-trans")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")
    (rec_dir / "transcript.txt").write_text("hello world")

    envelope = prepare_review_data("rec-trans")

    assert envelope["coverage"]["transcript_uploaded_scrubbed"] is True


@pytest.mark.privacy
@pytest.mark.real_scrub
def test_redaction_summary_populated_for_pii_recording(recordings_root):
    """Happy path: a recording with PII yields a non-empty redaction.summary
    (entity → count), with markers/blocked_intervals/fail_closed present as
    lists and the screenshot set exposed."""
    rec_dir = _make_recording(recordings_root, "rec-evidence")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(
        json.dumps({"_meta": True, "screencap_version": "0.1.0"}) + "\n"
        + json.dumps({"timestamp": 1.0, "type": "key.type",
                      "text": "reach me at carol@contoso.example", "children": []}) + "\n"
    )

    envelope = prepare_review_data("rec-evidence")

    red = envelope["redaction"]
    assert red["summary"], "PII must surface as an entity → count summary"
    assert sum(red["summary"].values()) >= 1
    # The email entity is what we planted; assert it surfaced (not just a tally).
    assert any("EMAIL" in entity for entity in red["summary"]), red["summary"]
    assert isinstance(red["markers"], list)
    assert isinstance(red["blocked_intervals"], list)
    assert isinstance(red["fail_closed"], list)
    # Markers, when present, carry export-safe shape (timestamp + category only).
    assert all(set(m) == {"t", "category"} for m in red["markers"]), red["markers"]
    assert "screenshots" in envelope


def test_event_set_outside_recordings_root_is_rejected(recordings_root):
    """Defense-in-depth: a scrubbed dir that resolves outside the recordings
    root (e.g. via a symlink escape after name validation) is refused before any
    path is emitted."""
    rec_dir = _make_recording(recordings_root, "rec-escape")
    _write_video(rec_dir / "video.mp4", (0, 0, 200), pix_fmt="yuv420p")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    from screencap.scrubber import ScrubResult

    escaped = recordings_root.parent / "rec-escape-scrubbed"  # outside the root
    escaped.mkdir()

    with mock.patch(
        "screencap.scrubber.scrub_recording",
        return_value=ScrubResult(output_dir=escaped),
    ):
        with pytest.raises(ReviewPrepareError, match="outside the recordings root"):
            prepare_review_data("rec-escape")


# ---------------------------------------------------------------------------
# Shared canonical-events export helper (review + upload must not drift)
# ---------------------------------------------------------------------------


def test_ensure_canonical_events_gating(tmp_path):
    """The single shared export gate: skip when chunked or already present
    (unless force); export with the upload config (exclude_moves=False)
    otherwise. Both review and `screencap upload` go through this so they
    cannot drift."""
    from screencap.exporter import ensure_canonical_events

    rec = tmp_path / "rec"
    rec.mkdir()

    # Chunked → skip (per-chunk events_*.jsonl are the canonical source).
    (rec / "events_0000.jsonl").write_text("{}\n")
    with mock.patch("screencap.exporter.export_recording") as ex:
        assert ensure_canonical_events(rec) is None
        ex.assert_not_called()
    (rec / "events_0000.jsonl").unlink()

    # Existing events.jsonl, no force → trust it, skip.
    (rec / "events.jsonl").write_text("{}\n")
    with mock.patch("screencap.exporter.export_recording") as ex:
        assert ensure_canonical_events(rec) is None
        ex.assert_not_called()

    # force=True → re-export with the canonical config.
    with mock.patch("screencap.exporter.export_recording", return_value=7) as ex:
        assert ensure_canonical_events(rec, force=True) == 7
        ex.assert_called_once()
        assert ex.call_args.kwargs["exclude_moves"] is False

    # Missing → export.
    (rec / "events.jsonl").unlink()
    with mock.patch("screencap.exporter.export_recording", return_value=3) as ex:
        assert ensure_canonical_events(rec) == 3
        ex.assert_called_once()
        assert ex.call_args.kwargs["exclude_moves"] is False


def test_redaction_markers_exclude_allow_screenshots():
    """mask_screenshots emits an AuditEntry for EVERY frame it processes,
    including clean ALLOW ones; the per-moment markers channel must exclude them
    so the review timeline doesn't draw a 'redaction' tick on un-redacted frames
    (todo 007). The summary tally is separate (entity_counts, text detections)
    and is unaffected."""
    from screencap.privacy.reasons import AuditEntry
    from screencap.review import _build_redaction_evidence
    from screencap.scrubber import ScrubResult

    result = ScrubResult()
    result.audit_entries = [
        AuditEntry(timestamp=1.0, surface="screenshot", action="allow",
                   reason="policy_allowed_app"),
        AuditEntry(timestamp=2.0, surface="screenshot", action="exclude",
                   reason="policy_excluded_app"),
        AuditEntry(timestamp=3.0, surface="event", action="mask_window",
                   reason="context_email_surface"),
    ]

    red = _build_redaction_evidence(result)

    marker_times = [m["t"] for m in red["markers"]]
    assert 1.0 not in marker_times, "clean ALLOW frame must not be a redaction marker"
    assert marker_times == [2.0, 3.0], "only genuinely redacted/masked moments mark"
