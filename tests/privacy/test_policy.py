"""Tests for privacy v3 policy core: enums, matrix, config, evaluator."""

from __future__ import annotations

import re

import pytest

from screencap.privacy.actions import ActionDecision, PrivacyAction, stricter
from screencap.privacy.policy import (
    ContextClass,
    ContextResult,
    DefaultPolicyEvaluator,
    FrameMetadata,
    InvalidPrivacyConfigError,
    PrivacyConfig,
    PrivacyMode,
    _ACTION_MATRIX,
    get_matrix_action,
    parse_privacy_config,
)
from screencap.privacy.reasons import ReasonCode

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Action matrix completeness
# ---------------------------------------------------------------------------


class TestActionMatrix:
    def test_every_pair_covered(self):
        """Every (ContextClass, PrivacyMode) maps to exactly one action."""
        for ctx in ContextClass:
            for mode in PrivacyMode:
                action = get_matrix_action(ctx, mode)
                assert isinstance(action, PrivacyAction), (
                    f"Missing or bad entry: ({ctx.value}, {mode.value})"
                )

    def test_matrix_size(self):
        expected = len(ContextClass) * len(PrivacyMode)
        assert len(_ACTION_MATRIX) == expected

    def test_unknown_fails_closed_in_public(self):
        assert (
            get_matrix_action(ContextClass.UNKNOWN, PrivacyMode.PUBLIC)
            == PrivacyAction.MASK_WINDOW
        )

    def test_browser_unverified_fails_closed_in_public(self):
        assert (
            get_matrix_action(ContextClass.BROWSER_UNVERIFIED, PrivacyMode.PUBLIC)
            == PrivacyAction.MASK_WINDOW
        )

    def test_video_call_mask_window_in_public(self):
        assert (
            get_matrix_action(ContextClass.VIDEO_CALL, PrivacyMode.PUBLIC)
            == PrivacyAction.MASK_WINDOW
        )

    def test_password_manager_always_excluded(self):
        for mode in PrivacyMode:
            assert (
                get_matrix_action(ContextClass.PASSWORD_MANAGER, mode)
                == PrivacyAction.EXCLUDE
            )

    def test_stricter_mode_never_less_restrictive(self):
        """For every context, public action is at least as strict as internal."""
        severity = {
            PrivacyAction.EXCLUDE: 0,
            PrivacyAction.MASK_WINDOW: 1,
            PrivacyAction.MASK_REGION: 2,
            PrivacyAction.TEXT_REDACT: 3,
            PrivacyAction.OCR_FALLBACK: 4,
            PrivacyAction.ALLOW: 5,
        }
        for ctx in ContextClass:
            pub = get_matrix_action(ctx, PrivacyMode.PUBLIC)
            internal = get_matrix_action(ctx, PrivacyMode.INTERNAL)
            assert severity[pub] <= severity[internal], (
                f"{ctx.value}: public={pub.value} is less strict than internal={internal.value}"
            )


# ---------------------------------------------------------------------------
# PrivacyAction helpers
# ---------------------------------------------------------------------------


class TestStricter:
    def test_exclude_beats_allow(self):
        assert stricter(PrivacyAction.EXCLUDE, PrivacyAction.ALLOW) == PrivacyAction.EXCLUDE

    def test_mask_window_beats_ocr(self):
        assert stricter(PrivacyAction.MASK_WINDOW, PrivacyAction.OCR_FALLBACK) == PrivacyAction.MASK_WINDOW

    def test_same_action(self):
        assert stricter(PrivacyAction.ALLOW, PrivacyAction.ALLOW) == PrivacyAction.ALLOW

    def test_symmetric(self):
        a, b = PrivacyAction.MASK_REGION, PrivacyAction.TEXT_REDACT
        assert stricter(a, b) == stricter(b, a)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestParsePrivacyConfig:
    def test_empty_dict_defaults(self):
        cfg = parse_privacy_config({})
        assert cfg.mode == PrivacyMode.INTERNAL
        assert cfg.exclude_apps == frozenset()
        assert cfg.mask_domains == frozenset()
        assert cfg.mask_title_patterns == ()

    def test_public_mode(self):
        cfg = parse_privacy_config({"privacy": {"mode": "public"}})
        assert cfg.mode == PrivacyMode.PUBLIC

    def test_case_insensitive_mode(self):
        cfg = parse_privacy_config({"privacy": {"mode": "PUBLIC"}})
        assert cfg.mode == PrivacyMode.PUBLIC

    def test_invalid_mode(self):
        with pytest.raises(InvalidPrivacyConfigError, match="Invalid privacy.mode"):
            parse_privacy_config({"privacy": {"mode": "paranoid"}})

    def test_mode_not_string(self):
        with pytest.raises(InvalidPrivacyConfigError, match="must be a string"):
            parse_privacy_config({"privacy": {"mode": 42}})

    def test_privacy_section_not_table(self):
        with pytest.raises(InvalidPrivacyConfigError, match="must be a table"):
            parse_privacy_config({"privacy": "oops"})

    def test_exclude_apps(self):
        cfg = parse_privacy_config(
            {"privacy": {"exclude_apps": ["com.1password.1password", "com.apple.MobileSMS"]}}
        )
        assert "com.1password.1password" in cfg.exclude_apps
        assert "com.apple.MobileSMS" in cfg.exclude_apps

    def test_exclude_apps_not_list(self):
        with pytest.raises(InvalidPrivacyConfigError, match="must be a list"):
            parse_privacy_config({"privacy": {"exclude_apps": "not-a-list"}})

    def test_exclude_apps_element_not_string(self):
        with pytest.raises(InvalidPrivacyConfigError, match="must be a string"):
            parse_privacy_config({"privacy": {"exclude_apps": [123]}})

    def test_mask_domains_lowered(self):
        cfg = parse_privacy_config(
            {"privacy": {"mask_domains": ["Mail.Google.Com"]}}
        )
        assert "mail.google.com" in cfg.mask_domains

    def test_mask_domains_not_list(self):
        with pytest.raises(InvalidPrivacyConfigError, match="must be a list"):
            parse_privacy_config({"privacy": {"mask_domains": "oops"}})

    def test_mask_title_patterns_compiled(self):
        cfg = parse_privacy_config(
            {"privacy": {"mask_title_patterns": ["(?i)inbox", "DM"]}}
        )
        assert len(cfg.mask_title_patterns) == 2
        assert cfg.mask_title_patterns[0].search("My Inbox")
        assert cfg.mask_title_patterns[1].search("DM with Bob")

    def test_invalid_regex_rejected(self):
        with pytest.raises(InvalidPrivacyConfigError, match="not a valid regex"):
            parse_privacy_config({"privacy": {"mask_title_patterns": ["(unclosed"]}})

    def test_mask_title_patterns_not_list(self):
        with pytest.raises(InvalidPrivacyConfigError, match="must be a list"):
            parse_privacy_config({"privacy": {"mask_title_patterns": 42}})

    def test_env_var_overrides_mode(self, monkeypatch):
        monkeypatch.setenv("SCREENCAP_PRIVACY_MODE", "public")
        cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
        assert cfg.mode == PrivacyMode.PUBLIC

    def test_invalid_env_var_mode(self, monkeypatch):
        monkeypatch.setenv("SCREENCAP_PRIVACY_MODE", "paranoid")
        with pytest.raises(InvalidPrivacyConfigError, match="Invalid privacy.mode"):
            parse_privacy_config({})


# ---------------------------------------------------------------------------
# PrivacyConfig methods
# ---------------------------------------------------------------------------


class TestPrivacyConfigMethods:
    def _make_config(self, **kwargs) -> PrivacyConfig:
        return parse_privacy_config({"privacy": kwargs})

    def test_is_excluded_app(self):
        cfg = self._make_config(exclude_apps=["com.1password.1password"])
        assert cfg.is_excluded_app("com.1password.1password")
        assert not cfg.is_excluded_app("com.apple.Safari")

    def test_is_masked_domain_exact(self):
        cfg = self._make_config(mask_domains=["mail.google.com"])
        assert cfg.is_masked_domain("mail.google.com")
        assert cfg.is_masked_domain("Mail.Google.Com")  # case insensitive
        assert not cfg.is_masked_domain("google.com")

    def test_is_masked_domain_subdomain(self):
        cfg = self._make_config(mask_domains=["google.com"])
        assert cfg.is_masked_domain("mail.google.com")
        assert cfg.is_masked_domain("google.com")
        assert not cfg.is_masked_domain("notgoogle.com")

    def test_matches_title_pattern(self):
        cfg = self._make_config(mask_title_patterns=["(?i)inbox"])
        assert cfg.matches_title_pattern("My Inbox - Gmail") == "(?i)inbox"
        assert cfg.matches_title_pattern("GitHub PR") is None


# ---------------------------------------------------------------------------
# DefaultPolicyEvaluator
# ---------------------------------------------------------------------------


class TestDefaultPolicyEvaluator:
    def _make_evaluator(self, **kwargs) -> DefaultPolicyEvaluator:
        cfg = parse_privacy_config({"privacy": kwargs})
        return DefaultPolicyEvaluator(cfg)

    def test_matrix_lookup(self):
        ev = self._make_evaluator(mode="public")
        ctx = ContextResult(ContextClass.EMAIL, evidence="com.apple.mail")
        decision = ev.evaluate(ctx)
        assert decision.action == PrivacyAction.MASK_WINDOW
        assert decision.reason == ReasonCode.CONTEXT_EMAIL

    def test_excluded_app_overrides_matrix(self):
        ev = self._make_evaluator(
            mode="internal",
            exclude_apps=["com.slack.Slack"],
        )
        ctx = ContextResult(ContextClass.CHAT, evidence="com.slack.Slack")
        decision = ev.evaluate(ctx)
        assert decision.action == PrivacyAction.EXCLUDE
        assert decision.reason == ReasonCode.POLICY_EXCLUDED_APP

    def test_title_pattern_escalates(self):
        ev = self._make_evaluator(
            mode="internal",
            mask_title_patterns=["(?i)inbox"],
        )
        ctx = ContextResult(
            ContextClass.CODE_EDITOR_TERMINAL,
            evidence="My Inbox - Gmail",
        )
        decision = ev.evaluate(ctx)
        # Title match forces at least MASK_WINDOW, which is stricter than ALLOW
        assert decision.action == PrivacyAction.MASK_WINDOW
        assert decision.reason == ReasonCode.POLICY_MASKED_TITLE

    def test_unknown_public_fail_closed(self):
        ev = self._make_evaluator(mode="public")
        ctx = ContextResult(ContextClass.UNKNOWN)
        decision = ev.evaluate(ctx)
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_evaluate_with_metadata_app_exclusion(self):
        ev = self._make_evaluator(
            mode="internal",
            exclude_apps=["com.1password.1password"],
        )
        ctx = ContextResult(ContextClass.UNKNOWN)
        meta = FrameMetadata(bundle_id="com.1password.1password")
        decision = ev.evaluate_with_metadata(ctx, meta)
        assert decision.action == PrivacyAction.EXCLUDE
        assert decision.reason == ReasonCode.POLICY_EXCLUDED_APP

    def test_evaluate_with_metadata_domain_mask(self):
        ev = self._make_evaluator(
            mode="internal",
            mask_domains=["mail.google.com"],
        )
        ctx = ContextResult(ContextClass.BROWSER_UNVERIFIED)
        meta = FrameMetadata(domain="mail.google.com")
        decision = ev.evaluate_with_metadata(ctx, meta)
        assert decision.action == PrivacyAction.MASK_WINDOW
        assert decision.reason == ReasonCode.POLICY_MASKED_DOMAIN

    def test_evaluate_with_metadata_title_mask(self):
        ev = self._make_evaluator(
            mode="internal",
            mask_title_patterns=["(?i)1password"],
        )
        ctx = ContextResult(ContextClass.UNKNOWN)
        meta = FrameMetadata(window_title="1Password — Vault")
        decision = ev.evaluate_with_metadata(ctx, meta)
        assert decision.action == PrivacyAction.MASK_WINDOW
        assert decision.reason == ReasonCode.POLICY_MASKED_TITLE

    def test_evaluate_with_metadata_precedence(self):
        """App exclusion beats domain mask beats title mask."""
        ev = self._make_evaluator(
            mode="internal",
            exclude_apps=["com.example.app"],
            mask_domains=["example.com"],
            mask_title_patterns=["(?i)example"],
        )
        ctx = ContextResult(ContextClass.CODE_EDITOR_TERMINAL)
        meta = FrameMetadata(
            bundle_id="com.example.app",
            domain="example.com",
            window_title="Example App",
        )
        decision = ev.evaluate_with_metadata(ctx, meta)
        # App exclusion wins
        assert decision.action == PrivacyAction.EXCLUDE
        assert decision.reason == ReasonCode.POLICY_EXCLUDED_APP

    def test_mode_override(self):
        ev = self._make_evaluator(mode="internal")
        ctx = ContextResult(ContextClass.UNKNOWN)
        decision = ev.evaluate(ctx, mode=PrivacyMode.PUBLIC)
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_internal_allows_unknown(self):
        ev = self._make_evaluator(mode="internal")
        ctx = ContextResult(ContextClass.UNKNOWN)
        decision = ev.evaluate(ctx)
        assert decision.action == PrivacyAction.ALLOW


# ---------------------------------------------------------------------------
# ActionDecision
# ---------------------------------------------------------------------------


class TestActionDecision:
    def test_frozen(self):
        d = ActionDecision(
            action=PrivacyAction.EXCLUDE,
            reason=ReasonCode.POLICY_EXCLUDED_APP,
        )
        with pytest.raises(AttributeError):
            d.action = PrivacyAction.ALLOW  # type: ignore[misc]

    def test_evidence_optional(self):
        d = ActionDecision(
            action=PrivacyAction.ALLOW,
            reason=ReasonCode.ALLOWED,
        )
        assert d.evidence == ""


# ---------------------------------------------------------------------------
# ReasonCode
# ---------------------------------------------------------------------------


class TestReasonCode:
    def test_all_reason_codes_are_strings(self):
        codes = {
            v
            for k, v in vars(ReasonCode).items()
            if not k.startswith("_") and isinstance(v, str)
        }
        assert len(codes) >= 15  # sanity check — we defined 15+
        for code in codes:
            assert isinstance(code, str)
            assert code  # non-empty

    def test_no_duplicates(self):
        codes = [
            v
            for k, v in vars(ReasonCode).items()
            if not k.startswith("_") and isinstance(v, str)
        ]
        assert len(codes) == len(set(codes))


# ---------------------------------------------------------------------------
# Config accessor in config.py
# ---------------------------------------------------------------------------


class TestGetPrivacyConfig:
    def test_returns_privacy_config(self, monkeypatch, tmp_path):
        from screencap import config

        toml_content = b'[privacy]\nmode = "public"\n'
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(toml_content)

        monkeypatch.setattr(config, "_CONFIG_PATH", config_file)
        monkeypatch.setattr(config, "_config_cache", None)

        result = config.get_privacy_config()
        assert isinstance(result, PrivacyConfig)
        assert result.mode == PrivacyMode.PUBLIC

    def test_default_when_no_section(self, monkeypatch):
        from screencap import config

        monkeypatch.setattr(config, "_config_cache", {})
        result = config.get_privacy_config()
        assert result.mode == PrivacyMode.INTERNAL
