"""Tests for setup_wizard module."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
import tomlkit

from screencap.privacy.policy import ContextClass, PrivacyMode
from screencap.setup_wizard import (
    _build_save_doc,
    _classify_with_overrides,
    _group_apps,
    _save_config_atomic,
    _toggle_override,
    _app_visual,
    reset_privacy_config,
    run_setup_wizard,
)
from screencap.app_discovery import AppMetadata


def _make_app(bundle_id: str, name: str = "App", is_background: bool = False) -> AppMetadata:
    return AppMetadata(
        path=f"/Applications/{name}.app",
        bundle_id=bundle_id,
        display_name=name,
        is_background=is_background,
    )


class TestClassifyWithOverrides:
    def test_existing_config_takes_priority(self):
        apps = [_make_app("com.example.test", "Test")]
        existing = {"com.example.test": ContextClass.EMAIL}
        result = _classify_with_overrides(apps, existing_app_classes=existing)
        meta, cls, source = result["com.example.test"]
        assert cls == ContextClass.EMAIL
        assert source == "user_config"

    def test_exclude_apps_treated_as_blocked(self):
        apps = [_make_app("com.blocked.app", "Blocked")]
        result = _classify_with_overrides(
            apps, existing_exclude_apps=frozenset({"com.blocked.app"})
        )
        meta, cls, source = result["com.blocked.app"]
        assert cls == ContextClass.PASSWORD_MANAGER
        assert source == "user_config"

    def test_hardcoded_map_used(self):
        apps = [_make_app("com.tinyspeck.slackmacgap", "Slack")]
        result = _classify_with_overrides(apps)
        meta, cls, source = result["com.tinyspeck.slackmacgap"]
        assert cls == ContextClass.CHAT
        assert source == "known_app"

    def test_auto_classify_fallback(self):
        apps = [_make_app("com.example.unknown", "RandomApp")]
        result = _classify_with_overrides(apps)
        meta, cls, source = result["com.example.unknown"]
        assert cls == ContextClass.UNKNOWN
        assert source == "unknown"

    def test_returns_source_from_heuristics(self):
        apps = [_make_app("com.apple.Preview", "Preview")]
        result = _classify_with_overrides(apps)
        meta, cls, source = result["com.apple.Preview"]
        assert source == "apple_prefix"


class TestGroupApps:
    def test_groups_by_category(self):
        classified = {
            "com.1password.1password": (_make_app("com.1password.1password", "1Password"), ContextClass.PASSWORD_MANAGER, "known_app"),
            "com.tinyspeck.slackmacgap": (_make_app("com.tinyspeck.slackmacgap", "Slack"), ContextClass.CHAT, "known_app"),
            "com.apple.Terminal": (_make_app("com.apple.Terminal", "Terminal"), ContextClass.CODE_EDITOR_TERMINAL, "known_app"),
            "com.example.unknown": (_make_app("com.example.unknown", "Unknown"), ContextClass.UNKNOWN, "unknown"),
        }
        groups, auto_allowed = _group_apps(classified)
        assert len(groups["blocked"]) == 1
        assert len(groups["communication"]) == 1
        assert len(groups["safe"]) == 1
        assert len(groups["unclassified"]) == 1

    def test_apple_prefix_auto_allowed(self):
        """Apple prefix apps are auto-allowed, not shown in groups."""
        classified = {
            "com.apple.Preview": (_make_app("com.apple.Preview", "Preview"), ContextClass.UNKNOWN, "apple_prefix"),
            "com.apple.Maps": (_make_app("com.apple.Maps", "Maps"), ContextClass.UNKNOWN, "apple_prefix"),
        }
        groups, auto_allowed = _group_apps(classified)
        assert len(auto_allowed) == 2
        assert all(len(g) == 0 for g in groups.values())

    def test_category_safe_auto_allowed(self):
        classified = {
            "com.example.game": (_make_app("com.example.game", "Game"), ContextClass.UNKNOWN, "category_safe"),
        }
        groups, auto_allowed = _group_apps(classified)
        assert len(auto_allowed) == 1
        assert all(len(g) == 0 for g in groups.values())

    def test_category_safe_force_review_lands_in_unclassified(self):
        """Under force_review (scan mode), category_safe apps surface for user review."""
        classified = {
            "notion.id": (_make_app("notion.id", "Notion"), ContextClass.UNKNOWN, "category_safe"),
        }
        groups, auto_allowed = _group_apps(classified, force_review=True)
        assert len(auto_allowed) == 0
        assert len(groups["unclassified"]) == 1
        assert groups["unclassified"][0][0].bundle_id == "notion.id"

    def test_force_review_covers_all_safe_sources(self):
        """Lifecycle/decoration/apple_prefix apps also surface under force_review."""
        classified = {
            "com.example.updater": (_make_app("com.example.updater", "AppUpdater"), ContextClass.UNKNOWN, "lifecycle"),
            "com.example.widget": (_make_app("com.example.widget", "Widget"), ContextClass.UNKNOWN, "decoration"),
            "com.apple.Something": (_make_app("com.apple.Something", "Something"), ContextClass.UNKNOWN, "apple_prefix"),
        }
        groups, auto_allowed = _group_apps(classified, force_review=True)
        assert len(auto_allowed) == 0
        assert len(groups["unclassified"]) == 3

    def test_force_review_still_filters_background_daemons(self):
        """Genuine LSUIElement/LSBackgroundOnly daemons stay auto-allowed even under force_review."""
        classified = {
            "com.example.daemon": (
                _make_app("com.example.daemon", "SomeDaemon", is_background=True),
                ContextClass.UNKNOWN, "category_safe",
            ),
        }
        groups, auto_allowed = _group_apps(classified, force_review=True)
        assert len(auto_allowed) == 1
        assert all(len(g) == 0 for g in groups.values())

    def test_background_apps_auto_allowed(self):
        classified = {
            "com.example.agent": (
                _make_app("com.example.agent", "SpotlightAgent", is_background=True),
                ContextClass.UNKNOWN, "system_service",
            ),
            "com.example.unknown": (_make_app("com.example.unknown", "Unknown"), ContextClass.UNKNOWN, "unknown"),
        }
        groups, auto_allowed = _group_apps(classified)
        assert len(auto_allowed) == 1
        assert auto_allowed[0][0].bundle_id == "com.example.agent"
        assert len(groups["unclassified"]) == 1

    def test_background_detected_by_name_pattern(self):
        """Apps with service-like names are detected as background even without plist flag."""
        classified = {
            "com.example.helper": (
                _make_app("com.example.helper", "CoreLocationHelper"),
                ContextClass.UNKNOWN, "system_service",
            ),
        }
        groups, auto_allowed = _group_apps(classified)
        assert len(auto_allowed) == 1

    def test_code_editors_go_to_safe_group(self):
        classified = {
            "com.microsoft.VSCode": (_make_app("com.microsoft.VSCode", "VS Code"), ContextClass.CODE_EDITOR_TERMINAL, "known_app"),
        }
        groups, auto_allowed = _group_apps(classified)
        assert len(groups["safe"]) == 1
        assert groups["safe"][0][0].display_name == "VS Code"


class TestToggleOverride:
    def test_blocked_first_toggle_allows(self):
        overrides: dict[str, str] = {}
        _toggle_override(overrides, "blocked", "com.example.test")
        assert overrides["com.example.test"] == "allow"

    def test_unclassified_first_toggle_allows(self):
        overrides: dict[str, str] = {}
        _toggle_override(overrides, "unclassified", "com.example.test")
        assert overrides["com.example.test"] == "allow"

    def test_safe_first_toggle_blocks(self):
        overrides: dict[str, str] = {}
        _toggle_override(overrides, "safe", "com.example.test")
        assert overrides["com.example.test"] == "block"

    def test_communication_first_toggle_blocks(self):
        overrides: dict[str, str] = {}
        _toggle_override(overrides, "communication", "com.example.test")
        assert overrides["com.example.test"] == "block"

    def test_toggle_flips(self):
        overrides = {"com.example.test": "allow"}
        _toggle_override(overrides, "blocked", "com.example.test")
        assert overrides["com.example.test"] == "block"
        _toggle_override(overrides, "blocked", "com.example.test")
        assert overrides["com.example.test"] == "allow"


class TestAppVisual:
    def test_no_override_returns_default(self):
        sym, col = _app_visual("safe", "com.ex", {}, "\u2713", "cyan")
        assert sym == "\u2713"
        assert col == "cyan"

    def test_override_block(self):
        sym, col = _app_visual("safe", "com.ex", {"com.ex": "block"}, "\u2713", "cyan")
        assert sym == "\u00d7"
        assert col == "pink"

    def test_override_allow(self):
        sym, col = _app_visual("blocked", "com.ex", {"com.ex": "allow"}, "\u00d7", "pink")
        assert sym == "\u2713"
        assert col == "cyan"


class TestBuildSaveDoc:
    def test_creates_privacy_section(self):
        doc = tomlkit.document()
        result = _build_save_doc(
            doc,
            PrivacyMode.PUBLIC,
            ["com.1password.1password"],
            ["com.apple.Finder"],
            {"com.tinyspeck.slackmacgap": "chat"},
        )
        assert result["privacy"]["mode"] == "public"
        assert "com.1password.1password" in result["privacy"]["exclude_apps"]
        assert "com.apple.Finder" in result["privacy"]["allow_apps"]
        assert result["privacy"]["app_classes"]["com.tinyspeck.slackmacgap"] == "chat"

    def test_preserves_existing_sections(self):
        doc = tomlkit.document()
        doc.add("recordings_dir", "/custom/path")
        result = _build_save_doc(doc, PrivacyMode.INTERNAL, [], [], {})
        assert result["recordings_dir"] == "/custom/path"
        assert result["privacy"]["mode"] == "internal"

    def test_removes_empty_allow_apps(self):
        doc = tomlkit.document()
        doc.add("privacy", tomlkit.table())
        doc["privacy"]["allow_apps"] = ["old"]
        result = _build_save_doc(doc, PrivacyMode.INTERNAL, [], [], {})
        assert "allow_apps" not in result["privacy"]


class TestAtomicSave:
    def test_atomic_write(self, tmp_path):
        config_path = tmp_path / "config.toml"
        doc = tomlkit.document()
        doc.add("test_key", "test_value")
        _save_config_atomic(config_path, doc)

        assert config_path.exists()
        content = config_path.read_text()
        assert "test_key" in content
        assert "test_value" in content

    def test_preserves_original_on_error(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text('original = "data"\n')

        doc = tomlkit.document()
        doc.add("new", "data")

        with mock.patch("os.rename", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                _save_config_atomic(config_path, doc)

        assert config_path.read_text() == 'original = "data"\n'


class TestRunSetupWizard:
    def test_non_interactive_exits(self, tmp_path):
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            result = run_setup_wizard(config_path=tmp_path / "config.toml")
            assert result is False

    @pytest.mark.parametrize("choice, expected_mode, expected_upload", [
        (1, "public", "cloud"),    # Cloud
        (2, "internal", "local"),  # Local
        (3, "public", "both"),     # Both
        (4, "internal", "ask"),    # Ask every time
    ])
    def test_destination_choice_sets_mode_and_upload(self, tmp_path, choice, expected_mode, expected_upload):
        """Each destination choice writes the correct mode + upload_default pair."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/Slack.app", "com.tinyspeck.slackmacgap", "Slack"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value={}), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = choice

            result = run_setup_wizard(config_path=config_path)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            assert doc["privacy"]["mode"] == expected_mode
            assert doc["privacy"]["upload_default"] == expected_upload

    def test_rerun_preselects_existing_destination(self, tmp_path):
        """Re-running setup with existing cloud config passes default=1 to prompt."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[privacy]\n'
            'mode = "public"\n'
            'upload_default = "cloud"\n'
        )
        apps = [
            AppMetadata("/test/Slack.app", "com.tinyspeck.slackmacgap", "Slack"),
        ]
        captured_defaults = []

        def fake_prompt(text, **kwargs):
            captured_defaults.append(kwargs.get("default"))
            return 1  # keep Cloud

        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value={}), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.side_effect = fake_prompt

            run_setup_wizard(config_path=config_path)

            assert captured_defaults == [1]  # Cloud = choice 1

    def test_wizard_saves_allow_apps(self, tmp_path):
        """Auto-allowed apps end up in allow_apps config."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/Preview.app", "com.apple.Preview", "Preview"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value={}), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 2  # Local

            result = run_setup_wizard(config_path=config_path)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            assert "com.apple.Preview" in doc["privacy"]["allow_apps"]

    def test_wizard_unclassified_goes_to_allow(self, tmp_path):
        """Unclassified apps the user doesn't touch go to allow_apps on accept."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/Figma.app", "com.figma.desktop", "Figma"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value={}), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 2  # Local

            result = run_setup_wizard(config_path=config_path)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            assert "com.figma.desktop" in doc["privacy"]["allow_apps"]

    def test_wizard_blocked_goes_to_exclude(self, tmp_path):
        """Blocked apps end up in exclude_apps config."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/1Password.app", "com.1password.1password", "1Password"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value={}), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 2  # Local

            result = run_setup_wizard(config_path=config_path)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            assert "com.1password.1password" in doc["privacy"]["exclude_apps"]

    def test_override_unblocks_app(self, tmp_path):
        """User overrides a blocked app to allow — goes to allow_apps, not exclude."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/1Password.app", "com.1password.1password", "1Password"),
        ]
        overrides = {"com.1password.1password": "allow"}
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value=overrides), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 2  # Local

            result = run_setup_wizard(config_path=config_path)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            assert "com.1password.1password" in doc["privacy"]["allow_apps"]
            assert "exclude_apps" not in doc["privacy"]

    def test_override_blocks_unclassified(self, tmp_path):
        """User overrides an unclassified app to block — goes to exclude_apps."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/Figma.app", "com.figma.desktop", "Figma"),
        ]
        overrides = {"com.figma.desktop": "block"}
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value=overrides), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 2  # Local

            result = run_setup_wizard(config_path=config_path)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            assert "com.figma.desktop" in doc["privacy"]["exclude_apps"]
            assert "com.figma.desktop" not in doc["privacy"].get("allow_apps", [])

    @pytest.mark.parametrize("choice, expected_mode, expected_upload", [
        (1, "public", "cloud"),
        (2, "internal", "local"),
        (3, "public", "both"),
        (4, "internal", "ask"),
    ])
    def test_wizard_cancel_saves_destination(self, tmp_path, choice, expected_mode, expected_upload):
        """User cancels TUI — destination preference still saved, no app keys written."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/Slack.app", "com.tinyspeck.slackmacgap", "Slack"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value=None):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = choice

            result = run_setup_wizard(config_path=config_path)
            assert result is False
            assert config_path.exists()
            cfg = tomlkit.parse(config_path.read_text())
            assert cfg["privacy"]["mode"] == expected_mode
            assert cfg["privacy"]["upload_default"] == expected_upload
            assert "exclude_apps" not in cfg["privacy"]
            assert "allow_apps" not in cfg["privacy"]

    def test_wizard_cancel_preserves_existing_app_config(self, tmp_path):
        """TUI cancel preserves existing app classifications, only updates mode/upload_default."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[privacy]\n'
            'mode = "internal"\n'
            'upload_default = "local"\n'
            'exclude_apps = ["com.1password.app"]\n'
            'allow_apps = ["com.apple.Safari"]\n'
            '\n'
            '[privacy.app_classes]\n'
            '"com.apple.Safari" = "browser"\n'
        )
        apps = [
            AppMetadata("/test/Slack.app", "com.tinyspeck.slackmacgap", "Slack"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value=None):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 1  # Switch to Cloud

            result = run_setup_wizard(config_path=config_path)
            assert result is False
            cfg = tomlkit.parse(config_path.read_text())
            # Mode and upload_default updated
            assert cfg["privacy"]["mode"] == "public"
            assert cfg["privacy"]["upload_default"] == "cloud"
            # Existing app classifications preserved
            assert cfg["privacy"]["exclude_apps"] == ["com.1password.app"]
            assert cfg["privacy"]["allow_apps"] == ["com.apple.Safari"]
            assert cfg["privacy"]["app_classes"]["com.apple.Safari"] == "browser"


class TestResetPrivacyConfig:
    def test_no_config_file(self, tmp_path):
        result = reset_privacy_config(config_path=tmp_path / "nonexistent.toml")
        assert result is False

    def test_reset_confirmed(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            'recordings_dir = "/custom"\n'
            '\n'
            '[privacy]\n'
            'mode = "public"\n'
        )
        with mock.patch("screencap.setup_wizard.click.confirm", return_value=True), \
             mock.patch("screencap.config.invalidate_config_cache"):
            result = reset_privacy_config(config_path=config_path)
            assert result is True

        doc = tomlkit.parse(config_path.read_text())
        assert "privacy" not in doc
        assert doc["recordings_dir"] == "/custom"

    def test_reset_cancelled(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text('[privacy]\nmode = "public"\n')
        with mock.patch("screencap.setup_wizard.click.confirm", return_value=False):
            result = reset_privacy_config(config_path=config_path)
            assert result is False


class TestCommentPreservation:
    def test_toml_round_trip_preserves_comments(self, tmp_path):
        """Config writes must preserve existing comments and formatting."""
        config_path = tmp_path / "config.toml"
        original = (
            '# User preferences\n'
            'recordings_dir = "/my/recordings"\n'
            '\n'
            '# Audio is disabled for battery\n'
            'audio_default = false\n'
        )
        config_path.write_text(original)

        doc = tomlkit.parse(config_path.read_text())
        _build_save_doc(doc, PrivacyMode.PUBLIC, [], [], {"com.example.app": "chat"})
        _save_config_atomic(config_path, doc)

        result = config_path.read_text()
        assert "# User preferences" in result
        assert "# Audio is disabled for battery" in result
        assert 'recordings_dir = "/my/recordings"' in result
        assert 'mode = "public"' in result


class TestScanOnlyMode:
    def test_scan_only_filters_to_recording_seen_apps(self, tmp_path):
        """--scan mode should only show apps seen in recordings that are unclassified."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[privacy]\n'
            'mode = "internal"\n'
            '\n'
            '[privacy.app_classes]\n'
            '"com.example.configured" = "chat"\n'
        )
        apps = [
            AppMetadata("/test/Configured.app", "com.example.configured", "Configured"),
            AppMetadata("/test/Preview.app", "com.apple.Preview", "Preview"),  # auto-classified
            AppMetadata("/test/New.app", "com.example.new", "New App"),  # truly unknown
            AppMetadata("/test/Other.app", "com.example.other", "Other App"),  # unknown but not in recordings
        ]
        # Only com.example.new was seen in a recording
        seen_bids = {"com.example.configured", "com.apple.Preview", "com.example.new"}
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.catalog.get_seen_bundle_ids", return_value=seen_bids), \
             mock.patch("screencap.setup_wizard._run_tui", return_value={}), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True

            result = run_setup_wizard(config_path=config_path, scan_only=True)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            assert doc["privacy"]["app_classes"]["com.example.configured"] == "chat"
            assert "com.example.new" in doc["privacy"]["allow_apps"]
            # com.example.other was NOT in recordings, should not appear
            assert "com.example.other" not in doc["privacy"].get("allow_apps", [])

    @pytest.mark.parametrize("upload_default", ["cloud", "local"])
    def test_scan_only_preserves_upload_default(self, tmp_path, upload_default):
        """--scan preserves the existing upload_default through to saved config."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[privacy]\n'
            f'mode = "internal"\n'
            f'upload_default = "{upload_default}"\n'
        )
        apps = [
            AppMetadata("/test/New.app", "com.example.new", "New App"),
        ]
        seen_bids = {"com.example.new"}
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.catalog.get_seen_bundle_ids", return_value=seen_bids), \
             mock.patch("screencap.setup_wizard._run_tui", return_value={}), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True

            result = run_setup_wizard(config_path=config_path, scan_only=True)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            assert doc["privacy"]["upload_default"] == upload_default

    def test_scan_only_no_unclassified_in_recordings(self, tmp_path):
        """--scan with no unclassified recording-seen apps reports all classified."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[privacy]\n'
            'mode = "internal"\n'
        )
        apps = [
            AppMetadata("/test/Preview.app", "com.apple.Preview", "Preview"),
        ]
        # Preview is in BUNDLE_ID_MAP, so all recording-seen apps are classified
        seen_bids = {"com.apple.Preview"}
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.catalog.get_seen_bundle_ids", return_value=seen_bids), \
             mock.patch("screencap.setup_wizard.click") as mock_click:
            mock_stdin.isatty.return_value = True
            result = run_setup_wizard(config_path=config_path, scan_only=True)
            assert result is False

    def test_scan_only_no_recordings(self, tmp_path):
        """--scan with no recordings returns False."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[privacy]\n'
            'mode = "internal"\n'
        )
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.catalog.get_seen_bundle_ids", return_value=set()):
            mock_stdin.isatty.return_value = True
            result = run_setup_wizard(config_path=config_path, scan_only=True)
            assert result is False
