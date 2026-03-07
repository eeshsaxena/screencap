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

    def test_non_dict_app_classes(self):
        with pytest.raises(InvalidPrivacyConfigError, match="must be a table"):
            parse_privacy_config({
                "privacy": {
                    "mode": "internal",
                    "app_classes": ["not", "a", "dict"],
                }
            })

    def test_non_string_class_value(self):
        with pytest.raises(InvalidPrivacyConfigError, match="must be a string"):
            parse_privacy_config({
                "privacy": {
                    "mode": "internal",
                    "app_classes": {"com.example.app": 42},
                }
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

    def test_without_user_config_uses_hardcoded(self):
        """Without user config, falls back to hardcoded map."""
        classifier = DefaultContextClassifier()
        from screencap.privacy.policy import FrameMetadata

        result = classifier.classify(
            FrameMetadata(bundle_id="com.tinyspeck.slackmacgap")
        )
        assert result.context_class == ContextClass.CHAT
        assert result.confidence == "bundle_id"

    def test_user_config_unknown_app(self):
        """User config for an app not in hardcoded map."""
        classifier = DefaultContextClassifier(
            app_classes={"com.figma.Desktop": ContextClass.UNKNOWN}
        )
        from screencap.privacy.policy import FrameMetadata

        result = classifier.classify(
            FrameMetadata(bundle_id="com.figma.Desktop")
        )
        assert result.context_class == ContextClass.UNKNOWN
        assert result.confidence == "user_config"


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
