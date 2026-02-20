"""Tests for screencap CLI argument parsing."""

import json
import sqlite3
import time
from unittest import mock

from click.testing import CliRunner

from screencap.cli import cli


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
    with mock.patch("screencap.recorder.start_recording") as mock_rec:
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


def test_start_with_flags():
    """Test start with all flags."""
    runner = CliRunner()
    with mock.patch("screencap.recorder.start_recording") as mock_rec:
        result = runner.invoke(
            cli,
            ["start", "--name", "test-rec", "--no-audio", "-d", "demo"],
        )
        assert result.exit_code == 0
        mock_rec.assert_called_once_with("test-rec", "demo", False, None)


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
            "schema_version": 1,
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
            },
            "start": {"collected_at": "2026-02-19T14:30:00+00:00", "cpu_percent": 12.5},
            "end": {"collected_at": "2026-02-19T14:35:00+00:00", "cpu_percent": 18.0},
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
