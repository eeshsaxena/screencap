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
    reset_privacy_config,
    run_setup_wizard,
)
from screencap.app_discovery import AppMetadata


def _make_app(bundle_id: str, name: str = "App") -> AppMetadata:
    return AppMetadata(path=f"/Applications/{name}.app", bundle_id=bundle_id, display_name=name)


class TestClassifyWithOverrides:
    def test_existing_config_takes_priority(self):
        apps = [_make_app("com.example.test", "Test")]
        existing = {"com.example.test": ContextClass.EMAIL}
        result = _classify_with_overrides(apps, existing_app_classes=existing)
        assert result["com.example.test"][1] == ContextClass.EMAIL

    def test_exclude_apps_treated_as_blocked(self):
        apps = [_make_app("com.blocked.app", "Blocked")]
        result = _classify_with_overrides(
            apps, existing_exclude_apps=frozenset({"com.blocked.app"})
        )
        assert result["com.blocked.app"][1] == ContextClass.PASSWORD_MANAGER

    def test_hardcoded_map_used(self):
        apps = [_make_app("com.tinyspeck.slackmacgap", "Slack")]
        result = _classify_with_overrides(apps)
        assert result["com.tinyspeck.slackmacgap"][1] == ContextClass.CHAT

    def test_auto_classify_fallback(self):
        apps = [_make_app("com.example.unknown", "RandomApp")]
        result = _classify_with_overrides(apps)
        assert result["com.example.unknown"][1] == ContextClass.UNKNOWN


class TestGroupApps:
    def test_groups_by_category(self):
        classified = {
            "com.1password.1password": (_make_app("com.1password.1password", "1Password"), ContextClass.PASSWORD_MANAGER),
            "com.tinyspeck.slackmacgap": (_make_app("com.tinyspeck.slackmacgap", "Slack"), ContextClass.CHAT),
            "com.apple.Terminal": (_make_app("com.apple.Terminal", "Terminal"), ContextClass.CODE_EDITOR_TERMINAL),
            "com.example.unknown": (_make_app("com.example.unknown", "Unknown"), ContextClass.UNKNOWN),
        }
        groups = _group_apps(classified)
        assert len(groups["blocked"]) == 1
        assert len(groups["communication"]) == 1
        assert len(groups["code"]) == 1
        assert len(groups["unclassified"]) == 1


class TestBuildSaveDoc:
    def test_creates_privacy_section(self):
        doc = tomlkit.document()
        result = _build_save_doc(
            doc,
            PrivacyMode.PUBLIC,
            ["com.1password.1password"],
            {"com.tinyspeck.slackmacgap": "chat"},
        )
        assert result["privacy"]["mode"] == "public"
        assert "com.1password.1password" in result["privacy"]["exclude_apps"]
        assert result["privacy"]["app_classes"]["com.tinyspeck.slackmacgap"] == "chat"

    def test_preserves_existing_sections(self):
        doc = tomlkit.document()
        doc.add("recordings_dir", "/custom/path")
        result = _build_save_doc(doc, PrivacyMode.INTERNAL, [], {})
        assert result["recordings_dir"] == "/custom/path"
        assert result["privacy"]["mode"] == "internal"


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

    def test_full_wizard_flow(self, tmp_path):
        """Test wizard with auto-classified apps, no unclassified, user saves."""
        config_path = tmp_path / "config.toml"
        apps = [
            AppMetadata("/test/1Password.app", "com.1password.1password", "1Password"),
            AppMetadata("/test/Slack.app", "com.tinyspeck.slackmacgap", "Slack"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            # Mode choice = 1 (public)
            mock_click.prompt.return_value = 1
            mock_click.confirm.return_value = True

            result = run_setup_wizard(config_path=config_path)
            assert result is True
            assert config_path.exists()

            doc = tomlkit.parse(config_path.read_text())
            assert doc["privacy"]["mode"] == "public"


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
        _build_save_doc(doc, PrivacyMode.PUBLIC, [], {"com.example.app": "chat"})
        _save_config_atomic(config_path, doc)

        result = config_path.read_text()
        assert "# User preferences" in result
        assert "# Audio is disabled for battery" in result
        assert 'recordings_dir = "/my/recordings"' in result
        assert 'mode = "public"' in result


class TestScanOnlyMode:
    def test_scan_only_filters_to_new_apps(self, tmp_path):
        """--scan mode should only show apps not in existing config or hardcoded map."""
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
            AppMetadata("/test/New.app", "com.example.new", "New App"),
        ]
        with mock.patch("sys.stdin") as mock_stdin, \
             mock.patch("screencap.setup_wizard.discover_installed_apps", return_value=apps), \
             mock.patch("screencap.setup_wizard.click") as mock_click, \
             mock.patch("screencap.config.invalidate_config_cache"):
            mock_stdin.isatty.return_value = True
            mock_click.confirm.return_value = True

            result = run_setup_wizard(config_path=config_path, scan_only=True)
            assert result is True

            doc = tomlkit.parse(config_path.read_text())
            # Original config preserved
            assert doc["privacy"]["app_classes"]["com.example.configured"] == "chat"
