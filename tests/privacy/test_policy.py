"""Tests for privacy v3 policy core: matrix, config, evaluator."""

from __future__ import annotations

import pytest

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.policy import (
    ContextClass,
    ContextResult,
    DefaultPolicyEvaluator,
    FrameMetadata,
    InvalidPrivacyConfigError,
    PrivacyConfig,
    PrivacyMode,
    get_matrix_action,
    parse_privacy_config,
)
from screencap.privacy.reasons import ReasonCode

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Action matrix security invariants
# ---------------------------------------------------------------------------


class TestActionMatrix:
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

    def test_unknown_fails_closed_in_public(self):
        assert (
            get_matrix_action(ContextClass.UNKNOWN, PrivacyMode.PUBLIC)
            == PrivacyAction.MASK_WINDOW
        )


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

    def test_mode_parsing(self):
        cfg = parse_privacy_config({"privacy": {"mode": "public"}})
        assert cfg.mode == PrivacyMode.PUBLIC
        # case insensitive
        cfg = parse_privacy_config({"privacy": {"mode": "PUBLIC"}})
        assert cfg.mode == PrivacyMode.PUBLIC

    @pytest.mark.parametrize("config,match", [
        ({"privacy": {"mode": "paranoid"}}, "Invalid privacy.mode"),
        ({"privacy": {"exclude_apps": "not-a-list"}}, "must be a list"),
    ])
    def test_invalid_config_rejected(self, config, match):
        with pytest.raises(InvalidPrivacyConfigError, match=match):
            parse_privacy_config(config)

    def test_mask_domains_lowered(self):
        cfg = parse_privacy_config(
            {"privacy": {"mask_domains": ["Mail.Google.Com"]}}
        )
        assert "mail.google.com" in cfg.mask_domains

    def test_invalid_regex_rejected(self):
        with pytest.raises(InvalidPrivacyConfigError, match="not a valid regex"):
            parse_privacy_config({"privacy": {"mask_title_patterns": ["(unclosed"]}})

    def test_allow_apps_parsing(self):
        cfg = parse_privacy_config(
            {"privacy": {"allow_apps": ["com.apple.Finder", "com.apple.Preview"]}}
        )
        assert cfg.allow_apps == frozenset({"com.apple.Finder", "com.apple.Preview"})

    def test_allow_apps_defaults_to_empty(self):
        cfg = parse_privacy_config({})
        assert cfg.allow_apps == frozenset()

    def test_allow_apps_invalid_type_rejected(self):
        with pytest.raises(InvalidPrivacyConfigError, match="must be a list"):
            parse_privacy_config({"privacy": {"allow_apps": "not-a-list"}})

    def test_env_var_overrides_mode(self, monkeypatch):
        monkeypatch.setenv("SCREENCAP_PRIVACY_MODE", "public")
        cfg = parse_privacy_config({"privacy": {"mode": "internal"}})
        assert cfg.mode == PrivacyMode.PUBLIC


# ---------------------------------------------------------------------------
# Domain matching (subtle suffix logic — security-relevant)
# ---------------------------------------------------------------------------


def test_subdomain_matching():
    cfg = parse_privacy_config({"privacy": {"mask_domains": ["google.com"]}})
    assert cfg.is_masked_domain("mail.google.com")
    assert cfg.is_masked_domain("google.com")
    assert not cfg.is_masked_domain("notgoogle.com")


# ---------------------------------------------------------------------------
# DefaultPolicyEvaluator
# ---------------------------------------------------------------------------


class TestDefaultPolicyEvaluator:
    def _make_evaluator(self, **kwargs) -> DefaultPolicyEvaluator:
        cfg = parse_privacy_config({"privacy": kwargs})
        return DefaultPolicyEvaluator(cfg)

    def test_evaluate_precedence_chain(self):
        """app exclusion > domain mask > title mask > matrix default."""
        ev = self._make_evaluator(
            mode="internal",
            exclude_apps=["com.example.app"],
            mask_domains=["example.com"],
            mask_title_patterns=["(?i)example"],
        )
        ctx = ContextResult(ContextClass.CODE_EDITOR_TERMINAL)

        # All three rules match — app exclusion wins
        meta = FrameMetadata(
            bundle_id="com.example.app",
            domain="example.com",
            window_title="Example App",
        )
        d = ev.evaluate(ctx, meta)
        assert d.action == PrivacyAction.EXCLUDE
        assert d.reason == ReasonCode.POLICY_EXCLUDED_APP

        # Domain + title match, no app — domain wins
        meta = FrameMetadata(domain="example.com", window_title="Example App")
        d = ev.evaluate(ctx, meta)
        assert d.action == PrivacyAction.MASK_WINDOW
        assert d.reason == ReasonCode.POLICY_MASKED_DOMAIN

        # Title only — title wins
        meta = FrameMetadata(window_title="Example App")
        d = ev.evaluate(ctx, meta)
        assert d.action == PrivacyAction.MASK_WINDOW
        assert d.reason == ReasonCode.POLICY_MASKED_TITLE

        # No metadata — matrix default (CODE_EDITOR_TERMINAL + INTERNAL = ALLOW)
        d = ev.evaluate(ctx, FrameMetadata())
        assert d.action == PrivacyAction.ALLOW
        assert d.reason == ReasonCode.CONTEXT_CODE_EDITOR_TERMINAL

    def test_allow_apps_returns_allow(self):
        """allow_apps grants ALLOW regardless of mode."""
        ev = self._make_evaluator(mode="public", allow_apps=["com.example.app"])
        ctx = ContextResult(ContextClass.UNKNOWN)
        meta = FrameMetadata(bundle_id="com.example.app")
        d = ev.evaluate(ctx, meta)
        assert d.action == PrivacyAction.ALLOW
        assert d.reason == ReasonCode.POLICY_ALLOWED_APP

    def test_exclude_apps_beats_allow_apps(self):
        """exclude_apps takes precedence over allow_apps."""
        ev = self._make_evaluator(
            mode="public",
            exclude_apps=["com.example.app"],
            allow_apps=["com.example.app"],
        )
        ctx = ContextResult(ContextClass.UNKNOWN)
        meta = FrameMetadata(bundle_id="com.example.app")
        d = ev.evaluate(ctx, meta)
        assert d.action == PrivacyAction.EXCLUDE

    def test_allow_apps_beats_domain_mask(self):
        """allow_apps takes precedence over domain mask rules."""
        ev = self._make_evaluator(
            mode="public",
            allow_apps=["com.example.app"],
            mask_domains=["example.com"],
        )
        ctx = ContextResult(ContextClass.UNKNOWN)
        meta = FrameMetadata(bundle_id="com.example.app", domain="example.com")
        d = ev.evaluate(ctx, meta)
        assert d.action == PrivacyAction.ALLOW
        assert d.reason == ReasonCode.POLICY_ALLOWED_APP

    def test_allow_apps_beats_title_mask(self):
        """allow_apps takes precedence over title mask rules."""
        ev = self._make_evaluator(
            mode="public",
            allow_apps=["com.example.app"],
            mask_title_patterns=["(?i)secret"],
        )
        ctx = ContextResult(ContextClass.UNKNOWN)
        meta = FrameMetadata(bundle_id="com.example.app", window_title="Secret Window")
        d = ev.evaluate(ctx, meta)
        assert d.action == PrivacyAction.ALLOW

    def test_domain_mask_defers_to_stricter_matrix_action(self):
        """When matrix already says EXCLUDE, domain mask doesn't weaken it."""
        ev = self._make_evaluator(
            mode="internal",
            mask_domains=["example.com"],
        )
        ctx = ContextResult(ContextClass.PASSWORD_MANAGER)
        meta = FrameMetadata(domain="example.com")
        decision = ev.evaluate(ctx, meta)
        assert decision.action == PrivacyAction.EXCLUDE


# ---------------------------------------------------------------------------
# Config accessor integration
# ---------------------------------------------------------------------------


def test_get_privacy_config_from_toml(monkeypatch, tmp_path):
    from screencap import config

    toml_content = b'[privacy]\nmode = "public"\n'
    config_file = tmp_path / "config.toml"
    config_file.write_bytes(toml_content)

    monkeypatch.setattr(config, "_CONFIG_PATH", config_file)
    monkeypatch.setattr(config, "_config_cache", None)

    result = config.get_privacy_config()
    assert isinstance(result, PrivacyConfig)
    assert result.mode == PrivacyMode.PUBLIC
