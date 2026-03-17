"""Tests for screencap download module and CLI command."""

from __future__ import annotations

import json
from unittest import mock

import pytest
from click.testing import CliRunner

from screencap.cli import cli
from screencap.download import (
    DOWNLOAD_STATUS_FILE,
    DownloadResult,
    RemoteSession,
    _fmt_size,
    _resolve_dest_dir,
    _write_download_status,
    fetch_session_index,
    filter_urls_by_category,
    is_downloaded,
    list_remote_sessions,
)


# ---------------------------------------------------------------------------
# Unit tests: marker helpers
# ---------------------------------------------------------------------------


def test_is_downloaded_false(tmp_path):
    """No status file → not downloaded."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    assert is_downloaded(rec) is False


def test_is_downloaded_true(tmp_path):
    """Status file present → downloaded."""
    rec = tmp_path / "my-rec"
    rec.mkdir()
    (rec / DOWNLOAD_STATUS_FILE).write_text('{"downloaded_at": "2026-01-01"}')
    assert is_downloaded(rec) is True


def test_write_download_status(tmp_path):
    """_write_download_status creates a valid JSON marker."""
    rec = tmp_path / "my-rec"
    rec.mkdir()

    result = DownloadResult(
        recording="my-rec",
        downloaded=["video.mp4", "audio.flac"],
        total_bytes=12345,
        gcs_prefix="gs://screencap-recordings/recordings/my-rec/",
    )
    _write_download_status(rec, result)

    status_path = rec / DOWNLOAD_STATUS_FILE
    assert status_path.exists()
    status = json.loads(status_path.read_text())
    assert status["files_downloaded"] == 2
    assert status["total_bytes"] == 12345
    assert "downloaded_at" in status
    assert status["gcs_prefix"] == "gs://screencap-recordings/recordings/my-rec/"


# ---------------------------------------------------------------------------
# Unit tests: _fmt_size
# ---------------------------------------------------------------------------


def test_fmt_size():
    assert _fmt_size(500) == "500 B"
    assert _fmt_size(2048) == "2.0 KB"
    assert _fmt_size(5 * 1024 * 1024) == "5.0 MB"
    assert _fmt_size(2 * 1024 * 1024 * 1024) == "2.0 GB"


# ---------------------------------------------------------------------------
# Unit tests: _resolve_dest_dir
# ---------------------------------------------------------------------------


def test_resolve_dest_dir_default():
    with mock.patch("screencap.download.get_downloads_dir") as m:
        m.return_value = mock.MagicMock()
        _resolve_dest_dir(None)
        m.assert_called_once()


def test_resolve_dest_dir_custom(tmp_path):
    dest = tmp_path / "custom-downloads"
    result = _resolve_dest_dir(str(dest))
    assert result == dest
    assert dest.is_dir()


# ---------------------------------------------------------------------------
# Unit tests: list_remote_recordings
# ---------------------------------------------------------------------------


def test_list_remote_recordings_success():
    from screencap.download import list_remote_recordings

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "recordings": [
            {"name": "rec-001", "file_count": 3, "total_size": 12345},
            {"name": "rec-002", "file_count": 5, "total_size": 67890},
        ]
    }

    with mock.patch("screencap.download.requests.post", return_value=mock_resp):
        result = list_remote_recordings()

    assert len(result) == 2
    assert result[0].name == "rec-001"
    assert result[0].file_count == 3
    assert result[0].total_size == 12345


def test_list_remote_recordings_empty():
    from screencap.download import list_remote_recordings

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"recordings": []}

    with mock.patch("screencap.download.requests.post", return_value=mock_resp):
        result = list_remote_recordings()

    assert result == []


def test_list_remote_recordings_connection_error():
    from screencap.download import list_remote_recordings
    import requests as req

    with mock.patch("screencap.download.requests.post", side_effect=req.ConnectionError):
        with pytest.raises(RuntimeError, match="unavailable"):
            list_remote_recordings()


def test_list_remote_recordings_timeout():
    from screencap.download import list_remote_recordings
    import requests as req

    with mock.patch("screencap.download.requests.post", side_effect=req.Timeout):
        with pytest.raises(RuntimeError, match="timed out"):
            list_remote_recordings()


def test_list_remote_recordings_server_error():
    from screencap.download import list_remote_recordings

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 500
    mock_resp.json.return_value = {"error": "internal error"}
    mock_resp.text = "internal error"

    with mock.patch("screencap.download.requests.post", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="Download service error"):
            list_remote_recordings()


# ---------------------------------------------------------------------------
# Unit tests: request_signed_urls
# ---------------------------------------------------------------------------


def test_request_signed_urls_success():
    from screencap.download import request_signed_urls

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "urls": {
            "video.mp4": "https://signed-url/video",
            "audio.flac": "https://signed-url/audio",
        },
        "gcs_prefix": "gs://screencap-recordings/recordings/rec-001/",
    }

    with mock.patch("screencap.download.requests.post", return_value=mock_resp):
        urls, prefix = request_signed_urls("rec-001")

    assert urls["video.mp4"] == "https://signed-url/video"
    assert "rec-001" in prefix


def test_request_signed_urls_not_found():
    from screencap.download import request_signed_urls

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"error": "Recording not found: nonexistent"}
    mock_resp.text = "Recording not found: nonexistent"

    with mock.patch("screencap.download.requests.post", return_value=mock_resp):
        with pytest.raises(FileNotFoundError, match="not found"):
            request_signed_urls("nonexistent")


def test_request_signed_urls_connection_error():
    from screencap.download import request_signed_urls
    import requests as req

    with mock.patch("screencap.download.requests.post", side_effect=req.ConnectionError):
        with pytest.raises(RuntimeError, match="unavailable"):
            request_signed_urls("rec-001")


def test_request_signed_urls_timeout():
    from screencap.download import request_signed_urls
    import requests as req

    with mock.patch("screencap.download.requests.post", side_effect=req.Timeout):
        with pytest.raises(RuntimeError, match="timed out"):
            request_signed_urls("rec-001")


# ---------------------------------------------------------------------------
# Unit tests: download_recording
# ---------------------------------------------------------------------------


def test_download_recording_dry_run(tmp_path):
    from screencap.download import download_recording

    mock_urls = {
        "video.mp4": "https://signed/video",
        "audio.flac": "https://signed/audio",
    }

    with mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")):
        result = download_recording("my-rec", tmp_path, dry_run=True)

    assert result.recording == "my-rec"
    assert result.downloaded == []
    # No files actually downloaded
    assert not (tmp_path / "my-rec" / "video.mp4").exists()


def test_download_recording_all_new(tmp_path):
    from screencap.download import download_recording

    mock_urls = {
        "video.mp4": "https://signed/video",
        "audio.flac": "https://signed/audio",
    }

    # Mock streaming response
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "100"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 100]

    with (
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://bucket/recordings/my-rec/")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
    ):
        result = download_recording("my-rec", tmp_path)

    assert set(result.downloaded) == {"video.mp4", "audio.flac"}
    assert result.failed == []
    assert (tmp_path / "my-rec" / DOWNLOAD_STATUS_FILE).exists()


def test_download_recording_incremental_skip(tmp_path):
    from screencap.download import download_recording

    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    (rec_dir / DOWNLOAD_STATUS_FILE).write_text('{"downloaded_at": "2026-01-01"}')

    # No mocks needed — should skip without network calls
    result = download_recording("my-rec", tmp_path)
    assert result.downloaded == []


def test_download_recording_force(tmp_path):
    from screencap.download import download_recording

    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    (rec_dir / DOWNLOAD_STATUS_FILE).write_text('{"downloaded_at": "2026-01-01"}')

    mock_urls = {"video.mp4": "https://signed/video"}

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "50"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 50]

    with (
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
    ):
        result = download_recording("my-rec", tmp_path, force=True)

    assert result.downloaded == ["video.mp4"]


def test_download_recording_empty(tmp_path):
    from screencap.download import download_recording

    with mock.patch("screencap.download.request_signed_urls", return_value=({}, "gs://...")):
        result = download_recording("empty-rec", tmp_path)

    assert result.downloaded == []
    assert not (tmp_path / "empty-rec" / DOWNLOAD_STATUS_FILE).exists()


def test_download_recording_partial_failure(tmp_path):
    from screencap.download import download_recording

    mock_urls = {
        "video.mp4": "https://signed/video",
        "audio.flac": "https://signed/audio",
    }

    # First call succeeds, second fails
    mock_resp_ok = mock.MagicMock()
    mock_resp_ok.status_code = 200
    mock_resp_ok.headers = {"content-length": "50"}
    mock_resp_ok.raise_for_status = mock.MagicMock()
    mock_resp_ok.iter_content.return_value = [b"x" * 50]

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_resp_ok
        raise ConnectionError("network drop")

    with (
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", side_effect=side_effect),
    ):
        result = download_recording("my-rec", tmp_path)

    assert len(result.downloaded) == 1
    assert len(result.failed) == 1
    # No status file when there are failures
    assert not (tmp_path / "my-rec" / DOWNLOAD_STATUS_FILE).exists()


# ---------------------------------------------------------------------------
# Unit tests: _download_file_with_progress
# ---------------------------------------------------------------------------


def test_download_file_with_progress_success(tmp_path):
    from screencap.download import _download_file_with_progress

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "100"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 50, b"y" * 50]

    progress = mock.MagicMock()

    dest = tmp_path / "video.mp4"
    with mock.patch("screencap.download.requests.get", return_value=mock_resp):
        nbytes = _download_file_with_progress(
            "https://signed/video", dest, progress, "task-1",
        )

    assert nbytes == 100
    assert dest.read_bytes() == b"x" * 50 + b"y" * 50


def test_download_file_with_progress_creates_parent_dirs(tmp_path):
    from screencap.download import _download_file_with_progress

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "10"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 10]

    progress = mock.MagicMock()

    dest = tmp_path / "sub" / "dir" / "file.txt"
    with mock.patch("screencap.download.requests.get", return_value=mock_resp):
        _download_file_with_progress(
            "https://signed/file", dest, progress, "task-1",
        )

    assert dest.exists()


def test_download_file_connection_drop(tmp_path):
    from screencap.download import _download_file_with_progress
    import requests as req

    progress = mock.MagicMock()
    dest = tmp_path / "video.mp4"

    with mock.patch("screencap.download.requests.get", side_effect=req.ConnectionError):
        with pytest.raises(req.ConnectionError):
            _download_file_with_progress(
                "https://signed/video", dest, progress, "task-1",
            )


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


def test_download_cli_no_recordings():
    runner = CliRunner()
    with mock.patch("screencap.download.list_remote_recordings", return_value=[]):
        result = runner.invoke(cli, ["download"])
    assert result.exit_code == 0
    assert "No recordings available" in result.output


def test_download_cli_dry_run(tmp_path):
    from screencap.download import RemoteRecording

    remote = [RemoteRecording("rec-001", file_count=2, total_size=1000)]

    mock_urls = {"video.mp4": "https://signed/video", "audio.flac": "https://signed/audio"}

    runner = CliRunner()
    with (
        mock.patch("screencap.download.list_remote_recordings", return_value=remote),
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.get_downloads_dir", return_value=tmp_path),
    ):
        result = runner.invoke(cli, ["download", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry run" in result.output


def test_download_cli_success(tmp_path):
    from screencap.download import RemoteRecording

    remote = [RemoteRecording("rec-001", file_count=1, total_size=100)]

    mock_urls = {"video.mp4": "https://signed/video"}

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "100"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 100]

    runner = CliRunner()
    with (
        mock.patch("screencap.download.list_remote_recordings", return_value=remote),
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
        mock.patch("screencap.download.get_downloads_dir", return_value=tmp_path),
    ):
        result = runner.invoke(cli, ["download"])

    assert result.exit_code == 0
    assert "Downloaded rec-001" in result.output
    assert "Done." in result.output


def test_download_cli_network_failure():
    import requests as req

    runner = CliRunner()
    with mock.patch("screencap.download.list_remote_recordings", side_effect=RuntimeError("Download service unavailable.")):
        result = runner.invoke(cli, ["download"])

    assert result.exit_code == 1
    assert "unavailable" in result.output


def test_download_cli_force(tmp_path):
    from screencap.download import RemoteRecording

    rec_dir = tmp_path / "rec-001"
    rec_dir.mkdir()
    (rec_dir / DOWNLOAD_STATUS_FILE).write_text('{"downloaded_at": "2026-01-01"}')

    remote = [RemoteRecording("rec-001", file_count=1, total_size=100)]
    mock_urls = {"video.mp4": "https://signed/video"}

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "50"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 50]

    runner = CliRunner()
    with (
        mock.patch("screencap.download.list_remote_recordings", return_value=remote),
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
        mock.patch("screencap.download.get_downloads_dir", return_value=tmp_path),
    ):
        result = runner.invoke(cli, ["download", "--force"])

    assert result.exit_code == 0
    assert "Downloaded rec-001" in result.output


def test_download_cli_all_already_downloaded(tmp_path):
    from screencap.download import RemoteRecording

    rec_dir = tmp_path / "rec-001"
    rec_dir.mkdir()
    (rec_dir / DOWNLOAD_STATUS_FILE).write_text('{"downloaded_at": "2026-01-01"}')

    remote = [RemoteRecording("rec-001", file_count=1, total_size=100)]

    runner = CliRunner()
    with (
        mock.patch("screencap.download.list_remote_recordings", return_value=remote),
        mock.patch("screencap.download.get_downloads_dir", return_value=tmp_path),
    ):
        result = runner.invoke(cli, ["download"])

    assert result.exit_code == 0
    assert "Already downloaded" in result.output
    assert "Done." in result.output


def test_download_cli_dest_flag(tmp_path):
    from screencap.download import RemoteRecording

    custom_dest = tmp_path / "custom"
    remote = [RemoteRecording("rec-001", file_count=1, total_size=100)]
    mock_urls = {"video.mp4": "https://signed/video"}

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "50"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 50]

    runner = CliRunner()
    with (
        mock.patch("screencap.download.list_remote_recordings", return_value=remote),
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
    ):
        result = runner.invoke(cli, ["download", "--dest", str(custom_dest)])

    assert result.exit_code == 0
    assert custom_dest.is_dir()


def test_download_cli_summary_output(tmp_path):
    """Multiple recordings should produce a summary line."""
    from screencap.download import RemoteRecording

    remote = [
        RemoteRecording("rec-001", file_count=1, total_size=100),
        RemoteRecording("rec-002", file_count=1, total_size=200),
    ]
    mock_urls = {"video.mp4": "https://signed/video"}

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "50"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 50]

    runner = CliRunner()
    with (
        mock.patch("screencap.download.list_remote_recordings", return_value=remote),
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
        mock.patch("screencap.download.get_downloads_dir", return_value=tmp_path),
    ):
        result = runner.invoke(cli, ["download"])

    assert result.exit_code == 0
    assert "Done." in result.output
    assert "downloaded" in result.output


# ---------------------------------------------------------------------------
# Parallel transfer tests
# ---------------------------------------------------------------------------


def test_download_recording_jobs_1_sequential(tmp_path):
    """--jobs 1 should produce correct results (matches sequential behavior)."""
    from screencap.download import download_recording

    mock_urls = {
        "video.mp4": "https://signed/video",
        "audio.flac": "https://signed/audio",
    }

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "100"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 100]

    with (
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
    ):
        result = download_recording("my-rec", tmp_path, jobs=1)

    assert set(result.downloaded) == {"video.mp4", "audio.flac"}
    assert result.failed == []
    assert (tmp_path / "my-rec" / DOWNLOAD_STATUS_FILE).exists()


def test_download_recording_jobs_more_than_files(tmp_path):
    """--jobs 4 with 2 files should not hang."""
    from screencap.download import download_recording

    mock_urls = {
        "video.mp4": "https://signed/video",
        "audio.flac": "https://signed/audio",
    }

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "50"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 50]

    with (
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
    ):
        result = download_recording("my-rec", tmp_path, jobs=4)

    assert set(result.downloaded) == {"video.mp4", "audio.flac"}
    assert result.failed == []


def test_download_recording_parallel_partial_failure(tmp_path):
    """--jobs 4 with 1 file failing: result lists correct."""
    from screencap.download import download_recording

    mock_urls = {
        "a.txt": "https://signed/a",
        "b.txt": "https://signed/b",
        "c.txt": "https://signed/c",
        "d.txt": "https://signed/d",
    }

    mock_resp_ok = mock.MagicMock()
    mock_resp_ok.status_code = 200
    mock_resp_ok.headers = {"content-length": "10"}
    mock_resp_ok.raise_for_status = mock.MagicMock()
    mock_resp_ok.iter_content.return_value = [b"x" * 10]

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ConnectionError("network drop")
        return mock_resp_ok

    with (
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", side_effect=side_effect),
    ):
        result = download_recording("my-rec", tmp_path, jobs=4)

    assert len(result.downloaded) == 3
    assert len(result.failed) == 1
    assert not (tmp_path / "my-rec" / DOWNLOAD_STATUS_FILE).exists()


def test_download_cli_jobs_flag_accepted(tmp_path):
    """CLI --jobs 2 should be accepted."""
    from screencap.download import RemoteRecording

    remote = [RemoteRecording("rec-001", file_count=1, total_size=100)]
    mock_urls = {"video.mp4": "https://signed/video"}

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "50"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 50]

    runner = CliRunner()
    with (
        mock.patch("screencap.download.list_remote_recordings", return_value=remote),
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
        mock.patch("screencap.download.get_downloads_dir", return_value=tmp_path),
    ):
        result = runner.invoke(cli, ["download", "--jobs", "2"])

    assert result.exit_code == 0


def test_download_cli_jobs_0_rejected():
    """CLI --jobs 0 should be rejected by Click validation."""
    runner = CliRunner()
    result = runner.invoke(cli, ["download", "--jobs", "0"])
    assert result.exit_code != 0


def test_download_cli_jobs_negative_rejected():
    """CLI --jobs -1 should be rejected."""
    runner = CliRunner()
    result = runner.invoke(cli, ["download", "--jobs", "-1"])
    assert result.exit_code != 0


def test_download_cli_jobs_abc_rejected():
    """CLI --jobs abc should be rejected."""
    runner = CliRunner()
    result = runner.invoke(cli, ["download", "--jobs", "abc"])
    assert result.exit_code != 0


def test_download_cli_short_flag(tmp_path):
    """CLI -j 1 short flag should work."""
    from screencap.download import RemoteRecording

    remote = [RemoteRecording("rec-001", file_count=1, total_size=100)]
    mock_urls = {"video.mp4": "https://signed/video"}

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "50"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 50]

    runner = CliRunner()
    with (
        mock.patch("screencap.download.list_remote_recordings", return_value=remote),
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
        mock.patch("screencap.download.get_downloads_dir", return_value=tmp_path),
    ):
        result = runner.invoke(cli, ["download", "-j", "1"])

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Unit tests: fetch_session_index
# ---------------------------------------------------------------------------


def test_fetch_session_index_success():
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "version": 1,
        "recordings": {
            "rec-001": {"total_tasks": 2, "tags": ["python"]},
        },
        "total_recordings": 1,
    }

    with mock.patch("screencap.download.requests.post", return_value=mock_resp):
        index = fetch_session_index()

    assert "rec-001" in index["recordings"]
    assert index["total_recordings"] == 1


def test_fetch_session_index_failure():
    import requests as req

    with mock.patch("screencap.download.requests.post", side_effect=req.ConnectionError):
        index = fetch_session_index()

    assert index == {"version": 1, "recordings": {}}


def test_fetch_session_index_server_error():
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 500

    with mock.patch("screencap.download.requests.post", return_value=mock_resp):
        index = fetch_session_index()

    assert index == {"version": 1, "recordings": {}}


# ---------------------------------------------------------------------------
# Unit tests: list_remote_sessions
# ---------------------------------------------------------------------------


def test_list_remote_sessions_no_filter():
    index = {
        "version": 1,
        "recordings": {
            "rec-001": {
                "processed_at": "2026-03-16T00:00:00Z",
                "total_tasks": 2,
                "total_duration_s": 3600.0,
                "primary_focus": "development",
                "categories": ["development", "communication"],
                "tags": ["python", "auth"],
            },
        },
    }
    with mock.patch("screencap.download.fetch_session_index", return_value=index):
        sessions = list_remote_sessions()

    assert len(sessions) == 1
    assert sessions[0].name == "rec-001"
    assert sessions[0].tags == ["python", "auth"]


def test_list_remote_sessions_filter_tag():
    index = {
        "version": 1,
        "recordings": {
            "rec-001": {
                "processed_at": "", "total_tasks": 1, "total_duration_s": 100,
                "primary_focus": "development", "categories": ["development"],
                "tags": ["python"],
            },
            "rec-002": {
                "processed_at": "", "total_tasks": 1, "total_duration_s": 200,
                "primary_focus": "research", "categories": ["research"],
                "tags": ["javascript"],
            },
        },
    }
    with mock.patch("screencap.download.fetch_session_index", return_value=index):
        sessions = list_remote_sessions(tag="python")

    assert len(sessions) == 1
    assert sessions[0].name == "rec-001"


def test_list_remote_sessions_filter_category():
    index = {
        "version": 1,
        "recordings": {
            "rec-001": {
                "processed_at": "", "total_tasks": 1, "total_duration_s": 100,
                "primary_focus": "development", "categories": ["development"],
                "tags": [],
            },
            "rec-002": {
                "processed_at": "", "total_tasks": 1, "total_duration_s": 200,
                "primary_focus": "research", "categories": ["research"],
                "tags": [],
            },
        },
    }
    with mock.patch("screencap.download.fetch_session_index", return_value=index):
        sessions = list_remote_sessions(category="research")

    assert len(sessions) == 1
    assert sessions[0].name == "rec-002"


# ---------------------------------------------------------------------------
# Unit tests: filter_urls_by_category
# ---------------------------------------------------------------------------


def test_filter_urls_by_category():
    urls = {
        "timeline.json": "https://signed/timeline",
        "_processing_status.json": "https://signed/status",
        "tasks/dev_000_coding/video.mp4": "https://signed/dev-video",
        "tasks/dev_000_coding/events.jsonl": "https://signed/dev-events",
        "tasks/com_001_email/video.mp4": "https://signed/com-video",
        "tasks/res_002_research/video.mp4": "https://signed/res-video",
    }

    filtered = filter_urls_by_category(urls, "development")

    assert "timeline.json" in filtered
    assert "_processing_status.json" in filtered
    assert "tasks/dev_000_coding/video.mp4" in filtered
    assert "tasks/dev_000_coding/events.jsonl" in filtered
    assert "tasks/com_001_email/video.mp4" not in filtered
    assert "tasks/res_002_research/video.mp4" not in filtered


def test_filter_urls_by_category_keeps_all_non_task_files():
    urls = {
        "timeline.json": "https://signed/timeline",
        "some_other_file.json": "https://signed/other",
    }
    filtered = filter_urls_by_category(urls, "development")
    assert len(filtered) == 2


# ---------------------------------------------------------------------------
# Unit tests: download_recording with category_filter
# ---------------------------------------------------------------------------


def test_download_with_category_filter(tmp_path):
    from screencap.download import download_recording

    mock_urls = {
        "timeline.json": "https://signed/timeline",
        "tasks/dev_000_coding/video.mp4": "https://signed/dev-video",
        "tasks/com_001_email/video.mp4": "https://signed/com-video",
    }

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "50"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 50]

    with (
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
    ):
        result = download_recording(
            "my-rec", tmp_path, category_filter="development",
        )

    # Should download timeline.json + dev task, but not com task
    assert "tasks/com_001_email/video.mp4" not in result.downloaded
    assert len(result.downloaded) == 2  # timeline.json + dev video


# ---------------------------------------------------------------------------
# CLI tests: list --remote
# ---------------------------------------------------------------------------


def test_list_remote_sessions_cli():
    runner = CliRunner()

    sessions = [
        RemoteSession(
            name="rec-001",
            processed_at="2026-03-16T00:00:00Z",
            total_tasks=3,
            total_duration_s=3600.0,
            primary_focus="development",
            categories=["development"],
            tags=["python", "auth"],
        ),
    ]

    with mock.patch("screencap.download.list_remote_sessions", return_value=sessions):
        result = runner.invoke(cli, ["list", "--remote"])

    assert result.exit_code == 0
    assert "rec-001" in result.output
    assert "python" in result.output


def test_list_remote_sessions_cli_json():
    runner = CliRunner()

    sessions = [
        RemoteSession(
            name="rec-001",
            processed_at="2026-03-16T00:00:00Z",
            total_tasks=2,
            total_duration_s=1800.0,
            primary_focus="development",
            categories=["development"],
            tags=["python"],
        ),
    ]

    with mock.patch("screencap.download.list_remote_sessions", return_value=sessions):
        result = runner.invoke(cli, ["list", "--remote", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["name"] == "rec-001"


def test_list_remote_empty():
    runner = CliRunner()

    with mock.patch("screencap.download.list_remote_sessions", return_value=[]):
        result = runner.invoke(cli, ["list", "--remote"])

    assert result.exit_code == 0
    assert "No remote sessions" in result.output


def test_list_tag_without_remote():
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--tag", "python"])
    assert result.exit_code == 0
    assert "--tag and --category require --remote" in result.output


# ---------------------------------------------------------------------------
# CLI tests: download --category
# ---------------------------------------------------------------------------


def test_download_category_requires_sessions():
    runner = CliRunner()
    result = runner.invoke(cli, ["download", "rec-001", "--category", "development"])
    assert result.exit_code == 1
    assert "--category requires --sessions" in result.output


def test_download_with_category_cli(tmp_path):
    from screencap.download import RemoteRecording

    remote = [RemoteRecording("rec-001", file_count=3, total_size=100)]
    mock_urls = {
        "timeline.json": "https://signed/timeline",
        "tasks/dev_000_coding/video.mp4": "https://signed/dev-video",
        "tasks/com_001_email/video.mp4": "https://signed/com-video",
    }

    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-length": "50"}
    mock_resp.raise_for_status = mock.MagicMock()
    mock_resp.iter_content.return_value = [b"x" * 50]

    runner = CliRunner()
    with (
        mock.patch("screencap.download.list_remote_recordings", return_value=remote),
        mock.patch("screencap.download.request_signed_urls", return_value=(mock_urls, "gs://...")),
        mock.patch("screencap.download.requests.get", return_value=mock_resp),
        mock.patch("screencap.config.get_sessions_dir", return_value=tmp_path),
    ):
        result = runner.invoke(cli, [
            "download", "--sessions", "--category", "development",
        ])

    assert result.exit_code == 0
