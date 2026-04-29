"""Tests for Unit 4d: ``screencap export <name> --privacy-filter`` flag.

The flag wires ``build_privacy_filter()`` from screencap.exporter into
``unified_export_events`` via the existing ``privacy_filter`` kwarg on
``_write_events``. Off by default (preserves the existing CLI behavior
of \"export emits events as captured\"); turn on for SwiftUI viewer or
other downstream consumers that need privacy-filtered events.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from screencap.cli import cli

from tests.test_export_integration import create_export_test_db


@pytest.fixture(autouse=True)
def _isolate_recordings(tmp_path, monkeypatch):
    """Point recordings dir at tmp_path so the CLI resolves the rec correctly."""
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    import screencap.config
    monkeypatch.setattr(screencap.config, "_config_cache", None)
    monkeypatch.delenv("SCREENCAP_PRIVACY_MODE", raising=False)
    yield


def _make_recording_with_slack(tmp_path):
    rec_dir = tmp_path / "test-rec"
    rec_dir.mkdir()
    extra_windows = [
        {
            "timestamp": 1009.0,
            "title": "#secret-channel — Slack",
            "app_bundle_id": "com.tinyspeck.slackmacgap",
            "window_id": "400",
            "left": 0, "top": 0, "width": 1512, "height": 982,
        },
    ]
    create_export_test_db(rec_dir / "recording.db", extra_window_events=extra_windows)
    return rec_dir


def _slack_window_titles(jsonl: str) -> list[str]:
    """Extract Slack window.switch event titles from the export JSONL."""
    titles = []
    for line in jsonl.strip().splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "window.switch" and evt.get("app_bundle_id") == "com.tinyspeck.slackmacgap":
            titles.append(evt.get("window_title"))
    return titles


class TestExportPrivacyFilterFlag:
    def test_default_emits_raw_titles(self, tmp_path):
        """Without --privacy-filter, Slack title passes through as-is."""
        _make_recording_with_slack(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["export", "test-rec", "--stdout"], catch_exceptions=False)
        assert result.exit_code == 0
        titles = _slack_window_titles(result.stdout)
        assert titles == ["#secret-channel — Slack"]

    def test_filter_masks_chat_window_title_under_internal(self, tmp_path, monkeypatch):
        """With --privacy-filter and default internal mode, Slack (CHAT) →
        MASK_WINDOW (post-Unit-7a) → title masked to app_name."""
        _make_recording_with_slack(tmp_path)
        # Default mode is internal — no config needed
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["export", "test-rec", "--stdout", "--privacy-filter"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        titles = _slack_window_titles(result.stdout)
        assert titles == ["Slackmacgap"]  # masked to app_name

    def test_filter_passes_unfiltered_apps_through(self, tmp_path):
        """Non-CHAT/EMAIL apps (Terminal=CODE_EDITOR_TERMINAL=ALLOW under
        internal) pass through with full title."""
        _make_recording_with_slack(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["export", "test-rec", "--stdout", "--privacy-filter"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        # Find Terminal window.switch event
        terminal_titles = []
        for line in result.stdout.strip().splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "window.switch" and evt.get("app_bundle_id") == "com.apple.Terminal":
                terminal_titles.append(evt.get("window_title"))
        assert "bash — 80×24" in terminal_titles  # unmasked

    def test_filter_drops_password_manager_window_event(self, tmp_path):
        """EXCLUDE-class apps (1Password = PASSWORD_MANAGER, EXCLUDE in every
        mode) must have their window.switch events suppressed entirely — not
        just masked. A regression to MASK_WINDOW would silently leak the
        timestamp during which a password manager was foregrounded (todo 024)."""
        rec_dir = tmp_path / "test-rec"
        rec_dir.mkdir()
        extra_windows = [
            {
                "timestamp": 1009.0,
                "title": "Vault: My Logins",
                "app_bundle_id": "com.1password.1password",
                "window_id": "500",
                "left": 0, "top": 0, "width": 1512, "height": 982,
            },
        ]
        create_export_test_db(rec_dir / "recording.db", extra_window_events=extra_windows)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["export", "test-rec", "--stdout", "--privacy-filter"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        # NO window.switch event for the password manager should appear in
        # the filtered output (the filter returns None → event is dropped).
        for line in result.stdout.strip().splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "window.switch":
                assert evt.get("app_bundle_id") != "com.1password.1password", (
                    f"EXCLUDE-class window.switch leaked: {evt}"
                )

    def test_filter_uses_recording_time_mode_from_intent_file(self, tmp_path):
        """Todo 001: `--privacy-filter` resolves the privacy mode from
        `<recording_dir>/.recording_intent` (recording-time posture), not
        from the current config.toml. Without this fix, a user who recorded
        under `mode=public` (BROWSER_UNVERIFIED → MASK_WINDOW) and later
        switched to `mode=internal` (BROWSER_UNVERIFIED → ALLOW) would get
        their browser-window events leaked at export time.

        Setup: write `.recording_intent` saying the recording was made under
        `public` mode, then put `internal` in the config. Confirm that
        Slack (CHAT) under public stays MASK_WINDOW (would also be
        MASK_WINDOW under internal post-Unit-7a, but this asserts the
        intent file IS being consulted via the diagnostic stderr surface).
        """
        rec_dir = _make_recording_with_slack(tmp_path)

        # Write the recording-time intent: public mode at capture time.
        import json as _json
        (rec_dir / ".recording_intent").write_text(_json.dumps({
            "version": 1,
            "destination": "local",
            "privacy_mode": "public",
            "created_at": "2026-04-28T12:00:00Z",
            "source": "flag",
        }))

        # Set the current config to a different mode (would silently flip
        # behavior for any consumer that read config.toml instead of
        # .recording_intent).
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('[privacy]\nmode = "internal"\n')
        import screencap.config
        original_path = screencap.config._CONFIG_PATH
        try:
            screencap.config._CONFIG_PATH = cfg_path
            screencap.config._config_cache = None

            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["export", "test-rec", "--stdout", "--privacy-filter"],
                catch_exceptions=False,
            )
        finally:
            screencap.config._CONFIG_PATH = original_path
            screencap.config._config_cache = None

        assert result.exit_code == 0
        # Slack masked at recording-time mode (public → CHAT → MASK_WINDOW).
        # The fallback warning about "no .recording_intent" must NOT appear.
        assert "No .recording_intent" not in result.stderr
        titles = _slack_window_titles(result.stdout)
        assert titles == ["Slackmacgap"]

    def test_filter_falls_back_to_config_with_warning_on_legacy_recording(self, tmp_path):
        """Legacy recording without `.recording_intent` → fall through to
        current config.toml mode and surface a stderr warning so the user
        knows the export is using current config, not recording-time
        posture."""
        rec_dir = _make_recording_with_slack(tmp_path)
        # No .recording_intent file written.

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["export", "test-rec", "--stdout", "--privacy-filter"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "No .recording_intent" in result.stderr or "applying current config" in result.stderr

    def test_filter_with_public_mode(self, tmp_path):
        """Mode = public (set in config) → still masks CHAT (matrix says
        MASK_WINDOW under public too)."""
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('[privacy]\nmode = "public"\n')

        # Need to redirect the _CONFIG_PATH to our tmp config
        import screencap.config

        # Note: the autouse fixture monkeypatch.setenv is not enough; we also
        # need _CONFIG_PATH to point at the right place. The export builds
        # the filter from _load_toml() which reads _CONFIG_PATH directly.
        original_path = screencap.config._CONFIG_PATH
        try:
            screencap.config._CONFIG_PATH = cfg_path
            screencap.config._config_cache = None
            _make_recording_with_slack(tmp_path)
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["export", "test-rec", "--stdout", "--privacy-filter"],
                catch_exceptions=False,
            )
        finally:
            screencap.config._CONFIG_PATH = original_path
            screencap.config._config_cache = None

        assert result.exit_code == 0
        titles = _slack_window_titles(result.stdout)
        assert titles == ["Slackmacgap"]
