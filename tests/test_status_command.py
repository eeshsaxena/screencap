"""Tests for Unit 4a: ``screencap status --json``.

The command reads recording.lock metadata via pidfile.read_lock_metadata()
and pidfile.lock_is_active() — no IPC, no SessionController spawn, no AppKit.
Designed for SwiftUI to poll at 1Hz.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from screencap import pidfile
from screencap.cli import cli


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect lock + config to tmp_path; reset module state."""
    import screencap.config

    # Lock file
    lock_dir = tmp_path / "run"
    monkeypatch.setattr(pidfile, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(pidfile, "LOCK_FILE", lock_dir / "recording.lock")
    monkeypatch.setattr(pidfile, "PID_FILE", tmp_path / "recording.pid")

    # Config file
    monkeypatch.setattr(screencap.config, "_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(screencap.config, "_DEFAULT_BASE", tmp_path)
    monkeypatch.setattr(screencap.config, "_config_cache", None)

    if pidfile._LOCKED_FD is not None:
        try:
            pidfile.release_lock()
        except Exception:
            pass
        pidfile._LOCKED_FD = None
    yield
    if pidfile._LOCKED_FD is not None:
        try:
            pidfile.release_lock()
        except Exception:
            pass
        pidfile._LOCKED_FD = None


def _run_status_json():
    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--json"], catch_exceptions=False)
    return result


class TestStatusJson:
    def test_no_lock_returns_not_recording(self):
        result = _run_status_json()
        assert result.exit_code == 0
        payload = json.loads(result.output.strip())
        assert payload["is_recording"] is False
        assert "elapsed" not in payload
        assert "started_at" not in payload

    def test_active_lock_returns_recording_metadata(self, tmp_path):
        capture_dir = tmp_path / "test-rec"
        pidfile.claim_lock(capture_dir, claimant="cli")
        try:
            result = _run_status_json()
        finally:
            pidfile.release_lock()
        assert result.exit_code == 0
        payload = json.loads(result.output.strip())
        assert payload["is_recording"] is True
        assert payload["claimant"] == "cli"
        assert payload["capture_dir"] == str(capture_dir)
        assert isinstance(payload.get("elapsed"), float)
        assert payload["elapsed"] >= 0.0
        assert isinstance(payload.get("started_at"), float)

    def test_swiftui_claimant_propagates(self, tmp_path):
        pidfile.claim_lock(tmp_path / "rec", claimant="swiftui")
        try:
            result = _run_status_json()
        finally:
            pidfile.release_lock()
        payload = json.loads(result.output.strip())
        assert payload["claimant"] == "swiftui"

    def test_elapsed_within_one_second_of_actual(self, tmp_path):
        pidfile.claim_lock(tmp_path / "rec")
        time.sleep(0.5)
        try:
            result = _run_status_json()
        finally:
            pidfile.release_lock()
        payload = json.loads(result.output.strip())
        # Should be ≥ 0.5s but not absurdly large
        assert 0.4 <= payload["elapsed"] < 5.0

    def test_stale_lock_with_no_holder_reports_not_recording(self, tmp_path):
        # Simulate a file left over from a process that died abruptly.
        pidfile.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        pidfile.LOCK_FILE.write_text(
            json.dumps({"pid": 99999, "claimant": "cli", "capture_dir": "/tmp/x"})
        )
        result = _run_status_json()
        assert result.exit_code == 0
        payload = json.loads(result.output.strip())
        assert payload["is_recording"] is False
        # The stale-file warning surfaces so debugging is easier.
        assert payload.get("warning") == "lock_unparseable_or_stale"

    def test_includes_privacy_configured_flag(self, tmp_path):
        import screencap.config

        # No config file → privacy_configured False
        result = _run_status_json()
        payload = json.loads(result.output.strip())
        assert payload["privacy_configured"] is False

        # Write a [privacy] section + invalidate cache → privacy_configured True
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('[privacy]\nmode = "internal"\n')
        screencap.config._config_cache = None
        result = _run_status_json()
        payload = json.loads(result.output.strip())
        assert payload["privacy_configured"] is True

    def test_includes_nlp_models_cached_flag(self, monkeypatch):
        """Either True or False, but always present and a bool."""
        result = _run_status_json()
        payload = json.loads(result.output.strip())
        assert "nlp_models_cached" in payload
        assert isinstance(payload["nlp_models_cached"], bool)


class TestStatusHumanOutput:
    def test_no_lock_human_output(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["status"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Not recording" in result.output

    def test_active_lock_human_output(self, tmp_path):
        pidfile.claim_lock(tmp_path / "rec", claimant="cli")
        try:
            runner = CliRunner()
            result = runner.invoke(cli, ["status"], catch_exceptions=False)
        finally:
            pidfile.release_lock()
        assert result.exit_code == 0
        assert "Recording" in result.output
