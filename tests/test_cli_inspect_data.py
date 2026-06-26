"""Tests for the ``screencap inspect-data`` CLI subcommand and the
``screencap.review.prepare_inspect_data`` orchestration (native read-only
inspect window, U1).

``inspect-data`` is the no-scrub sibling of ``review-data``: the SwiftUI shell
calls it before opening the read-only inspect window (from a search result or a
recordings-list click — "just looking", not uploading). It reads the LOCAL
recording from the ORIGINAL dir with no scrub, so these tests assert the
distinct contract: the no-scrub invariant (no ``-scrubbed`` dir, the scrub lock
never acquired), original-dir event resolution (per-chunk and combined),
tolerated-empty events (video-first), null redaction/coverage, path containment,
and the structural failure flavors. The shared video/timing core is already
covered by ``test_cli_review_data.py``.
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
from screencap.engine.video import VideoWriter
from screencap.review import (
    REVIEW_SCHEMA_VERSION,
    ReviewPrepareError,
    prepare_inspect_data,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _init_timestamp():
    """VideoWriter relies on the engine timestamp system being initialized."""
    utils.set_start_time(time.time())


def _write_video(path: Path, color: tuple[int, int, int] = (0, 0, 200)) -> None:
    """Write a small, already-AVKit-safe (yuv420p) H.264 video via VideoWriter."""
    base = time.time()
    writer = VideoWriter(str(path), width=64, height=64, fps=24, pix_fmt="yuv420p")
    for i in range(5):
        writer.write_frame(Image.new("RGB", (64, 64), color=color), base + i / 24)
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
    """A recordings root wired so ``resolve_recording_dir`` and the containment
    check both see the test tree."""
    root = tmp_path / "recordings"
    root.mkdir()
    monkeypatch.setattr("screencap.config.get_recordings_dir", lambda: root)
    return root


# ---------------------------------------------------------------------------
# prepare_inspect_data — reads the original dir, never scrubs
# ---------------------------------------------------------------------------


def test_inspect_reads_original_dir_and_never_scrubs(recordings_root):
    """Happy path: the envelope's video + events resolve under the ORIGINAL
    recording dir (no ``-scrubbed`` sibling is produced), redaction/coverage are
    null, and screenshots is empty (video-first, no masked pane)."""
    rec_dir = _make_recording(recordings_root, "rec-inspect")
    _write_video(rec_dir / "video.mp4")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    envelope = prepare_inspect_data("rec-inspect")

    assert envelope["ok"] is True
    assert envelope["schema_version"] == REVIEW_SCHEMA_VERSION
    # Video + events live under the ORIGINAL dir, never a scrubbed copy.
    assert envelope["video_path"].endswith("/rec-inspect/video.mp4")
    assert envelope["events_path"].endswith("/rec-inspect/events.jsonl")
    assert all("-scrubbed" not in p for p in envelope["events_paths"])
    assert "-scrubbed" not in envelope["video_path"]
    # No scrubbed dir was produced anywhere under the root.
    assert list(recordings_root.glob("*-scrubbed")) == []
    # No upload-payload evidence on a "just looking" surface.
    assert envelope["redaction"] is None
    assert envelope["coverage"] is None
    assert envelope["screenshots"] == []


def test_no_scrub_invariant_lock_never_acquired(recordings_root):
    """The no-scrub invariant, durably: inspect-data must never call
    ``scrub_recording`` nor acquire ``recording_scrub_lock`` — a future refactor
    that pulled in the lock would block concurrent review/upload for the
    recording without scrubbing anything. Patch both to fail loudly if touched."""
    import contextlib

    rec_dir = _make_recording(recordings_root, "rec-nolock")
    _write_video(rec_dir / "video.mp4")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    @contextlib.contextmanager
    def forbidden_lock(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("inspect-data must not acquire recording_scrub_lock")
        yield

    def forbidden_scrub(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("inspect-data must not call scrub_recording")

    with mock.patch("screencap.scrubber.recording_scrub_lock", forbidden_lock), \
         mock.patch("screencap.scrubber.scrub_recording", forbidden_scrub):
        envelope = prepare_inspect_data("rec-nolock")

    assert envelope["ok"] is True
    assert list(recordings_root.glob("*-scrubbed")) == []


def test_chunked_events_resolve_from_original_dir(recordings_root):
    """A chunked recording's events are the per-chunk ``events_*.jsonl`` set in
    the ORIGINAL dir (the chunked-recording branch the doc-review flagged) — not
    a scrubbed copy, and not a null events_path."""
    rec_dir = _make_recording(recordings_root, "rec-chunked")
    _write_video(rec_dir / "video.mp4")
    for idx in (0, 1):
        (rec_dir / f"events_{idx:04d}.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    envelope = prepare_inspect_data("rec-chunked")

    names = sorted(Path(p).name for p in envelope["events_paths"])
    assert names == ["events_0000.jsonl", "events_0001.jsonl"]
    assert all("/rec-chunked/" in p and "-scrubbed" not in p for p in envelope["events_paths"])


def test_nonchunked_events_exported_to_original_dir(recordings_root):
    """A non-chunked recording with no events.jsonl gets one exported into the
    ORIGINAL dir via the shared ``ensure_canonical_events`` gate (which the
    review/upload path also uses — so include_network=False is inherited, not a
    new decision), and the envelope points at it."""
    rec_dir = _make_recording(recordings_root, "rec-export")
    _write_video(rec_dir / "video.mp4")
    assert not (rec_dir / "events.jsonl").exists()

    def fake_export(rec_dir, output_path, exclude_moves, metadata, **kwargs):
        Path(output_path).write_text(json.dumps(metadata) + "\n")
        return 1

    with mock.patch("screencap.exporter.export_recording", side_effect=fake_export) as ex:
        envelope = prepare_inspect_data("rec-export")

    assert (rec_dir / "events.jsonl").exists(), "canonical export lands in the original dir"
    assert envelope["events_path"].endswith("/rec-export/events.jsonl")
    assert "-scrubbed" not in envelope["events_path"]
    # Shared gate → canonical upload config (the network-exclusion guarantee).
    assert ex.call_args.kwargs["exclude_moves"] is False


def test_empty_events_is_tolerated_video_first(recordings_root):
    """Unlike review (which fails on no reviewable events), inspect is
    video-first: a recording whose event set is empty still yields ok=True with
    events_path=None / events_paths=[], so the operator can still watch it."""
    rec_dir = _make_recording(recordings_root, "rec-noevents")
    _write_video(rec_dir / "video.mp4")

    # No events.jsonl, no chunks; skip the export so none is conjured.
    with mock.patch("screencap.review._export_canonical_events", lambda _d: None):
        envelope = prepare_inspect_data("rec-noevents")

    assert envelope["ok"] is True
    assert envelope["events_paths"] == []
    assert envelope["events_path"] is None
    assert envelope["video_path"].endswith("/rec-noevents/video.mp4")


def test_events_export_failure_is_tolerated(recordings_root):
    """A failed events export (e.g. a corrupt recording.db) must not block
    looking at the video — it is logged to stderr and the window still opens
    with an empty event set, rather than failing the whole inspect."""
    rec_dir = _make_recording(recordings_root, "rec-exportfail")
    _write_video(rec_dir / "video.mp4")

    def boom(_d):
        raise RuntimeError("events export blew up")

    with mock.patch("screencap.review._export_canonical_events", side_effect=boom):
        envelope = prepare_inspect_data("rec-exportfail")

    assert envelope["ok"] is True
    assert envelope["events_paths"] == []
    assert envelope["video_path"].endswith("/rec-exportfail/video.mp4")


def test_nullable_timing_serialized_as_json_null(recordings_root):
    """SCR-102 parity: a playable recording with no action events still yields
    ok=True with null timing — the Swift readiness guard must not gate on it."""
    rec_dir = _make_recording(recordings_root, "rec-nometa")
    _write_video(rec_dir / "video.mp4")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    with mock.patch(
        "screencap.catalog._read_recording_meta", return_value=(None, None, "ok")
    ):
        envelope = prepare_inspect_data("rec-nometa")

    assert envelope["ok"] is True
    assert envelope["started_at"] is None
    assert envelope["duration_seconds"] is None
    assert envelope["timing_status"] == "ok"
    assert envelope["timing_error"] is False


# ---------------------------------------------------------------------------
# prepare_inspect_data — security / structural failure flavors
# ---------------------------------------------------------------------------


def test_path_outside_recordings_root_is_rejected(recordings_root, tmp_path):
    """Defense-in-depth: a symlinked event file that resolves OUTSIDE the
    recordings root is refused before any path is emitted (a symlink inside the
    dir could otherwise put an out-of-tree absolute path into the envelope)."""
    rec_dir = _make_recording(recordings_root, "rec-escape")
    _write_video(rec_dir / "video.mp4")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"_meta": True}) + "\n")
    (rec_dir / "events.jsonl").symlink_to(outside)

    with pytest.raises(ReviewPrepareError, match="outside the recordings root"):
        prepare_inspect_data("rec-escape")


def test_no_video_is_distinct_error(recordings_root):
    """A recording with neither video.mp4 nor chunks yields the distinct 'no
    video to review' error — nothing to look at, so inspect cannot open."""
    _make_recording(recordings_root, "rec-novideo")  # DB only

    with pytest.raises(ReviewPrepareError, match="no video to review") as exc:
        prepare_inspect_data("rec-novideo")
    assert "can't process this video" not in str(exc.value)


def test_invalid_name_traversal_is_rejected(recordings_root):
    """A path-traversal name is rejected before any path work — distinct from a
    decode failure."""
    with pytest.raises(ReviewPrepareError) as exc:
        prepare_inspect_data("../escaping")
    assert "can't process this video" not in str(exc.value)


def test_missing_recording_is_distinct_error(recordings_root):
    with pytest.raises(ReviewPrepareError, match="not found"):
        prepare_inspect_data("does-not-exist")


# ---------------------------------------------------------------------------
# CLI command (`screencap inspect-data`)
# ---------------------------------------------------------------------------


def test_cli_emits_json_envelope(recordings_root):
    rec_dir = _make_recording(recordings_root, "rec-cli")
    _write_video(rec_dir / "video.mp4")
    (rec_dir / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")

    result = CliRunner().invoke(cli, ["inspect-data", "--json", "rec-cli"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["schema_version"] == REVIEW_SCHEMA_VERSION
    assert payload["video_path"].endswith("/rec-cli/video.mp4")
    assert payload["events_path"].endswith("/rec-cli/events.jsonl")
    assert payload["redaction"] is None
    assert payload["coverage"] is None


def test_cli_cant_process_emits_error_envelope(recordings_root):
    """A corrupt source surfaces ok=false + non-zero exit + the structural
    'can't process this video' message — never a raw traceback."""
    rec_dir = _make_recording(recordings_root, "rec-cli-bad")
    (rec_dir / "video.mp4").write_bytes(b"not a real mp4")

    result = CliRunner().invoke(cli, ["inspect-data", "--json", "rec-cli-bad"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["schema_version"] == REVIEW_SCHEMA_VERSION
    assert "can't process this video" in payload["error"]
    assert "ffmpeg" not in payload["error"].lower()
