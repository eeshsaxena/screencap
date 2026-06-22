"""Tests for screencap.catalog."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from screencap.catalog import find_db, get_seen_bundle_ids, list_recordings, read_intent


@pytest.fixture
def recordings_dir(tmp_path):
    return tmp_path / "recordings"


def _make_recording(base: Path, name: str, *, audio: bool = False, duration: float = 60.0):
    """Create a recording directory with real engine DB schema."""
    from screencap.engine.db import create_db, crud

    d = base / name
    d.mkdir(parents=True)

    db_path = d / "recording.db"
    started = time.time() - duration
    engine, Session = create_db(str(db_path))
    session = Session()

    recording = crud.insert_recording(session, {
        "timestamp": started,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    crud.insert_action_event(session, recording, started + duration, {
        "name": "click",
        "mouse_x": 100.0,
        "mouse_y": 200.0,
        "mouse_button_name": "left",
        "mouse_pressed": True,
    })
    session.close()
    engine.dispose()

    if audio:
        (d / "audio.flac").write_bytes(b"fake")

    return d


def _make_recording_with_window_events(base: Path, name: str, bundle_ids: list[str]):
    """Create a recording dir with real engine DB and window events."""
    from screencap.engine.db import create_db, crud

    d = base / name
    d.mkdir(parents=True)

    db_path = d / "recording.db"
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
    for i, bid in enumerate(bundle_ids):
        crud.insert_window_event(session, recording, 1000.0 + i, {
            "title": "title",
            "app_bundle_id": bid,
            "window_id": "w1",
            "left": 0, "top": 0, "width": 1920, "height": 1080,
        })
    session.close()
    engine.dispose()

    return d


def test_list_empty(recordings_dir):
    recordings_dir.mkdir(parents=True)
    result = list_recordings(recordings_dir)
    assert result == []


def test_list_nonexistent(tmp_path):
    result = list_recordings(tmp_path / "nope")
    assert result == []


def test_list_single(recordings_dir):
    _make_recording(recordings_dir, "test1", audio=True, duration=120)
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    assert result[0].name == "test1"
    assert result[0].has_audio is True
    assert "2m" in result[0].duration


def test_list_multiple(recordings_dir):
    _make_recording(recordings_dir, "alpha", duration=60)
    _make_recording(recordings_dir, "beta", audio=True, duration=300)
    result = list_recordings(recordings_dir)
    assert len(result) == 2
    names = [r.name for r in result]
    assert "alpha" in names
    assert "beta" in names


def test_skips_non_recording_dirs(recordings_dir):
    recordings_dir.mkdir(parents=True)
    # Dir without recording.db should be skipped
    (recordings_dir / "random_dir").mkdir()
    result = list_recordings(recordings_dir)
    assert len(result) == 0


def test_find_db_returns_recording_db(tmp_path):
    (tmp_path / "recording.db").touch()
    assert find_db(tmp_path).name == "recording.db"


def test_find_db_returns_none(tmp_path):
    assert find_db(tmp_path) is None


# --- read_intent tests ---


@pytest.mark.parametrize("destination", ["cloud", "local"])
def test_read_intent_returns_destination(tmp_path, destination):
    intent_path = tmp_path / ".recording_intent"
    intent_path.write_text(
        json.dumps(
            {
                "version": 1,
                "destination": destination,
                "privacy_mode": "public",
                "created_at": "2026-03-07T14:30:00Z",
                "source": "flag",
            }
        )
    )
    assert read_intent(tmp_path) == destination


def test_read_intent_missing(tmp_path):
    assert read_intent(tmp_path) is None


def test_read_intent_corrupt(tmp_path):
    intent_path = tmp_path / ".recording_intent"
    intent_path.write_text("NOT VALID JSON {{{")
    assert read_intent(tmp_path) is None


# --- list_recordings intent integration tests ---


def test_list_recordings_with_intent(recordings_dir):
    d = _make_recording(recordings_dir, "cloud-rec", duration=30)
    intent_path = d / ".recording_intent"
    intent_path.write_text(
        json.dumps(
            {
                "version": 1,
                "destination": "cloud",
                "privacy_mode": "public",
                "created_at": "2026-03-07T14:30:00Z",
                "source": "flag",
            }
        )
    )
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    assert result[0].name == "cloud-rec"
    assert result[0].intent == "cloud"


def test_list_recordings_legacy_no_intent(recordings_dir):
    _make_recording(recordings_dir, "legacy-rec", duration=45)
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    assert result[0].name == "legacy-rec"
    assert result[0].intent is None


# --- Unit 4c: raw started_at + duration_seconds for SwiftUI consumers ---


def test_list_recordings_populates_raw_started_at(recordings_dir):
    """RecordingInfo.started_at carries the Unix timestamp from the recording row."""
    _make_recording(recordings_dir, "raw-fields-rec", duration=42.5)
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    info = result[0]
    assert info.started_at is not None
    assert isinstance(info.started_at, float)
    # Started ~42.5s ago, so it should be in the past
    assert info.started_at < time.time()
    # Sanity: the formatted date matches the raw timestamp
    from datetime import datetime
    assert datetime.fromtimestamp(info.started_at).strftime("%Y-%m-%d") == info.date


def test_list_recordings_populates_raw_duration_seconds(recordings_dir):
    """RecordingInfo.duration_seconds carries the raw float duration."""
    _make_recording(recordings_dir, "duration-rec", duration=125.0)
    result = list_recordings(recordings_dir)
    info = result[0]
    assert info.duration_seconds is not None
    assert isinstance(info.duration_seconds, float)
    assert info.duration_seconds > 0
    # Sanity: formatted duration matches the raw float
    # (should be "2m 5s" for 125s)
    assert "2m" in info.duration


def test_recording_info_namedtuple_asdict_includes_new_fields(recordings_dir):
    """_asdict() serializes the new fields (so JSON output picks them up)."""
    _make_recording(recordings_dir, "asdict-rec", duration=10.0)
    info = list_recordings(recordings_dir)[0]
    d = info._asdict()
    assert "started_at" in d
    assert "duration_seconds" in d
    assert d["started_at"] is not None
    assert d["duration_seconds"] is not None


# --- U3 catalog guard: hidden review artifact must not mask stub detection ---


def test_lingering_review_artifact_does_not_mask_stub(recordings_dir):
    """An uploaded recording with only a hidden .video_review.mp4 is still a stub.

    pathlib glob("*.mp4") matches dotfiles, so without the guard the review
    artifact would count as media and hide a post-upload stub (R6).
    """
    d = _make_recording(recordings_dir, "stub-rec", duration=30)
    (d / ".upload_status.json").write_text("{}")  # mark uploaded
    (d / ".video_review.mp4").write_bytes(b"fake review video")  # lingering artifact

    info = list_recordings(recordings_dir)[0]
    assert info.uploaded is True
    assert info.is_stub is True


def test_real_video_is_not_a_stub(recordings_dir):
    """Control: a real (non-hidden) video.mp4 counts as media even when uploaded."""
    d = _make_recording(recordings_dir, "live-rec", duration=30)
    (d / ".upload_status.json").write_text("{}")
    (d / "video.mp4").write_bytes(b"real video bytes")

    info = list_recordings(recordings_dir)[0]
    assert info.uploaded is True
    assert info.is_stub is False


# --- U9: chunked vs. legacy single-file flag (R14) ---


def _add_chunks(d: Path, n: int):
    """Add n chunk_*.mp4 + manifests to a recording dir (chunked recording)."""
    for i in range(n):
        (d / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 64)
        (d / f"chunk_{i:04d}_manifest.json").write_text("{}")


def test_chunked_recording_flagged_is_chunked(recordings_dir):
    """A recording with chunk_*.mp4 files is flagged is_chunked=True."""
    d = _make_recording(recordings_dir, "chunked-rec", duration=30)
    _add_chunks(d, 3)
    info = list_recordings(recordings_dir)[0]
    assert info.is_chunked is True
    assert info.chunks_total == 3


def test_legacy_single_file_flagged_not_chunked(recordings_dir):
    """A legacy single-file recording (video.mp4, no chunks) is is_chunked=False
    but remains listable and readable (R14)."""
    d = _make_recording(recordings_dir, "legacy-single", duration=30)
    (d / "video.mp4").write_bytes(b"single-file video")
    info = list_recordings(recordings_dir)[0]
    assert info.is_chunked is False
    assert info.chunks_total == 0
    # Still fully listable/readable: name + duration + raw fields populated.
    assert info.name == "legacy-single"
    assert info.started_at is not None


def test_stub_chunked_recording_flagged_is_chunked(recordings_dir):
    """A stubbed (uploaded, media-evicted) chunked recording is still flagged
    chunked via its surviving manifests / status files, not only local media."""
    d = _make_recording(recordings_dir, "stub-chunked", duration=30)
    # Manifests + status files survive eviction even when chunk_*.mp4 are gone.
    for i in range(2):
        (d / f"chunk_{i:04d}_manifest.json").write_text("{}")
        (d / f".chunk_chunk_{i:04d}_status.json").write_text("{}")
    info = list_recordings(recordings_dir)[0]
    assert info.is_chunked is True


# --- U10: chunks-are-the-record stub detection (R2), ledger-aware ---


def _seed_ledger(d: Path, n: int, *, uploaded: bool, evicted: bool = False):
    """Seed a U1 ledger for ``d`` with ``n`` chunks in the given upload state.

    ``uploaded`` marks every chunk UPLOADED; ``evicted`` additionally walks each
    through the begin/commit eviction transitions (UPLOADED -> EVICTED) so the
    ledger reflects a post-upload-confirm reclaim.
    """
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    db_path = d / "recording.db"
    ensure_pipeline_state_schema(db_path)
    led = PipelineLedger(db_path)
    for i in range(n):
        led.seed_chunk(i)
    led.freeze_chunks_expected(n)
    if uploaded:
        for i in range(n):
            led.mark_uploaded(i)
    if evicted:
        for i in range(n):
            led.begin_eviction(i, remote_exists=lambda: True)
            led.commit_eviction(i, unlink=lambda: None)
    return led


def test_chunked_recording_with_chunks_is_not_a_false_stub(recordings_dir):
    """R2: the chunk set is the record. A chunked recording whose chunks are
    still on disk is NOT a stub even though it has no single ``video.mp4`` (the
    merged file is a derived on-demand artifact, not the record). Detection must
    not rely on a single ``video.mp4`` being present."""
    d = _make_recording(recordings_dir, "chunked-live", duration=30)
    _add_chunks(d, 3)
    # Uploaded, but the rich chunk media is still present (not yet evicted).
    _seed_ledger(d, 3, uploaded=True)
    assert not (d / "video.mp4").exists(), "no merged video.mp4 — chunks are the record"

    info = list_recordings(recordings_dir)[0]
    assert info.is_chunked is True
    assert info.uploaded is True
    assert info.is_stub is False, "chunks present on disk -> not a stub"


def test_uploaded_then_evicted_chunked_recording_is_a_legitimate_stub(recordings_dir):
    """An uploaded chunked recording whose media was evicted post-upload-confirm
    is a LEGITIMATE stub — derived from the ledger's UPLOADED state, with no
    legacy ``.chunk_*_status.json`` markers and no merged ``video.mp4``."""
    d = _make_recording(recordings_dir, "evicted-stub", duration=30)
    # Manifests survive eviction; the chunk_*.mp4 media is gone (evicted).
    for i in range(2):
        (d / f"chunk_{i:04d}_manifest.json").write_text("{}")
    _seed_ledger(d, 2, uploaded=True, evicted=True)
    assert not any(d.glob("chunk_*.mp4")), "media evicted"
    assert not any(d.glob(".chunk_*_status.json")), "no legacy upload markers"

    info = list_recordings(recordings_dir)[0]
    assert info.is_chunked is True
    assert info.uploaded is True, "ledger UPLOADED state makes it uploaded"
    assert info.is_stub is True, "uploaded + media evicted = legitimate stub"


def test_ledger_failed_chunk_is_not_a_false_stub(recordings_dir):
    """The ledger never manufactures a FALSE stub: a chunked recording whose
    chunks FAILED cloud masking (fail-closed, never UPLOADED) with its local
    media preserved is NOT uploaded and NOT a stub."""
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    d = _make_recording(recordings_dir, "failed-rec", duration=30)
    _add_chunks(d, 2)
    db_path = d / "recording.db"
    ensure_pipeline_state_schema(db_path)
    led = PipelineLedger(db_path)
    for i in range(2):
        led.seed_chunk(i)
    led.freeze_chunks_expected(2)
    for i in range(2):
        led.mark_failed(i, detail="video_mask FAILED")

    info = list_recordings(recordings_dir)[0]
    assert info.uploaded is False, "FAILED chunks are never UPLOADED"
    assert info.is_stub is False, "fail-closed local media preserved -> not a stub"


def test_locally_evicted_recording_is_not_a_stub(recordings_dir):
    """A local recording (never uploaded) whose chunks were evicted under a
    size/time cap (LOCAL_DONE -> EVICTED) is NOT a stub — `uploaded` stays
    False, so the ledger never flips it to a stub."""
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    d = _make_recording(recordings_dir, "local-evicted", duration=30)
    (d / f"chunk_{0:04d}_manifest.json").write_text("{}")
    db_path = d / "recording.db"
    ensure_pipeline_state_schema(db_path)
    led = PipelineLedger(db_path)
    led.seed_chunk(0)
    led.freeze_chunks_expected(1)
    led.mark_local_done(0)
    led.begin_local_eviction(0)
    led.commit_eviction(0, unlink=lambda: None)

    info = list_recordings(recordings_dir)[0]
    assert info.uploaded is False, "local recording was never uploaded"
    assert info.is_stub is False, "local eviction is not a stub (R11)"


def test_pre_u1_recording_without_ledger_table_classifies_via_files(recordings_dir):
    """Backward-compat (R14): a genuinely pre-U1 recording.db (no
    ``pipeline_chunk_state`` table at all) falls back to the file-presence
    heuristics unchanged — the read-only ledger probe returns None and must not
    migrate the schema or alter classification."""
    import sqlite3

    d = _make_recording(recordings_dir, "pre-u1-uploaded", duration=30)
    # Simulate a pre-U1 DB by dropping the ledger table the current engine adds.
    conn = sqlite3.connect(d / "recording.db")
    try:
        conn.execute("DROP TABLE IF EXISTS pipeline_chunk_state")
        conn.commit()
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='pipeline_chunk_state'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
    (d / ".upload_status.json").write_text("{}")  # legacy upload marker, no media

    info = list_recordings(recordings_dir)[0]
    assert info.uploaded is True
    assert info.is_stub is True, "legacy uploaded + no media still classifies as a stub"


def test_empty_ledger_table_falls_back_to_file_heuristics(recordings_dir):
    """A current-engine recording.db has an (empty) ``pipeline_chunk_state``
    table; with no UPLOADED rows the ledger probe contributes nothing and
    classification is identical to the pre-ledger file-presence path."""
    d = _make_recording(recordings_dir, "empty-ledger-uploaded", duration=30)
    (d / ".upload_status.json").write_text("{}")  # legacy upload marker, no media
    info = list_recordings(recordings_dir)[0]
    assert info.uploaded is True
    assert info.is_stub is True


# --- get_seen_bundle_ids tests ---


def test_get_seen_bundle_ids_single_dir(tmp_path):
    d = _make_recording_with_window_events(
        tmp_path, "rec1", ["com.example.foo", "com.example.bar"]
    )
    result = get_seen_bundle_ids([d])
    assert result == {"com.example.foo", "com.example.bar"}


def test_get_seen_bundle_ids_multiple_dirs(tmp_path):
    d1 = _make_recording_with_window_events(tmp_path, "rec1", ["com.example.foo"])
    d2 = _make_recording_with_window_events(tmp_path, "rec2", ["com.example.bar"])
    result = get_seen_bundle_ids([d1, d2])
    assert result == {"com.example.foo", "com.example.bar"}


def test_get_seen_bundle_ids_deduplicates(tmp_path):
    d1 = _make_recording_with_window_events(tmp_path, "rec1", ["com.example.foo"])
    d2 = _make_recording_with_window_events(tmp_path, "rec2", ["com.example.foo"])
    result = get_seen_bundle_ids([d1, d2])
    assert result == {"com.example.foo"}


def test_get_seen_bundle_ids_no_window_event_table(tmp_path):
    """Dirs without window_event table are silently skipped."""
    d = tmp_path / "rec1"
    d.mkdir()
    db_path = d / "recording.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    result = get_seen_bundle_ids([d])
    assert result == set()


def test_get_seen_bundle_ids_empty_dirs(tmp_path):
    result = get_seen_bundle_ids([])
    assert result == set()


# ---------------------------------------------------------------------------
# _read_recording_meta — timing_error discriminator (SCR-107)
#
# A swallowed DB-read exception must be distinguishable from a structurally
# event-free recording. ``timing_error`` (the third tuple element) is True ONLY
# on the caught-exception path (corrupt / unreadable DB); every readable-but-
# empty case stays False so a legitimately event-free recording is never
# flagged. Display-only callers discard the third element.
# ---------------------------------------------------------------------------


def test_read_recording_meta_healthy_recording(tmp_path):
    """A healthy recording reads timing with timing_error=False."""
    from screencap.catalog import _read_recording_meta

    d = _make_recording(tmp_path, "healthy", duration=60.0)
    started, duration, timing_error = _read_recording_meta(d / "recording.db")
    assert started is not None
    assert duration is not None and duration > 0
    assert timing_error is False


def test_read_recording_meta_corrupt_db_flags_error(tmp_path):
    """A corrupt / unreadable DB raises inside the read -> timing_error=True.

    This is the SCR-107 case: the swallowed exception previously collapsed
    into the same (None, None) a benign recording produces.
    """
    from screencap.catalog import _read_recording_meta

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database at all")
    started, duration, timing_error = _read_recording_meta(corrupt)
    assert started is None
    assert duration is None
    assert timing_error is True


def test_read_recording_meta_valid_db_no_recording_table(tmp_path):
    """A valid sqlite file missing the recording table is NOT an error.

    The DB opened cleanly; it simply carries no timing. timing_error stays
    False so it is not mistaken for corruption.
    """
    from screencap.catalog import _read_recording_meta

    db_path = tmp_path / "noschema.db"
    sqlite3.connect(str(db_path)).close()
    started, duration, timing_error = _read_recording_meta(db_path)
    assert (started, duration) == (None, None)
    assert timing_error is False


def test_read_recording_meta_zero_timestamp_not_error(tmp_path):
    """A recording row with a zero/NULL timestamp is empty, not corrupt."""
    from screencap.catalog import _read_recording_meta

    db_path = tmp_path / "zerots.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE recording (timestamp REAL)")
    conn.execute("INSERT INTO recording VALUES (0)")
    conn.commit()
    conn.close()
    started, duration, timing_error = _read_recording_meta(db_path)
    assert (started, duration) == (None, None)
    assert timing_error is False


def test_list_recordings_tolerates_corrupt_db(tmp_path):
    """The display caller (list_recordings) still lists a recording whose DB is
    corrupt — it discards timing_error and shows the '—' date fallback rather
    than crashing on the 3-tuple read."""
    rec_dir = tmp_path / "rec-corrupt"
    rec_dir.mkdir()
    (rec_dir / "recording.db").write_bytes(b"not a sqlite database")
    result = list_recordings(tmp_path)
    assert len(result) == 1
    assert result[0].name == "rec-corrupt"
    assert result[0].date == "—"
