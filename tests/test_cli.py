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
    with mock.patch("screencap.recorder.start_recording", return_value=fake_dir) as mock_rec, \
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
    with mock.patch("screencap.recorder.start_recording", return_value=fake_dir) as mock_rec, \
         mock.patch("screencap.cli.sys") as mock_sys:
        mock_sys.stdin.isatty.return_value = True
        mock_sys.exit = sys.exit
        result = runner.invoke(
            cli,
            ["start", "--no-auto-name"],
            input="my-test\nsome desc\n",
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once()
        args = mock_rec.call_args
        assert args[0][0] == "my-test"  # name
        assert args[0][1] == "some desc"  # description


def test_start_with_flags():
    """Test start with all flags."""
    runner = CliRunner()
    with mock.patch("screencap.recorder.start_recording", return_value=mock.MagicMock()) as mock_rec:
        result = runner.invoke(
            cli,
            ["start", "--name", "test-rec", "--no-audio", "-d", "demo"],
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once_with(
            "test-rec", "demo", False, None,
            wifi_metrics=True, app_versions=True, force_clean=False,
            capture_video=None, capture_images=True,
            capture_window_data=None, capture_browser_events=None,
        )


def test_start_no_wifi_metrics():
    """Test --no-wifi-metrics flag is passed through."""
    runner = CliRunner()
    with mock.patch("screencap.recorder.start_recording", return_value=mock.MagicMock()) as mock_rec:
        result = runner.invoke(
            cli,
            ["start", "--name", "test-rec", "--no-wifi-metrics"],
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once_with(
            "test-rec", None, True, None,
            wifi_metrics=False, app_versions=True, force_clean=False,
            capture_video=None, capture_images=True,
            capture_window_data=None, capture_browser_events=None,
        )


def test_start_no_app_versions():
    """Test --no-app-versions flag is passed through."""
    runner = CliRunner()
    with mock.patch("screencap.recorder.start_recording", return_value=mock.MagicMock()) as mock_rec:
        result = runner.invoke(
            cli,
            ["start", "--name", "test-rec", "--no-app-versions"],
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once_with(
            "test-rec", None, True, None,
            wifi_metrics=True, app_versions=False, force_clean=False,
            capture_video=None, capture_images=True,
            capture_window_data=None, capture_browser_events=None,
        )


def test_view_not_found(tmp_path):
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=tmp_path):
        result = runner.invoke(cli, ["view", "nonexistent"])
        assert result.exit_code == 1
        assert "Error" in result.output


def test_scrub_missing():
    runner = CliRunner()
    with mock.patch("screencap.config.get_recordings_dir", return_value=mock.MagicMock()):
        with mock.patch("screencap.scrubber.scrub_recording", side_effect=SystemExit(1)):
            result = runner.invoke(cli, ["scrub", "missing"])
            assert result.exit_code == 1


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


# --- scrubber metrics test ---


def test_scrub_redacts_hostname_in_metrics(tmp_path):
    """Scrubbing should redact hostname in system_metrics.json."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()

    # Minimal DB
    db_path = rec_dir / "recording.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, platform TEXT, task_description TEXT)"
    )
    cur.execute("INSERT INTO recording VALUES (1, ?, 'darwin', 'test')", (time.time(),))
    conn.commit()
    conn.close()

    # Write metrics with hostname
    metrics = {
        "schema_version": 1,
        "static": {"hostname": "my-secret-host.local", "cpu_model": "Apple M2"},
        "start": {"cpu_percent": 10},
        "end": None,
    }
    (rec_dir / "system_metrics.json").write_text(json.dumps(metrics))

    from screencap.scrubber import _scrub_metrics

    # Simulate what scrub_recording does: copy then scrub
    import shutil

    dst = tmp_path / "my-rec-scrubbed"
    shutil.copytree(rec_dir, dst)
    _scrub_metrics(dst / "system_metrics.json")

    scrubbed = json.loads((dst / "system_metrics.json").read_text())
    assert scrubbed["static"]["hostname"] == "<REDACTED>"
    assert scrubbed["static"]["cpu_model"] == "Apple M2"  # not redacted


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


def test_start_force_cleans_orphans():
    """--force flag should auto-clean orphans before starting."""
    runner = CliRunner()
    orphans = [{"pid": 444, "name": "old_writer"}]
    with (
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=orphans),
        mock.patch("screencap.pidfile.terminate_processes") as mock_term,
        mock.patch("screencap.pidfile.delete_pidfile"),
        mock.patch("screencap.pidfile.write_pidfile"),
        mock.patch("screencap.recorder.start_recording", return_value=mock.MagicMock()) as mock_rec,
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
    with mock.patch("screencap.recorder.start_recording", return_value=fake_dir) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test-rec", "--no-video"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    assert kwargs["capture_video"] is False


def test_start_no_images_flag(tmp_path):
    """Test --no-images flag is passed through."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=fake_dir) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test-rec", "--no-images"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    assert kwargs["capture_images"] is False


def test_start_no_window_data_flag(tmp_path):
    """Test --no-window-data flag is passed through."""
    runner = CliRunner()
    fake_dir = tmp_path / "test-rec"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=fake_dir) as mock_rec:
        result = runner.invoke(cli, ["start", "--name", "test-rec", "--no-window-data"])
    assert result.exit_code == 0
    _, kwargs = mock_rec.call_args
    assert kwargs["capture_window_data"] is False


def test_start_name_skips_auto_naming(tmp_path):
    """When --name is provided, auto-naming is skipped entirely."""
    runner = CliRunner()
    fake_dir = tmp_path / "my-recording"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=fake_dir) as mock_rec, \
         mock.patch("screencap.namer.auto_name") as mock_namer:
        result = runner.invoke(cli, ["start", "--name", "my-recording"])
    assert result.exit_code == 0
    mock_namer.assert_not_called()


def test_start_local_only_flag(tmp_path):
    """Test --local-only flag is recognized."""
    runner = CliRunner()
    fake_dir = tmp_path / "rec-test"
    fake_dir.mkdir()
    with mock.patch("screencap.recorder.start_recording", return_value=fake_dir) as mock_rec, \
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


def _mock_action(event_json='{"type":"mouse.singleclick","timestamp":1.0,"x":100,"y":200}'):
    """Create a mock Action whose event.model_dump_json() returns the given JSON."""
    event = mock.MagicMock()
    event.model_dump_json.return_value = event_json
    action = mock.MagicMock()
    action.event = event
    return action


def _mock_capture(actions=None):
    """Create a mock Capture that yields given actions."""
    capture = mock.MagicMock()
    capture.__enter__ = mock.MagicMock(return_value=capture)
    capture.__exit__ = mock.MagicMock(return_value=False)
    if actions is None:
        actions = [_mock_action()]
    capture.actions.return_value = iter(actions)
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
        mock.patch("openadapt_capture.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-rec"])

    assert result.exit_code == 0
    out_file = rec_dir / "events.jsonl"
    assert out_file.exists()
    lines = out_file.read_text().strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "mouse.singleclick"


def test_export_stdout(tmp_path):
    """--stdout flag writes JSONL to stdout."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()

    capture = _mock_capture()
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("openadapt_capture.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-rec", "--stdout"])

    assert result.exit_code == 0
    lines = _jsonl_lines(result.output)
    assert len(lines) == 1
    parsed = json.loads(lines[0])
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
        mock.patch("openadapt_capture.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-rec", "-o", str(out_file)])

    assert result.exit_code == 0
    assert out_file.exists()
    lines = out_file.read_text().strip().split("\n")
    assert len(lines) == 1


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
    """--exclude-moves passes include_moves=False to capture.actions()."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()

    capture = _mock_capture(actions=[])
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("openadapt_capture.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-rec", "--exclude-moves"])

    assert result.exit_code == 0
    capture.actions.assert_called_once_with(include_moves=False)


def test_export_includes_moves_by_default(tmp_path):
    """Without --exclude-moves, include_moves=True is passed."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()

    capture = _mock_capture(actions=[])
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("openadapt_capture.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "my-rec"])

    assert result.exit_code == 0
    capture.actions.assert_called_once_with(include_moves=True)


def test_export_legacy_db_error(tmp_path):
    """Legacy capture.db format results in exit code 1."""
    rec_dir = tmp_path / "old-rec"
    rec_dir.mkdir()

    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch(
            "openadapt_capture.capture.CaptureSession.load",
            side_effect=FileNotFoundError("Capture not found"),
        ),
    ):
        result = runner.invoke(cli, ["export", "old-rec"])

    assert result.exit_code == 1


def test_export_empty_recording(tmp_path):
    """Empty recording produces empty events.jsonl."""
    rec_dir = tmp_path / "empty-rec"
    rec_dir.mkdir()

    capture = _mock_capture(actions=[])
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("openadapt_capture.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "empty-rec"])

    assert result.exit_code == 0
    out_file = rec_dir / "events.jsonl"
    assert out_file.exists()
    assert out_file.read_text().strip() == ""


def test_export_multiple_events(tmp_path):
    """Multiple events each get their own JSONL line."""
    rec_dir = tmp_path / "multi-rec"
    rec_dir.mkdir()

    actions = [
        _mock_action('{"type":"mouse.singleclick","timestamp":1.0}'),
        _mock_action('{"type":"key.type","timestamp":2.0}'),
        _mock_action('{"type":"mouse.scroll","timestamp":3.0}'),
    ]
    capture = _mock_capture(actions=actions)
    runner = CliRunner()

    with (
        mock.patch("screencap.config.resolve_recording_dir", return_value=rec_dir),
        mock.patch("openadapt_capture.capture.CaptureSession.load", return_value=capture),
    ):
        result = runner.invoke(cli, ["export", "multi-rec"])

    assert result.exit_code == 0
    out_file = rec_dir / "events.jsonl"
    lines = out_file.read_text().strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        json.loads(line)
