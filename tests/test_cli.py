"""Tests for screencap CLI argument parsing."""

import json
import sys
import time
from unittest import mock

from click.testing import CliRunner

from screencap.cli import cli
from screencap import pidfile


def test_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "screencap" in result.output


def test_list_empty(tmp_path):
    runner = CliRunner()
    with mock.patch("screencap.catalog.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "No recordings" in result.output


def test_list_json_empty(tmp_path):
    runner = CliRunner()
    with mock.patch("screencap.catalog.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["list", "--json"])
        assert result.exit_code == 0
        # Empty list still outputs "No recordings" message
        assert "No recordings" in result.output


def test_start_auto_name(tmp_path):
    """Test that start command auto-generates a timestamp name when no --name given."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec, \
         mock.patch("screencap.namer.auto_name", return_value=fake_dir) as mock_namer:
        result = runner.invoke(cli, ["start"])
        assert result.exit_code == 0
        mock_rec.assert_called_once()
        args = mock_rec.call_args
        # Name should be a timestamp like rec-20260222T143000
        assert args[0][0].startswith("rec-")


def test_start_no_auto_name_interactive(tmp_path):
    """Test that --no-auto-name restores the old interactive prompt."""
    runner = CliRunner()
    fake_dir = tmp_path / "my-test"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec, \
         mock.patch("screencap.cli.sys") as mock_sys:
        mock_sys.stdin.isatty.return_value = True
        mock_sys.exit = sys.exit
        result = runner.invoke(
            cli,
            ["start", "--no-auto-name", "--local"],
            input="my-test\nsome desc\n",
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once()
        args = mock_rec.call_args
        assert args[0][0] == "my-test"  # name
        assert args[0][1] == "some desc"  # description


def test_start_with_flags(tmp_path):
    """Test start with all flags."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec:
        result = runner.invoke(
            cli,
            ["start", "--name", "test-rec", "--no-audio", "-d", "demo"],
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once_with(
            "test-rec", "demo", False, None,
            wifi_metrics=True, app_versions=True, force_clean=False,
            capture_video=None, capture_images=True,
            capture_window_data=None,
            verbose=False,
            chunk_duration=None, live_upload=True,
            force_mode=None, cloud_intent=False,
            intent_source="non_interactive_default",
        )


def test_start_no_wifi_metrics(tmp_path):
    """Test --no-wifi-metrics flag is passed through."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec:
        result = runner.invoke(
            cli,
            ["start", "--name", "test-rec", "--no-wifi-metrics"],
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once_with(
            "test-rec", None, True, None,
            wifi_metrics=False, app_versions=True, force_clean=False,
            capture_video=None, capture_images=True,
            capture_window_data=None,
            verbose=False,
            chunk_duration=None, live_upload=True,
            force_mode=None, cloud_intent=False,
            intent_source="non_interactive_default",
        )


def test_start_no_app_versions(tmp_path):
    """Test --no-app-versions flag is passed through."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec:
        result = runner.invoke(
            cli,
            ["start", "--name", "test-rec", "--no-app-versions"],
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once_with(
            "test-rec", None, True, None,
            wifi_metrics=True, app_versions=False, force_clean=False,
            capture_video=None, capture_images=True,
            capture_window_data=None,
            verbose=False,
            chunk_duration=None, live_upload=True,
            force_mode=None, cloud_intent=False,
            intent_source="non_interactive_default",
        )


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
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
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
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["info", "old-rec"])
    assert result.exit_code == 0
    assert "No system metrics" in result.output


def test_info_command_with_running_applications(tmp_path):
    _make_recording_dir(tmp_path, "demo", with_metrics=True)
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
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


# --- stop command tests ---


def test_stop_with_orphans():
    runner = CliRunner()
    orphans = [{"pid": 111, "name": "screen_writer"}, {"pid": 222, "name": "video_writer"}]
    with (
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=orphans),
        mock.patch("screencap.pidfile.terminate_processes", return_value=orphans),
        mock.patch("screencap.pidfile.delete_pidfile"),
    ):
        result = runner.invoke(cli, ["stop"])
    assert result.exit_code == 0
    assert "2 orphaned" in result.output
    assert "Cleaned up 2" in result.output


def test_stop_with_force_flag():
    runner = CliRunner()
    orphans = [{"pid": 333, "name": "writer"}]
    with (
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=orphans),
        mock.patch("screencap.pidfile.terminate_processes", return_value=orphans) as mock_term,
        mock.patch("screencap.pidfile.delete_pidfile"),
    ):
        result = runner.invoke(cli, ["stop", "--force"])
    assert result.exit_code == 0
    mock_term.assert_called_once_with(orphans, force=True)


# --- start --force tests ---


def test_start_force_cleans_orphans(tmp_path):
    """--force flag should auto-clean orphans before starting."""
    runner = CliRunner()
    fake_dir = tmp_path / "test"
    fake_dir.mkdir()
    orphans = [{"pid": 444, "name": "old_writer"}]
    with (
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=orphans),
        mock.patch("screencap.pidfile.terminate_processes") as mock_term,
        mock.patch("screencap.pidfile.delete_pidfile"),
        mock.patch("screencap.pidfile.write_pidfile"),
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec,
    ):
        result = runner.invoke(cli, ["start", "--name", "test", "--force"])
    assert result.exit_code == 0
    mock_rec.assert_called_once()
    _, kwargs = mock_rec.call_args
    assert kwargs["force_clean"] is True


def test_start_warns_about_orphans():
    """Without --force, start should warn and exit if orphans exist."""
    runner = CliRunner()
    orphans = [{"pid": 555, "name": "old_writer"}]
    with (
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=orphans),
        mock.patch("screencap.pidfile.terminate_processes"),
        mock.patch("screencap.pidfile.delete_pidfile"),
    ):
        # start_recording will raise SystemExit(1) when orphans found without --force
        with mock.patch("screencap.recorder.start_recording", side_effect=SystemExit(1)):
            result = runner.invoke(cli, ["start", "--name", "test"])
    assert result.exit_code == 1


# --- new capture flag tests ---


def test_start_no_video_flag(tmp_path):
    """Test --no-video flag is passed through."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test-rec", "--no-video"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    assert kwargs["capture_video"] is False


def test_start_no_images_flag(tmp_path):
    """Test --no-images flag is passed through."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test-rec", "--no-images"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    assert kwargs["capture_images"] is False


def test_start_no_window_data_flag(tmp_path):
    """Test --no-window-data flag is passed through."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test-rec", "--no-window-data"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    assert kwargs["capture_window_data"] is False


def test_start_name_skips_auto_naming(tmp_path):
    """When --name is provided, auto-naming is skipped entirely."""
    runner = CliRunner()
    fake_dir = tmp_path / "my-recording"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec, \
         mock.patch("screencap.namer.auto_name") as mock_namer:
        result = runner.invoke(cli, ["start", "--name", "my-recording"])
    assert result.exit_code == 0
    mock_namer.assert_not_called()


def test_start_local_only_flag(tmp_path):
    """Test --local-only flag is recognized."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec, \
         mock.patch("screencap.namer.auto_name", return_value=fake_dir) as mock_namer:
        result = runner.invoke(cli, ["start", "--local-only"])
    assert result.exit_code == 0
    if mock_namer.called:
        _, kwargs = mock_namer.call_args
        assert kwargs.get("local_only") is True


# --- missing [record] extras tests ---


def _import_error(name, *args, **kwargs):
    """Simulate missing recording deps by raising ImportError for specific modules."""
    raise ImportError(f"No module named '{name}'")


def test_start_missing_record_deps():
    """start should show helpful message when recording deps are missing."""
    runner = CliRunner()
    import builtins
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "screencap.recorder":
            raise ImportError("No module named 'psutil'")
        return original_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        result = runner.invoke(cli, ["start", "--name", "test"])
    assert result.exit_code == 1
    assert "recording dependencies" in result.output
    assert "pip install screencap[record]" in result.output


def test_stop_missing_record_deps():
    """stop should show helpful message when recording deps are missing."""
    runner = CliRunner()
    import builtins
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "screencap.pidfile":
            raise ImportError("No module named 'psutil'")
        return original_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        result = runner.invoke(cli, ["stop"])
    assert result.exit_code == 1
    assert "recording dependencies" in result.output
    assert "pip install screencap[record]" in result.output


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
    with mock.patch("screencap.catalog.get_recordings_dir", return_value=tmp_path):
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


def test_upload_auto_exports_missing_jsonl(tmp_path):
    """Upload auto-generates events.jsonl when it doesn't exist."""
    rec_dir = _make_upload_recording(tmp_path, "rec-a")
    runner = CliRunner()

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.upload.upload_recording") as mock_upload,
        mock.patch("screencap.exporter.export_recording", return_value=5) as mock_export,
    ):
        mock_upload.return_value = mock.MagicMock(
            uploaded=["recording.db"], skipped=[], failed=[], total_bytes=100, gcs_prefix="gs://bucket/rec-a",
        )
        result = runner.invoke(cli, ["upload", "rec-a"])

    assert result.exit_code == 0
    mock_export.assert_called_once()
    assert "Exported 5 events" in result.output


def test_upload_skips_export_when_exists(tmp_path):
    """Upload does NOT export when events.jsonl already exists."""
    rec_dir = _make_upload_recording(tmp_path, "rec-b", with_jsonl=True)
    runner = CliRunner()

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.upload.upload_recording") as mock_upload,
        mock.patch("screencap.exporter.export_recording") as mock_export,
    ):
        mock_upload.return_value = mock.MagicMock(
            uploaded=[], skipped=["events.jsonl"], failed=[], total_bytes=0, gcs_prefix=None,
        )
        result = runner.invoke(cli, ["upload", "rec-b"])

    assert result.exit_code == 0
    mock_export.assert_not_called()


def test_upload_force_reexports(tmp_path):
    """--force triggers export even when events.jsonl exists."""
    rec_dir = _make_upload_recording(tmp_path, "rec-c", with_jsonl=True)
    runner = CliRunner()

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.upload.upload_recording") as mock_upload,
        mock.patch("screencap.exporter.export_recording", return_value=3) as mock_export,
    ):
        mock_upload.return_value = mock.MagicMock(
            uploaded=["events.jsonl"], skipped=[], failed=[], total_bytes=50, gcs_prefix="gs://bucket/rec-c",
        )
        result = runner.invoke(cli, ["upload", "rec-c", "--force"])

    assert result.exit_code == 0
    mock_export.assert_called_once()


def test_upload_dry_run_skips_export(tmp_path):
    """Dry run does NOT trigger export."""
    rec_dir = _make_upload_recording(tmp_path, "rec-d")
    runner = CliRunner()

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.upload.upload_recording") as mock_upload,
        mock.patch("screencap.exporter.export_recording") as mock_export,
    ):
        mock_upload.return_value = mock.MagicMock(
            uploaded=[], skipped=[], failed=[], total_bytes=0, gcs_prefix=None,
        )
        result = runner.invoke(cli, ["upload", "rec-d", "--dry-run"])

    assert result.exit_code == 0
    mock_export.assert_not_called()


def test_upload_export_failure_continues(tmp_path):
    """Export failure warns but upload still proceeds."""
    rec_dir = _make_upload_recording(tmp_path, "rec-e")
    runner = CliRunner()

    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.upload.upload_recording") as mock_upload,
        mock.patch("screencap.exporter.export_recording", side_effect=RuntimeError("boom")),
    ):
        mock_upload.return_value = mock.MagicMock(
            uploaded=["recording.db"], skipped=[], failed=[], total_bytes=100, gcs_prefix="gs://bucket/rec-e",
        )
        result = runner.invoke(cli, ["upload", "rec-e"])

    assert result.exit_code == 0
    mock_upload.assert_called_once()
    assert "Warning" in result.output
    assert "boom" in result.output


# --- DiskFullError handling tests ---


def test_start_disk_full_skips_pipeline(tmp_path):
    """DiskFullError is caught and post-recording pipeline is skipped."""
    from pathlib import Path
    from screencap.recorder import DiskFullError

    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()

    with (
        mock.patch(
            "screencap.recorder.start_recording",
            side_effect=DiskFullError(fake_dir, 42.0),
        ),
        mock.patch("screencap.namer.auto_name") as mock_namer,
    ):
        result = runner.invoke(cli, ["start", "--name", "rec-test"])

    assert result.exit_code == 0
    assert "Skipping auto-naming/transcription" in result.output
    mock_namer.assert_not_called()


def test_start_disk_full_still_prints_summary(tmp_path):
    """print_summary() still runs after DiskFullError."""
    from screencap.recorder import DiskFullError

    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()

    with mock.patch(
        "screencap.recorder.start_recording",
        side_effect=DiskFullError(fake_dir, 42.0),
    ):
        result = runner.invoke(cli, ["start", "--name", "rec-test"])

    assert result.exit_code == 0
    # print_summary outputs "Recording complete"
    assert "Recording complete" in result.output


# --- Auto-export tests ---


def test_start_auto_export_called(tmp_path):
    """Auto-export is called during start command with correct args."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()

    with (
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)),
        mock.patch("screencap.namer.auto_name", return_value=fake_dir),
        mock.patch("screencap.cli._auto_export") as mock_auto_export,
    ):
        result = runner.invoke(cli, ["start"])
        assert result.exit_code == 0
        mock_auto_export.assert_called_once_with(fake_dir)


def test_start_auto_export_failure_does_not_crash(tmp_path):
    """Auto-export failure logs a warning but does not affect exit code."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()

    with (
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)),
        mock.patch("screencap.namer.auto_name", return_value=fake_dir),
        mock.patch("screencap.cli._auto_export", side_effect=RuntimeError("boom")),
    ):
        result = runner.invoke(cli, ["start"])
        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "boom" in result.output
        assert "Recording complete" in result.output


def test_start_auto_export_runs_with_no_auto_name(tmp_path):
    """Auto-export runs even when --no-auto-name is used."""
    runner = CliRunner()
    fake_dir = tmp_path / "my-test"
    fake_dir.mkdir()

    with (
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)),
        mock.patch("screencap.cli.sys") as mock_sys,
        mock.patch("screencap.cli._auto_export") as mock_auto_export,
    ):
        mock_sys.stdin.isatty.return_value = True
        mock_sys.exit = sys.exit
        result = runner.invoke(
            cli,
            ["start", "--no-auto-name", "--local"],
            input="my-test\nsome desc\n",
        )
        assert result.exit_code == 0
        mock_auto_export.assert_called_once()


def test_start_auto_export_keyboard_interrupt(tmp_path):
    """KeyboardInterrupt during auto-export is caught and pipeline continues."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)), \
         mock.patch("screencap.namer.auto_name", return_value=fake_dir), \
         mock.patch("screencap.cli._auto_export", side_effect=KeyboardInterrupt):
        result = runner.invoke(cli, ["start"])
        assert result.exit_code == 0
        assert "Export cancelled." in result.output
        assert "Recording complete" in result.output


# --- Cloud/Local intent flag tests ---


def test_start_cloud_flag(tmp_path):
    """--cloud forces PUBLIC privacy mode and sets cloud_intent=True."""
    from screencap.privacy.policy import PrivacyMode

    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test", "--cloud"])
    assert result.exit_code == 0
    mock_rec.assert_called_once()
    _, kwargs = mock_rec.call_args
    assert kwargs["force_mode"] == PrivacyMode.PUBLIC
    assert kwargs["cloud_intent"] is True
    assert kwargs["intent_source"] == "flag"


def test_start_local_flag(tmp_path):
    """--local uses configured mode and sets cloud_intent=False."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test", "--local"])
    assert result.exit_code == 0
    mock_rec.assert_called_once()
    _, kwargs = mock_rec.call_args
    assert kwargs["force_mode"] is None
    assert kwargs["cloud_intent"] is False
    assert kwargs["intent_source"] == "flag"


def test_start_no_flag_non_interactive(tmp_path):
    """No flag in non-interactive mode defaults to local."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test"])
    assert result.exit_code == 0
    mock_rec.assert_called_once()
    _, kwargs = mock_rec.call_args
    assert kwargs["force_mode"] is None
    assert kwargs["cloud_intent"] is False
    assert kwargs["intent_source"] == "non_interactive_default"


def test_start_cloud_and_local_last_wins(tmp_path):
    """--cloud --local: last flag wins (Click flag_value semantics)."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test", "--cloud", "--local"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    # Last flag (--local) wins
    assert kwargs["force_mode"] is None
    assert kwargs["cloud_intent"] is False


def test_cloud_start_nlp_model_gate(tmp_path):
    """Cloud recordings are gated on NLP model availability.

    Exercises the full decision tree: models cached, download flow,
    local fallback, abort, and non-interactive failure.
    """
    from screencap.privacy.policy import PrivacyMode

    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()

    def _invoke(args, **kw):
        """Invoke CLI with os._exit patched (start() hard-exits after recording)."""
        with mock.patch("os._exit"):
            return runner.invoke(cli, ["start", "--name", "t"] + args, **kw)

    tty = mock.patch("screencap.cli._stdin_is_tty", return_value=True)
    no_setup = mock.patch("screencap.cli._maybe_prompt_privacy_setup")

    # 1. Models cached → proceeds with cloud_intent=True
    with (
        no_setup, tty,
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec,
        mock.patch("screencap.privacy.are_nlp_models_cached", return_value=True),
    ):
        result = _invoke(["--cloud"])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_rec.call_args
        assert kwargs["cloud_intent"] is True
        assert kwargs["force_mode"] == PrivacyMode.PUBLIC

    # 2. Models missing, download succeeds → cloud proceeds
    call_count = 0

    def cached_after_download():
        nonlocal call_count
        call_count += 1
        return call_count > 1  # False first, True after download

    with (
        mock.patch("screencap.cli._maybe_prompt_privacy_setup"),
        mock.patch("screencap.cli._stdin_is_tty", return_value=True),
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec,
        mock.patch("screencap.privacy.are_nlp_models_cached", side_effect=cached_after_download),
        mock.patch("screencap.cli._download_nlp_models"),
    ):
        call_count = 0
        result = _invoke(["--cloud"], input="y\n")
        assert result.exit_code == 0, result.output
        _, kwargs = mock_rec.call_args
        assert kwargs["cloud_intent"] is True

    # 3. Models missing, download fails, user accepts local fallback
    with (
        mock.patch("screencap.cli._maybe_prompt_privacy_setup"),
        mock.patch("screencap.cli._stdin_is_tty", return_value=True),
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec,
        mock.patch("screencap.privacy.are_nlp_models_cached", return_value=False),
        mock.patch("screencap.cli._download_nlp_models", side_effect=RuntimeError("network")),
    ):
        result = _invoke(["--cloud"], input="y\ny\n")
        assert result.exit_code == 0, result.output
        _, kwargs = mock_rec.call_args
        assert kwargs["cloud_intent"] is False
        assert kwargs["force_mode"] is None  # reverted from PUBLIC

    # 4. Models missing, user declines download, declines local → abort
    with (
        mock.patch("screencap.cli._maybe_prompt_privacy_setup"),
        mock.patch("screencap.cli._stdin_is_tty", return_value=True),
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec,
        mock.patch("screencap.privacy.are_nlp_models_cached", return_value=False),
    ):
        result = _invoke(["--cloud"], input="n\nn\n")
        assert result.exit_code != 0
        mock_rec.assert_not_called()

    # 5. Non-interactive, models missing → exit code 1 with actionable error
    with (
        mock.patch("screencap.cli._maybe_prompt_privacy_setup"),
        mock.patch("screencap.cli._stdin_is_tty", return_value=False),
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec,
        mock.patch("screencap.privacy.are_nlp_models_cached", return_value=False),
    ):
        result = _invoke(["--cloud"])
        assert result.exit_code != 0
        assert "screencap" in result.output and "setup" in result.output
        mock_rec.assert_not_called()

    # 6. Local-only → no model check (are_nlp_models_cached not called)
    with (
        mock.patch("screencap.cli._maybe_prompt_privacy_setup"),
        mock.patch("screencap.cli._stdin_is_tty", return_value=True),
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0, None, None)) as mock_rec,
        mock.patch("screencap.privacy.are_nlp_models_cached") as mock_cached,
    ):
        result = _invoke(["--local"])
        assert result.exit_code == 0, result.output
        mock_cached.assert_not_called()
        _, kwargs = mock_rec.call_args
        assert kwargs["cloud_intent"] is False


def test_upload_skips_local_intent_in_all_mode(tmp_path):
    """upload --all skips recordings with local intent."""
    rec_dir = tmp_path / "local-rec"
    rec_dir.mkdir(parents=True)
    (rec_dir / "recording.db").touch()
    # Write a local-intent file
    intent = {"destination": "local", "source": "flag"}
    (rec_dir / ".recording_intent").write_text(json.dumps(intent))

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.upload.upload_recording") as mock_upload,
    ):
        mock_upload.return_value = mock.MagicMock(
            uploaded=[], skipped=[], failed=[], total_bytes=0, gcs_prefix=None,
        )
        result = runner.invoke(cli, ["upload", "--all"])

    assert result.exit_code == 0
    assert "Skipping" in result.output
    assert "local intent" in result.output
    # upload_recording should NOT have been called for the skipped recording
    mock_upload.assert_not_called()


def test_upload_cloud_intent_proceeds(tmp_path):
    """upload with cloud intent proceeds without scrub prompt."""
    rec_dir = tmp_path / "cloud-rec"
    rec_dir.mkdir(parents=True)
    (rec_dir / "recording.db").touch()
    (rec_dir / "events.jsonl").write_text('{"_meta":true}\n')
    # Write a cloud-intent file
    intent = {"destination": "cloud", "source": "flag"}
    (rec_dir / ".recording_intent").write_text(json.dumps(intent))

    runner = CliRunner()
    with (
        mock.patch("screencap.upload.resolve_recording_dirs", return_value=[rec_dir]),
        mock.patch("screencap.upload.upload_recording") as mock_upload,
    ):
        mock_upload.return_value = mock.MagicMock(
            uploaded=["recording.db", "events.jsonl"],
            skipped=[], failed=[], total_bytes=200,
            gcs_prefix="gs://bucket/cloud-rec",
        )
        result = runner.invoke(cli, ["upload", "cloud-rec"])

    assert result.exit_code == 0
    # upload_recording was called (no scrub prompt for cloud intent)
    mock_upload.assert_called_once()
    # No scrub warning in output
    assert "scrub" not in result.output.lower()


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
