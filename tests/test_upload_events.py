"""Tests for the structured upload stderr lifecycle events (plan U1).

These cover the upload_started, upload_file_done, upload_finished, and
upload_failed event taxonomy that the SwiftUI review window's
UploadController consumes via the existing CLIClient.spawn line-buffered
reader. The Swift parser is drift-resilient (ignores unknown fields,
returns nil on non-JSON), so these tests assert the *shape* and
*ordering* of emitted events without over-specifying field names a
future revision might extend.
"""

from __future__ import annotations

import json
import signal
import threading
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_events(captured_stderr: str) -> list[dict]:
    """Split captured stderr into JSON event dicts.

    Tolerates non-event lines (e.g. Rich console bleed onto stderr) so
    the test mirrors the Swift parser's drift-resilient contract.
    """
    events: list[dict] = []
    for line in captured_stderr.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "type" in payload:
            events.append(payload)
    return events


def _mock_urls_response(files: dict[str, str | None], prefix: str = "gs://bucket/"):
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"urls": files, "gcs_prefix": prefix}
    return resp


def _mock_put_response():
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.raise_for_status = mock.MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Happy-path event ordering
# ---------------------------------------------------------------------------


def test_upload_emits_started_filedone_finished_for_single_file(tmp_path, capfd):
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-a"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    with (
        mock.patch(
            "screencap.upload.requests.post",
            return_value=_mock_urls_response({"video.mp4": "https://signed/v"}),
        ),
        mock.patch("screencap.upload.requests.put", return_value=_mock_put_response()),
    ):
        upload_recording(rec)

    err = capfd.readouterr().err
    events = _parse_events(err)
    types = [e["type"] for e in events]
    assert types == ["upload_started", "upload_file_done", "upload_finished"]


def test_upload_emits_one_filedone_per_uploaded_file(tmp_path, capfd):
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-b"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / "audio.flac").write_bytes(b"x" * 50)
    (rec / "events.jsonl").write_text("{}\n")

    urls = {
        "video.mp4": "https://signed/v",
        "audio.flac": "https://signed/a",
        "events.jsonl": "https://signed/e",
    }

    with (
        mock.patch(
            "screencap.upload.requests.post", return_value=_mock_urls_response(urls)
        ),
        mock.patch("screencap.upload.requests.put", return_value=_mock_put_response()),
    ):
        upload_recording(rec, jobs=1)  # jobs=1 keeps event order deterministic

    events = _parse_events(capfd.readouterr().err)
    types = [e["type"] for e in events]
    assert types[0] == "upload_started"
    assert types[-1] == "upload_finished"
    assert types.count("upload_file_done") == 3


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


def test_all_emitted_events_carry_schema_and_recording(tmp_path, capfd):
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-c"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    with (
        mock.patch(
            "screencap.upload.requests.post",
            return_value=_mock_urls_response({"video.mp4": "https://signed/v"}),
        ),
        mock.patch("screencap.upload.requests.put", return_value=_mock_put_response()),
    ):
        upload_recording(rec)

    events = _parse_events(capfd.readouterr().err)
    assert events, "expected at least one event"
    for e in events:
        # Universal envelope from emit_event in _stderr_events.py.
        assert "type" in e
        assert "ts" in e
        assert "schema_version" in e
        # Upload events also carry the recording name so the Swift side
        # can associate stderr lines with the right window.
        assert e["recording"] == "rec-c"


def test_upload_started_includes_file_count_and_total_bytes(tmp_path, capfd):
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-d"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)
    (rec / "audio.flac").write_bytes(b"x" * 50)

    with (
        mock.patch(
            "screencap.upload.requests.post",
            return_value=_mock_urls_response(
                {"video.mp4": "https://v", "audio.flac": "https://a"}
            ),
        ),
        mock.patch("screencap.upload.requests.put", return_value=_mock_put_response()),
    ):
        upload_recording(rec)

    events = _parse_events(capfd.readouterr().err)
    started = next(e for e in events if e["type"] == "upload_started")
    assert started["file_count"] == 2
    assert started["total_bytes"] == 150


# ---------------------------------------------------------------------------
# Edge: dry-run emits nothing
# ---------------------------------------------------------------------------


def test_dry_run_emits_no_events(tmp_path, capfd):
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-dry"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    upload_recording(rec, dry_run=True)

    events = _parse_events(capfd.readouterr().err)
    assert events == []


# ---------------------------------------------------------------------------
# Edge: all files already uploaded server-side
# ---------------------------------------------------------------------------


def test_all_files_skipped_emits_started_then_finished_only(tmp_path, capfd):
    """When server says every file already exists, we still bracket the
    no-op with started/finished events so the Swift side sees a complete
    lifecycle and can close the window with a success state."""
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-skip"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    with mock.patch(
        "screencap.upload.requests.post",
        return_value=_mock_urls_response({"video.mp4": None}),
    ):
        upload_recording(rec)

    events = _parse_events(capfd.readouterr().err)
    types = [e["type"] for e in events]
    assert types == ["upload_started", "upload_finished"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_signed_urls_connection_error_emits_upload_failed(tmp_path, capfd):
    """RuntimeError from request_signed_urls propagates after the
    catch-all emits upload_failed."""
    import requests as req
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-net"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    with mock.patch("screencap.upload.requests.post", side_effect=req.ConnectionError):
        with pytest.raises(RuntimeError):
            upload_recording(rec)

    events = _parse_events(capfd.readouterr().err)
    types = [e["type"] for e in events]
    assert "upload_started" in types
    assert types[-1] == "upload_failed"
    failed = next(e for e in events if e["type"] == "upload_failed")
    assert failed["error"]  # non-empty


def test_per_file_failure_emits_upload_failed_terminal(tmp_path, capfd):
    """Per-file PUT failure → terminal event is upload_failed, not
    upload_finished. The Swift side uses the terminal type to choose
    between auto-close and retry-UI."""
    import requests as req
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-perfile"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    put_resp = mock.MagicMock()
    put_resp.raise_for_status.side_effect = req.HTTPError("500 boom")
    put_resp.status_code = 500

    with (
        mock.patch(
            "screencap.upload.requests.post",
            return_value=_mock_urls_response({"video.mp4": "https://signed/v"}),
        ),
        mock.patch("screencap.upload.requests.put", return_value=put_resp),
    ):
        upload_recording(rec)

    events = _parse_events(capfd.readouterr().err)
    types = [e["type"] for e in events]
    assert types[0] == "upload_started"
    assert types[-1] == "upload_failed"
    # No upload_file_done — the file failed to upload.
    assert "upload_file_done" not in types


# ---------------------------------------------------------------------------
# SIGTERM → KeyboardInterrupt handler
# ---------------------------------------------------------------------------


def test_sigterm_handler_raises_keyboard_interrupt():
    """The handler function itself converts SIGTERM into KeyboardInterrupt.

    Asserting at the handler level (vs end-to-end signal delivery) keeps
    the test deterministic across CI environments where signal timing is
    unreliable.
    """
    from screencap.upload import _raise_keyboard_interrupt

    with pytest.raises(KeyboardInterrupt):
        _raise_keyboard_interrupt(signal.SIGTERM, None)


def test_upload_restores_previous_sigterm_handler(tmp_path):
    """The handler installed at the top of upload_recording must be
    restored on exit (success path). Without this, repeated uploads in
    the same process leave the SIGTERM handler permanently installed."""
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-restore"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    sentinel_handler = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        with (
            mock.patch(
                "screencap.upload.requests.post",
                return_value=_mock_urls_response({"video.mp4": "https://v"}),
            ),
            mock.patch(
                "screencap.upload.requests.put", return_value=_mock_put_response()
            ),
        ):
            upload_recording(rec)
        # After upload_recording returns, the previous handler should be
        # back in place — not _raise_keyboard_interrupt.
        current = signal.getsignal(signal.SIGTERM)
        assert current == signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, sentinel_handler)


def test_upload_handler_installation_skipped_off_main_thread(tmp_path):
    """signal.signal raises ValueError when called off the main thread.
    upload_recording must swallow that and continue — cancel won't work
    via SIGTERM on background threads, but normal upload still succeeds.
    """
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-thread"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    result_box: dict = {}

    def _run_off_main():
        with (
            mock.patch(
                "screencap.upload.requests.post",
                return_value=_mock_urls_response({"video.mp4": "https://v"}),
            ),
            mock.patch(
                "screencap.upload.requests.put", return_value=_mock_put_response()
            ),
        ):
            try:
                result_box["result"] = upload_recording(rec)
            except Exception as e:  # pragma: no cover - failure surfaces below
                result_box["error"] = e

    t = threading.Thread(target=_run_off_main)
    t.start()
    t.join(timeout=10)
    assert "error" not in result_box, f"unexpected error: {result_box.get('error')}"
    assert result_box["result"].uploaded == ["video.mp4"]
