"""Tests for screencap upload module and CLI command."""

from __future__ import annotations

import json
import sqlite3
from unittest import mock

import pytest
from click.testing import CliRunner

from screencap.cli import cli
from screencap.upload import (
    UPLOAD_STATUS_FILE,
    FileInfo,
    _content_type,
    _fmt_size,
    is_uploaded,
    list_recording_files,
)


@pytest.fixture(autouse=True)
def _signed_in_autouse(_signed_in):
    """Apply the shared ``_signed_in`` fixture (tests/conftest.py) to every test
    in this module. Not-signed-in / 401 tests override get_id_token themselves."""


@pytest.fixture(autouse=True)
def _isolate_scrub_lock(tmp_path, monkeypatch):
    """Point the per-recording scrub lock (recording_scrub_lock →
    scrubber.get_recordings_dir) at the test's tmp dir, so the upload command's
    lock files land in tmp instead of the real ~/.screencap/recordings."""
    monkeypatch.setattr("screencap.scrubber.get_recordings_dir", lambda: tmp_path)


# ---------------------------------------------------------------------------
# Unit tests: upload module helpers
# ---------------------------------------------------------------------------


def test_fmt_size():
    assert _fmt_size(500) == "500 B"
    assert _fmt_size(2048) == "2.0 KB"
    assert _fmt_size(5 * 1024 * 1024) == "5.0 MB"
    assert _fmt_size(2 * 1024 * 1024 * 1024) == "2.0 GB"


def test_content_type_known():
    from pathlib import Path

    assert _content_type(Path("video.mp4")) == "video/mp4"
    assert _content_type(Path("audio.flac")) == "audio/flac"
    assert _content_type(Path("recording.db")) == "application/x-sqlite3"
    assert _content_type(Path("viewer.html")) == "text/html"
    assert _content_type(Path("transcript.json")) == "application/json"
    assert _content_type(Path("transcript.txt")) == "text/plain"
    assert _content_type(Path("0.png")) == "image/png"
    assert _content_type(Path("photo.jpg")) == "image/jpeg"
    assert _content_type(Path("data.csv")) == "text/csv"


def test_content_type_unknown():
    from pathlib import Path

    ct = _content_type(Path("data.xyz123"))
    assert ct == "application/octet-stream"


def test_put_sends_content_type_and_no_checksum_header(tmp_path):
    """Client end of the SCR-140/R2 upload contract: the raw PUT to a signed URL sends
    only Content-Type (+ Content-Length) and NO checksum header.

    This is the companion to ``scripts/cloud-function/test_signing_contract.py``: that
    test proves the signed PUT URL pins no ``x-goog-hash``; this proves the client never
    sends one. Together they show both ends of the upload stay checksum-free, so the gcs
    3.x ``crc32c="auto"`` default — which only affects the library's ``upload_from_*``
    transfer methods, not a raw ``requests.put`` — cannot break the upload."""
    from rich.progress import Progress

    from screencap.upload import _content_type, _upload_with_progress, FileInfo

    payload = tmp_path / "video.mp4"
    payload.write_bytes(b"\x00\x01\x02fake-video-bytes")
    f = FileInfo("video.mp4", payload, _content_type(payload), payload.stat().st_size)

    captured = {}

    def _fake_put(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return mock.Mock(status_code=200, raise_for_status=mock.Mock())

    with Progress() as progress:
        task_id = progress.add_task("up", total=f.size)
        with mock.patch("screencap.upload.requests.put", side_effect=_fake_put):
            _upload_with_progress(
                f, "https://signed.example/video.mp4", progress, task_id,
                recording_name="rec1", max_retries=0,
            )

    headers = captured["headers"]
    assert headers.get("Content-Type") == "video/mp4"
    # No checksum header in any casing — a raw PUT must not commit to a hash the
    # signed URL never required.
    lowered = {k.lower() for k in headers}
    assert "x-goog-hash" not in lowered
    assert "content-md5" not in lowered
    assert not any("crc32c" in k.lower() or "checksum" in k.lower() for k in headers)


def test_list_recording_files(tmp_path):
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 1000)
    (rec / "audio.flac").write_bytes(b"x" * 500)
    (rec / "transcript.txt").write_text("hello")

    files = list_recording_files(rec)
    assert len(files) == 3
    # Sorted largest first
    assert files[0].name == "video.mp4"
    assert files[0].size == 1000
    assert files[1].name == "audio.flac"


def test_list_recording_files_with_subdir(tmp_path):
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "recording.db").write_bytes(b"x" * 100)
    screenshots = rec / "screenshots"
    screenshots.mkdir()
    (screenshots / "0.png").write_bytes(b"x" * 200)
    (screenshots / "1.png").write_bytes(b"x" * 150)

    files = list_recording_files(rec)
    names = [f.name for f in files]
    # Forward slashes (as_posix) regardless of platform
    assert "screenshots/0.png" in names
    assert "screenshots/1.png" in names
    # U2: recording.db is the raw, unscrubbed-PII local-only artifact (R8). The
    # unified pipeline uploads from the source dir, so this exclusion is the only
    # thing standing between raw PII and GCS — it must NEVER appear in the set.
    # (Inverted from the pre-U2 assertion that validated the old unsafe behavior.)
    assert "recording.db" not in names


# ---------------------------------------------------------------------------
# U2: recording.db local-only invariant at the single upload seam (R8 / AE11)
# ---------------------------------------------------------------------------


def test_list_recording_files_never_includes_recording_db(tmp_path):
    """Covers AE11: a source dir with recording.db + chunks → the uploaded set
    never contains recording.db (the raw, unscrubbed-PII local-only artifact)."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "recording.db").write_bytes(b"x" * 100)
    (rec / "chunk_0000.mp4").write_bytes(b"x" * 500)
    (rec / "events_0000.jsonl").write_text("scrubbed")

    names = [f.name for f in list_recording_files(rec)]
    assert "recording.db" not in names
    # legitimate artifacts still upload
    assert "chunk_0000.mp4" in names
    assert "events_0000.jsonl" in names


def test_list_recording_files_excludes_all_raw_db_artifacts_incl_nested(tmp_path):
    """recording.db, its WAL/SHM sidecars, and *.scrub_failed are all absent
    from the uploaded set — including when nested in a subdirectory (the
    function rglobs subdirs, so a nested recording.db must also be excluded,
    matched by full relative path, not just a top-level basename)."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "recording.db").write_bytes(b"x" * 100)
    (rec / "recording.db-wal").write_bytes(b"x" * 100)
    (rec / "recording.db-shm").write_bytes(b"x" * 100)
    (rec / "events_0000.jsonl.scrub_failed").write_text("RAW")
    # Same raw artifacts nested under a subdir — must also be excluded.
    sub = rec / "nested"
    sub.mkdir()
    (sub / "recording.db").write_bytes(b"x" * 100)
    (sub / "recording.db-wal").write_bytes(b"x" * 100)
    (sub / "recording.db-shm").write_bytes(b"x" * 100)
    (sub / "frame.jsonl.scrub_failed").write_text("RAW")
    # A legitimate artifact so the set is not trivially empty.
    (rec / "chunk_0000.mp4").write_bytes(b"x" * 500)

    names = [f.name for f in list_recording_files(rec)]
    assert "chunk_0000.mp4" in names
    assert "recording.db" not in names
    assert "nested/recording.db" not in names
    assert not any(n.endswith(".db-wal") for n in names)
    assert not any(n.endswith(".db-shm") for n in names)
    assert not any(n.endswith(".scrub_failed") for n in names)


def test_list_recording_files_only_db_no_chunks_is_empty(tmp_path):
    """A recording with only recording.db and no chunks → empty uploaded set,
    no error (the raw DB is the one excluded artifact)."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    conn = sqlite3.connect(rec / "recording.db")
    conn.execute("CREATE TABLE recording (id INTEGER)")
    conn.commit()
    conn.close()

    files = list_recording_files(rec)
    assert files == []


def test_enqueue_recording_db_is_rejected_not_silently_dropped(tmp_path):
    """An explicit attempt to enqueue recording.db (or a sidecar / scrub_failed
    artifact) for upload is REJECTED with a raise — never silently dropped, so a
    caller that bypasses list_recording_files still hits the hard safety gate."""
    from screencap.upload import FileInfo, assert_uploadable, _content_type

    db = tmp_path / "recording.db"
    db.write_bytes(b"x" * 10)
    fi = FileInfo("recording.db", db, _content_type(db), 10)
    with pytest.raises(ValueError, match="recording.db"):
        assert_uploadable(fi)

    # Nested by full relative path is also rejected.
    nested = FileInfo("nested/recording.db", db, _content_type(db), 10)
    with pytest.raises(ValueError, match="recording.db"):
        assert_uploadable(nested)

    # Sidecars + fail-closed scrub artifacts are rejected too.
    for name in ("recording.db-wal", "recording.db-shm", "events.jsonl.scrub_failed"):
        with pytest.raises(ValueError):
            assert_uploadable(FileInfo(name, db, _content_type(db), 10))

    # A legitimate artifact passes through untouched.
    ok = FileInfo("chunk_0000.mp4", db, "video/mp4", 10)
    assert assert_uploadable(ok) is ok


# ---------------------------------------------------------------------------
# SCR-126 Fix 1: the masked-video upload-seam gate (assert_video_masked).
# ---------------------------------------------------------------------------


def test_assert_video_masked_gate(tmp_path):
    """With masked_video_upload ON, a cloud chunk_*.mp4 must be the masker's copy
    under masked_video/ — a rich-source chunk raises (fail closed); with the flag
    OFF the gate is inert and the source chunk passes (today's behavior)."""
    from screencap.upload import FileInfo, _content_type, assert_video_masked

    src_dir = tmp_path / "rec"
    masked_dir = tmp_path / "rec-scrubbed" / "masked_video"
    src_dir.mkdir()
    masked_dir.mkdir(parents=True)
    src_chunk = src_dir / "chunk_0000.mp4"
    src_chunk.write_bytes(b"x" * 10)
    masked_chunk = masked_dir / "chunk_0000.mp4"
    masked_chunk.write_bytes(b"x" * 10)

    src_fi = FileInfo("chunk_0000.mp4", src_chunk, "video/mp4", 10)
    masked_fi = FileInfo("chunk_0000.mp4", masked_chunk, "video/mp4", 10)
    nested_masked_fi = FileInfo(
        "masked_video/chunk_0000.mp4", masked_chunk, "video/mp4", 10
    )

    # Flag OFF: the source chunk passes unchanged (capture-blocked source is the
    # cloud copy) — byte-for-byte today's behavior.
    assert assert_video_masked(src_fi, masked_upload_on=False) is src_fi

    # Flag ON + masked path (plain key and nested key): both pass.
    assert assert_video_masked(masked_fi, masked_upload_on=True) is masked_fi
    assert assert_video_masked(nested_masked_fi, masked_upload_on=True) is nested_masked_fi

    # Flag ON + rich source path: REJECTED (fail closed) — the core fix.
    with pytest.raises(ValueError, match="masked_video"):
        assert_video_masked(src_fi, masked_upload_on=True)

    # Non-video slots pass regardless of flag state.
    for name in ("audio_0000.flac", "events_0000.jsonl", "chunk_0000_manifest.json"):
        p = src_dir / name
        p.write_bytes(b"x")
        fi = FileInfo(name, p, _content_type(p), 1)
        assert assert_video_masked(fi, masked_upload_on=True) is fi


def test_upload_recording_rglob_rejects_unmasked_chunk_when_flag_on(tmp_path):
    """The scrubbed-dir rglob path (upload_recording → list_recording_files) gates
    chunk videos too: with masked_video_upload ON, a chunk_*.mp4 planted at the
    scrubbed-dir root (outside masked_video/) is rejected before any network call
    (SCR-126 Fix 1, H3 — the path that today bypasses the masked switch)."""
    from screencap.upload import upload_recording

    scrubbed = tmp_path / "rec-scrubbed"
    (scrubbed / "masked_video").mkdir(parents=True)
    # A legitimately-masked chunk under masked_video/ (would pass on its own)…
    (scrubbed / "masked_video" / "chunk_0001.mp4").write_bytes(b"x" * 5)
    # …and a stray rich-source chunk at the scrubbed-dir root (must be rejected).
    (scrubbed / "chunk_0000.mp4").write_bytes(b"x" * 5)

    with pytest.raises(ValueError, match="masked_video"):
        upload_recording(scrubbed, masked_video_upload=True)

    # Flag OFF: the same enumeration does not raise at the gate (it proceeds to
    # the network layer, which we don't exercise here — assert it gets past the
    # masked-video gate by reaching request_signed_urls).
    with mock.patch(
        "screencap.upload.request_signed_urls",
        side_effect=RuntimeError("reached network"),
    ):
        with pytest.raises(RuntimeError, match="reached network"):
            upload_recording(scrubbed, masked_video_upload=False)


def test_upload_chunk_files_rejects_source_chunk_when_masked_on(tmp_path):
    """The live/terminal chunk-media enqueue (upload_chunk_files) fails closed at
    the shared seam when the frozen flag is ON and the video slot is the rich
    source — the raise happens before any signed-URL request."""
    from screencap import chunk_processor

    capture_dir = tmp_path / "rec"
    capture_dir.mkdir()
    src_chunk = capture_dir / "chunk_0000.mp4"
    src_chunk.write_bytes(b"x" * 10)
    files = [{"name": "chunk_0000.mp4", "path": src_chunk}]

    with mock.patch(
        "screencap.pipeline_chunk_ops.get_frozen_masked_video_upload",
        return_value=True,
    ):
        with mock.patch(
            "screencap.upload.request_signed_urls",
            side_effect=AssertionError("network reached before gate"),
        ):
            with pytest.raises(ValueError, match="masked_video"):
                chunk_processor.upload_chunk_files("rec", files, capture_dir)


def test_list_recording_files_legacy_no_db_does_not_crash(tmp_path):
    """Legacy/migrated recordings have no recording.db; the WAL-checkpoint side
    effect must not crash the upload, and legitimate files still upload."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    # No recording.db at all.
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / "events.jsonl").write_text("scrubbed")

    files = list_recording_files(rec)
    names = [f.name for f in files]
    assert "video.mp4" in names
    assert "events.jsonl" in names


def test_list_recording_files_scrubbed_exports_still_upload(tmp_path):
    """Regression: the DB exclusion must not block the legitimate scrubbed
    structured exports (events JSONL, transcript, manifest) that ARE the cloud
    payload — structured cloud data derives only from these."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "recording.db").write_bytes(b"x" * 100)
    (rec / "events_0000.jsonl").write_text("scrubbed events")
    (rec / "transcript_0000.txt").write_text("scrubbed transcript")
    (rec / "transcript_0000.json").write_text("{}")
    (rec / "chunk_0000_manifest.json").write_text("{}")

    names = [f.name for f in list_recording_files(rec)]
    assert "recording.db" not in names
    assert "events_0000.jsonl" in names
    assert "transcript_0000.txt" in names
    assert "transcript_0000.json" in names
    assert "chunk_0000_manifest.json" in names


def test_list_recording_files_empty(tmp_path):
    rec = tmp_path / "empty"
    rec.mkdir()
    files = list_recording_files(rec)
    assert files == []


def test_list_recording_files_excludes_scrub_failed(tmp_path):
    """Fail-closed scrub artifacts retain raw, unredacted content; the scrubber
    renames them `*.scrub_failed` to mark them ineligible for upload. The upload
    sink must exclude them (top-level and in subdirs) so reviewed == uploaded and
    raw bytes never ship."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "events_0000.jsonl").write_text("scrubbed")
    (rec / "events_0001.jsonl.scrub_failed").write_text("RAW UNREDACTED")
    sub = rec / "screenshots"
    sub.mkdir()
    (sub / "frame.jsonl.scrub_failed").write_text("RAW UNREDACTED")

    names = [f.name for f in list_recording_files(rec)]
    assert "events_0000.jsonl" in names
    assert not any(n.endswith(".scrub_failed") for n in names)


# ---------------------------------------------------------------------------
# Unit tests: request_signed_urls
# ---------------------------------------------------------------------------


def test_request_signed_urls_success():
    from screencap.upload import request_signed_urls

    files = [
        FileInfo("video.mp4", mock.MagicMock(), "video/mp4", 1000),
        FileInfo("audio.flac", mock.MagicMock(), "audio/flac", 500),
    ]

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "urls": {"video.mp4": "https://signed-url/video", "audio.flac": None},
        "gcs_prefix": "gs://screencap-recordings/recordings/rec1/",
    }

    with mock.patch("screencap.upload.requests.post", return_value=mock_resp):
        urls, prefix = request_signed_urls("rec1", files)

    assert urls["video.mp4"] == "https://signed-url/video"
    assert urls["audio.flac"] is None
    assert "rec1" in prefix


def test_request_signed_urls_connection_error():
    from screencap.upload import request_signed_urls
    import pytest

    files = [FileInfo("video.mp4", mock.MagicMock(), "video/mp4", 1000)]

    import requests as req

    with mock.patch("screencap.upload.requests.post", side_effect=req.ConnectionError):
        with pytest.raises(RuntimeError, match="unavailable"):
            request_signed_urls("rec1", files)


def test_request_signed_urls_server_error():
    from screencap.upload import request_signed_urls
    import pytest

    files = [FileInfo("video.mp4", mock.MagicMock(), "video/mp4", 1000)]

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 500
    mock_resp.json.return_value = {"error": "internal error"}
    mock_resp.text = "internal error"

    with mock.patch("screencap.upload.requests.post", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="Upload service error"):
            request_signed_urls("rec1", files)


def test_request_signed_urls_timeout():
    from screencap.upload import request_signed_urls
    import pytest

    files = [FileInfo("video.mp4", mock.MagicMock(), "video/mp4", 1000)]

    import requests as req

    with mock.patch("screencap.upload.requests.post", side_effect=req.Timeout):
        with pytest.raises(RuntimeError, match="timed out"):
            request_signed_urls("rec1", files)


# ---------------------------------------------------------------------------
# Unit tests: request_signed_urls auth threading (U5)
# ---------------------------------------------------------------------------


def test_request_signed_urls_attaches_bearer_token():
    from screencap.upload import request_signed_urls

    files = [FileInfo("video.mp4", mock.MagicMock(), "video/mp4", 1000)]
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"urls": {}, "gcs_prefix": ""}

    with mock.patch("screencap.upload.requests.post", return_value=mock_resp) as post:
        request_signed_urls("rec1", files)

    _, kwargs = post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer test-id-token"


def test_request_signed_urls_refreshes_and_retries_once_on_401(monkeypatch):
    from screencap.upload import request_signed_urls

    files = [FileInfo("video.mp4", mock.MagicMock(), "video/mp4", 1000)]

    token_calls: list[bool] = []

    def fake_token(force_refresh=False):
        token_calls.append(force_refresh)
        return "fresh" if force_refresh else "stale"

    monkeypatch.setattr("screencap.auth.get_id_token", fake_token)

    r401 = mock.MagicMock(status_code=401)
    r200 = mock.MagicMock(status_code=200)
    r200.json.return_value = {"urls": {"video.mp4": "u"}, "gcs_prefix": "p"}

    with mock.patch("screencap.upload.requests.post", side_effect=[r401, r200]) as post:
        urls, _ = request_signed_urls("rec1", files)

    assert urls == {"video.mp4": "u"}
    assert token_calls == [False, True]  # second attempt forced a refresh
    assert post.call_count == 2
    assert post.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer fresh"


def test_request_signed_urls_401_persists_after_refresh_raises(monkeypatch):
    from screencap.upload import request_signed_urls

    files = [FileInfo("video.mp4", mock.MagicMock(), "video/mp4", 1000)]
    monkeypatch.setattr("screencap.auth.get_id_token", lambda force_refresh=False: "tok")

    r401 = mock.MagicMock(status_code=401)
    r401.json.return_value = {"error": "unauthorized"}
    r401.text = "unauthorized"

    with mock.patch("screencap.upload.requests.post", return_value=r401):
        with pytest.raises(RuntimeError, match="Upload service error"):
            request_signed_urls("rec1", files)


def test_request_signed_urls_not_signed_in_raises_sign_in_message(monkeypatch):
    from screencap import auth
    from screencap.upload import request_signed_urls

    files = [FileInfo("video.mp4", mock.MagicMock(), "video/mp4", 1000)]

    def not_signed_in(force_refresh=False):
        raise auth.NotSignedIn("no creds")

    monkeypatch.setattr("screencap.auth.get_id_token", not_signed_in)

    with mock.patch("screencap.upload.requests.post") as post:
        with pytest.raises(RuntimeError, match="screencap login"):
            request_signed_urls("rec1", files)


def test_request_signed_urls_transient_autherror_on_refresh_maps_to_retryable(monkeypatch):
    """A transient AuthError raised by the forced refresh on the 401 retry must
    surface as a clean, retryable RuntimeError — never a raw AuthError traceback."""
    from screencap import auth
    from screencap.upload import request_signed_urls

    files = [FileInfo("video.mp4", mock.MagicMock(), "video/mp4", 1000)]

    def token(force_refresh=False):
        if force_refresh:
            # The 401 retry forces a re-mint, which hits a transient outage.
            raise auth.AuthError("token service 503")
        return "stale"

    monkeypatch.setattr("screencap.auth.get_id_token", token)

    r401 = mock.MagicMock(status_code=401)
    with mock.patch("screencap.upload.requests.post", return_value=r401):
        with pytest.raises(RuntimeError, match="temporarily unavailable"):
            request_signed_urls("rec1", files)


# ---------------------------------------------------------------------------
# Unit tests: upload_recording
# ---------------------------------------------------------------------------


def test_upload_recording_dry_run(tmp_path):
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 1000)
    (rec / "audio.flac").write_bytes(b"x" * 500)

    result = upload_recording(rec, dry_run=True)
    assert result.recording == "my-rec"
    assert result.uploaded == []
    assert result.total_bytes == 1500  # reports total size even in dry run


def test_upload_recording_all_new(tmp_path):
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / "audio.flac").write_bytes(b"x" * 50)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": "https://url/video", "audio.flac": "https://url/audio"},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 200
    mock_put_resp.raise_for_status = mock.MagicMock()

    with (
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = upload_recording(rec)

    assert set(result.uploaded) == {"video.mp4", "audio.flac"}
    assert result.skipped == []
    assert result.failed == []


def test_upload_recording_partial_skip(tmp_path):
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / "audio.flac").write_bytes(b"x" * 50)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": None, "audio.flac": "https://url/audio"},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 200
    mock_put_resp.raise_for_status = mock.MagicMock()

    with (
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = upload_recording(rec)

    assert result.uploaded == ["audio.flac"]
    assert result.skipped == ["video.mp4"]


def test_upload_recording_all_skipped(tmp_path):
    """When server says all files exist, return early with all skipped."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": None},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    with mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp):
        result = upload_recording(rec)

    assert result.uploaded == []
    assert result.skipped == ["video.mp4"]


def test_upload_recording_empty_dir(tmp_path):
    from screencap.upload import upload_recording
    import pytest

    rec = tmp_path / "empty"
    rec.mkdir()

    with pytest.raises(FileNotFoundError, match="No files"):
        upload_recording(rec)


# ---------------------------------------------------------------------------
# Unit tests: resolve_recording_dirs
# ---------------------------------------------------------------------------


def test_resolve_by_name(tmp_path):
    from screencap.upload import resolve_recording_dirs

    rec = tmp_path / "my-rec"
    rec.mkdir()

    with mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path):
        dirs = resolve_recording_dirs(("my-rec",))
    assert dirs == [rec]


def test_resolve_not_found(tmp_path):
    from screencap.upload import resolve_recording_dirs
    import pytest

    with mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            resolve_recording_dirs(("nonexistent",))


def test_resolve_rejects_file_as_recording(tmp_path):
    """Passing a file path (not directory) should fail."""
    from screencap.upload import resolve_recording_dirs
    import pytest

    (tmp_path / "not-a-dir").write_text("I am a file")

    with mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            resolve_recording_dirs(("not-a-dir",))


def test_resolve_all(tmp_path):
    from screencap.upload import resolve_recording_dirs

    r1 = tmp_path / "rec1"
    r1.mkdir()
    conn = sqlite3.connect(str(r1 / "recording.db"))
    conn.execute("CREATE TABLE recording (id INTEGER)")
    conn.close()

    r2 = tmp_path / "rec2"
    r2.mkdir()
    conn = sqlite3.connect(str(r2 / "recording.db"))
    conn.execute("CREATE TABLE recording (id INTEGER)")
    conn.close()

    with mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path):
        dirs = resolve_recording_dirs((), all_recordings=True)
    assert len(dirs) == 2



def test_resolve_all_empty(tmp_path):
    from screencap.upload import resolve_recording_dirs
    import pytest

    with mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path):
        with pytest.raises(FileNotFoundError, match="No recordings"):
            resolve_recording_dirs((), all_recordings=True)


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


def test_upload_command_not_found(tmp_path):
    runner = CliRunner()
    with mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["upload", "nonexistent"])
    assert result.exit_code == 1
    assert "not found" in result.output


def _terminal_result_stub(**kw):
    from screencap.terminal_stage import TerminalResult

    defaults = dict(destination="cloud", routed=True, n_uploaded=1, sentinel_uploaded=True)
    defaults.update(kw)
    return TerminalResult(**defaults)


def test_upload_command_dry_run(tmp_path):
    """SCR-125 U5: --dry-run threads dry_run into the terminal stage (a read-only
    preview); the CLI reports the routing decision."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 1000)
    (rec / "audio.flac").write_bytes(b"x" * 500)

    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result_stub(routed=True))
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "my-rec", "--dry-run"])
    assert result.exit_code == 0
    assert fake.call_args.kwargs["dry_run"] is True
    assert "Would upload my-rec" in result.output


def _stub_scrub_recording(rec_dir):
    """Build a stub ScrubResult that points at *rec_dir* with no redactions.

    cli.py:upload calls scrub_recording before uploading (see
    src/screencap/cli.py:2742). These CLI tests don't exercise scrubber
    semantics — they pin upload-engine behavior — so we substitute the
    real scrubber with a MagicMock that returns a ScrubResult-shaped
    value pointing at the test fixture directory.
    """
    return mock.MagicMock(output_dir=rec_dir, entity_counts={})


def test_upload_command_success(tmp_path):
    """SCR-125 U5: a converged upload prints 'Uploaded' and no public viewer URL.
    The CLI drives the terminal stage; the HTTP upload engine is tested directly
    elsewhere (test_upload_recording_*)."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result_stub(n_uploaded=1, sentinel_uploaded=True))
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])
    assert result.exit_code == 0
    assert "Uploaded" in result.output
    # User recordings are private (users/{uid}/) — no public viewer URL is emitted.
    assert "screencap.sh" not in result.output


def test_upload_command_not_signed_in_refuses_and_touches_nothing(tmp_path, monkeypatch):
    from screencap import auth

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    def not_signed_in(force_refresh=False):
        raise auth.NotSignedIn("no creds")

    monkeypatch.setattr("screencap.auth.get_id_token", not_signed_in)

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.upload.requests.post") as post,
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])

    assert result.exit_code == 1
    assert "screencap login" in result.output
    post.assert_not_called()  # never reached the network
    # Pre-flight refused before the upload loop: no auto-export, no status sentinel.
    assert not (rec / "events.jsonl").exists()
    assert not (rec / UPLOAD_STATUS_FILE).exists()


def test_upload_command_keychain_locked_refuses_and_touches_nothing(tmp_path, monkeypatch):
    import keyring.errors

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    def keychain_locked(force_refresh=False):
        raise keyring.errors.KeyringError("Keychain locked")

    monkeypatch.setattr("screencap.auth.get_id_token", keychain_locked)

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.upload.requests.post") as post,
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])

    assert result.exit_code == 1
    assert "Keychain locked" in result.output  # distinct from "not signed in"
    post.assert_not_called()
    assert not (rec / "events.jsonl").exists()
    assert not (rec / UPLOAD_STATUS_FILE).exists()


def test_upload_command_dry_run_does_not_require_auth(tmp_path, monkeypatch):
    from screencap import auth

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 1000)

    def not_signed_in(force_refresh=False):
        raise auth.NotSignedIn("no creds")

    monkeypatch.setattr("screencap.auth.get_id_token", not_signed_in)

    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result_stub(routed=True))
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "my-rec", "--dry-run"])

    assert result.exit_code == 0  # dry-run is local-only; no sign-in needed
    assert "Would upload" in result.output


def test_upload_command_service_unavailable(tmp_path):
    """SCR-125 U5: the terminal stage catches an upload failure and surfaces it as
    a warning (it never crashes the CLI mid-batch); local media is preserved."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result_stub(
        routed=False, sentinel_uploaded=False,
        upload_warning="upload failed: service unavailable",
    ))
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])
    assert "unavailable" in result.output


def test_upload_command_multiple_recordings(tmp_path):
    """Batch upload with multiple recording names."""
    for name in ("rec1", "rec2"):
        rec = tmp_path / name
        rec.mkdir()
        (rec / "video.mp4").write_bytes(b"x" * 100)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": "https://url/video"},
        "gcs_prefix": "gs://bucket/recordings/rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 200
    mock_put_resp.raise_for_status = mock.MagicMock()

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = runner.invoke(cli, ["upload", "rec1", "rec2"])
    # Both minimal fixtures (no recording.db) fail cloud-copy production, so the
    # batch iterates both, prints the "Done." summary, and exits non-zero — the
    # SCR-79 contract (a failed upload must not exit 0).
    assert result.exit_code == 1
    assert "Done." in result.output
    assert "2 failed" in result.output


# ---------------------------------------------------------------------------
# Validation round 2 — bug fixes
# ---------------------------------------------------------------------------


def test_resolve_rejects_absolute_path(tmp_path):
    """Absolute path as recording name must not escape recordings dir."""
    from screencap.upload import resolve_recording_dirs
    import pytest

    with mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            resolve_recording_dirs(("/etc",))


def test_resolve_rejects_dot_dot_traversal(tmp_path):
    """Path traversal via '..' must be blocked."""
    from screencap.upload import resolve_recording_dirs
    import pytest

    # recordings_dir = tmp_path/recordings, escape target = tmp_path/secret
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()

    with mock.patch("screencap.upload.get_recordings_dir", return_value=recordings):
        with pytest.raises(FileNotFoundError, match="not found"):
            resolve_recording_dirs(("../secret",))


def test_list_recording_files_skips_symlinks(tmp_path):
    """Symlinks in recording directories must be skipped."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    # Create a symlink (could point to sensitive file)
    target = tmp_path / "secret.txt"
    target.write_text("secret data")
    (rec / "link.txt").symlink_to(target)

    files = list_recording_files(rec)
    names = [f.name for f in files]
    assert "video.mp4" in names
    assert "link.txt" not in names


def test_list_recording_files_skips_broken_symlinks(tmp_path):
    """Broken symlinks must not crash file listing."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    # Create a broken symlink
    (rec / "broken.txt").symlink_to(tmp_path / "nonexistent")

    files = list_recording_files(rec)
    names = [f.name for f in files]
    assert "video.mp4" in names
    assert "broken.txt" not in names


def test_upload_recording_server_rejects_filename(tmp_path):
    """Files rejected by server (not in urls dict) should be reported as rejected, not skipped."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / "audio.flac").write_bytes(b"x" * 50)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    # Server only returns url for video.mp4; audio.flac is absent (rejected by regex)
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": "https://url/video"},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 200
    mock_put_resp.raise_for_status = mock.MagicMock()

    with (
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = upload_recording(rec)

    assert result.uploaded == ["video.mp4"]
    assert result.skipped == []
    # audio.flac should be in failed (rejected by server), NOT skipped
    assert "audio.flac" in result.failed


def test_retry_url_none_means_success(tmp_path):
    """When retry gets None URL (file now exists), treat as success not failure."""
    from screencap.upload import _upload_with_progress, FileInfo

    f_path = tmp_path / "video.mp4"
    f_path.write_bytes(b"x" * 100)
    f = FileInfo("video.mp4", f_path, "video/mp4", 100)

    progress = mock.MagicMock()

    # First PUT returns 403
    resp_403 = mock.MagicMock(status_code=403)
    resp_403.raise_for_status = mock.MagicMock(
        side_effect=Exception("should not be called")
    )

    # Re-request returns None (file now exists on GCS)
    mock_urls_resp = mock.MagicMock(status_code=200)
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": None},
        "gcs_prefix": "",
    }

    with (
        mock.patch("screencap.upload.requests.put", return_value=resp_403),
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
    ):
        # Should NOT raise — None URL means file already uploaded
        _upload_with_progress(f, "https://old-url", progress, "t", "rec1", max_retries=1)


# ---------------------------------------------------------------------------
# Upload status tracking
# ---------------------------------------------------------------------------


def test_is_uploaded_false(tmp_path):
    """No status file → not uploaded."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    assert is_uploaded(rec) is False


def test_is_uploaded_true(tmp_path):
    """Status file present → uploaded."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / UPLOAD_STATUS_FILE).write_text('{"uploaded_at": "2026-01-01"}')
    assert is_uploaded(rec) is True


def test_upload_writes_status_file(tmp_path):
    """Successful upload should create .upload_status.json."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": "https://url/video"},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 200
    mock_put_resp.raise_for_status = mock.MagicMock()

    with (
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = upload_recording(rec)

    assert result.uploaded == ["video.mp4"]

    status_path = rec / UPLOAD_STATUS_FILE
    assert status_path.exists()
    status = json.loads(status_path.read_text())
    assert status["gcs_prefix"] == "gs://bucket/recordings/my-rec/"
    assert status["files_uploaded"] == 1
    assert "uploaded_at" in status


def test_upload_writes_status_when_all_skipped(tmp_path):
    """When server says all files exist, status file should still be written."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": None},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    with mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp):
        upload_recording(rec)

    assert (rec / UPLOAD_STATUS_FILE).exists()


def test_upload_does_not_write_status_on_failure(tmp_path):
    """Failed uploads should NOT write status file."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": "https://url/video"},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 500
    mock_put_resp.raise_for_status.side_effect = Exception("upload failed")

    with (
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = upload_recording(rec)

    assert result.failed == ["video.mp4"]
    assert not (rec / UPLOAD_STATUS_FILE).exists()


def test_upload_skips_if_already_uploaded(tmp_path):
    """When status file exists, upload_recording should skip (no network calls)."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / UPLOAD_STATUS_FILE).write_text('{"uploaded_at": "2026-01-01"}')

    # No mocks for requests — if it tries to call the network, it'll fail
    result = upload_recording(rec)
    assert result.recording == "my-rec"
    assert result.uploaded == []


def test_upload_force_bypasses_status_check(tmp_path):
    """--force should upload even when status file exists."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / UPLOAD_STATUS_FILE).write_text('{"uploaded_at": "2026-01-01"}')

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": "https://url/video"},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 200
    mock_put_resp.raise_for_status = mock.MagicMock()

    with (
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = upload_recording(rec, force=True)

    assert result.uploaded == ["video.mp4"]


def test_dry_run_does_not_write_status(tmp_path):
    """Dry run should NOT write status file."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    result = upload_recording(rec, dry_run=True)
    assert not (rec / UPLOAD_STATUS_FILE).exists()


def test_upload_cli_force_flag(tmp_path):
    """CLI --force threads force into the terminal stage (re-upload / rebuild)."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / UPLOAD_STATUS_FILE).write_text('{"uploaded_at": "2026-01-01"}')

    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result_stub(n_uploaded=1, sentinel_uploaded=True))
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "my-rec", "--force"])
    assert result.exit_code == 0
    assert fake.call_args.kwargs["force"] is True
    assert "Uploaded" in result.output


def test_upload_cli_skips_already_uploaded(tmp_path):
    """The terminal stage reconciles already-uploaded chunks (no re-upload) and
    reports them as skipped."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / UPLOAD_STATUS_FILE).write_text('{"uploaded_at": "2026-01-01"}')

    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result_stub(
        n_uploaded=0, n_skipped=1, sentinel_uploaded=True,
    ))
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])
    assert result.exit_code == 0
    assert "skipped" in result.output


# ---------------------------------------------------------------------------
# Parallel transfer tests
# ---------------------------------------------------------------------------


def test_upload_recording_jobs_1_sequential(tmp_path):
    """--jobs 1 should produce correct results (matches sequential behavior)."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / "audio.flac").write_bytes(b"x" * 50)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": "https://url/video", "audio.flac": "https://url/audio"},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 200
    mock_put_resp.raise_for_status = mock.MagicMock()

    with (
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = upload_recording(rec, jobs=1)

    assert set(result.uploaded) == {"video.mp4", "audio.flac"}
    assert result.failed == []


def test_upload_recording_jobs_more_than_files(tmp_path):
    """--jobs 4 with 2 files should not hang."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / "audio.flac").write_bytes(b"x" * 50)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": "https://url/video", "audio.flac": "https://url/audio"},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 200
    mock_put_resp.raise_for_status = mock.MagicMock()

    with (
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = upload_recording(rec, jobs=4)

    assert set(result.uploaded) == {"video.mp4", "audio.flac"}
    assert result.failed == []


def test_upload_recording_parallel_partial_failure(tmp_path):
    """--jobs 4 with partial failure: result lists correct."""
    from screencap.upload import upload_recording

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "a.txt").write_bytes(b"x" * 10)
    (rec / "b.txt").write_bytes(b"x" * 10)
    (rec / "c.txt").write_bytes(b"x" * 10)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {
            "a.txt": "https://url/a",
            "b.txt": "https://url/b",
            "c.txt": "https://url/c",
        },
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    call_count = 0

    def put_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            resp = mock.MagicMock()
            resp.status_code = 500
            resp.raise_for_status.side_effect = Exception("upload failed")
            return resp
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.raise_for_status = mock.MagicMock()
        return resp

    with (
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", side_effect=put_side_effect),
    ):
        result = upload_recording(rec, jobs=4, max_retries=0)

    assert len(result.uploaded) == 2
    assert len(result.failed) == 1
    assert not (rec / UPLOAD_STATUS_FILE).exists()


def test_upload_cli_jobs_flag_accepted(tmp_path):
    """CLI --jobs 2 should be accepted."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": "https://url/video"},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 200
    mock_put_resp.raise_for_status = mock.MagicMock()

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = runner.invoke(cli, ["upload", "my-rec", "--jobs", "2"])
    # --jobs is accepted (no Click usage error); the minimal fixture then fails
    # production, which SCR-79 surfaces as exit 1 (not the usage-error exit 2).
    assert "No such option" not in result.output
    assert result.exit_code == 1


@pytest.mark.parametrize("bad_value", ["0", "-1", "abc"])
def test_upload_cli_jobs_invalid_rejected(bad_value):
    """CLI --jobs rejects 0, negatives, and non-integers via Click validation."""
    runner = CliRunner()
    result = runner.invoke(cli, ["upload", "my-rec", "--jobs", bad_value])
    assert result.exit_code != 0


def test_upload_cli_short_flag(tmp_path):
    """CLI -j 1 short flag should work."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    mock_urls_resp = mock.MagicMock()
    mock_urls_resp.status_code = 200
    mock_urls_resp.json.return_value = {
        "urls": {"video.mp4": "https://url/video"},
        "gcs_prefix": "gs://bucket/recordings/my-rec/",
    }

    mock_put_resp = mock.MagicMock()
    mock_put_resp.status_code = 200
    mock_put_resp.raise_for_status = mock.MagicMock()

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = runner.invoke(cli, ["upload", "my-rec", "-j", "1"])
    # -j is accepted (no Click usage error); the minimal fixture then fails
    # production, which SCR-79 surfaces as exit 1 (not the usage-error exit 2).
    assert "No such option" not in result.output
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# U4: upload-side reuse guard — reviewed == uploaded (R3/R14)
# ---------------------------------------------------------------------------


def _make_uploadable_recording(tmp_path):
    """A non-chunked recording the upload loop won't mutate before the guard
    (events.jsonl present → no export; no chunks → no recovery).

    SCR-125: every real recording is seeded with the pipeline-ledger schema
    during recording, so ``run_terminal_stage``'s ``_open_ledger`` migration is a
    no-op and the scrubbed-copy source hash is stable across review → upload.
    Bake the schema in here so the fixture matches production (otherwise the
    upload-time migration would mutate recording.db and defeat reuse)."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / "events.jsonl").write_text(json.dumps({"_meta": True}) + "\n")
    conn = sqlite3.connect(rec / "recording.db")
    conn.execute("CREATE TABLE recording (timestamp REAL)")
    conn.execute("INSERT INTO recording VALUES (1716800000.0)")
    conn.commit()
    conn.close()
    from screencap.pipeline_state import ensure_pipeline_state_schema

    ensure_pipeline_state_schema(rec / "recording.db")
    return rec


def _upload_result_stub():
    return mock.MagicMock(
        uploaded=["video.mp4"], skipped=[], failed=[],
        total_bytes=100, gcs_prefix="gs://bucket/recordings/my-rec/",
    )


@pytest.mark.privacy
def test_scrub_recording_writes_reusable_sentinel(tmp_path):
    """scrub_recording writes the completion sentinel + provenance as its final
    step, and the result is reusable (Covers the sentinel-gating posture)."""
    from screencap.scrubber import (
        SCRUB_SENTINEL_NAME,
        is_scrubbed_copy_reusable,
        scrub_recording,
    )

    rec = tmp_path / "rec"
    rec.mkdir()
    conn = sqlite3.connect(rec / "recording.db")
    conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)")
    conn.execute("INSERT INTO recording VALUES (1, 'hello')")
    conn.commit()
    conn.close()

    with mock.patch(
        "screencap.config.get_recordings_dir", return_value=tmp_path,
    ), mock.patch(
        "screencap.scrubber.get_recordings_dir", return_value=tmp_path,
    ):
        scrub_recording("rec", cloud_bound_recovery=True)

        scrubbed = tmp_path / "rec-scrubbed"
        sentinel = scrubbed / SCRUB_SENTINEL_NAME
        assert sentinel.exists(), "the completion sentinel must be written"
        prov = json.loads(sentinel.read_text())
        assert prov["cloud_bound_recovery"] is True
        assert prov["scrubber_version"] >= 1
        assert prov["source_hash"]
        assert is_scrubbed_copy_reusable(rec, scrubbed) is True


def test_is_reusable_false_without_sentinel(tmp_path):
    from screencap.scrubber import is_scrubbed_copy_reusable

    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "recording.db").write_bytes(b"db")
    scrubbed = tmp_path / "rec-scrubbed"
    scrubbed.mkdir()  # partial: no .scrub_complete

    assert is_scrubbed_copy_reusable(rec, scrubbed) is False


def test_is_reusable_false_on_version_mismatch(tmp_path):
    from screencap.scrubber import (
        SCRUB_SENTINEL_NAME,
        _write_scrub_sentinel,
        is_scrubbed_copy_reusable,
    )

    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "recording.db").write_bytes(b"db")
    scrubbed = tmp_path / "rec-scrubbed"
    scrubbed.mkdir()
    _write_scrub_sentinel(scrubbed, rec, cloud_bound_recovery=True)

    sentinel = scrubbed / SCRUB_SENTINEL_NAME
    prov = json.loads(sentinel.read_text())
    prov["scrubber_version"] = prov["scrubber_version"] + 999
    sentinel.write_text(json.dumps(prov))

    assert is_scrubbed_copy_reusable(rec, scrubbed) is False


def test_is_reusable_false_on_source_mutation(tmp_path):
    from screencap.scrubber import _write_scrub_sentinel, is_scrubbed_copy_reusable

    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "recording.db").write_bytes(b"original")
    scrubbed = tmp_path / "rec-scrubbed"
    scrubbed.mkdir()
    _write_scrub_sentinel(scrubbed, rec, cloud_bound_recovery=True)
    assert is_scrubbed_copy_reusable(rec, scrubbed) is True

    # Mutate the source (e.g. a fresh events export / WAL checkpoint).
    (rec / "recording.db").write_bytes(b"mutated-and-longer")
    assert is_scrubbed_copy_reusable(rec, scrubbed) is False


def test_is_reusable_false_when_recovery_flag_absent(tmp_path):
    """A dir scrubbed WITHOUT cloud-bound recovery (e.g. `screencap scrub`) is
    not reusable by upload — it could leak in-interval pointer geometry."""
    from screencap.scrubber import _write_scrub_sentinel, is_scrubbed_copy_reusable

    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "recording.db").write_bytes(b"db")
    scrubbed = tmp_path / "rec-scrubbed"
    scrubbed.mkdir()
    _write_scrub_sentinel(scrubbed, rec, cloud_bound_recovery=False)

    assert is_scrubbed_copy_reusable(rec, scrubbed) is False


def test_is_reusable_false_on_wal_only_source_change(tmp_path):
    """A committed-but-uncheckpointed change lives in recording.db-wal while
    recording.db stays byte-identical; the reuse guard must still detect it,
    else it ships a stale scrubbed copy built from the pre-WAL state."""
    from screencap.scrubber import _write_scrub_sentinel, is_scrubbed_copy_reusable

    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "recording.db").write_bytes(b"db-committed")
    (rec / "recording.db-wal").write_bytes(b"wal-v1")
    scrubbed = tmp_path / "rec-scrubbed"
    scrubbed.mkdir()
    _write_scrub_sentinel(scrubbed, rec, cloud_bound_recovery=True)
    assert is_scrubbed_copy_reusable(rec, scrubbed) is True

    # WAL changes, recording.db unchanged → reuse must be rejected (rebuild).
    (rec / "recording.db-wal").write_bytes(b"wal-v2-longer")
    assert is_scrubbed_copy_reusable(rec, scrubbed) is False


def test_is_reusable_ignores_shm_churn(tmp_path):
    """The volatile -shm sidecar (regenerated, churns on read) must NOT affect
    reuse — hashing it would force spurious rebuilds with no real change."""
    from screencap.scrubber import _write_scrub_sentinel, is_scrubbed_copy_reusable

    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "recording.db").write_bytes(b"db")
    (rec / "recording.db-shm").write_bytes(b"shm-v1")
    scrubbed = tmp_path / "rec-scrubbed"
    scrubbed.mkdir()
    _write_scrub_sentinel(scrubbed, rec, cloud_bound_recovery=True)
    assert is_scrubbed_copy_reusable(rec, scrubbed) is True

    (rec / "recording.db-shm").write_bytes(b"shm-v2-changed-and-longer")
    assert is_scrubbed_copy_reusable(rec, scrubbed) is True


def test_upload_reuses_valid_sentineled_scrubbed_copy(tmp_path):
    """Covers AE1: after review-data prepares the scrubbed copy, upload ships
    that exact dir without re-scrubbing."""
    from screencap.scrubber import _write_scrub_sentinel

    rec = _make_uploadable_recording(tmp_path)
    scrubbed = tmp_path / "my-rec-scrubbed"
    scrubbed.mkdir()
    (scrubbed / "video.mp4").write_bytes(b"x" * 100)
    _write_scrub_sentinel(scrubbed, rec, cloud_bound_recovery=True)

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch("screencap.scrubber.scrub_recording") as scrub_spy,
        mock.patch("screencap.upload.upload_recording", return_value=_upload_result_stub()) as up_spy,
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])

    assert result.exit_code == 0, result.output
    scrub_spy.assert_not_called()  # reused — no second scrub
    assert up_spy.call_args[0][0] == scrubbed  # shipped the reviewed copy
    assert "Reusing" in result.output


def test_upload_rebuilds_partial_scrubbed_copy(tmp_path):
    """A partial scrubbed dir (no completion sentinel) is rebuilt, not shipped."""
    rec = _make_uploadable_recording(tmp_path)
    scrubbed = tmp_path / "my-rec-scrubbed"
    scrubbed.mkdir()  # partial — no sentinel

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch(
            "screencap.scrubber.scrub_recording",
            return_value=_stub_scrub_recording(scrubbed),
        ) as scrub_spy,
        mock.patch("screencap.upload.upload_recording", return_value=_upload_result_stub()),
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])

    assert result.exit_code == 0, result.output
    scrub_spy.assert_called_once()  # rebuilt
    assert "Reusing" not in result.output


def test_upload_rebuilds_stale_scrubbed_copy(tmp_path):
    """A stale scrubbed copy (source-hash mismatch) is rebuilt."""
    from screencap.scrubber import _write_scrub_sentinel

    rec = _make_uploadable_recording(tmp_path)
    scrubbed = tmp_path / "my-rec-scrubbed"
    scrubbed.mkdir()
    _write_scrub_sentinel(scrubbed, rec, cloud_bound_recovery=True)
    # Mutate the source AFTER sentinel write so the recorded hash is stale.
    (rec / "events.jsonl").write_text(json.dumps({"_meta": True, "changed": 1}) + "\n")

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch(
            "screencap.scrubber.scrub_recording",
            return_value=_stub_scrub_recording(scrubbed),
        ) as scrub_spy,
        mock.patch("screencap.upload.upload_recording", return_value=_upload_result_stub()),
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])

    assert result.exit_code == 0, result.output
    scrub_spy.assert_called_once()  # rebuilt on stale provenance


def test_upload_scrubs_when_no_scrubbed_copy(tmp_path):
    """Unchanged behavior: with no pre-existing scrubbed copy, upload scrubs."""
    rec = _make_uploadable_recording(tmp_path)
    scrubbed = tmp_path / "my-rec-scrubbed"

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch(
            "screencap.scrubber.scrub_recording",
            return_value=_stub_scrub_recording(scrubbed),
        ) as scrub_spy,
        mock.patch("screencap.upload.upload_recording", return_value=_upload_result_stub()),
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])

    assert result.exit_code == 0, result.output
    scrub_spy.assert_called_once()
    # cloud_bound_recovery=True is load-bearing: without it the written sentinel
    # records cloud_bound_recovery=False and is_scrubbed_copy_reusable refuses to
    # reuse it, defeating reviewed == uploaded.
    assert scrub_spy.call_args.kwargs.get("cloud_bound_recovery") is True
    # SCR-125 U5: the terminal stage's CloudCopyProducer holds the terminal
    # flock (a DIFFERENT lock file than recording_scrub_lock), so it does NOT
    # pass _already_locked — scrub_recording self-serializes its own rebuild.
    assert scrub_spy.call_args.kwargs.get("_already_locked") is not True


def test_recording_scrub_lock_acquires_and_releases(tmp_path, monkeypatch):
    """recording_scrub_lock is a working exclusive lock keyed per recording; the
    dot-prefixed lock file lives under the recordings root (never uploaded)."""
    from screencap.scrubber import recording_scrub_lock

    monkeypatch.setattr("screencap.scrubber.get_recordings_dir", lambda: tmp_path)
    with recording_scrub_lock("my-rec"):
        assert (tmp_path / ".my-rec.scrublock").exists()
    # Re-acquirable after release (no leaked fd / held lock).
    with recording_scrub_lock("my-rec"):
        pass


def test_upload_force_rebuilds_even_with_valid_sentinel(tmp_path):
    """--force always rebuilds, even when a valid sentinel'd dir exists."""
    from screencap.scrubber import _write_scrub_sentinel

    rec = _make_uploadable_recording(tmp_path)
    scrubbed = tmp_path / "my-rec-scrubbed"
    scrubbed.mkdir()
    _write_scrub_sentinel(scrubbed, rec, cloud_bound_recovery=True)

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch(
            "screencap.scrubber.scrub_recording",
            return_value=_stub_scrub_recording(scrubbed),
        ) as scrub_spy,
        mock.patch("screencap.upload.upload_recording", return_value=_upload_result_stub()),
    ):
        result = runner.invoke(cli, ["upload", "my-rec", "--force"])

    assert result.exit_code == 0, result.output
    scrub_spy.assert_called_once()  # force bypasses reuse
    assert "Reusing" not in result.output


def test_upload_scrub_failure_blocks_upload(tmp_path):
    """Fail-closed preserved: a scrub failure blocks the upload (never ships
    unscrubbed)."""
    _make_uploadable_recording(tmp_path)

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch(
            "screencap.scrubber.scrub_recording",
            side_effect=RuntimeError("scrub exploded"),
        ),
        mock.patch("screencap.upload.upload_recording") as up_spy,
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])

    up_spy.assert_not_called()  # never uploaded without a scrub (fail-closed)
    # The terminal stage surfaces the scrub failure as a warning; the recording
    # is not uploaded and local media is preserved.
    assert "scrub/mask failed" in result.output
