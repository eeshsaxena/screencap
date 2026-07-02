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
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _signed_in_autouse(_signed_in):
    """Apply the shared ``_signed_in`` fixture (tests/conftest.py) to every test
    in this module so in-process upload_recording calls get a token via
    request_signed_urls. The real-subprocess test sets the token via env instead."""


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


# ---------------------------------------------------------------------------
# Inner KeyboardInterrupt branch (the SIGTERM-converted cancel inside the
# as_completed loop). The outer-except path is covered by the
# request_signed_urls ConnectionError test; this one drives cancel mid-PUT.
# Plan U1 test scenario, todo 004.
# ---------------------------------------------------------------------------


def test_inner_keyboard_interrupt_emits_upload_failed_interrupted(tmp_path, capfd):
    """KeyboardInterrupt raised from inside the executor block (mocked via
    requests.put) drives the inner except path: emits upload_failed with
    error="interrupted", shuts down the executor, and re-raises."""
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-kbi"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    with (
        mock.patch(
            "screencap.upload.requests.post",
            return_value=_mock_urls_response({"video.mp4": "https://v"}),
        ),
        mock.patch(
            "screencap.upload.requests.put", side_effect=KeyboardInterrupt
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        upload_recording(rec)

    events = _parse_events(capfd.readouterr().err)
    types = [e["type"] for e in events]
    # upload_started must precede the cancel; upload_failed must be terminal.
    assert types[0] == "upload_started"
    assert types[-1] == "upload_failed"
    # The plan pins error="interrupted" as the cancel signal the Swift
    # consumer dispatches on. A future refactor that changes this string
    # silently breaks the cross-language contract.
    failed = next(e for e in events if e["type"] == "upload_failed")
    assert failed["error"] == "interrupted"
    # upload_finished MUST NOT appear — interrupted is not success.
    assert "upload_finished" not in types


# ---------------------------------------------------------------------------
# Server-rejected-all path (TODO 012): when every offered file is rejected
# by server-side validation, terminal event is upload_failed (not
# upload_finished). The upload_status.json sentinel must NOT be written —
# nothing actually landed.
# ---------------------------------------------------------------------------


def test_all_files_server_rejected_emits_upload_failed(tmp_path, capfd):
    from screencap.upload import UPLOAD_STATUS_FILE, upload_recording

    rec = tmp_path / "rec-rejected"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    # Server response includes no urls for our files → rejected branch.
    with mock.patch(
        "screencap.upload.requests.post",
        return_value=_mock_urls_response({}),
    ):
        upload_recording(rec)

    events = _parse_events(capfd.readouterr().err)
    types = [e["type"] for e in events]
    assert types[0] == "upload_started"
    assert types[-1] == "upload_failed"
    assert "upload_finished" not in types
    failed = next(e for e in events if e["type"] == "upload_failed")
    assert failed["failed"] >= 1
    assert "rejected" in failed["error"].lower()
    # No local success sentinel on failure path — re-running upload would
    # have to retry, not skip-via-is_uploaded.
    assert not (rec / UPLOAD_STATUS_FILE).exists()


# ---------------------------------------------------------------------------
# Decouple upload_finished from _write_upload_status success (TODO 013):
# an OSError on the sentinel write must NOT flip a successful upload to
# upload_failed via the catch-all. The cloud-side state is what counts.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SIGTERM subprocess test (plan U1, todo 003): a real signal delivered to
# a real child process must convert to KeyboardInterrupt and emit
# upload_failed(error="interrupted") before the process exits. The
# in-process tests above can't prove signal.signal install + delivery in
# the actual entry path — only a subprocess can.
# ---------------------------------------------------------------------------


# Helper script executed via `python -c`. Mocks requests.post/put so the
# upload stays in-flight (PUT sleeps for 30s) long enough for SIGTERM to
# arrive. Prints a sentinel line to stdout once upload_started has been
# emitted so the parent knows it's safe to signal.
_SIGTERM_TEST_HARNESS = textwrap.dedent("""
    import sys
    import time
    import json
    from pathlib import Path
    from unittest import mock

    # Stay-in-flight PUT — sleeps long enough for the parent to SIGTERM
    # us, but short enough that the ThreadPoolExecutor's `with` block
    # exit (which calls shutdown(wait=True)) doesn't dominate runtime.
    # 1s is well above the parent's 200ms-after-READY signal window.
    def _slow_put(*a, **kw):
        time.sleep(1)
        raise RuntimeError("PUT was not interrupted — test setup broken")

    def _post_returning_urls(*a, **kw):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "urls": {"video.mp4": "https://signed/v"},
            "gcs_prefix": "gs://bucket/",
        }
        return resp

    rec = Path(sys.argv[1])

    # Print the ready sentinel BEFORE entering upload_recording so the
    # parent doesn't race with the upload_started emit. We rely on
    # PYTHONUNBUFFERED=1 to flush immediately.
    print("READY", flush=True)

    with (
        mock.patch("screencap.upload.requests.post", side_effect=_post_returning_urls),
        mock.patch("screencap.upload.requests.put", side_effect=_slow_put),
    ):
        from screencap.upload import upload_recording
        try:
            upload_recording(rec)
        except KeyboardInterrupt:
            sys.exit(130)  # conventional Ctrl+C exit code
""")


@pytest.mark.timeout(30)  # safety net — should complete in <2s
def test_sigterm_delivered_to_real_subprocess_emits_interrupted(tmp_path):
    rec = tmp_path / "rec-sigterm"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    src_root = Path(__file__).resolve().parent.parent / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{src_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["PYTHONUNBUFFERED"] = "1"
    # The child can't see the parent's monkeypatch, so deliver a token via the
    # out-of-band engine channel: auth.get_id_token reads this 0600 file instead
    # of the Keychain, so request_signed_urls attaches a bearer and proceeds. The
    # token carries a founding `plan` claim so the child's real U4 entitlement
    # pre-check (assert_entitled_to_upload) passes and the upload proceeds.
    from tests._jwt import _jwt

    token_file = tmp_path / "engine-token.jwt"
    token_file.write_text(_jwt({"user_id": "uid-sigterm", "plan": "founding"}))
    env["SCREENCAP_ENGINE_TOKEN_FILE"] = str(token_file)

    proc = subprocess.Popen(
        [sys.executable, "-c", _SIGTERM_TEST_HARNESS, str(rec)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )

    try:
        # Wait for the READY sentinel — proves the harness reached the
        # upload_recording call and signal.signal install succeeded.
        ready_line = proc.stdout.readline()
        assert ready_line.strip() == "READY", f"unexpected harness output: {ready_line!r}"

        # Give the child a moment to install the handler and emit
        # upload_started + start the slow PUT. 200ms is enough on macOS;
        # the SIGTERM handler must be installed before this elapses.
        time.sleep(0.2)

        os.kill(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=5)
    except Exception:
        proc.kill()
        raise

    # KeyboardInterrupt was re-raised; harness exits 130. If SIGTERM
    # killed the process before the handler installed, exit code would
    # be -SIGTERM (negative). Either path that's not 130 is a regression.
    assert proc.returncode == 130, (
        f"unexpected exit code {proc.returncode}; stderr:\n{stderr}"
    )

    events = _parse_events(stderr)
    types = [e["type"] for e in events]
    assert "upload_started" in types, f"upload_started missing; stderr:\n{stderr}"
    assert types[-1] == "upload_failed", f"terminal event wrong; types: {types}"
    failed = next(e for e in events if e["type"] == "upload_failed")
    assert failed["error"] == "interrupted"


def test_upload_finished_emitted_even_when_status_write_fails(tmp_path, capfd):
    from screencap.upload import upload_recording

    rec = tmp_path / "rec-sentinel-fail"
    rec.mkdir()
    (rec / "video.mp4").write_bytes(b"x" * 100)

    with (
        mock.patch(
            "screencap.upload.requests.post",
            return_value=_mock_urls_response({"video.mp4": "https://v"}),
        ),
        mock.patch("screencap.upload.requests.put", return_value=_mock_put_response()),
        # Simulate disk-full / RO recording dir during sentinel write.
        mock.patch(
            "screencap.upload._write_upload_status",
            side_effect=OSError("disk full"),
        ),
    ):
        result = upload_recording(rec)

    events = _parse_events(capfd.readouterr().err)
    types = [e["type"] for e in events]
    # Successful upload reaches upload_finished; the sentinel write happens
    # AFTER the emit, so an OSError there cannot demote the terminal event.
    assert "upload_finished" in types
    # upload_failed must NOT appear — sentinel write failure is not an
    # upload failure (the cloud already has the file).
    assert "upload_failed" not in types
    assert result.uploaded == ["video.mp4"]
