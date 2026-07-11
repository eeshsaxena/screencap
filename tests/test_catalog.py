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


def _make_audio_only_recording(base: Path, name: str, *, audio_seconds: float, samplerate: int = 16000):
    """A recording whose DB has a start row but NO action_event — so the
    DB-derived duration is ``None`` — plus a real FLAC of known length. Models
    the audio-only capture that used to render "—" for its duration.
    """
    import numpy as np
    import soundfile

    from screencap.engine.db import create_db, crud

    d = base / name
    d.mkdir(parents=True)

    db_path = d / "recording.db"
    started = time.time() - audio_seconds
    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(session, {
        "timestamp": started,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    # Deliberately NO action_event: the DB has no activity to derive duration from.
    session.close()
    engine.dispose()

    frames = int(audio_seconds * samplerate)
    soundfile.write(
        str(d / "audio_0000.flac"),
        np.zeros(frames, dtype="float32"),
        samplerate,
        format="FLAC",
    )
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


def test_audio_only_recording_uses_audio_duration(recordings_dir):
    """A recording with audio but no action events derives its duration from the
    FLAC length instead of showing "—" (QA: blank duration on audio-only recs).
    """
    _make_audio_only_recording(recordings_dir, "audio-only", audio_seconds=3.0)
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    rec = result[0]
    assert rec.has_audio is True
    assert rec.duration_seconds == pytest.approx(3.0, abs=0.2)
    assert rec.duration == "0m 3s"


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
    # SCR-148: account-mismatch observability fields are serialized too, so the
    # daemon recording.list field-set parity assertion and the JSON output both
    # pick them up.
    assert "owner_uid" in d
    assert "upload_warning" in d


# --- SCR-148: cloud account-mismatch observability fields ---


def test_list_recordings_owner_uid_from_pin(recordings_dir):
    """owner_uid is read from the pinned .cloud_owner_uid dotfile (SCR-116)."""
    from screencap.catalog import write_owner_uid

    d = _make_recording(recordings_dir, "owned-rec", duration=10.0)
    write_owner_uid(d, "firebase-uid-123")

    info = list_recordings(recordings_dir)[0]
    assert info.owner_uid == "firebase-uid-123"


def test_list_recordings_owner_uid_none_when_unpinned(recordings_dir):
    """owner_uid is None for a legacy/local recording with no pin."""
    _make_recording(recordings_dir, "unpinned-rec", duration=10.0)
    info = list_recordings(recordings_dir)[0]
    assert info.owner_uid is None


def test_list_recordings_upload_warning_from_followup(recordings_dir):
    """upload_warning surfaces the .upload_followup.json warning text."""
    d = _make_recording(recordings_dir, "warned-rec", duration=10.0)
    (d / ".upload_followup.json").write_text(json.dumps({
        "kind": "upload_disabled",
        "n_uploaded": 0,
        "n_total": 3,
        "upload_warning": "uploads disabled: network unreachable",
    }))
    info = list_recordings(recordings_dir)[0]
    assert info.upload_warning == "uploads disabled: network unreachable"


def test_list_recordings_upload_warning_none_when_absent(recordings_dir):
    """upload_warning is None when no follow-up file is present (the common case)."""
    _make_recording(recordings_dir, "clean-rec", duration=10.0)
    info = list_recordings(recordings_dir)[0]
    assert info.upload_warning is None


def test_list_recordings_upload_warning_none_when_followup_corrupt(recordings_dir):
    """A corrupt/empty follow-up file degrades to None, never raises."""
    d = _make_recording(recordings_dir, "corrupt-rec", duration=10.0)
    (d / ".upload_followup.json").write_text("{not valid json")
    info = list_recordings(recordings_dir)[0]
    assert info.upload_warning is None


def test_list_recordings_upload_warning_none_when_field_null(recordings_dir):
    """A follow-up with no warning text (e.g. partial upload) yields None."""
    d = _make_recording(recordings_dir, "partial-rec", duration=10.0)
    (d / ".upload_followup.json").write_text(json.dumps({
        "kind": "partial", "n_uploaded": 1, "n_total": 3, "upload_warning": None,
    }))
    info = list_recordings(recordings_dir)[0]
    assert info.upload_warning is None


def test_list_recordings_owner_uid_none_when_pin_not_utf8(recordings_dir):
    """owner_uid is None (not a UnicodeDecodeError 500) when the pin file is non-UTF-8.

    Path.read_text() raises UnicodeDecodeError (a ValueError) on non-UTF-8 bytes.
    The guard must catch (OSError, ValueError) — a bare OSError would let the
    decode error escape and crash the whole recording.list call (regression for
    the fix in catalog.read_owner_uid).
    """
    d = _make_recording(recordings_dir, "bad-pin-rec", duration=10.0)
    (d / ".cloud_owner_uid").write_bytes(b"\xff\xfe")  # invalid UTF-8
    info = list_recordings(recordings_dir)[0]
    assert info.owner_uid is None


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
    # The merged _ledger_probe serves both `uploaded` and `state`; pin state so a
    # future edit that touches only one of its SQL branches is caught.
    assert info.state == "ready"


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
    assert info.state == "ready", "a FAILED chunk maps to ready, not eternal processing"


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
    assert info.state == "ready", "all frozen chunks terminal (LOCAL_DONE->EVICTED) -> ready"


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
# _read_recording_meta — timing_status discriminator (SCR-107 / SCR-166)
#
# A swallowed DB-read exception must be distinguishable from a structurally
# event-free recording (SCR-107), AND a transient lock must be distinguishable
# from corruption (SCR-166). ``timing_status`` (the third tuple element) is
# "ok" for every clean read (including readable-but-empty), "locked" for a
# transient lock/busy, and "corrupt" for any other read failure. Display-only
# callers discard the third element.
# ---------------------------------------------------------------------------


def test_read_recording_meta_healthy_recording(tmp_path):
    """A healthy recording reads timing with timing_status="ok"."""
    from screencap.catalog import _read_recording_meta

    d = _make_recording(tmp_path, "healthy", duration=60.0)
    started, duration, timing_status = _read_recording_meta(d / "recording.db")
    assert started is not None
    assert duration is not None and duration > 0
    assert timing_status == "ok"


def test_read_recording_meta_corrupt_db_reports_corrupt(tmp_path):
    """A corrupt / unreadable DB raises inside the read -> timing_status="corrupt".

    This is the SCR-107 case: the swallowed exception previously collapsed
    into the same (None, None) a benign recording produces.
    """
    from screencap.catalog import _read_recording_meta

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database at all")
    started, duration, timing_status = _read_recording_meta(corrupt)
    assert started is None
    assert duration is None
    assert timing_status == "corrupt"


def test_read_recording_meta_valid_db_no_recording_table(tmp_path):
    """A valid sqlite file missing the recording table is NOT an error.

    The DB opened cleanly; it simply carries no timing. timing_status stays
    "ok" so it is not mistaken for corruption.
    """
    from screencap.catalog import _read_recording_meta

    db_path = tmp_path / "noschema.db"
    sqlite3.connect(str(db_path)).close()
    started, duration, timing_status = _read_recording_meta(db_path)
    assert (started, duration) == (None, None)
    assert timing_status == "ok"


def test_read_recording_meta_zero_timestamp_not_error(tmp_path):
    """A recording row with a zero/NULL timestamp is empty, not corrupt."""
    from screencap.catalog import _read_recording_meta

    db_path = tmp_path / "zerots.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE recording (timestamp REAL)")
    conn.execute("INSERT INTO recording VALUES (0)")
    conn.commit()
    conn.close()
    started, duration, timing_status = _read_recording_meta(db_path)
    assert (started, duration) == (None, None)
    assert timing_status == "ok"


def test_read_recording_meta_valid_timestamp_no_action_event_table(tmp_path):
    """A DB with a valid non-zero timestamp but no action_event table is NOT
    an error: started is populated, duration is unknown, timing_status is "ok"."""
    from screencap.catalog import _read_recording_meta

    db_path = tmp_path / "notimeline.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE recording (timestamp REAL)")
    conn.execute("INSERT INTO recording VALUES (1716800000.0)")
    conn.commit()
    conn.close()
    started, duration, timing_status = _read_recording_meta(db_path)
    assert started is not None
    assert duration is None
    assert timing_status == "ok"


# ---------------------------------------------------------------------------
# _read_recording_meta — locked vs corrupt discriminator (SCR-166)
#
# A *transient* lock (an active recording / concurrent writer holding the DB)
# must report "locked", distinct from the *permanent* "corrupt" a truly
# unreadable DB reports, so the review window can show "temporarily
# unavailable" instead of a corruption-flavored advisory. "ok" covers every
# clean read (including a benign event-free recording).
# ---------------------------------------------------------------------------


def test_is_lock_error_classifies_lock_and_busy_messages():
    """The message-fallback branch (Python 3.10, no `sqlite_errorcode`) maps a
    locked/busy OperationalError to a lock; manually-built exceptions carry no
    errorcode, so this exercises the fallback."""
    from screencap.catalog import _is_lock_error

    assert _is_lock_error(sqlite3.OperationalError("database is locked")) is True
    assert _is_lock_error(sqlite3.OperationalError("database table is locked")) is True
    assert _is_lock_error(sqlite3.OperationalError("database is busy")) is True


def test_is_lock_error_rejects_non_lock_operational_errors():
    """A non-lock OperationalError (disk I/O, missing table) is NOT a transient
    lock and must fall through to the corrupt/error path."""
    from screencap.catalog import _is_lock_error

    assert _is_lock_error(sqlite3.OperationalError("disk I/O error")) is False
    assert _is_lock_error(sqlite3.OperationalError("no such table: recording")) is False


def test_read_recording_meta_locked_db_reports_locked(tmp_path):
    """SCR-166: a recording.db locked by a concurrent writer reports "locked",
    NOT "corrupt".

    recording.db is WAL-mode (engine/db sets journal_mode=WAL), and a plain
    ``BEGIN EXCLUSIVE`` does NOT block a WAL reader — so the lock is forced with
    ``PRAGMA locking_mode=EXCLUSIVE`` + a held write, which takes the
    file-level EXCLUSIVE lock a reader's ``busy_timeout`` then expires against,
    raising ``OperationalError("database is locked")``. ``_read_recording_meta``
    opens with a 500ms ``busy_timeout`` (the SCR-166 fast-fail), so the read
    fails fast rather than waiting the 5s production default.
    """
    from screencap import catalog

    d = _make_recording(tmp_path, "locked", duration=60.0)
    db_path = d / "recording.db"

    holder = sqlite3.connect(str(db_path))
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE recording SET timestamp = timestamp")  # hold the write lock
    try:
        started, duration, timing_status = catalog._read_recording_meta(db_path)
    finally:
        holder.rollback()
        holder.close()

    assert started is None
    assert duration is None
    assert timing_status == "locked"


def test_list_recordings_tolerates_corrupt_db(tmp_path):
    """The display caller (list_recordings) still lists a recording whose DB is
    corrupt — it discards timing_status and shows the '—' date fallback rather
    than crashing on the 3-tuple read."""
    rec_dir = tmp_path / "rec-corrupt"
    rec_dir.mkdir()
    (rec_dir / "recording.db").write_bytes(b"not a sqlite database")
    result = list_recordings(tmp_path)
    assert len(result) == 1
    assert result[0].name == "rec-corrupt"
    assert result[0].date == "—"


# ---------------------------------------------------------------------------
# U2 (prototype UI): additive fields — size_bytes, summary, title,
# recording_id, and the derived lifecycle state (KTD-7).
# ---------------------------------------------------------------------------


def _set_task_description(d: Path, text: str) -> None:
    conn = sqlite3.connect(str(d / "recording.db"))
    try:
        conn.execute("UPDATE recording SET task_description = ?", (text,))
        conn.commit()
    finally:
        conn.close()


def test_size_bytes_is_numeric_and_matches_dir(recordings_dir):
    d = _make_recording(recordings_dir, "sized", duration=10)
    (d / "blob.bin").write_bytes(b"\x00" * 4096)
    info = list_recordings(recordings_dir)[0]
    expected = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    assert isinstance(info.size_bytes, int)
    assert info.size_bytes == expected
    assert info.size_bytes > 0
    # The formatted string is derived from the same byte total.
    assert info.size_mb.endswith("KB") or info.size_mb.endswith("MB")


def test_summary_from_task_description(recordings_dir):
    d = _make_recording(recordings_dir, "summ", duration=10)
    _set_task_description(d, "User debugged Stripe webhooks in Django.")
    info = list_recordings(recordings_dir)[0]
    assert info.summary == "User debugged Stripe webhooks in Django."


def test_summary_none_when_task_description_absent(recordings_dir):
    _make_recording(recordings_dir, "nosumm", duration=10)
    info = list_recordings(recordings_dir)[0]
    assert info.summary is None


def test_summary_none_when_db_locked(recordings_dir):
    """A locked DB yields a null summary, never an exception (nullable-timing)."""
    d = _make_recording(recordings_dir, "lockedsumm", duration=10)
    _set_task_description(d, "should not be read while the DB is locked")
    db_path = d / "recording.db"
    holder = sqlite3.connect(str(db_path))
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE recording SET timestamp = timestamp")  # hold write lock
    try:
        info = list_recordings(recordings_dir)[0]
    finally:
        holder.rollback()
        holder.close()
    assert info.summary is None


def test_title_humanizes_slug(recordings_dir):
    _make_recording(recordings_dir, "stripe-webhook-debugging", duration=10)
    info = list_recordings(recordings_dir)[0]
    assert info.title == "Stripe Webhook Debugging"


def test_recording_id_stable_across_directory_rename(recordings_dir):
    """`.recording_id` is pinned at start and moves with the dir, so it is stable
    across the post-stop auto-name rename even as `name` changes."""
    d = _make_recording(recordings_dir, "temp-capture-name", duration=10)
    (d / ".recording_id").write_text("temp-capture-name")
    info = list_recordings(recordings_dir)[0]
    assert info.recording_id == "temp-capture-name"

    d.rename(recordings_dir / "final-slug")  # legacy post-stop directory rename
    info2 = list_recordings(recordings_dir)[0]
    assert info2.name == "final-slug"
    assert info2.recording_id == "temp-capture-name"


def test_recording_id_none_when_sidecar_absent(recordings_dir):
    _make_recording(recordings_dir, "legacy-noid", duration=10)
    info = list_recordings(recordings_dir)[0]
    assert info.recording_id is None


def test_state_active_recording_reports_recording(recordings_dir, monkeypatch):
    from screencap import catalog

    _make_recording(recordings_dir, "live-rec", duration=10)
    monkeypatch.setattr(catalog, "_active_recording_name", lambda: "live-rec")
    info = list_recordings(recordings_dir)[0]
    assert info.state == "recording"


def test_state_legacy_no_ledger_is_ready(recordings_dir):
    _make_recording(recordings_dir, "legacy-ready", duration=10)
    info = list_recordings(recordings_dir)[0]
    assert info.state == "ready"


def test_state_local_processing_until_all_chunks_local_done(recordings_dir):
    """A stopped local recording is `processing` until every frozen
    `chunks_expected` chunk is `LOCAL_DONE`, then flips to `ready` — no sentinel
    involved (KTD-7)."""
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    d = _make_recording(recordings_dir, "local-proc", duration=10)
    db_path = d / "recording.db"
    ensure_pipeline_state_schema(db_path)
    led = PipelineLedger(db_path)
    for i in range(2):
        led.seed_chunk(i)
    led.freeze_chunks_expected(2)
    led.mark_local_done(0)  # one of two done
    assert list_recordings(recordings_dir)[0].state == "processing"
    led.mark_local_done(1)  # now all done
    assert list_recordings(recordings_dir)[0].state == "ready"


def test_state_cloud_ready_on_completeness_sentinel(recordings_dir):
    """A cloud-routed recording is `ready` only once its completeness sentinel
    (`recording_complete.json`) exists — the local ledger gate is not consulted."""
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    d = _make_recording(recordings_dir, "cloud-rec", duration=10)
    (d / ".recording_intent").write_text(
        json.dumps({"version": 1, "destination": "cloud", "privacy_mode": "public"})
    )
    db_path = d / "recording.db"
    ensure_pipeline_state_schema(db_path)
    led = PipelineLedger(db_path)
    led.seed_chunk(0)
    led.freeze_chunks_expected(1)
    assert list_recordings(recordings_dir)[0].state == "processing"
    (d / "recording_complete.json").write_text("{}")
    assert list_recordings(recordings_dir)[0].state == "ready"


def test_state_failed_chunk_maps_to_ready_not_eternal_processing(recordings_dir):
    """A recording with a FAILED chunk maps to `ready` rather than sitting forever
    in `processing` — the FAILED short-circuit runs before the cloud sentinel wait."""
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    d = _make_recording(recordings_dir, "failed-state", duration=10)
    (d / ".recording_intent").write_text(
        json.dumps({"version": 1, "destination": "cloud", "privacy_mode": "public"})
    )
    db_path = d / "recording.db"
    ensure_pipeline_state_schema(db_path)
    led = PipelineLedger(db_path)
    led.seed_chunk(0)
    led.freeze_chunks_expected(1)
    led.mark_failed(0, detail="video_mask FAILED")
    assert list_recordings(recordings_dir)[0].state == "ready"


def test_state_cloud_stub_without_sentinel_is_ready(recordings_dir):
    """An uploaded-then-evicted cloud recording is `ready` even without a
    completeness sentinel — the stub short-circuit (its media is gone, so nothing
    is converging it)."""
    d = _make_recording(recordings_dir, "cloud-stub", duration=10)
    (d / ".recording_intent").write_text(
        json.dumps({"version": 1, "destination": "cloud", "privacy_mode": "public"})
    )
    (d / "chunk_0000_manifest.json").write_text("{}")  # manifest survives eviction
    _seed_ledger(d, 1, uploaded=True, evicted=True)
    assert not (d / "recording_complete.json").exists()
    info = list_recordings(recordings_dir)[0]
    assert info.is_stub is True
    assert info.state == "ready"


def test_state_cloud_without_frozen_completion_gate_is_ready(recordings_dir):
    """A cloud recording with no completeness sentinel AND no frozen
    `chunks_expected` (an empty/unfrozen ledger, or none at all) has no path to a
    sentinel, so it reports `ready`, not eternal `processing`."""
    d = _make_recording(recordings_dir, "cloud-noledger", duration=10)
    (d / ".recording_intent").write_text(
        json.dumps({"version": 1, "destination": "cloud", "privacy_mode": "public"})
    )
    # `_make_recording` leaves an empty (unfrozen) pipeline_chunk_state table.
    assert list_recordings(recordings_dir)[0].state == "ready"


def test_state_ledger_seeded_but_unfrozen_is_ready(recordings_dir):
    """A local recording with a seeded ledger whose `chunks_expected` was never
    frozen has no gate to wait on, so it reports `ready` rather than `processing`."""
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    d = _make_recording(recordings_dir, "unfrozen", duration=10)
    db_path = d / "recording.db"
    ensure_pipeline_state_schema(db_path)
    led = PipelineLedger(db_path)
    led.seed_chunk(0)  # seeded but deliberately NOT frozen
    assert list_recordings(recordings_dir)[0].state == "ready"


def test_asdict_includes_all_u2_fields(recordings_dir):
    """The new fields serialize via `_asdict()` so `list --json` and the daemon
    field-parity assertion both pick them up."""
    _make_recording(recordings_dir, "u2-fields", duration=10)
    d = list_recordings(recordings_dir)[0]._asdict()
    for key in ("size_bytes", "summary", "title", "state", "recording_id"):
        assert key in d
