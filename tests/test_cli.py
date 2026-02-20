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


def test_start_interactive():
    """Test that start command prompts when no --name given."""
    runner = CliRunner()
    with mock.patch("screencap.recorder.start_recording") as mock_rec, \
         mock.patch("screencap.cli.sys") as mock_sys:
        mock_sys.stdin.isatty.return_value = True
        mock_sys.exit = sys.exit
        result = runner.invoke(
            cli,
            ["start"],
            input="my-test\nsome desc\ny\n",
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once()
        args = mock_rec.call_args
        assert args[0][0] == "my-test"  # name
        assert args[0][1] == "some desc"  # description
        assert args[0][2] is True  # audio
        assert args[1]["wifi_metrics"] is True


def test_start_with_flags():
    """Test start with all flags."""
    runner = CliRunner()
    with mock.patch("screencap.recorder.start_recording") as mock_rec:
        result = runner.invoke(
            cli,
            ["start", "--name", "test-rec", "--no-audio", "-d", "demo"],
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once_with(
            "test-rec", "demo", False, None, wifi_metrics=True, force_clean=False,
        )


def test_start_no_wifi_metrics():
    """Test --no-wifi-metrics flag is passed through."""
    runner = CliRunner()
    with mock.patch("screencap.recorder.start_recording") as mock_rec:
        result = runner.invoke(
            cli,
            ["start", "--name", "test-rec", "--no-wifi-metrics"],
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once_with(
            "test-rec", None, True, None, wifi_metrics=False, force_clean=False,
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
            "schema_version": 3,
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
        mock.patch("screencap.recorder.start_recording") as mock_rec,
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
