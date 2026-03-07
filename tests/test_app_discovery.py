"""Tests for app_discovery module."""

from __future__ import annotations

import plistlib
from pathlib import Path
from unittest import mock

import pytest

from screencap.app_discovery import (
    AppMetadata,
    ClassificationResult,
    auto_classify,
    auto_classify_detailed,
    discover_installed_apps,
    get_app_metadata,
    is_background_app,
    _scan_filesystem,
    _scan_spotlight,
)
from screencap.privacy.policy import ContextClass


class TestGetAppMetadata:
    def test_valid_app_bundle(self, tmp_path):
        app = tmp_path / "Test.app" / "Contents"
        app.mkdir(parents=True)
        plist = {
            "CFBundleIdentifier": "com.example.test",
            "CFBundleDisplayName": "Test App",
            "LSApplicationCategoryType": "public.app-category.developer-tools",
        }
        with open(app / "Info.plist", "wb") as f:
            plistlib.dump(plist, f)

        result = get_app_metadata(tmp_path / "Test.app")
        assert result is not None
        assert result.bundle_id == "com.example.test"
        assert result.display_name == "Test App"
        assert result.category == "public.app-category.developer-tools"

    def test_missing_plist(self, tmp_path):
        app = tmp_path / "NoInfo.app"
        app.mkdir()
        assert get_app_metadata(app) is None

    def test_no_bundle_id(self, tmp_path):
        app = tmp_path / "NoBID.app" / "Contents"
        app.mkdir(parents=True)
        plist = {"CFBundleName": "No Bundle ID"}
        with open(app / "Info.plist", "wb") as f:
            plistlib.dump(plist, f)
        assert get_app_metadata(tmp_path / "NoBID.app") is None

    def test_fallback_display_name(self, tmp_path):
        app = tmp_path / "MyApp.app" / "Contents"
        app.mkdir(parents=True)
        plist = {"CFBundleIdentifier": "com.example.myapp"}
        with open(app / "Info.plist", "wb") as f:
            plistlib.dump(plist, f)

        result = get_app_metadata(tmp_path / "MyApp.app")
        assert result.display_name == "MyApp"  # stem of .app path


class TestAutoClassify:
    @pytest.mark.parametrize("bundle_id, expected", [
        ("com.1password.1password", ContextClass.PASSWORD_MANAGER),
        ("com.chase.banking", ContextClass.BANKING),
        ("com.tinyspeck.slackmacgap", ContextClass.CHAT),
        ("com.apple.Terminal", ContextClass.CODE_EDITOR_TERMINAL),
    ])
    def test_bundle_id_patterns(self, bundle_id, expected):
        meta = AppMetadata(path="/test", bundle_id=bundle_id, display_name="Test")
        assert auto_classify(meta) == expected

    def test_display_name_fallback(self):
        """When bundle_id has no match, display name is checked."""
        meta = AppMetadata(path="/test", bundle_id="com.example.unknown", display_name="Slack")
        assert auto_classify(meta) == ContextClass.CHAT

    def test_bundle_id_wins_over_display_name(self):
        """Bundle ID pattern match takes priority over display name match."""
        meta = AppMetadata(
            path="/test",
            bundle_id="com.example.slack",  # matches CHAT via bundle_id
            display_name="Finance Tracker",  # would match BANKING via name
        )
        assert auto_classify(meta) == ContextClass.CHAT

    def test_category_classification(self):
        meta = AppMetadata(
            path="/test",
            bundle_id="com.example.thing",
            display_name="Thing",
            category="public.app-category.finance",
        )
        assert auto_classify(meta) == ContextClass.BANKING

    def test_unknown_app(self):
        meta = AppMetadata(
            path="/test",
            bundle_id="com.example.random",
            display_name="RandomApp",
        )
        assert auto_classify(meta) == ContextClass.UNKNOWN


class TestAutoClassifyDetailed:
    """Tests for auto_classify_detailed with source provenance."""

    def test_known_app_db(self):
        meta = AppMetadata(path="/test", bundle_id="com.tinyspeck.slackmacgap", display_name="Slack")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.CHAT
        assert result.source == "known_app"

    def test_apple_sensitive_mail(self):
        """com.apple.mail is in BUNDLE_ID_MAP, so it matches as known_app."""
        meta = AppMetadata(path="/test", bundle_id="com.apple.mail", display_name="Mail")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.source == "known_app"

    def test_apple_sensitive_messages(self):
        """com.apple.MobileSMS is in BUNDLE_ID_MAP, so it matches as known_app."""
        meta = AppMetadata(path="/test", bundle_id="com.apple.MobileSMS", display_name="Messages")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.CHAT
        assert result.source == "known_app"

    def test_apple_passwords_in_known_db(self):
        """com.apple.Passwords is in BUNDLE_ID_MAP, matches as known_app."""
        meta = AppMetadata(path="/test", bundle_id="com.apple.Passwords", display_name="Passwords")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.PASSWORD_MANAGER
        assert result.source == "known_app"

    def test_apple_facetime_in_known_db(self):
        """com.apple.FaceTime is in BUNDLE_ID_MAP, matches as known_app."""
        meta = AppMetadata(path="/test", bundle_id="com.apple.FaceTime", display_name="FaceTime")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.VIDEO_CALL
        assert result.source == "known_app"

    def test_apple_prefix_generic(self):
        meta = AppMetadata(path="/test", bundle_id="com.apple.Preview", display_name="Preview")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.UNKNOWN
        assert result.source == "apple_prefix"

    def test_known_app_takes_priority_over_apple_prefix(self):
        """Apps in BUNDLE_ID_MAP must not fall through to the generic prefix rule."""
        meta = AppMetadata(path="/test", bundle_id="com.apple.iCal", display_name="Calendar")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.CALENDAR
        assert result.source == "known_app"

    def test_apple_sensitive_not_in_known_db(self):
        """Apple sensitive apps not in BUNDLE_ID_MAP use the apple_sensitive layer."""
        meta = AppMetadata(path="/test", bundle_id="com.apple.Messages", display_name="Messages")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.CHAT
        assert result.source == "apple_sensitive"

    def test_system_service_pattern(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.foo", display_name="SpotlightHelper")
        result = auto_classify_detailed(meta)
        assert result.source == "system_service"

    def test_input_method_pattern(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.foo", display_name="JapaneseIM")
        result = auto_classify_detailed(meta)
        assert result.source == "input_method"

    def test_lifecycle_pattern(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.foo", display_name="AppInstaller")
        result = auto_classify_detailed(meta)
        assert result.source == "lifecycle"

    def test_decoration_pattern(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.foo", display_name="ScreenSaverEngine")
        result = auto_classify_detailed(meta)
        assert result.source == "decoration"

    def test_pattern_rule_bundle_id(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.discord", display_name="MyApp")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.CHAT
        assert result.source == "pattern_rule"

    def test_pattern_rule_display_name(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.foo", display_name="Slack Chat")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.CHAT
        assert result.source == "pattern_rule"

    def test_category_map(self):
        meta = AppMetadata(
            path="/test", bundle_id="com.example.thing", display_name="Thing",
            category="public.app-category.finance",
        )
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.BANKING
        assert result.source == "category_map"

    def test_category_safe(self):
        meta = AppMetadata(
            path="/test", bundle_id="com.example.thing", display_name="Thing",
            category="public.app-category.games",
        )
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.UNKNOWN
        assert result.source == "category_safe"

    def test_known_browser(self):
        meta = AppMetadata(path="/test", bundle_id="com.google.Chrome", display_name="Google Chrome")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED
        assert result.source == "known_browser"

    def test_dev_runtime(self):
        meta = AppMetadata(path="/test", bundle_id="org.python.python", display_name="Python")
        result = auto_classify_detailed(meta)
        assert result.source == "dev_runtime"

    def test_browser_pwa(self):
        meta = AppMetadata(path="/test", bundle_id="com.google.Chrome.app.xyz123", display_name="YouTube")
        result = auto_classify_detailed(meta)
        assert result.source == "browser_pwa"

    def test_lifecycle_update_suffix(self):
        meta = AppMetadata(path="/test", bundle_id="com.microsoft.foo", display_name="Visual Studio Update")
        result = auto_classify_detailed(meta)
        assert result.source == "lifecycle"

    def test_system_service_srv_suffix(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.foo", display_name="pteiddialogsQTsrv")
        result = auto_classify_detailed(meta)
        assert result.source == "system_service"

    def test_wallet_category(self):
        meta = AppMetadata(path="/test", bundle_id="com.ledger.live", display_name="Ledger", category="public.app-category.wallet")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.BANKING
        assert result.source == "category_map"

    def test_unknown_fallback(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.random", display_name="RandomApp")
        result = auto_classify_detailed(meta)
        assert result.context_class == ContextClass.UNKNOWN
        assert result.source == "unknown"

    def test_wrapper_compatibility(self):
        """auto_classify wrapper returns same ContextClass for pattern-matched apps."""
        meta = AppMetadata(path="/test", bundle_id="com.example.discord", display_name="MyApp")
        assert auto_classify(meta) == auto_classify_detailed(meta).context_class


class TestBackgroundAppDetection:
    def test_plist_lsuielement(self, tmp_path):
        app = tmp_path / "Agent.app" / "Contents"
        app.mkdir(parents=True)
        plist = {
            "CFBundleIdentifier": "com.example.agent",
            "CFBundleName": "Agent",
            "LSUIElement": True,
        }
        with open(app / "Info.plist", "wb") as f:
            plistlib.dump(plist, f)
        result = get_app_metadata(tmp_path / "Agent.app")
        assert result is not None
        assert result.is_background is True

    def test_plist_lsbackgroundonly(self, tmp_path):
        app = tmp_path / "Daemon.app" / "Contents"
        app.mkdir(parents=True)
        plist = {
            "CFBundleIdentifier": "com.example.daemon",
            "CFBundleName": "Daemon",
            "LSBackgroundOnly": True,
        }
        with open(app / "Info.plist", "wb") as f:
            plistlib.dump(plist, f)
        result = get_app_metadata(tmp_path / "Daemon.app")
        assert result is not None
        assert result.is_background is True

    def test_not_background(self, tmp_path):
        app = tmp_path / "Normal.app" / "Contents"
        app.mkdir(parents=True)
        plist = {
            "CFBundleIdentifier": "com.example.normal",
            "CFBundleName": "Normal",
        }
        with open(app / "Info.plist", "wb") as f:
            plistlib.dump(plist, f)
        result = get_app_metadata(tmp_path / "Normal.app")
        assert result is not None
        assert result.is_background is False

    def test_is_background_app_from_name(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.foo", display_name="CoreLocationAgent")
        assert is_background_app(meta) is True

    def test_is_background_app_input_method(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.foo", display_name="JapaneseIM")
        assert is_background_app(meta) is True

    def test_is_background_app_normal(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.foo", display_name="Safari")
        assert is_background_app(meta) is False

    def test_is_background_from_plist_flag(self):
        meta = AppMetadata(path="/test", bundle_id="com.example.foo", display_name="NormalName", is_background=True)
        assert is_background_app(meta) is True


class TestScanSpotlight:
    def test_timeout_returns_empty(self):
        with mock.patch("screencap.app_discovery.subprocess.run") as mock_run:
            mock_run.side_effect = __import__("subprocess").TimeoutExpired(
                cmd="mdfind", timeout=5
            )
            assert _scan_spotlight(timeout=5.0) == []

    def test_success_returns_paths(self):
        with mock.patch("screencap.app_discovery.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="/Applications/Foo.app\n/Applications/Bar.app\n",
            )
            result = _scan_spotlight()
            assert "/Applications/Foo.app" in result
            assert "/Applications/Bar.app" in result


class TestDiscoverInstalledApps:
    def test_deduplicates_by_bundle_id(self, tmp_path):
        """Same bundle ID from different sources → only one result."""
        app1 = tmp_path / "App1.app" / "Contents"
        app1.mkdir(parents=True)
        app2 = tmp_path / "App1Copy.app" / "Contents"
        app2.mkdir(parents=True)

        plist = {"CFBundleIdentifier": "com.example.app1", "CFBundleName": "App1"}
        for d in [app1, app2]:
            with open(d / "Info.plist", "wb") as f:
                plistlib.dump(plist, f)

        with mock.patch("screencap.app_discovery._scan_filesystem") as mock_fs, \
             mock.patch("screencap.app_discovery._scan_spotlight") as mock_sl:
            mock_fs.return_value = [str(tmp_path / "App1.app")]
            mock_sl.return_value = [str(tmp_path / "App1Copy.app")]

            apps = discover_installed_apps(use_spotlight=True)
            bundle_ids = [a.bundle_id for a in apps]
            assert bundle_ids.count("com.example.app1") == 1

    def test_spotlight_disabled(self, tmp_path):
        app = tmp_path / "Test.app" / "Contents"
        app.mkdir(parents=True)
        plist = {"CFBundleIdentifier": "com.example.test", "CFBundleName": "Test"}
        with open(app / "Info.plist", "wb") as f:
            plistlib.dump(plist, f)

        with mock.patch("screencap.app_discovery._scan_filesystem") as mock_fs, \
             mock.patch("screencap.app_discovery._scan_spotlight") as mock_sl:
            mock_fs.return_value = [str(tmp_path / "Test.app")]

            apps = discover_installed_apps(use_spotlight=False)
            mock_sl.assert_not_called()
            assert len(apps) == 1
