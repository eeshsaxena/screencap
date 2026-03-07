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
    _toggle_app_tui,
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


class TestToggleAppTui:
    def test_blocked_to_safe(self):
        meta = _make_app("com.example.test", "Test")
        groups = {
            "blocked": [(meta, ContextClass.PASSWORD_MANAGER, "known_app")],
            "communication": [],
            "safe": [],
            "unclassified": [],
        }
        _toggle_app_tui(groups, "blocked", meta)
        assert len(groups["blocked"]) == 0
        assert len(groups["safe"]) == 1
        assert groups["safe"][0][0].bundle_id == "com.example.test"

    def test_safe_to_blocked(self):
        meta = _make_app("com.example.test", "Test")
        groups = {
            "blocked": [],
            "communication": [],
            "safe": [(meta, ContextClass.CODE_EDITOR_TERMINAL, "known_app")],
            "unclassified": [],
        }
        _toggle_app_tui(groups, "safe", meta)
        assert len(groups["safe"]) == 0
        assert len(groups["blocked"]) == 1

    def test_communication_to_blocked(self):
        meta = _make_app("com.example.test", "Test")
        groups = {
            "blocked": [],
            "communication": [(meta, ContextClass.CHAT, "known_app")],
            "safe": [],
            "unclassified": [],
        }
        _toggle_app_tui(groups, "communication", meta)
        assert len(groups["communication"]) == 0
        assert len(groups["blocked"]) == 1

    def test_unclassified_to_blocked(self):
        meta = _make_app("com.example.test", "Test")
        groups = {
            "blocked": [],
            "communication": [],
            "safe": [],
            "unclassified": [(meta, ContextClass.UNKNOWN, "unknown")],
        }
        _toggle_app_tui(groups, "unclassified", meta)
        assert len(groups["unclassified"]) == 0
        assert len(groups["blocked"]) == 1

    def test_noop_if_not_found(self):
        meta = _make_app("com.example.test", "Test")
        other = _make_app("com.example.other", "Other")
        groups = {
            "blocked": [(other, ContextClass.PASSWORD_MANAGER, "known_app")],
            "communication": [],
            "safe": [],
            "unclassified": [],
        }
        _toggle_app_tui(groups, "blocked", meta)
        assert len(groups["blocked"]) == 1  # unchanged


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

    def test_full_wizard_accept_all(self, tmp_path):
        """Test wizard: user accepts all classifications and saves."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/1Password.app", "com.1password.1password", "1Password"),
            AppMetadata("/test/Slack.app", "com.tinyspeck.slackmacgap", "Slack"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value=True), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 1  # public mode

            result = run_setup_wizard(config_path=config_path)
            assert result is True
            assert config_path.exists()

            doc = tomlkit.parse(config_path.read_text())
            assert doc["privacy"]["mode"] == "public"

    def test_wizard_saves_allow_apps(self, tmp_path):
        """Auto-allowed apps end up in allow_apps config."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/Preview.app", "com.apple.Preview", "Preview"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value=True), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 2  # internal mode

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
             mock.patch("screencap.setup_wizard._run_tui", return_value=True), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 2  # internal mode

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
             mock.patch("screencap.setup_wizard._run_tui", return_value=True), \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 2  # internal mode

            result = run_setup_wizard(config_path=config_path)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            assert "com.1password.1password" in doc["privacy"]["exclude_apps"]

    def test_wizard_cancel(self, tmp_path):
        """User cancels in TUI, no config saved."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/Slack.app", "com.tinyspeck.slackmacgap", "Slack"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.setup_wizard._run_tui", return_value=False):
            mock_stdin.isatty.return_value = True
            mock_click.prompt.return_value = 2

            result = run_setup_wizard(config_path=config_path)
            assert result is False
            assert not config_path.exists()


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
    def test_scan_only_filters_to_new_unknown_apps(self, tmp_path):
        """--scan mode should only show apps that heuristics can't classify."""
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
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard._run_tui", return_value=True), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True

            result = run_setup_wizard(config_path=config_path, scan_only=True)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            assert doc["privacy"]["app_classes"]["com.example.configured"] == "chat"
            assert "com.example.new" in doc["privacy"]["allow_apps"]

    def test_scan_only_all_classified(self, tmp_path):
        """--scan with no new unknown apps reports all classified."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[privacy]\n'
            'mode = "internal"\n'
        )
        apps = [
            AppMetadata("/test/Preview.app", "com.apple.Preview", "Preview"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click:
            mock_stdin.isatty.return_value = True
            result = run_setup_wizard(config_path=config_path, scan_only=True)
            assert result is False
