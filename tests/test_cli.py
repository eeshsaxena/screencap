"""Tests for screencap CLI argument parsing."""

import json
import time
from contextlib import contextmanager
from unittest import mock

import pytest
from click.testing import CliRunner

from screencap.cli import cli
from tests.conftest import _fake_provisioned, _install_provisioned


@pytest.fixture(autouse=True)
def _signed_in_autouse(_signed_in):
    """Apply the shared ``_signed_in`` fixture (tests/conftest.py) to every test
    in this module so the upload command's pre-flight auth check passes without
    touching the Keychain. Upload's not-signed-in refusal is covered in
    test_upload.py."""


@contextmanager
def _safe_start_prompts():
    """Patch the four entry points that interact with stdin or hard-exit during
    `screencap start` so the command can run as a unit test:

      _maybe_prompt_privacy_setup          # may launch setup wizard
      _maybe_prompt_matrix_acknowledgement # 5s select.select on stdin
      _stdin_is_tty                        # gates redaction + website prompts
      os._exit                             # `start` hard-exits to bypass background threads

    Tests still need to mock `screencap.recorder.start_recording` (and
    optionally `screencap.cli._auto_export`) themselves; this helper only
    neutralizes the prompt/exit path.
    """
    with (
        mock.patch("screencap.cli._maybe_prompt_privacy_setup"),
        mock.patch("screencap.cli._maybe_prompt_matrix_acknowledgement"),
        mock.patch("screencap.cli._stdin_is_tty", return_value=False),
        mock.patch("os._exit"),
    ):
        yield


def test_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "screencap" in result.output


def test_list_empty(tmp_path):
    """Empty archive in human (prose) mode prints "No recordings" not JSON.
    `_should_default_to_json` is mocked False because CliRunner pipes are
    non-TTY and would otherwise auto-flip the command into JSON mode."""
    runner = CliRunner()
    with mock.patch("screencap.catalog.get_recordings_dir", return_value=tmp_path), \
         mock.patch("screencap.cli._should_default_to_json", return_value=False):
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "No recordings" in result.output


def test_list_json_empty(tmp_path):
    """`list --json` must emit a parseable empty JSON list when the archive
    is empty. JSON consumers (e.g. the SwiftUI shell) cannot tolerate
    Rich-styled prose like "No recordings found" — JSONDecoder throws and
    the consumer can't tell empty-archive from a real CLI failure."""
    runner = CliRunner()
    with mock.patch("screencap.catalog.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["list", "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed == []


def test_view_not_found(tmp_path):
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["view", "nonexistent"])
        assert result.exit_code == 1
        assert "Error" in result.output


# --- info command tests ---


def _make_recording_dir(base, name, *, duration=60.0, with_metrics=False):
    """Create a minimal recording dir with real engine DB and optional metrics."""
    from screencap.engine.db import create_db, crud

    rec_dir = base / name
    rec_dir.mkdir(parents=True)

    db_path = rec_dir / "recording.db"
    started = time.time() - duration
    engine, Session = create_db(str(db_path))
    session = Session()
    rec = crud.insert_recording(session, {
        "timestamp": started, "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080,
        "pixel_ratio": 2.0, "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    crud.insert_action_event(session, rec, started + duration, {
        "name": "click", "mouse_x": 100.0, "mouse_y": 200.0,
        "mouse_button_name": "left", "mouse_pressed": True,
    })
    session.close()
    engine.dispose()

    if with_metrics:
        metrics = {
            "schema_version": 4,
            "static": {
                "hostname": "test-host.local",
                "macos_version": "15.3",
                "cpu_model": "Apple M2",
                "cpu_cores_physical": 8,
                "cpu_cores_logical": 8,
                "memory_total_gb": 16.0,
                "gpu_model": "Apple M2",
                "python_version": "3.11.6",
                "screencap_version": "0.1.0",
                "kernel_version": "Darwin 24.6.0",
                "displays": [{"width": 2560, "height": 1600}],
                "display_count": 1,
                "locale": {
                    "system_locale": "en_US",
                    "preferred_languages": ["en-US", "pt-BR"],
                    "keyboard_layout": "com.apple.keylayout.US",
                    "input_sources": ["com.apple.keylayout.US"],
                    "timezone": "America/New_York",
                    "timezone_offset": "-05:00",
                    "date_format": "M/d/yy",
                    "number_format": {"decimal_separator": ".", "grouping_separator": ","},
                    "currency_code": "USD",
                },
                "wifi": {"connected": True, "phy_mode": "802.11ax"},
                "running_applications": [
                    {"name": "Finder", "bundle_id": "com.apple.finder", "version": "14.2"},
                    {"name": "Google Chrome", "bundle_id": "com.google.Chrome", "version": "131.0.6778.86"},
                ],
            },
            "start": {
                "collected_at": "2026-02-19T14:30:00+00:00",
                "cpu_percent": 12.5,
                "wifi": {"rssi_dbm": -55, "tx_rate_mbps": 540.0},
            },
            "end": {
                "collected_at": "2026-02-19T14:35:00+00:00",
                "cpu_percent": 18.0,
                "wifi": {"rssi_dbm": -52, "tx_rate_mbps": 780.0},
            },
        }
        (rec_dir / "system_metrics.json").write_text(json.dumps(metrics))

    return rec_dir


def test_info_command_with_metrics(tmp_path):
    _make_recording_dir(tmp_path, "demo", with_metrics=True)
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path), \
         mock.patch("screencap.cli._should_default_to_json", return_value=False):
        result = runner.invoke(cli, ["info", "demo"])
    assert result.exit_code == 0
    assert "demo" in result.output
    assert "Apple M2" in result.output
    assert "15.3" in result.output
    # Locale rendering
    assert "locale:" in result.output
    assert "en_US" in result.output
    assert "en-US" in result.output
    assert "America/New_York" in result.output
    # WiFi rendering
    assert "wifi:" in result.output
    assert "802.11ax" in result.output


def test_info_command_json_output(tmp_path):
    _make_recording_dir(tmp_path, "demo", with_metrics=True)
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["info", "demo", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "recording" in data
    assert "metrics" in data
    assert data["metrics"]["static"]["hostname"] == "test-host.local"


def test_info_command_no_metrics(tmp_path):
    _make_recording_dir(tmp_path, "old-rec", with_metrics=False)
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path), \
         mock.patch("screencap.cli._should_default_to_json", return_value=False):
        result = runner.invoke(cli, ["info", "old-rec"])
    assert result.exit_code == 0
    assert "No system metrics" in result.output


def test_info_command_with_running_applications(tmp_path):
    _make_recording_dir(tmp_path, "demo", with_metrics=True)
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path), \
         mock.patch("screencap.cli._should_default_to_json", return_value=False):
        result = runner.invoke(cli, ["info", "demo"])
    assert result.exit_code == 0
    assert "running apps:" in result.output
    assert "Finder (com.apple.finder) v14.2" in result.output
    assert "Google Chrome (com.google.Chrome) v131.0.6778.86" in result.output


def test_info_command_nonexistent_recording(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["info", "doesnotexist"])
    assert result.exit_code == 1
    assert "Error" in result.output


# --- stop command tests (Phase 2 U1.5: thin daemon client) ---


def _stop_daemon_envelope(**payload):
    body = {
        "ok": True,
        "schema_version": 1,
        "daemon_version": "test",
        "api_schema_version": 1,
    }
    body.update(payload)
    return body


def _patch_stop_daemon_client(handler):
    """Returns (start_patcher, autospawn_patcher) context managers."""
    import httpx

    from screencap.cli._daemon_client import DaemonHTTPClient

    transport = httpx.MockTransport(handler)

    class _PatchedClient(DaemonHTTPClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    return (
        mock.patch("screencap.cli._daemon_client.DaemonHTTPClient", _PatchedClient),
        mock.patch("screencap.cli._autospawn.ensure_daemon_or_spawn", lambda *a, **k: None),
    )


def test_stop_graceful_against_active_recording():
    import httpx

    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_stop_daemon_envelope(stopped=True, final_state="stopped"),
        )

    client_p, autospawn_p = _patch_stop_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["stop", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["stopped"] is True
    assert payload["action"] == "sigterm"
    assert payload["final_state"] == "stopped"
    assert captured["path"] == "/v0/recording.stop"
    assert captured["body"] == {"force": False}


def test_stop_force_passes_through_to_daemon():
    import httpx

    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_stop_daemon_envelope(stopped=True, final_state="force_stopped"),
        )

    client_p, autospawn_p = _patch_stop_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["stop", "--force", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["action"] == "sigkill"
    assert payload["final_state"] == "force_stopped"
    assert captured["body"] == {"force": True}


def test_stop_no_daemon_reports_clean_no_op():
    import httpx

    def handler(request):
        raise httpx.ConnectError("socket missing")

    client_p, autospawn_p = _patch_stop_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["stop", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["action"] == "no_daemon"
    assert payload["stopped"] is False


def test_stop_not_owned_by_daemon_is_zero_exit():
    """`stop` returns 0 when the daemon reports no recording to stop."""
    import httpx

    def handler(request):
        return httpx.Response(
            409,
            json={
                "ok": False,
                "error": "not_owned_by_daemon",
                "schema_version": 1,
                "api_schema_version": 1,
                "daemon_version": "test",
            },
        )

    client_p, autospawn_p = _patch_stop_daemon_client(handler)
    runner = CliRunner()
    with client_p, autospawn_p:
        result = runner.invoke(cli, ["stop", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["action"] == "no_recording"
    assert payload["error"] == "not_owned_by_daemon"


# --- missing [record] extras tests ---


def test_info_missing_record_deps(tmp_path):
    """info should show helpful message when recording deps are missing."""
    runner = CliRunner()
    import builtins
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "screencap.metrics":
            raise ImportError("No module named 'mss'")
        return original_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        result = runner.invoke(cli, ["info", "some-recording"])
    assert result.exit_code == 1
    assert "recording dependencies" in result.output
    assert "pip install screencap[record]" in result.output


def test_download_works_without_record_deps(tmp_path):
    """download should work even without [record] extras installed."""
    runner = CliRunner()
    # download only needs requests + rich, both in base deps
    with mock.patch(
        "screencap.download.list_remote_recordings", return_value=[]
    ):
        result = runner.invoke(cli, ["download"])
    assert result.exit_code == 0
    assert "No recordings" in result.output


def test_list_works_without_record_deps(tmp_path):
    """list should work even without [record] extras installed."""
    runner = CliRunner()
    with mock.patch("screencap.catalog.get_recordings_dir", return_value=tmp_path), \
         mock.patch("screencap.cli._should_default_to_json", return_value=False):
        result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "No recordings" in result.output


# --- export command tests ---


def test_export_missing_recording(tmp_path):
    """Missing recording directory results in exit code 1."""
    rec_dir = tmp_path / "nonexistent"  # Does not exist

    runner = CliRunner()

    with mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir):
        result = runner.invoke(cli, ["export", "nonexistent"])

    assert result.exit_code == 1


def test_export_path_traversal(tmp_path):
    """Path traversal attempt results in exit code 1."""
    runner = CliRunner()

    with mock.patch(
        "screencap.config.resolve_recording_dir",
        side_effect=ValueError("Invalid recording name"),
    ):
        result = runner.invoke(cli, ["export", "../../etc"])

    assert result.exit_code == 1


def test_export_missing_db_error(tmp_path):
    """Missing recording database results in exit code 1."""
    rec_dir = tmp_path / "old-rec"
    rec_dir.mkdir()

    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch(
            "screencap.engine.capture.CaptureSession.load",
            side_effect=FileNotFoundError("Capture not found"),
        ),
    ):
        result = runner.invoke(cli, ["export", "old-rec"])

    assert result.exit_code == 1


def test_export_batch_warns_only_for_empty(tmp_path, monkeypatch):
    """Batch export shows warning only for empty recordings."""
    from screencap.engine.db import create_db, crud

    # "has-events" — real DB with events
    rec_a = tmp_path / "has-events"
    rec_a.mkdir()
    engine, Session = create_db(str(rec_a / "recording.db"))
    session = Session()
    rec = crud.insert_recording(session, {
        "timestamp": 1000.0, "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080,
        "pixel_ratio": 2.0, "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    crud.insert_action_event(session, rec, 1000.5, {
        "name": "click", "mouse_x": 100.0, "mouse_y": 200.0,
        "mouse_button_name": "left", "mouse_pressed": True,
    })
    crud.insert_action_event(session, rec, 1000.55, {
        "name": "click", "mouse_x": 100.0, "mouse_y": 200.0,
        "mouse_button_name": "left", "mouse_pressed": False,
    })
    session.close()
    engine.dispose()

    # "no-events" — real DB, empty
    rec_b = tmp_path / "no-events"
    rec_b.mkdir()
    engine2, Session2 = create_db(str(rec_b / "recording.db"))
    session2 = Session2()
    crud.insert_recording(session2, {
        "timestamp": 2000.0, "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080,
        "pixel_ratio": 2.0, "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    session2.close()
    engine2.dispose()

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ["export", "--all"])

    assert result.exit_code == 0
    assert "Recording 'no-events' contains no events" in result.output
    assert "Recording 'has-events' contains no events" not in result.output


def test_export_no_name_no_all():
    """No name and no --all results in exit code 1."""
    runner = CliRunner()
    result = runner.invoke(cli, ["export"])
    assert result.exit_code == 1


def test_export_all_with_stdout():
    """--all cannot be combined with --stdout."""
    runner = CliRunner()
    result = runner.invoke(cli, ["export", "--all", "--stdout"])
    assert result.exit_code == 1


def test_export_all_with_output():
    """--all cannot be combined with -o."""
    runner = CliRunner()
    result = runner.invoke(cli, ["export", "--all", "-o", "out.jsonl"])
    assert result.exit_code == 1


def test_export_all_no_recordings(tmp_path):
    """--all with no recordings prints message and exits cleanly."""
    runner = CliRunner()

    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["export", "--all"])

    assert result.exit_code == 0


# --- export --downloads tests ---


def _create_export_db(rec_dir):
    """Create a minimal recording.db with one click pair for export tests."""
    from screencap.engine.db import create_db, crud

    rec_dir.mkdir(parents=True, exist_ok=True)
    engine, Session = create_db(str(rec_dir / "recording.db"))
    session = Session()
    rec = crud.insert_recording(session, {
        "timestamp": 1000.0, "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080,
        "pixel_ratio": 2.0, "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    crud.insert_action_event(session, rec, 1000.5, {
        "name": "click", "mouse_x": 100.0, "mouse_y": 200.0,
        "mouse_button_name": "left", "mouse_pressed": True,
    })
    crud.insert_action_event(session, rec, 1000.55, {
        "name": "click", "mouse_x": 100.0, "mouse_y": 200.0,
        "mouse_button_name": "left", "mouse_pressed": False,
    })
    session.close()
    engine.dispose()


def test_export_downloads_only(tmp_path, monkeypatch):
    """--downloads (without --all) exports only downloaded recordings."""
    dl_dir = tmp_path / "downloads"
    rec_dir = tmp_path / "recordings"

    _create_export_db(dl_dir / "dl-rec")
    _create_export_db(rec_dir / "local-rec")

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(rec_dir))

    runner = CliRunner()
    with mock.patch("screencap.config.get_downloads_dir", return_value=dl_dir):
        result = runner.invoke(cli, ["export", "--downloads"])

    assert result.exit_code == 0
    assert (dl_dir / "dl-rec" / "events.jsonl").exists()
    assert not (rec_dir / "local-rec" / "events.jsonl").exists()


def test_export_all_and_downloads(tmp_path, monkeypatch):
    """--all --downloads exports from both recordings and downloads dirs."""
    dl_dir = tmp_path / "downloads"
    rec_dir = tmp_path / "recordings"

    _create_export_db(dl_dir / "dl-rec")
    _create_export_db(rec_dir / "local-rec")

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(rec_dir))

    runner = CliRunner()
    with mock.patch("screencap.config.get_downloads_dir", return_value=dl_dir):
        result = runner.invoke(cli, ["export", "--all", "--downloads"])

    assert result.exit_code == 0
    assert (rec_dir / "local-rec" / "events.jsonl").exists()
    assert (dl_dir / "dl-rec" / "events.jsonl").exists()


def test_export_downloads_no_recordings(tmp_path):
    """--downloads with empty downloads dir prints message and exits cleanly."""
    runner = CliRunner()

    with mock.patch("screencap.config.get_downloads_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["export", "--downloads"])

    assert result.exit_code == 0


def test_export_downloads_cannot_use_stdout():
    """--downloads cannot be combined with --stdout."""
    runner = CliRunner()
    result = runner.invoke(cli, ["export", "--downloads", "--stdout"])
    assert result.exit_code == 1


def test_export_single_by_name_with_downloads_fallback(tmp_path, monkeypatch):
    """Single recording name with --downloads falls back to downloads dir."""
    rec_dir = tmp_path / "recordings"
    dl_dir = tmp_path / "downloads"
    rec_dir.mkdir()

    _create_export_db(dl_dir / "my-dl")

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(rec_dir))

    runner = CliRunner()
    with mock.patch("screencap.config.get_downloads_dir", return_value=dl_dir):
        result = runner.invoke(cli, ["export", "my-dl", "--downloads"])

    assert result.exit_code == 0
    assert (dl_dir / "my-dl" / "events.jsonl").exists()


# --- export with V1.5 NetworkScrubPipeline tests ---


def _create_v15_export_db(rec_dir):
    """Create a recording.db with a V1.5 encrypted body (meta + ciphertext).

    A meta row alone is not enough — after the PR #157 review, the
    ``recording_has_encrypted_bodies`` predicate queries actual
    ``body_ciphertext IS NOT NULL`` rows so metadata-only V1.5
    recordings (where the user only browsed non-allowlisted hosts)
    don't trigger Keychain prompts. Tests that simulate "encrypted
    recording" must therefore insert at least one ciphertext row.
    """
    from screencap.engine.db import create_db, crud
    from screencap.network import crypto

    rec_dir.mkdir(parents=True, exist_ok=True)
    engine, Session = create_db(str(rec_dir / "recording.db"))
    session = Session()
    rec = crud.insert_recording(session, {
        "timestamp": 1000.0, "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080,
        "pixel_ratio": 2.0, "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    crud.insert_action_event(session, rec, 1000.5, {
        "name": "click", "mouse_x": 100.0, "mouse_y": 200.0,
        "mouse_button_name": "left", "mouse_pressed": True,
    })
    crud.insert_action_event(session, rec, 1000.55, {
        "name": "click", "mouse_x": 100.0, "mouse_y": 200.0,
        "mouse_button_name": "left", "mouse_pressed": False,
    })
    # V1.5 wrapped DEK — every --network recording gets one of these.
    kek = crypto._generate_kek()
    dek = crypto.generate_dek()
    wrapped, nonce = crypto.wrap_dek(dek, kek)
    crud.insert_network_event_meta(
        session,
        recording_id=rec.id,
        dek_wrapped=wrapped,
        dek_nonce=nonce,
    )
    # ALSO insert a ciphertext-bearing network_event row so
    # recording_has_encrypted_bodies returns True. This is what marks
    # the recording as "actually has encrypted bodies on disk" rather
    # than just "V1.5 vintage."
    crud.insert_network_event(session, rec, {
        "kind": "request",
        "flow_id": "f1",
        "method": "POST",
        "url": "https://api.github.com/x",
        "host": "api.github.com",
        "body_ciphertext": b"\xde\xad\xbe\xef" * 4,
        "body_nonce": b"\x01" * 12,
        "body_aad": b"some-aad",
        "timestamp": 1000.7,
        "timestamp_ns": 1_000_700_000_000,
    })
    crud.flush_buffers(session)
    session.commit()
    session.close()
    engine.dispose()
    return kek


def test_export_with_encrypted_recording_constructs_pipeline(
    tmp_path, monkeypatch,
):
    """When the recording has a NetworkEventMeta row, _export_one
    constructs a NetworkScrubPipeline and forwards it to export_recording."""
    rec_dir = tmp_path / "recordings"
    _create_v15_export_db(rec_dir / "v15-rec")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(rec_dir))

    runner = CliRunner()

    # Stub out the actual NetworkScrubPipeline so we don't need the
    # real Keychain (and to capture the construction args). Patch the
    # symbol where it's looked up inside _export_one (re-imported there
    # from screencap.network.export_pipeline).
    captured = {}

    class _StubPipeline:
        def __init__(self, db_path, recording_id):
            captured["db_path"] = db_path
            captured["recording_id"] = recording_id

        def decrypt_and_scrub(self, evt):  # pragma: no cover - unused
            return evt

    with (
        mock.patch(
            "screencap.network.export_pipeline.NetworkScrubPipeline",
            _StubPipeline,
        ),
        mock.patch(
            "screencap.exporter.export_recording",
            return_value=2,
        ) as mock_export,
    ):
        # Use --stdout to opt into network row emission. Default output
        # (<recording_dir>/events.jsonl) is cloud-safe by gate per the
        # round-4 review fix; only explicit -o / --stdout enables the
        # network rows.
        result = runner.invoke(cli, ["export", "v15-rec", "--stdout"])

    assert result.exit_code == 0, result.output
    assert "db_path" in captured, "Pipeline was not constructed"
    assert captured["db_path"].endswith("recording.db")
    # Pipeline forwarded as the new kwarg on export_recording.
    mock_export.assert_called_once()
    kwargs = mock_export.call_args.kwargs
    assert "network_scrub_pipeline" in kwargs
    assert kwargs["network_scrub_pipeline"] is not None
    # V1.5 P1 #2: explicit-export must opt in to network row emission.
    # Without this flag, network_scrub_pipeline construction is wasted
    # because Capture.export_events skips network_rows entirely.
    assert kwargs.get("include_network") is True


def test_export_default_output_skips_network_rows(tmp_path, monkeypatch):
    """Round-4 P1: ``screencap export <name>`` with no -o writes to
    ``<recording_dir>/events.jsonl`` — the same file ``screencap upload``
    later picks up. Network rows must NOT land there, otherwise a
    later upload leaks them to cloud (bypasses the V1.5 cloud-safety
    gate that was supposed to keep network metadata local until
    V1.75 ships build_cloud_network_filter).
    """
    rec_dir = tmp_path / "recordings"
    _create_v15_export_db(rec_dir / "v15-rec")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(rec_dir))

    runner = CliRunner()
    pipeline_constructions = []

    class _StubPipeline:
        def __init__(self, db_path, recording_id):
            pipeline_constructions.append((db_path, recording_id))

        def decrypt_and_scrub(self, evt):  # pragma: no cover - unused
            return evt

    with (
        mock.patch(
            "screencap.network.export_pipeline.NetworkScrubPipeline",
            _StubPipeline,
        ),
        mock.patch(
            "screencap.exporter.export_recording",
            return_value=0,
        ) as mock_export,
    ):
        # No -o, no --stdout → default = recording_dir/events.jsonl.
        result = runner.invoke(cli, ["export", "v15-rec"])

    assert result.exit_code == 0, result.output
    assert mock_export.called
    kwargs = mock_export.call_args.kwargs
    assert kwargs.get("include_network") is False, (
        "default output writes to the same file `screencap upload` "
        "uses; including network rows there leaks them to cloud"
    )
    # No pipeline construction either — saves the Keychain prompt.
    assert pipeline_constructions == [], (
        "pipeline construction wasted on a path that drops network "
        "rows; should be gated on include_network"
    )


def test_export_with_custom_output_includes_network(tmp_path, monkeypatch):
    """``screencap export <name> -o /tmp/out.jsonl`` writes to a
    user-controlled path — outside the cloud-pickup pipeline. Network
    rows ARE emitted there because the user opted in by directing
    output elsewhere.
    """
    rec_dir = tmp_path / "recordings"
    _create_v15_export_db(rec_dir / "v15-rec")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(rec_dir))

    runner = CliRunner()
    custom_output = str(tmp_path / "custom-out.jsonl")

    class _StubPipeline:
        def __init__(self, db_path, recording_id):
            pass

        def decrypt_and_scrub(self, evt):  # pragma: no cover
            return evt

    with (
        mock.patch(
            "screencap.network.export_pipeline.NetworkScrubPipeline",
            _StubPipeline,
        ),
        mock.patch(
            "screencap.exporter.export_recording",
            return_value=2,
        ) as mock_export,
    ):
        result = runner.invoke(cli, ["export", "v15-rec", "-o", custom_output])

    assert result.exit_code == 0, result.output
    kwargs = mock_export.call_args.kwargs
    assert kwargs.get("include_network") is True


def test_export_all_skips_network_rows(tmp_path, monkeypatch):
    """``screencap export --all`` writes every recording's events.jsonl
    to ``<recording_dir>/events.jsonl`` — same cloud-pickup path as
    the no-arg single-recording export. Must NOT include network rows.
    """
    rec_dir = tmp_path / "recordings"
    _create_v15_export_db(rec_dir / "v15-rec")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(rec_dir))

    runner = CliRunner()

    class _StubPipeline:
        def __init__(self, db_path, recording_id):
            pass

        def decrypt_and_scrub(self, evt):  # pragma: no cover
            return evt

    with (
        mock.patch(
            "screencap.network.export_pipeline.NetworkScrubPipeline",
            _StubPipeline,
        ),
        mock.patch(
            "screencap.exporter.export_recording",
            return_value=2,
        ) as mock_export,
    ):
        result = runner.invoke(cli, ["export", "--all"])

    assert result.exit_code == 0, result.output
    assert mock_export.called
    # Every call from --all path must have include_network=False.
    for call in mock_export.call_args_list:
        assert call.kwargs.get("include_network") is False


def test_export_with_v1_recording_no_pipeline(tmp_path, monkeypatch):
    """V1-vintage recording (no NetworkEventMeta row) → no pipeline
    construction; export_recording receives ``None``."""
    rec_dir = tmp_path / "recordings"
    _create_export_db(rec_dir / "v1-rec")  # No meta row
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(rec_dir))

    runner = CliRunner()

    construction_calls = []

    class _StubPipeline:
        def __init__(self, db_path, recording_id):
            construction_calls.append((db_path, recording_id))

        def decrypt_and_scrub(self, evt):  # pragma: no cover - unused
            return evt

    with (
        mock.patch(
            "screencap.network.export_pipeline.NetworkScrubPipeline",
            _StubPipeline,
        ),
        mock.patch(
            "screencap.exporter.export_recording",
            return_value=2,
        ) as mock_export,
    ):
        result = runner.invoke(cli, ["export", "v1-rec"])

    assert result.exit_code == 0, result.output
    assert construction_calls == [], (
        "NetworkScrubPipeline must NOT be constructed for V1-vintage "
        "recordings (no NetworkEventMeta row)"
    )
    kwargs = mock_export.call_args.kwargs
    assert kwargs.get("network_scrub_pipeline") is None


def test_auto_export_does_not_include_network(tmp_path, monkeypatch):
    """Cloud-safety guarantee: ``_auto_export`` (post-recording, feeds
    ``screencap upload``) must NOT pass ``include_network=True``. V1.5
    keeps network row emission gated to the explicit ``screencap export``
    CLI path; V1.75 will land the cloud-bound filter factory before any
    network row reaches a cloud bucket.
    """
    from screencap.cli import _auto_export

    rec_dir = tmp_path / "auto-rec"
    rec_dir.mkdir()
    # Touch a recording.db file so export_recording's existence check
    # short-circuits to the mock without actually loading anything.

    with mock.patch(
        "screencap.exporter.export_recording", return_value=0,
    ) as mock_export:
        # _auto_export catches its own exceptions; we just need to
        # confirm the call shape regardless of the result.
        _auto_export(rec_dir)

    assert mock_export.called
    kwargs = mock_export.call_args.kwargs
    # The flag must be absent OR False — never True from _auto_export.
    assert not kwargs.get("include_network", False), (
        "_auto_export must not include_network=True; that flag is "
        "reserved for the explicit `screencap export` CLI path until "
        "V1.75 ships build_cloud_network_filter."
    )


def test_export_kek_unavailable_fails_loud(tmp_path, monkeypatch):
    """When KEK is unavailable (Keychain failure), CLI export exits
    non-zero with the actionable regenerate message."""
    from screencap.network.export_pipeline import KekUnavailableError

    rec_dir = tmp_path / "recordings"
    _create_v15_export_db(rec_dir / "v15-rec")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(rec_dir))

    runner = CliRunner()

    with mock.patch(
        "screencap.network.export_pipeline.NetworkScrubPipeline",
        side_effect=KekUnavailableError("keychain locked"),
    ):
        # --stdout opts into network row emission, which is what
        # triggers the pipeline construction. With the default
        # output, pipeline construction is skipped (cloud-safe),
        # so the KEK-unavailable branch wouldn't fire.
        result = runner.invoke(cli, ["export", "v15-rec", "--stdout"])

    assert result.exit_code == 1, result.output
    # The actionable error message must mention the regenerate path.
    # rich's console wraps long lines, so collapse whitespace before matching.
    flattened = " ".join(result.output.split())
    assert "Cannot decrypt network bodies" in flattened
    assert "screencap network uninstall" in flattened


def test_export_pipeline_setup_unexpected_error_drops_network_rows(
    tmp_path, monkeypatch,
):
    """When pipeline construction raises something other than
    KekUnavailableError (defensive fallback path), CLI export must
    DROP network rows from the output rather than feed unscrubbed
    capture-side events with body_ciphertext bytes to Pydantic's JSON
    serializer.

    Regression for PR #157 review P1: previously the broad-Exception
    branch fell through with include_network=True and pipeline=None,
    which violated the V1.5 schema invariant 'ciphertext can never
    reach JSONL by construction.' The fix is to set
    include_network=False on that branch and warn the user.
    """
    rec_dir = tmp_path / "recordings"
    _create_v15_export_db(rec_dir / "v15-rec")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(rec_dir))

    runner = CliRunner()

    with (
        mock.patch(
            "screencap.network.export_pipeline.NetworkScrubPipeline",
            side_effect=RuntimeError("simulated unexpected pipeline error"),
        ),
        mock.patch(
            "screencap.exporter.export_recording", return_value=0,
        ) as mock_export,
    ):
        # --stdout opts into network rows, which is what makes the
        # pipeline construction path run at all.
        result = runner.invoke(cli, ["export", "v15-rec", "--stdout"])

    # The export still runs (don't crash on unexpected pipeline
    # errors) — but it MUST NOT include network rows.
    assert result.exit_code == 0, result.output
    assert mock_export.called
    kwargs = mock_export.call_args.kwargs
    assert kwargs.get("include_network") is False, (
        "broad-Exception fallback must drop network rows entirely; "
        "leaving include_network=True with pipeline=None feeds "
        "ciphertext bytes to the JSONL writer"
    )
    assert kwargs.get("network_scrub_pipeline") is None
    # User sees a yellow warning so they know network events were
    # silently omitted.
    flattened = " ".join(result.output.split())
    assert "Warning" in flattened
    assert "omit network events" in flattened


# --- upload auto-export tests ---


def _make_upload_recording(base, name, *, with_jsonl=False):
    """Create a minimal recording dir for upload tests."""
    rec_dir = base / name
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / "recording.db").touch()
    if with_jsonl:
        (rec_dir / "events.jsonl").write_text('{"_meta":true}\n')
    return rec_dir


def _terminal_result(**kw):
    """A TerminalResult with cloud-converged defaults for the upload tests."""
    from screencap.terminal_stage import TerminalResult

    defaults = dict(
        destination="cloud", routed=True, n_uploaded=2, n_skipped=0,
        sentinel_uploaded=True,
    )
    defaults.update(kw)
    return TerminalResult(**defaults)


def test_upload_routes_through_terminal_stage(tmp_path):
    """SCR-125 U5: upload drives the SINGLE terminal stage with an explicit
    cloud promotion — no direct scrub_recording / upload_recording call."""
    rec_dir = _make_upload_recording(tmp_path, "rec-a")
    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result(n_uploaded=3))

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "rec-a"])

    from screencap.pipeline_policy import Destination

    assert result.exit_code == 0
    fake.assert_called_once()
    _, kwargs = fake.call_args
    assert kwargs["force_destination"] == Destination.CLOUD
    assert kwargs["dry_run"] is False
    assert kwargs["retention_override"] is None
    assert "Uploaded rec-a" in result.output


def test_upload_exits_nonzero_when_recording_fails(tmp_path):
    """SCR-79: a recording the terminal stage reports as failed (``upload_warning``
    / ``failed_indices`` — the per-file ``upload_failed`` path) makes ``screencap
    upload`` exit non-zero, so a consumer that trusts the exit code (the U7 Swift
    UploadController reads ``terminationStatus`` alongside the stderr event) agrees
    with the terminal event instead of reading a per-file failure as success."""
    rec_dir = _make_upload_recording(tmp_path, "rec-fail")
    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result(
        sentinel_uploaded=False,
        failed_indices=[0],
        upload_warning=(
            "1 file(s) failed to upload — sentinel withheld, local media preserved"
        ),
    ))

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "rec-fail"])

    assert result.exit_code == 1
    # The failure is reported, never a misleading success line.
    assert "Uploaded rec-fail" not in result.output


def test_upload_warning_only_exits_nonzero(tmp_path):
    """SCR-79: the ``upload_warning``-only arm of the CLI guard
    ``if result.failed_indices or result.upload_warning:``. A per-file
    ``upload_failed`` surfaces as an ``upload_warning`` from _route_cloud WITHOUT
    ``failed_indices`` (those come from the ledger, not ``upload_result.failed``),
    so the warning alone must still exit non-zero and never print 'Uploaded'."""
    rec_dir = _make_upload_recording(tmp_path, "rec-warn")
    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result(
        sentinel_uploaded=False,
        upload_warning=(
            "1 file(s) failed to upload — sentinel withheld, local media preserved"
        ),
    ))

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "rec-warn"])

    assert result.exit_code == 1
    assert "Uploaded" not in result.output


def test_upload_promotion_refused_exits_nonzero(tmp_path):
    """AE8 + SCR-79: a recording with holes → PromotionRefused is non-fatal to the
    BATCH (it continues to the next recording, no traceback), but a refused upload
    is still a failure, so the command exits non-zero."""
    rec_dir = _make_upload_recording(tmp_path, "rec-holes")
    runner = CliRunner()
    from screencap.terminal_stage import PromotionRefused

    def _raise(d, **kw):
        raise PromotionRefused("chunk(s) 1 are missing locally and unconfirmable")

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", _raise),
    ):
        result = runner.invoke(cli, ["upload", "rec-holes"])

    assert result.exit_code == 1
    assert "missing locally" in result.output
    assert "skipped" in result.output.lower()
    assert "Traceback" not in result.output


def test_upload_dry_run_passes_dry_run(tmp_path):
    """--dry-run threads dry_run into run_terminal_stage (a read-only preview)."""
    rec_dir = _make_upload_recording(tmp_path, "rec-d")
    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result(routed=True))

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "rec-d", "--dry-run"])

    assert result.exit_code == 0
    _, kwargs = fake.call_args
    assert kwargs["dry_run"] is True
    assert "Would upload rec-d" in result.output


def test_upload_no_delete_passes_keep_forever_override(tmp_path):
    """--no-delete wires a keep_forever retention override into the run."""
    rec_dir = _make_upload_recording(tmp_path, "rec-keep")
    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result())

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "rec-keep", "--no-delete"])

    from screencap.pipeline_policy import RetentionPolicy

    assert result.exit_code == 0
    _, kwargs = fake.call_args
    assert kwargs["retention_override"] == RetentionPolicy.KEEP_FOREVER


def test_upload_busy_shows_friendly_message(tmp_path):
    """TerminalStageBusy (a live finalize / daemon resume holds the lock) → a
    friendly 'in progress' message, not a traceback."""
    rec_dir = _make_upload_recording(tmp_path, "rec-busy")
    runner = CliRunner()
    from screencap.terminal_stage import TerminalStageBusy

    def _busy(d, **kw):
        raise TerminalStageBusy("held")

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", _busy),
    ):
        result = runner.invoke(cli, ["upload", "rec-busy"])

    assert result.exit_code == 0
    assert "in progress" in result.output
    assert "Traceback" not in result.output


def test_upload_busy_emits_structured_stderr_event(tmp_path):
    """SCR-158: a ``TerminalStageBusy`` skip must emit a structured ``upload_busy``
    stderr event (``retryable=True``) so the event-first Swift UploadController and
    autonomous agents can tell a retryable busy-lock apart from a silent no-op —
    instead of exit-0-without-a-terminal-event being rendered as a hard failure.
    Stays exit 0, and is deliberately NOT an ``upload_failed`` event (SCR-79 ties
    that event to a non-zero exit)."""
    rec_dir = _make_upload_recording(tmp_path, "rec-busy")
    # Click 8.2 always captures stderr separately; result.stderr holds the
    # structured lifecycle events (emit_event writes to sys.stderr).
    runner = CliRunner()
    from screencap.terminal_stage import TerminalStageBusy

    def _busy(d, **kw):
        raise TerminalStageBusy("held")

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", _busy),
    ):
        result = runner.invoke(cli, ["upload", "rec-busy"])

    assert result.exit_code == 0
    events = [
        json.loads(line)
        for line in result.stderr.splitlines()
        if line.strip().startswith("{")
    ]
    busy = [e for e in events if e.get("type") == "upload_busy"]
    assert busy, f"expected an upload_busy event on stderr, got: {result.stderr!r}"
    assert busy[0]["retryable"] is True
    assert busy[0]["recording"] == "rec-busy"
    # SCR-79: a busy skip is exit 0, so it must not masquerade as upload_failed.
    assert not any(e.get("type") == "upload_failed" for e in events)


def test_upload_busy_plus_success_exits_zero(tmp_path):
    """SCR-79: in a multi-recording batch, a ``TerminalStageBusy`` skip is
    retryable (n_busy), not a failure — so a batch where one recording is busy and
    the other uploads cleanly still exits 0, and the Done summary reports the busy
    one as 'in progress'."""
    rec_busy = _make_upload_recording(tmp_path, "rec-busy")
    rec_ok = _make_upload_recording(tmp_path, "rec-ok")
    runner = CliRunner()
    from screencap.terminal_stage import TerminalStageBusy

    def _busy_then_ok(d, **kw):
        if d.name == "rec-busy":
            raise TerminalStageBusy("held")
        return _terminal_result(routed=True, sentinel_uploaded=True)

    with (
        mock.patch(
            "screencap.upload.resolve_recording_dirs",
            return_value=[rec_busy, rec_ok],
        ),
        mock.patch("screencap.terminal_stage.run_terminal_stage", _busy_then_ok),
    ):
        result = runner.invoke(cli, ["upload", "rec-busy", "rec-ok"])

    assert result.exit_code == 0
    assert "in progress" in result.output


def test_upload_sigterm_during_prep_emits_interrupted(tmp_path):
    """SCR-94: a SIGTERM during the pre-upload prep phase — run_terminal_stage's
    reconcile / recovery / scrub, which runs BEFORE upload_recording installs its
    own SIGTERM handler — must still emit a terminal ``upload_failed`` event.

    Without the top-level handler the child dies on the default SIGTERM
    disposition with no event, and the SwiftUI UploadController surfaces a raw
    "exited with code N" instead of a clean cancel.
    """
    import os
    import signal

    rec_dir = _make_upload_recording(tmp_path, "rec-cancel")
    runner = CliRunner()

    def _sigterm_mid_prep(d, **kw):
        # Stand in for the review-window-close SIGTERM landing while the terminal
        # stage is still scrubbing/exporting. The command's handler fires during
        # the sleep, emits the terminal event, and raises KeyboardInterrupt — so
        # the sleep never completes and the AssertionError is never reached.
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
        raise AssertionError("SIGTERM handler did not interrupt the prep phase")

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", _sigterm_mid_prep),
    ):
        result = runner.invoke(cli, ["upload", "rec-cancel"])

    # Cancelled → non-zero exit, but a clean one (no traceback). The Swift side
    # keys off the terminal event, not the exit code.
    assert result.exit_code != 0
    assert "Traceback" not in result.output

    events = [
        json.loads(line)
        for line in result.stderr.splitlines()
        if line.strip().startswith("{")
    ]
    failed = [e for e in events if e.get("type") == "upload_failed"]
    # Exactly one terminal event — the command's handler and upload_recording's
    # own handler are never both installed at once (SCR-94), so cancel can't
    # double-emit.
    assert len(failed) == 1, f"expected one upload_failed, got {events}"
    assert failed[0]["error"] == "interrupted"
    assert failed[0]["recording"] == "rec-cancel"
    # We cancelled before the transfer phase, so upload_started never fired.
    assert not any(e.get("type") == "upload_started" for e in events)


def test_upload_restores_sigterm_handler_after_run(tmp_path):
    """SCR-94: the command must restore the previous SIGTERM disposition on exit
    so a long-lived host process (or a back-to-back invocation) is never left
    with the upload command's handler permanently installed."""
    import signal

    rec_dir = _make_upload_recording(tmp_path, "rec-restore")
    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result())

    previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        with (
            mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
            mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
        ):
            result = runner.invoke(cli, ["upload", "rec-restore"])
        assert result.exit_code == 0
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_upload_warns_but_proceeds_for_local_intent_in_all_mode(tmp_path):
    """`upload --all` no longer skips local-intent recordings.

    An explicit upload overrides the recorded intent (the data becomes
    cloud-bound by user choice at upload time), so a local-intent recording
    surfaces a weaker-guarantees warning and then proceeds to scrub + upload.
    """
    rec_dir = tmp_path / "local-rec"
    rec_dir.mkdir(parents=True)
    (rec_dir / "recording.db").touch()
    (rec_dir / "events.jsonl").write_text('{"_meta":true}\n')
    # Write a local-intent file
    intent = {"destination": "local", "source": "flag"}
    (rec_dir / ".recording_intent").write_text(json.dumps(intent))

    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result())
    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "--all"])

    from screencap.pipeline_policy import Destination

    assert result.exit_code == 0
    # Local intent surfaces a weaker-guarantees warning...
    assert "local-intent" in result.output
    assert "post-hoc scrubbing" in result.output
    # ...but the upload still proceeds as an EXPLICIT cloud promotion (U5).
    fake.assert_called_once()
    promoted_dir, kwargs = fake.call_args
    assert promoted_dir[0] == rec_dir
    assert kwargs["force_destination"] == Destination.CLOUD


def test_upload_cloud_intent_proceeds(tmp_path):
    """upload with cloud intent converges through the terminal stage (no prompt)."""
    rec_dir = tmp_path / "cloud-rec"
    rec_dir.mkdir(parents=True)
    (rec_dir / "recording.db").touch()
    (rec_dir / "events.jsonl").write_text('{"_meta":true}\n')
    # Write a cloud-intent file
    intent = {"destination": "cloud", "source": "flag"}
    (rec_dir / ".recording_intent").write_text(json.dumps(intent))

    runner = CliRunner()
    fake = mock.MagicMock(return_value=_terminal_result())
    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.terminal_stage.run_terminal_stage", fake),
    ):
        result = runner.invoke(cli, ["upload", "cloud-rec"])

    assert result.exit_code == 0
    # The terminal stage was driven (no direct upload_recording / scrub call).
    fake.assert_called_once()
    # No interactive scrub prompt.
    assert "Continue without scrubbing?" not in result.output


# --- _smoke-test command tests ---


def test_smoke_test_hidden_from_help():
    """_smoke-test should not appear in --help output."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "_smoke-test" not in result.output


def _make_failing_check():
    """Return a check function that fails with a traceback."""
    def _check_broken():
        import traceback as _tb
        try:
            raise RuntimeError("broken subsystem")
        except Exception:
            return "broken_check", False, _tb.format_exc()
    return _check_broken


def test_smoke_test_exits_nonzero_on_failure():
    """_smoke-test exits 1 when any check fails — this is the CI contract."""
    runner = CliRunner()
    with mock.patch(
        "screencap.cli._SMOKE_CHECKS",
        [_make_failing_check()],
    ):
        result = runner.invoke(cli, ["_smoke-test"])
    assert result.exit_code == 1
    assert "FAIL" in result.output
    # Non-verbose: shows error summary (last line of traceback), not full traceback
    assert "RuntimeError: broken subsystem" in result.output
    assert "Traceback" not in result.output


def test_smoke_test_verbose_shows_full_traceback():
    """--verbose shows the full traceback, not just the summary line."""
    runner = CliRunner()
    with mock.patch(
        "screencap.cli._SMOKE_CHECKS",
        [_make_failing_check()],
    ):
        result = runner.invoke(cli, ["_smoke-test", "--verbose"])
    assert result.exit_code == 1
    assert "Traceback" in result.output
    assert "RuntimeError: broken subsystem" in result.output


def test_smoke_test_exits_zero_on_all_pass():
    """_smoke-test exits 0 when all checks pass and reports dev/frozen mode."""
    runner = CliRunner()
    with mock.patch(
        "screencap.cli._SMOKE_CHECKS",
        [lambda: ("always_passes", True, "")],
    ):
        result = runner.invoke(cli, ["_smoke-test"])
    assert result.exit_code == 0
    assert "PASS" in result.output
    assert "dev install" in result.output


# --- _auth-config-check command tests (U2 fail-closed release guard) ---
#
# The guard checks the BUNDLED creds (auth._provisioned > placeholder), ignoring the
# env layer, so these tests inject via a fake screencap._provisioned module rather
# than env vars — mirroring how the shipped binary resolves creds for an end user.


def test_auth_config_check_hidden_from_help():
    """_auth-config-check should not appear in --help output."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "_auth-config-check" not in result.output


def test_auth_config_check_passes_when_provisioned(monkeypatch):
    """Exit 0 when the BUNDLED _provisioned module carries non-placeholder creds."""
    _install_provisioned(
        monkeypatch,
        _fake_provisioned(
            FIREBASE_API_KEY="AIzaSyRealLookingWebKey",
            OAUTH_CLIENT_ID="123456789.apps.googleusercontent.com",
        ),
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["_auth-config-check"], env={"SCREENCAP_RELEASE_BUILD": "1"})
    assert result.exit_code == 0
    assert "provisioned" in result.output


def test_auth_config_check_fails_release_build_with_placeholders(monkeypatch):
    """Exit non-zero when a release build's bundled creds are the REPLACE_WITH_PROVISIONED_* sentinels."""
    _install_provisioned(monkeypatch, None)  # nothing bundled → placeholders
    runner = CliRunner()
    result = runner.invoke(cli, ["_auth-config-check"], env={"SCREENCAP_RELEASE_BUILD": "1"})
    assert result.exit_code == 1
    assert "FAILED" in result.output


def test_auth_config_check_ignores_env_creds(monkeypatch):
    """Anti-masking: env-var creds must NOT satisfy the guard when nothing is bundled.

    An end user has no env override, so a build-shell env var (or a stray .env read by
    load_dotenv) cannot be allowed to hide a _provisioned bundling failure."""
    _install_provisioned(monkeypatch, None)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["_auth-config-check"],
        env={
            "SCREENCAP_RELEASE_BUILD": "1",
            "SCREENCAP_FIREBASE_API_KEY": "AIzaSyRealLookingWebKey",
            "SCREENCAP_OAUTH_CLIENT_ID": "123456789.apps.googleusercontent.com",
        },
    )
    assert result.exit_code == 1
    assert "FAILED" in result.output


def test_auth_config_check_passes_dev_build_with_placeholders(monkeypatch):
    """PR/dev builds (SCREENCAP_RELEASE_BUILD unset) stay green with placeholders."""
    _install_provisioned(monkeypatch, None)
    runner = CliRunner()
    result = runner.invoke(cli, ["_auth-config-check"], env={"SCREENCAP_RELEASE_BUILD": None})
    assert result.exit_code == 0
    assert "dev/PR build" in result.output


def _collect_followup_output(recording_name, capture_dir):
    """Run print_upload_followup and return the joined console.print args."""
    from screencap.recorder import print_upload_followup

    with mock.patch("screencap.recorder.console.print") as mock_print:
        print_upload_followup(recording_name, capture_dir)
    return "\n".join(
        " ".join(str(a) for a in call.args)
        for call in mock_print.call_args_list
    )


def test_print_upload_followup_uses_final_name_after_rename(tmp_path):
    """Follow-up warning must name the final (post-rename) directory."""
    renamed_dir = tmp_path / "my-awesome-task"
    renamed_dir.mkdir()
    (renamed_dir / ".upload_followup.json").write_text(json.dumps({
        "kind": "partial",
        "n_uploaded": 2,
        "n_total": 3,
        "upload_warning": None,
    }))

    output = _collect_followup_output("my-awesome-task", renamed_dir)

    assert "screencap upload my-awesome-task" in output
    assert "2 of 3 chunks uploaded" in output
    assert not (renamed_dir / ".upload_followup.json").exists()


def test_print_upload_followup_noop_without_marker(tmp_path):
    """Missing .upload_followup.json is a no-op."""
    output = _collect_followup_output("whatever", tmp_path)
    assert output == ""


def test_print_upload_followup_force_stopped_message(tmp_path):
    """force_stopped kind emits the timeout message."""
    (tmp_path / ".upload_followup.json").write_text(json.dumps({
        "kind": "force_stopped",
        "n_uploaded": 0,
        "n_total": 0,
        "upload_warning": None,
    }))
    output = _collect_followup_output("rec-X", tmp_path)
    assert "processing timed out" in output
    assert "screencap upload rec-X" in output


def test_cloud_function_filename_regex_allows_marker():
    """Regression: _unlisted marker must match the server filename regex."""
    import re
    # Mirror the regex at scripts/cloud-function/main.py:47 — duplicated here
    # so the test does not need to import the cloud-function module (which
    # pulls in Flask / GCP clients not available in the dev test env).
    filename_re = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._/-]{0,511}$")
    assert filename_re.match("_unlisted")
    assert filename_re.match("chunk_0000.mp4")
    assert filename_re.match("screenshots/0.jpg")
    # Hidden dotfiles and traversal must still be rejected
    assert not filename_re.match(".hidden")
    assert not filename_re.match("-leading-dash")
    assert not filename_re.match("/absolute/path")


# ---------------------------------------------------------------------------
# Legacy `screencap start` flag/path tests
#
# These tests exercise the in-process ``recorder.start_recording`` call
# path that Phase 2 U1.6 removed. The daemon-side equivalents live in
# ``tests/cli/test_start_daemon_client.py`` (CLI translation) and
# ``tests/daemon/test_control_verbs.py`` (engine spawn semantics).
# Kept as skipped scaffolding so future readers see the migration.
# ---------------------------------------------------------------------------


pytestmark_legacy_start = pytest.mark.skip(
    reason=(
        "Phase 2 U1.6: in-process ``recorder.start_recording`` path removed "
        "from screencap start; see tests/cli/test_start_daemon_client.py."
    )
)


@pytestmark_legacy_start
@pytest.mark.parametrize("flag,kwarg,expected", [
    ("--no-video", "capture_video", False),
    ("--no-images", "capture_images", False),
    ("--no-window-data", "capture_window_data", False),
    ("--no-wifi-metrics", "wifi_metrics", False),
    ("--no-app-versions", "app_versions", False),
])
def test_start_capture_flag_propagates_to_recorder(tmp_path, flag, kwarg, expected):
    """Each capture-opt-out flag flows through to the matching start_recording kwarg."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with _safe_start_prompts(), mock.patch(
        "screencap.recorder.start_recording",
        return_value=(fake_dir, 42.0, None, None),
    ) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test-rec", flag])
    assert result.exit_code == 0, result.output
    _, kwargs = mock_rec.call_args
    assert kwargs[kwarg] is expected


@pytestmark_legacy_start
@pytest.mark.parametrize("args,expected_cloud,expected_source", [
    ([], False, "non_interactive_default"),
    (["--cloud"], True, "flag"),
    (["--local"], False, "flag"),
])
def test_start_intent_flag_resolves_to_recorder_kwargs(
    tmp_path, args, expected_cloud, expected_source,
):
    """--cloud / --local / no-flag map to cloud_intent + intent_source on the recorder call.
    Cloud also forces force_mode=PrivacyMode.PUBLIC."""
    from screencap.privacy.policy import PrivacyMode

    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with (
        _safe_start_prompts(),
        mock.patch("screencap.redaction.engine.are_nlp_models_cached", return_value=True),
        mock.patch(
            "screencap.recorder.start_recording",
            return_value=(fake_dir, 42.0, None, None),
        ) as mock_rec,
    ):
        result = runner.invoke(cli, ["start", "--name", "test", *args])
    assert result.exit_code == 0, result.output
    _, kwargs = mock_rec.call_args
    assert kwargs["cloud_intent"] is expected_cloud
    assert kwargs["intent_source"] == expected_source
    assert kwargs["force_mode"] == (PrivacyMode.PUBLIC if expected_cloud else None)


@pytestmark_legacy_start
def test_start_cloud_then_local_resolves_to_local(tmp_path):
    """Click flag_value semantics: when both --cloud and --local are passed,
    the last one on the command line wins. Regression for an earlier bug
    where the order was reversed."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with _safe_start_prompts(), mock.patch(
        "screencap.recorder.start_recording",
        return_value=(fake_dir, 42.0, None, None),
    ) as mock_rec:
        result = runner.invoke(
            cli, ["start", "--name", "test", "--cloud", "--local"],
        )
    assert result.exit_code == 0, result.output
    _, kwargs = mock_rec.call_args
    assert kwargs["cloud_intent"] is False
    assert kwargs["force_mode"] is None


@pytestmark_legacy_start
def test_start_disk_full_skips_auto_naming_but_prints_summary(tmp_path):
    """DiskFullError from start_recording is caught: the post-recording pipeline
    skips auto-naming/transcription, but the summary still prints so the user
    sees what was captured."""
    from screencap.recorder import DiskFullError

    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()
    with (
        _safe_start_prompts(),
        mock.patch(
            "screencap.recorder.start_recording",
            side_effect=DiskFullError(fake_dir, 42.0),
        ),
        mock.patch("screencap.namer.auto_name") as mock_namer,
    ):
        result = runner.invoke(cli, ["start", "--name", "rec-test"])
    assert result.exit_code == 0
    assert "Skipping auto-naming/transcription" in result.output
    assert "Recording complete" in result.output
    mock_namer.assert_not_called()


@pytestmark_legacy_start
def test_start_auto_export_called_with_capture_dir(tmp_path):
    """Auto-export runs after recording with the capture dir."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()
    with (
        _safe_start_prompts(),
        mock.patch(
            "screencap.recorder.start_recording",
            return_value=(fake_dir, 42.0, None, None),
        ),
        mock.patch("screencap.namer.auto_name", return_value=fake_dir),
        mock.patch("screencap.cli._auto_export") as mock_auto_export,
    ):
        result = runner.invoke(cli, ["start", "--name", "rec-test"])
    assert result.exit_code == 0, result.output
    assert "Recording complete" in result.output
    mock_auto_export.assert_called_once_with(fake_dir)


@pytestmark_legacy_start
def test_start_auto_export_keyboard_interrupt_does_not_abort(tmp_path):
    """Ctrl-C during auto-export prints 'Export cancelled.' and the pipeline
    continues to the summary instead of crashing."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()
    with (
        _safe_start_prompts(),
        mock.patch(
            "screencap.recorder.start_recording",
            return_value=(fake_dir, 42.0, None, None),
        ),
        mock.patch("screencap.namer.auto_name", return_value=fake_dir),
        mock.patch("screencap.cli._auto_export", side_effect=KeyboardInterrupt),
    ):
        result = runner.invoke(cli, ["start", "--name", "rec-test"])
    assert result.exit_code == 0, result.output
    assert "Export cancelled." in result.output
    assert "Recording complete" in result.output


@pytestmark_legacy_start
def test_start_auto_export_internal_failure_warns_but_succeeds(tmp_path):
    """When export_recording inside _auto_export raises, the warning path
    inside _auto_export catches it; start still finishes with exit 0 and the
    summary prints."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()
    with (
        _safe_start_prompts(),
        mock.patch(
            "screencap.recorder.start_recording",
            return_value=(fake_dir, 42.0, None, None),
        ),
        mock.patch("screencap.namer.auto_name", return_value=fake_dir),
        mock.patch(
            "screencap.exporter.export_recording", side_effect=RuntimeError("boom"),
        ),
    ):
        result = runner.invoke(cli, ["start", "--name", "rec-test"])
    assert result.exit_code == 0, result.output
    assert "Could not auto-export" in result.output
    assert "boom" in result.output
    assert "Recording complete" in result.output


# ---------------------------------------------------------------------------
# `screencap network` command group (Unit 8)
# ---------------------------------------------------------------------------


class TestNetworkCommandGroup:
    """V1 surface: `screencap network uninstall` + `screencap network restore`."""

    def test_network_help_lists_subcommands(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["network", "--help"])
        assert result.exit_code == 0
        assert "uninstall" in result.output
        assert "restore" in result.output

    def test_network_restore_no_orphans(self, tmp_path, monkeypatch):
        """No snapshots → friendly message, exit 0."""
        from unittest.mock import patch as _patch

        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        with _patch(
            "screencap.network.lifecycle.restore_orphaned_proxy_state",
            return_value=[],
        ):
            result = runner.invoke(cli, ["network", "restore"])
        assert result.exit_code == 0
        assert "No orphaned proxy state found" in result.output

    def test_network_restore_with_orphans(self, tmp_path, monkeypatch):
        """Restored snapshots → success line + per-path bullets."""
        from pathlib import Path as _Path
        from unittest.mock import patch as _patch

        monkeypatch.setenv("HOME", str(tmp_path))
        fake_paths = [_Path("/tmp/rec-1/.proxy_state.json")]
        runner = CliRunner()
        with _patch(
            "screencap.network.lifecycle.restore_orphaned_proxy_state",
            return_value=fake_paths,
        ):
            result = runner.invoke(cli, ["network", "restore"])
        assert result.exit_code == 0
        assert "Restored proxy state for 1" in result.output
        assert "/tmp/rec-1/.proxy_state.json" in result.output

    def test_network_uninstall_idempotent(self, tmp_path, monkeypatch):
        """Second invocation on already-uninstalled state succeeds silently."""
        from unittest.mock import patch as _patch

        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        with _patch(
            "screencap.network.lifecycle.full_uninstall"
        ) as full_mock:
            r1 = runner.invoke(cli, ["network", "uninstall"])
            r2 = runner.invoke(cli, ["network", "uninstall"])
        assert r1.exit_code == 0
        assert r2.exit_code == 0
        assert full_mock.call_count == 2
        assert "Done" in r1.output

    def test_network_uninstall_calls_full_uninstall(self, tmp_path, monkeypatch):
        """The CLI subcommand delegates to lifecycle.full_uninstall()."""
        from unittest.mock import patch as _patch

        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        with _patch(
            "screencap.network.lifecycle.full_uninstall"
        ) as full_mock:
            result = runner.invoke(cli, ["network", "uninstall"])
        assert result.exit_code == 0
        full_mock.assert_called_once()


class TestNetworkRemoveKekCommand:
    """V1.5 surface: `screencap network remove-kek` + safety check."""

    def _make_recording(
        self,
        recordings_dir,
        name: str,
        *,
        with_meta: bool = True,
        with_ciphertext: bool = True,
    ) -> None:
        """Create a recording dir with a recording.db.

        ``with_meta``: insert a network_event_meta row (V1.5 vintage).
        ``with_ciphertext``: insert a network_event row with non-NULL
        body_ciphertext (must be True to block remove-kek per the
        post-PR-#157 ciphertext-presence semantics).
        """
        from screencap.engine.db import (
            create_db,
            crud,
            get_session_for_path,
        )

        rec_dir = recordings_dir / name
        rec_dir.mkdir(parents=True)
        db_path = rec_dir / "recording.db"
        create_db(str(db_path))
        session = get_session_for_path(str(db_path))
        try:
            from screencap.engine.db.models import Recording
            rec = Recording(
                task_description=name,
                timestamp=1.0,
            )
            session.add(rec)
            session.commit()
            if with_meta:
                crud.insert_network_event_meta(
                    session,
                    recording_id=rec.id,
                    dek_wrapped=b"\x00" * 32,
                    dek_nonce=b"\x00" * 12,
                )
            if with_ciphertext:
                crud.insert_network_event(session, rec, {
                    "kind": "request",
                    "flow_id": "f1",
                    "method": "POST",
                    "url": "https://api.github.com/x",
                    "host": "api.github.com",
                    "body_ciphertext": b"\xde\xad\xbe\xef" * 4,
                    "body_nonce": b"\x01" * 12,
                    "body_aad": b"some-aad",
                    "timestamp": 1.0,
                    "timestamp_ns": 1_000_000_000,
                })
                crud.flush_buffers(session)
                session.commit()
        finally:
            session.close()

    def test_remove_kek_no_encrypted_recordings_succeeds(self, tmp_path, monkeypatch):
        from unittest.mock import patch as _patch

        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir()
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

        runner = CliRunner()
        with _patch("keyring.delete_password") as delete_mock:
            result = runner.invoke(cli, ["network", "remove-kek"])
        assert result.exit_code == 0
        delete_mock.assert_called_once()

    def test_remove_kek_blocked_by_encrypted_recording(self, tmp_path, monkeypatch):
        from unittest.mock import patch as _patch

        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir()
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
        self._make_recording(recordings_dir, "rec-encrypted")

        runner = CliRunner()
        with _patch("keyring.delete_password") as delete_mock:
            result = runner.invoke(cli, ["network", "remove-kek"])
        assert result.exit_code == 1
        assert "rec-encrypted" in result.output
        assert "Refusing to delete KEK" in result.output
        # Critically: the deletion is NOT performed.
        delete_mock.assert_not_called()

    def test_remove_kek_force_overrides_safety(self, tmp_path, monkeypatch):
        from unittest.mock import patch as _patch

        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir()
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
        self._make_recording(recordings_dir, "rec-encrypted")

        runner = CliRunner()
        with _patch("keyring.delete_password") as delete_mock:
            result = runner.invoke(cli, ["network", "remove-kek", "--force"])
        assert result.exit_code == 0
        assert "--force given" in result.output
        delete_mock.assert_called_once()

    def test_remove_kek_does_not_block_metadata_only_recording(
        self, tmp_path, monkeypatch,
    ):
        """V1.5 P2 fix: a recording where the user only browsed
        non-allowlisted hosts has a network_event_meta row (pre-flight
        always inserts one) but every body_ciphertext is NULL. Such a
        recording must NOT block ``network remove-kek`` — the wrapped
        DEK on disk is decryption-irrelevant.

        Before this fix, the safety scan blocked remove-kek for ANY
        V1.5 --network recording, regardless of whether ciphertext was
        actually persisted.
        """
        from unittest.mock import patch as _patch

        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir()
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
        # V1.5 metadata-only: meta row present, NO ciphertext rows.
        self._make_recording(
            recordings_dir,
            "rec-metadata-only",
            with_meta=True,
            with_ciphertext=False,
        )

        runner = CliRunner()
        with _patch("keyring.delete_password") as delete_mock:
            result = runner.invoke(cli, ["network", "remove-kek"])
        assert result.exit_code == 0, result.output
        # The deletion DID proceed — no encrypted bodies exist on disk.
        delete_mock.assert_called_once()
        assert "Refusing to delete KEK" not in result.output

    def test_remove_kek_blocked_by_unreadable_db_fail_closed(
        self, tmp_path, monkeypatch,
    ):
        """V1.5 round-4 P2: a recording.db that fails to open must
        block remove-kek (fail closed). KEK deletion is irreversible
        and silently skipping unreadable recordings could orphan
        ciphertext we never had a chance to inspect.
        """
        from unittest.mock import patch as _patch

        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir()
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
        # Create a recording dir with a corrupted recording.db.
        bad_dir = recordings_dir / "rec-corrupt"
        bad_dir.mkdir()
        (bad_dir / "recording.db").write_text("this is not a valid sqlite db")

        runner = CliRunner()
        with _patch("keyring.delete_password") as delete_mock:
            result = runner.invoke(cli, ["network", "remove-kek"])
        assert result.exit_code == 1
        # User sees the unreadable recording surfaced — silent skip
        # is exactly the fail-open mode the round-4 review flagged.
        flattened = " ".join(result.output.split())
        assert "rec-corrupt" in flattened
        assert "could not be scanned" in flattened
        delete_mock.assert_not_called()

    def test_remove_kek_force_overrides_unreadable_db(
        self, tmp_path, monkeypatch,
    ):
        """--force MUST be required to override the unreadable-DB
        fail-closed. Same contract as encrypted-recordings: the user
        explicitly accepts the risk."""
        from unittest.mock import patch as _patch

        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir()
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
        bad_dir = recordings_dir / "rec-corrupt"
        bad_dir.mkdir()
        (bad_dir / "recording.db").write_text("corrupt")

        runner = CliRunner()
        with _patch("keyring.delete_password") as delete_mock:
            result = runner.invoke(
                cli, ["network", "remove-kek", "--force"],
            )
        assert result.exit_code == 0
        flattened = " ".join(result.output.split())
        assert "could not be scanned" in flattened
        delete_mock.assert_called_once()

    def test_remove_kek_idempotent_when_kek_absent(self, tmp_path, monkeypatch):
        """No KEK in keychain → still exits 0 (idempotent)."""
        from unittest.mock import patch as _patch

        recordings_dir = tmp_path / "recordings"
        recordings_dir.mkdir()
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

        # Simulate keyring's PasswordDeleteError shape with the friendly text.
        class _FakeNoSuchPassword(Exception):
            pass
        _FakeNoSuchPassword.__name__ = "PasswordDeleteError"
        runner = CliRunner()
        with _patch(
            "keyring.delete_password",
            side_effect=_FakeNoSuchPassword("no such password"),
        ):
            result = runner.invoke(cli, ["network", "remove-kek"])
        assert result.exit_code == 0
        assert "already removed" in result.output
