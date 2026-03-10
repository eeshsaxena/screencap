"""Tests for screencap CLI argument parsing."""

import json
import sqlite3
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
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec, \
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
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec, \
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
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec:
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
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec:
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
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec:
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
    """Create a minimal recording dir with DB and optional metrics."""
    rec_dir = base / name
    rec_dir.mkdir(parents=True)

    db_path = rec_dir / "recording.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, platform TEXT)"
    )
    cur.execute(
        "CREATE TABLE action_event (id INTEGER PRIMARY KEY, timestamp REAL)"
    )
    started = time.time() - duration
    cur.execute("INSERT INTO recording VALUES (1, ?, 'darwin')", (started,))
    cur.execute("INSERT INTO action_event VALUES (1, ?)", (started + duration,))
    conn.commit()
    conn.close()

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


def test_stop_no_orphans():
    runner = CliRunner()
    with mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]):
        result = runner.invoke(cli, ["stop"])
    assert result.exit_code == 0
    assert "No orphaned" in result.output


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
        mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec,
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
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test-rec", "--no-video"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    assert kwargs["capture_video"] is False


def test_start_no_images_flag(tmp_path):
    """Test --no-images flag is passed through."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test-rec", "--no-images"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    assert kwargs["capture_images"] is False


def test_start_no_window_data_flag(tmp_path):
    """Test --no-window-data flag is passed through."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test-rec", "--no-window-data"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    assert kwargs["capture_window_data"] is False


def test_start_name_skips_auto_naming(tmp_path):
    """When --name is provided, auto-naming is skipped entirely."""
    runner = CliRunner()
    fake_dir = tmp_path / "my-recording"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec, \
         mock.patch("screencap.namer.auto_name") as mock_namer:
        result = runner.invoke(cli, ["start", "--name", "my-recording"])
    assert result.exit_code == 0
    mock_namer.assert_not_called()


def test_start_local_only_flag(tmp_path):
    """Test --local-only flag is recognized."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec, \
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


def _mock_event(event_json='{"type":"mouse.singleclick","timestamp":1.0,"x":100,"y":200}'):
    """Create a mock BaseEvent whose model_dump_json() returns the given JSON."""
    event = mock.MagicMock()
    event.model_dump_json.return_value = event_json
    return event


def _mock_capture(events=None):
    """Create a mock Capture that yields given events from export_events()."""
    capture = mock.MagicMock()
    capture.__enter__ = mock.MagicMock(return_value=capture)
    capture.__exit__ = mock.MagicMock(return_value=False)
    if events is None:
        events = [_mock_event()]
    capture.export_events.return_value = iter(events)
    return capture


def _jsonl_lines(output):
    """Extract valid JSON lines from mixed output (JSONL + stderr warnings)."""
    lines = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if line.startswith("{"):
            lines.append(line)
    return lines


def test_export_default_writes_to_recording_dir(tmp_path):
    """Default export writes events.jsonl into the recording directory."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()

    capture = _mock_capture()
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-rec"])

    assert result.exit_code == 0
    out_file = rec_dir / "events.jsonl"
    assert out_file.exists()
    lines = out_file.read_text().strip().split("\n")
    assert len(lines) == 2  # metadata header + 1 event
    header = json.loads(lines[0])
    assert header["_meta"] is True
    parsed = json.loads(lines[1])
    assert parsed["type"] == "mouse.singleclick"


def test_export_stdout(tmp_path):
    """--stdout flag writes JSONL to stdout."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()

    capture = _mock_capture()
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-rec", "--stdout"])

    assert result.exit_code == 0
    lines = _jsonl_lines(result.output)
    assert len(lines) == 2  # metadata header + 1 event
    header = json.loads(lines[0])
    assert header["_meta"] is True
    parsed = json.loads(lines[1])
    assert parsed["type"] == "mouse.singleclick"
    # No file written in recording dir
    assert not (rec_dir / "events.jsonl").exists()


def test_export_to_file(tmp_path):
    """Export to custom file path with -o."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    out_file = tmp_path / "custom.jsonl"

    capture = _mock_capture()
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-rec", "-o", str(out_file)])

    assert result.exit_code == 0
    assert out_file.exists()
    lines = out_file.read_text().strip().split("\n")
    assert len(lines) == 2  # metadata header + 1 event


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


def test_export_exclude_moves(tmp_path):
    """--exclude-moves passes include_moves=False to capture.export_events()."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()

    capture = _mock_capture(events=[])
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-rec", "--exclude-moves"])

    assert result.exit_code == 0
    capture.export_events.assert_called_once_with(include_moves=False)


def test_export_includes_moves_by_default(tmp_path):
    """Without --exclude-moves, include_moves=True is passed."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()

    capture = _mock_capture(events=[])
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-rec"])

    assert result.exit_code == 0
    capture.export_events.assert_called_once_with(include_moves=True)


def test_export_legacy_db_error(tmp_path):
    """Legacy capture.db format results in exit code 1."""
    rec_dir = tmp_path / "old-rec"
    rec_dir.mkdir()

    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch(
            "sc_engine.capture.CaptureSession.load",
            side_effect=FileNotFoundError("Capture not found"),
        ),
    ):
        result = runner.invoke(cli, ["export", "old-rec"])

    assert result.exit_code == 1


def test_export_empty_recording(tmp_path):
    """Empty recording produces empty events.jsonl."""
    rec_dir = tmp_path / "empty-rec"
    rec_dir.mkdir()

    capture = _mock_capture(events=[])
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "empty-rec"])

    assert result.exit_code == 0
    out_file = rec_dir / "events.jsonl"
    assert out_file.exists()
    # Now includes metadata header even when no events
    content = out_file.read_text().strip()
    header = json.loads(content)
    assert header["_meta"] is True


def test_export_multiple_events(tmp_path):
    """Multiple events each get their own JSONL line."""
    rec_dir = tmp_path / "multi-rec"
    rec_dir.mkdir()

    events = [
        _mock_event('{"type":"mouse.singleclick","timestamp":1.0}'),
        _mock_event('{"type":"key.type","timestamp":2.0}'),
        _mock_event('{"type":"mouse.scroll","timestamp":3.0}'),
    ]
    capture = _mock_capture(events=events)
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "multi-rec"])

    assert result.exit_code == 0
    out_file = rec_dir / "events.jsonl"
    lines = out_file.read_text().strip().split("\n")
    assert len(lines) == 4  # metadata header + 3 events
    header = json.loads(lines[0])
    assert header["_meta"] is True
    for line in lines[1:]:
        json.loads(line)


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


def test_export_all(tmp_path):
    """--all exports every recording to its own events.jsonl."""
    # Create two recording dirs with recording.db
    for name in ("rec-a", "rec-b"):
        d = tmp_path / name
        d.mkdir()
        (d / "recording.db").touch()

    # A non-recording dir (no recording.db) — should be skipped
    (tmp_path / "not-a-recording").mkdir()

    capture = _mock_capture()
    runner = CliRunner()

    def fresh_capture(*args, **kwargs):
        return _mock_capture()

    with (
        mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path),
        mock.patch("sc_engine.capture.CaptureSession.load", side_effect=fresh_capture),
    ):
        result = runner.invoke(cli, ["export", "--all"])

    assert result.exit_code == 0
    # Both recordings should have events.jsonl
    assert (tmp_path / "rec-a" / "events.jsonl").exists()
    assert (tmp_path / "rec-b" / "events.jsonl").exists()


def test_export_all_no_recordings(tmp_path):
    """--all with no recordings prints message and exits cleanly."""
    runner = CliRunner()

    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["export", "--all"])

    assert result.exit_code == 0


# --- export --downloads tests ---


def test_export_downloads_only(tmp_path):
    """--downloads (without --all) exports only downloaded recordings."""
    dl_dir = tmp_path / "downloads"
    rec_dir = tmp_path / "recordings"
    dl_dir.mkdir()
    rec_dir.mkdir()

    # Create a downloaded recording
    d = dl_dir / "dl-rec"
    d.mkdir()
    (d / "recording.db").touch()

    # Create a local recording — should NOT be exported
    r = rec_dir / "local-rec"
    r.mkdir()
    (r / "recording.db").touch()

    capture = _mock_capture()
    runner = CliRunner()

    def fresh_capture(*args, **kwargs):
        return _mock_capture()

    with (
        mock.patch("screencap.config.get_recordings_dir", return_value=rec_dir),
        mock.patch("screencap.config.get_downloads_dir", return_value=dl_dir),
        mock.patch("sc_engine.capture.CaptureSession.load", side_effect=fresh_capture),
    ):
        result = runner.invoke(cli, ["export", "--downloads"])

    assert result.exit_code == 0
    assert (dl_dir / "dl-rec" / "events.jsonl").exists()
    assert not (rec_dir / "local-rec" / "events.jsonl").exists()


def test_export_all_and_downloads(tmp_path):
    """--all --downloads exports from both recordings and downloads dirs."""
    dl_dir = tmp_path / "downloads"
    rec_dir = tmp_path / "recordings"
    dl_dir.mkdir()
    rec_dir.mkdir()

    # Downloaded recording
    d = dl_dir / "dl-rec"
    d.mkdir()
    (d / "recording.db").touch()

    # Local recording
    r = rec_dir / "local-rec"
    r.mkdir()
    (r / "recording.db").touch()

    def fresh_capture(*args, **kwargs):
        return _mock_capture()

    runner = CliRunner()

    with (
        mock.patch("screencap.config.get_recordings_dir", return_value=rec_dir),
        mock.patch("screencap.config.get_downloads_dir", return_value=dl_dir),
        mock.patch("sc_engine.capture.CaptureSession.load", side_effect=fresh_capture),
    ):
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


def test_export_single_by_name_with_downloads_fallback(tmp_path):
    """Single recording name with --downloads falls back to downloads dir."""
    rec_dir = tmp_path / "recordings"
    dl_dir = tmp_path / "downloads"
    rec_dir.mkdir()
    dl_dir.mkdir()

    # Only in downloads dir
    d = dl_dir / "my-dl"
    d.mkdir()
    (d / "recording.db").touch()

    capture = _mock_capture()
    runner = CliRunner()

    with (
        mock.patch("screencap.config.get_recordings_dir", return_value=rec_dir),
        mock.patch("screencap.config.get_downloads_dir", return_value=dl_dir),
        mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-dl", "--downloads"])

    assert result.exit_code == 0
    assert (dl_dir / "my-dl" / "events.jsonl").exists()


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

    mock_export = mock.MagicMock(return_value=5)
    mock_meta = mock.MagicMock(return_value={"_meta": True})

    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)), \
         mock.patch("screencap.namer.auto_name", return_value=fake_dir), \
         mock.patch.dict("sys.modules", {"screencap.exporter": mock.MagicMock(
             export_recording=mock_export,
             build_export_metadata=mock_meta,
         )}):
        result = runner.invoke(cli, ["start"])
        assert result.exit_code == 0
        mock_meta.assert_called_once_with(exclude_moves=False)
        mock_export.assert_called_once_with(
            fake_dir, str(fake_dir / "events.jsonl"), exclude_moves=False, metadata={"_meta": True},
        )
        assert "Exported 5 events" in result.output


def test_start_auto_export_failure_does_not_crash(tmp_path):
    """Auto-export failure logs a warning but does not affect exit code."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()

    mock_export = mock.MagicMock(side_effect=RuntimeError("boom"))
    mock_meta = mock.MagicMock(return_value={"_meta": True})

    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)), \
         mock.patch("screencap.namer.auto_name", return_value=fake_dir), \
         mock.patch.dict("sys.modules", {"screencap.exporter": mock.MagicMock(
             export_recording=mock_export,
             build_export_metadata=mock_meta,
         )}):
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

    mock_export = mock.MagicMock(return_value=3)
    mock_meta = mock.MagicMock(return_value={"_meta": True})

    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)), \
         mock.patch("screencap.cli.sys") as mock_sys, \
         mock.patch.dict("sys.modules", {"screencap.exporter": mock.MagicMock(
             export_recording=mock_export,
             build_export_metadata=mock_meta,
         )}):
        mock_sys.stdin.isatty.return_value = True
        mock_sys.exit = sys.exit
        result = runner.invoke(
            cli,
            ["start", "--no-auto-name", "--local"],
            input="my-test\nsome desc\n",
        )
        assert result.exit_code == 0
        mock_export.assert_called_once()
        assert "Exported 3 events" in result.output


def test_start_auto_export_keyboard_interrupt(tmp_path):
    """KeyboardInterrupt during auto-export is caught and pipeline continues."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)), \
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
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec:
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
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec:
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
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec:
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
    with mock.patch("screencap.recorder.start_recording", return_value=(fake_dir, 42.0)) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test", "--cloud", "--local"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    # Last flag (--local) wins
    assert kwargs["force_mode"] is None
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
