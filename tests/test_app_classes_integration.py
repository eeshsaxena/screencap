"""Tests for app_classes runtime integration.

Covers: PrivacyConfig.app_classes, parse_privacy_config with app_classes,
and DefaultContextClassifier user config override.
"""

from __future__ import annotations

import pytest

from screencap.privacy.context import DefaultContextClassifier
from screencap.privacy.policy import (
    ContextClass,
    InvalidPrivacyConfigError,
    PrivacyConfig,
    PrivacyMode,
    parse_privacy_config,
)


class TestParsePrivacyConfigAppClasses:
    def test_parses_app_classes(self):
        cfg = parse_privacy_config({
            "privacy": {
                "mode": "internal",
                "app_classes": {
                    "com.tinyspeck.slackmacgap": "chat",
                    "com.apple.mail": "email",
                },
            }
        })
        assert cfg.app_classes["com.tinyspeck.slackmacgap"] == ContextClass.CHAT
        assert cfg.app_classes["com.apple.mail"] == ContextClass.EMAIL

    def test_empty_app_classes(self):
        cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
        assert cfg.app_classes == {}

    def test_invalid_class_value(self):
        with pytest.raises(InvalidPrivacyConfigError, match="not_a_class"):
            parse_privacy_config({
                "privacy": {
                    "mode": "internal",
                    "app_classes": {"com.example.app": "not_a_class"},
                }
            })

    @pytest.mark.parametrize("bad_input, match", [
        (["not", "a", "dict"], "must be a table"),
        ({"com.example.app": 42}, "must be a string"),
    ])
    def test_rejects_malformed_app_classes(self, bad_input, match):
        with pytest.raises(InvalidPrivacyConfigError, match=match):
            parse_privacy_config({
                "privacy": {"mode": "internal", "app_classes": bad_input}
            })


class TestClassifierUserConfigOverride:
    def test_user_config_overrides_hardcoded_map(self):
        """User config should take priority over _BUNDLE_ID_MAP."""
        classifier = DefaultContextClassifier(
            app_classes={"com.tinyspeck.slackmacgap": ContextClass.CODE_EDITOR_TERMINAL}
        )
        from screencap.privacy.policy import FrameMetadata

        result = classifier.classify(
            FrameMetadata(bundle_id="com.tinyspeck.slackmacgap")
        )
        # Hardcoded map says CHAT, but user config says CODE_EDITOR_TERMINAL
        assert result.context_class == ContextClass.CODE_EDITOR_TERMINAL
        assert result.confidence == "user_config"



class TestPostRecordingReport:
    """Tests for _report_unclassified_apps in cli.py."""

    def test_reports_unclassified_apps(self, tmp_path):
        import sqlite3
        from unittest import mock

        # Create a recording DB with window events
        db_path = tmp_path / "recording.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE window_event "
            "(timestamp REAL, app_bundle_id TEXT, title TEXT, window_id TEXT)"
        )
        conn.execute(
            "INSERT INTO window_event VALUES (1.0, 'com.figma.Desktop', 'Figma', 'w1')"
        )
        conn.execute(
            "INSERT INTO window_event VALUES (2.0, 'com.microsoft.VSCode', 'VS Code', 'w2')"
        )
        conn.commit()
        conn.close()

        config = PrivacyConfig(mode=PrivacyMode.INTERNAL)

        with mock.patch("screencap.catalog.find_db", return_value=db_path), \
             mock.patch("screencap.config.get_privacy_config", return_value=config):
            from screencap.cli import _report_unclassified_apps

            # VSCode is in _BUNDLE_ID_MAP, Figma is not
            _report_unclassified_apps(tmp_path)
            # Should not raise; Figma should be reported as unclassified

    def test_no_report_when_all_classified(self, tmp_path):
        import sqlite3
        from unittest import mock

        db_path = tmp_path / "recording.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE window_event "
            "(timestamp REAL, app_bundle_id TEXT, title TEXT, window_id TEXT)"
        )
        conn.execute(
            "INSERT INTO window_event VALUES (1.0, 'com.microsoft.VSCode', 'VS Code', 'w1')"
        )
        conn.commit()
        conn.close()

        config = PrivacyConfig(mode=PrivacyMode.INTERNAL)

        with mock.patch("screencap.catalog.find_db", return_value=db_path), \
             mock.patch("screencap.config.get_privacy_config", return_value=config), \
             mock.patch("screencap.cli.console") as mock_console:
            from screencap.cli import _report_unclassified_apps
            _report_unclassified_apps(tmp_path)
            # Should not print anything since VSCode is in _BUNDLE_ID_MAP
            mock_console.print.assert_not_called()


class TestEndToEndConfigToAction:
    """Config → classifier → evaluator → action pipeline."""

    def test_user_classified_app_gets_correct_action(self):
        from screencap.privacy.policy import (
            DefaultPolicyEvaluator,
            FrameMetadata,
        )

        config = PrivacyConfig(
            mode=PrivacyMode.PUBLIC,
            app_classes={"com.figma.Desktop": ContextClass.CODE_EDITOR_TERMINAL},
        )
        classifier = DefaultContextClassifier(app_classes=config.app_classes)
        evaluator = DefaultPolicyEvaluator(config)

        meta = FrameMetadata(bundle_id="com.figma.Desktop")
        ctx = classifier.classify(meta)
        decision = evaluator.evaluate(ctx, meta)

        # code_editor_terminal in public mode → OCR_FALLBACK
        from screencap.privacy.actions import PrivacyAction
        assert decision.action == PrivacyAction.OCR_FALLBACK
