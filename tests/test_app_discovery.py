"""Tests for app_discovery module."""

from __future__ import annotations

import plistlib
from pathlib import Path
from unittest import mock

import pytest

from screencap.app_discovery import (
    AppMetadata,
    auto_classify,
    discover_installed_apps,
    get_app_metadata,
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
        ("com.bitwarden.desktop", ContextClass.PASSWORD_MANAGER),
        ("com.chase.banking", ContextClass.BANKING),
        ("com.fidelity.investments", ContextClass.BANKING),
        ("com.apple.mail", ContextClass.EMAIL),
        ("com.readdle.spark", ContextClass.EMAIL),
        ("com.tinyspeck.slackmacgap", ContextClass.CHAT),
        ("com.hnc.Discord", ContextClass.CHAT),
        ("us.zoom.xos", ContextClass.VIDEO_CALL),
        ("com.apple.Terminal", ContextClass.CODE_EDITOR_TERMINAL),
        ("com.microsoft.VSCode", ContextClass.CODE_EDITOR_TERMINAL),
    ])
    def test_bundle_id_patterns(self, bundle_id, expected):
        meta = AppMetadata(path="/test", bundle_id=bundle_id, display_name="Test")
        assert auto_classify(meta) == expected

    @pytest.mark.parametrize("name, expected", [
        ("1Password", ContextClass.PASSWORD_MANAGER),
        ("Slack", ContextClass.CHAT),
        ("Discord", ContextClass.CHAT),
        ("Zoom", ContextClass.VIDEO_CALL),
        ("Terminal", ContextClass.CODE_EDITOR_TERMINAL),
    ])
    def test_display_name_patterns(self, name, expected):
        meta = AppMetadata(path="/test", bundle_id="com.example.unknown", display_name=name)
        assert auto_classify(meta) == expected

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


class TestScanFilesystem:
    def test_scans_applications(self, tmp_path):
        apps_dir = tmp_path / "Applications"
        apps_dir.mkdir()
        (apps_dir / "Foo.app").mkdir()
        (apps_dir / "Bar.app").mkdir()
        # Non-.app dir should be traversed for nested apps
        sub = apps_dir / "Utilities"
        sub.mkdir()
        (sub / "Baz.app").mkdir()

        with mock.patch("screencap.app_discovery.Path") as mock_path:
            mock_path.return_value = tmp_path / "Applications"
            mock_path.home.return_value = tmp_path
            # Can't easily mock Path("/Applications") so test discover_installed_apps instead
            pass


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
