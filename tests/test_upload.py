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
    assert "recording.db" in names


def test_list_recording_files_empty(tmp_path):
    rec = tmp_path / "empty"
    rec.mkdir()
    files = list_recording_files(rec)
    assert files == []


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


def test_upload_command_dry_run(tmp_path):
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 1000)
    (rec / "audio.flac").write_bytes(b"x" * 500)

    runner = CliRunner()
    with mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["upload", "my-rec", "--dry-run"])
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "video.mp4" in result.output


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
        mock.patch(
            "screencap.scrubber.scrub_recording",
            return_value=_stub_scrub_recording(rec),
        ),
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
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


def test_upload_command_dry_run_does_not_require_auth(tmp_path, monkeypatch):
    from screencap import auth

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 1000)

    def not_signed_in(force_refresh=False):
        raise auth.NotSignedIn("no creds")

    monkeypatch.setattr("screencap.auth.get_id_token", not_signed_in)

    runner = CliRunner()
    with mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["upload", "my-rec", "--dry-run"])

    assert result.exit_code == 0  # dry-run is local-only; no sign-in needed
    assert "Dry run" in result.output


def test_upload_command_service_unavailable(tmp_path):
    import requests as req

    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch(
            "screencap.scrubber.scrub_recording",
            return_value=_stub_scrub_recording(rec),
        ),
        mock.patch("screencap.upload.requests.post", side_effect=req.ConnectionError),
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])
    assert result.exit_code == 1
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
    assert result.exit_code == 0
    assert "Done." in result.output


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
    """CLI --force flag should bypass local upload check."""
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

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch(
            "screencap.scrubber.scrub_recording",
            return_value=_stub_scrub_recording(rec),
        ),
        mock.patch("screencap.upload.requests.post", return_value=mock_urls_resp),
        mock.patch("screencap.upload.requests.put", return_value=mock_put_resp),
    ):
        result = runner.invoke(cli, ["upload", "my-rec", "--force"])
    assert result.exit_code == 0
    assert "Uploaded" in result.output


def test_upload_cli_skips_already_uploaded(tmp_path):
    """CLI upload should print skip message when already uploaded."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / UPLOAD_STATUS_FILE).write_text('{"uploaded_at": "2026-01-01"}')

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.get_recordings_dir", return_value=tmp_path),
        mock.patch(
            "screencap.scrubber.scrub_recording",
            return_value=_stub_scrub_recording(rec),
        ),
    ):
        result = runner.invoke(cli, ["upload", "my-rec"])
    assert result.exit_code == 0
    assert "Already uploaded" in result.output


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
    assert result.exit_code == 0


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
    assert result.exit_code == 0
