"""Tests for the per-app Mask rule and the default-action floor (SCR-225).

Both rules are strictly tightening: they raise the floor over the (context ×
mode) matrix and can never loosen it. `default_action = allow` is the identity
floor, so a config that does not set either rule must resolve exactly as it did
before SCR-225.

Precedence pinned here (policy.py DefaultPolicyEvaluator):
  exclude_apps > mask_apps > mask_domains/mask_title_patterns >
  confirmed allow (browser carve-out) > legacy-allow floor >
  stricter(default_action, matrix)

U7's scrub-time mask coverage lives at the bottom: once a user can set Mask on
any app, the scrub-time strategy map can no longer assume a class's matrix
action implies whether masking is ever requested.
"""

from __future__ import annotations

import pickle

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


# Bundles with known BUNDLE_ID_MAP classes, so the classifier resolves them.
PASSWORD_MANAGER_BUNDLE = "com.1password.1password"
TERMINAL_BUNDLE = "com.apple.Terminal"
CHAT_BUNDLE = "com.tinyspeck.slackmacgap"
UNMAPPED_BUNDLE = "com.example.somerandomapp"

# The two modes parse_privacy_config actually accepts; SHARED is rejected
# because MASK_REGION is unimplemented.
ENFORCED_MODES = (PrivacyMode.PUBLIC, PrivacyMode.INTERNAL)


def _evaluator(
    *,
    mode="internal",
    exclude_apps=(),
    mask_apps=(),
    allow_apps=(),
    confirmed_allow_apps=(),
    default_action=PrivacyAction.ALLOW,
):
    cfg = PrivacyConfig(
        mode=PrivacyMode(mode),
        exclude_apps=frozenset(exclude_apps),
        mask_apps=frozenset(mask_apps),
        allow_apps=frozenset(allow_apps),
        confirmed_allow_apps=frozenset(confirmed_allow_apps),
        default_action=default_action,
    )
    return DefaultPolicyEvaluator(cfg)


def _eval_classified(evaluator, bundle_id, title="Whatever"):
    """Classify via the real classifier, then evaluate."""
    from screencap.privacy.classify import DefaultContextClassifier

    metadata = FrameMetadata(bundle_id=bundle_id, window_title=title)
    context = DefaultContextClassifier().classify(metadata)
    return evaluator.evaluate(context, metadata)


def _eval_as(evaluator, context_class, bundle_id=UNMAPPED_BUNDLE):
    """Evaluate with an explicit context class, bypassing the classifier."""
    metadata = FrameMetadata(bundle_id=bundle_id, window_title="Whatever")
    context = ContextResult(
        context_class=context_class, confidence="test", evidence=bundle_id
    )
    return evaluator.evaluate(context, metadata)


# ---------------------------------------------------------------------------
# Per-app Mask rule
# ---------------------------------------------------------------------------


class TestPerAppMask:
    def test_mask_rule_never_weakens_a_stricter_matrix_action(self):
        """A user Mask on a password manager stays EXCLUDE — Mask cannot loosen."""
        evaluator = _evaluator(mask_apps=[PASSWORD_MANAGER_BUNDLE])
        decision = _eval_classified(evaluator, PASSWORD_MANAGER_BUNDLE)
        assert decision.action == PrivacyAction.EXCLUDE

    def test_mask_rule_tightens_an_allow_class(self):
        """A user Mask on a terminal (ALLOW at internal) becomes MASK_WINDOW."""
        assert (
            get_matrix_action(ContextClass.CODE_EDITOR_TERMINAL, PrivacyMode.INTERNAL)
            == PrivacyAction.ALLOW
        )
        evaluator = _evaluator(mask_apps=[TERMINAL_BUNDLE])
        decision = _eval_classified(evaluator, TERMINAL_BUNDLE)
        assert decision.action == PrivacyAction.MASK_WINDOW
        assert decision.reason == ReasonCode.POLICY_MASKED_APP

    def test_explicit_deny_beats_a_mask_rule(self):
        evaluator = _evaluator(
            exclude_apps=[TERMINAL_BUNDLE], mask_apps=[TERMINAL_BUNDLE]
        )
        decision = _eval_classified(evaluator, TERMINAL_BUNDLE)
        assert decision.action == PrivacyAction.EXCLUDE
        assert decision.reason == ReasonCode.POLICY_EXCLUDED_APP

    def test_mask_rule_beats_a_confirmed_allow(self):
        """A hand-edited config listing a bundle in both fails closed to mask."""
        evaluator = _evaluator(
            mask_apps=[TERMINAL_BUNDLE],
            allow_apps=[TERMINAL_BUNDLE],
            confirmed_allow_apps=[TERMINAL_BUNDLE],
        )
        decision = _eval_classified(evaluator, TERMINAL_BUNDLE)
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_mask_apps_membership_is_case_insensitive(self):
        evaluator = _evaluator(mask_apps=["COM.APPLE.TERMINAL"])
        decision = _eval_classified(evaluator, TERMINAL_BUNDLE)
        assert decision.action == PrivacyAction.MASK_WINDOW


# ---------------------------------------------------------------------------
# Default-action floor
# ---------------------------------------------------------------------------


class TestDefaultActionFloor:
    def test_exclude_default_blocks_an_unruled_app(self):
        evaluator = _evaluator(default_action=PrivacyAction.EXCLUDE)
        decision = _eval_classified(evaluator, UNMAPPED_BUNDLE)
        assert decision.action == PrivacyAction.EXCLUDE
        assert decision.reason == ReasonCode.POLICY_DEFAULT_FLOOR

    def test_mask_default_masks_an_unruled_allow_class_app(self):
        evaluator = _evaluator(default_action=PrivacyAction.MASK_WINDOW)
        decision = _eval_as(evaluator, ContextClass.CODE_EDITOR_TERMINAL)
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_floor_never_weakens_a_stricter_matrix_action(self):
        """A mask default cannot loosen an EXCLUDE-class app."""
        evaluator = _evaluator(default_action=PrivacyAction.MASK_WINDOW)
        decision = _eval_as(evaluator, ContextClass.PASSWORD_MANAGER)
        assert decision.action == PrivacyAction.EXCLUDE

    def test_confirmed_allow_escapes_a_tightened_default(self):
        """The floor applies only where no explicit rule already decided."""
        evaluator = _evaluator(
            allow_apps=[CHAT_BUNDLE],
            confirmed_allow_apps=[CHAT_BUNDLE],
            default_action=PrivacyAction.EXCLUDE,
        )
        decision = _eval_classified(evaluator, CHAT_BUNDLE)
        assert decision.action == PrivacyAction.ALLOW

    def test_legacy_allow_is_floor_bound_and_picks_up_the_default(self):
        """A legacy (unconfirmed) allow whose matrix action is MASK_WINDOW falls
        through to the matrix step, so a tightened default reaches it."""
        evaluator = _evaluator(
            allow_apps=[CHAT_BUNDLE], default_action=PrivacyAction.EXCLUDE
        )
        decision = _eval_classified(evaluator, CHAT_BUNDLE)
        assert decision.action == PrivacyAction.EXCLUDE

    @pytest.mark.parametrize("ctx", list(ContextClass))
    @pytest.mark.parametrize("mode", ENFORCED_MODES)
    def test_allow_default_is_the_identity_floor(self, ctx, mode):
        """The regression gate: default_action=allow resolves exactly as the
        matrix does, for every context class and enforced mode."""
        evaluator = _evaluator(mode=mode.value, default_action=PrivacyAction.ALLOW)
        decision = _eval_as(evaluator, ctx)
        assert decision.action == get_matrix_action(ctx, mode)

    @pytest.mark.parametrize("ctx", list(ContextClass))
    @pytest.mark.parametrize("mode", ENFORCED_MODES)
    def test_floor_is_never_weaker_than_the_matrix(self, ctx, mode):
        """No default value can produce a weaker action than the matrix alone."""
        from screencap.privacy.actions import _ACTION_SEVERITY

        baseline = get_matrix_action(ctx, mode)
        for floor in (
            PrivacyAction.ALLOW,
            PrivacyAction.MASK_WINDOW,
            PrivacyAction.EXCLUDE,
        ):
            evaluator = _evaluator(mode=mode.value, default_action=floor)
            action = _eval_as(evaluator, ctx).action
            assert _ACTION_SEVERITY[action] <= _ACTION_SEVERITY[baseline]

    def test_unchanged_floor_keeps_the_context_reason(self):
        """The floor reason is emitted only when the floor actually bit."""
        evaluator = _evaluator(default_action=PrivacyAction.ALLOW)
        decision = _eval_as(evaluator, ContextClass.CHAT)
        assert decision.reason != ReasonCode.POLICY_DEFAULT_FLOOR


# ---------------------------------------------------------------------------
# Config parsing and transport
# ---------------------------------------------------------------------------


class TestConfigPlumbing:
    def test_parse_accepts_each_valid_default_action(self):
        for raw, expected in (
            ("allow", PrivacyAction.ALLOW),
            ("mask_window", PrivacyAction.MASK_WINDOW),
            ("exclude", PrivacyAction.EXCLUDE),
        ):
            cfg = parse_privacy_config({"privacy": {"default_action": raw}})
            assert cfg.default_action == expected

    def test_parse_defaults_to_allow_when_absent(self):
        cfg = parse_privacy_config({"privacy": {}})
        assert cfg.default_action == PrivacyAction.ALLOW
        assert cfg.mask_apps == frozenset()

    def test_parse_rejects_an_unknown_default_action(self):
        with pytest.raises(InvalidPrivacyConfigError):
            parse_privacy_config({"privacy": {"default_action": "banana"}})

    def test_parse_rejects_an_unenforceable_default_action(self):
        """Only the three UI-reachable actions are valid; MASK_REGION and the
        text/OCR actions are not settable as a blanket default."""
        for raw in ("mask_region", "text_redact", "ocr_fallback"):
            with pytest.raises(InvalidPrivacyConfigError):
                parse_privacy_config({"privacy": {"default_action": raw}})

    def test_parse_reads_mask_apps(self):
        cfg = parse_privacy_config(
            {"privacy": {"mask_apps": ["COM.Apple.Terminal"]}}
        )
        assert cfg.is_masked_app(TERMINAL_BUNDLE)

    def test_parse_rejects_non_list_mask_apps(self):
        with pytest.raises(InvalidPrivacyConfigError):
            parse_privacy_config({"privacy": {"mask_apps": "not-a-list"}})

    def test_config_survives_a_pickle_round_trip(self):
        """PrivacyConfig crosses into writer processes by pickle."""
        cfg = PrivacyConfig(
            mask_apps=frozenset({"COM.APPLE.TERMINAL"}),
            default_action=PrivacyAction.EXCLUDE,
        )
        restored = pickle.loads(pickle.dumps(cfg))
        assert restored.default_action == PrivacyAction.EXCLUDE
        assert restored.is_masked_app(TERMINAL_BUNDLE)

    def test_restricted_to_confirmed_preserves_the_tightening_rules(self):
        """Cloud posture narrows allow_apps but must not drop tightening knobs."""
        cfg = PrivacyConfig(
            allow_apps=frozenset({CHAT_BUNDLE}),
            confirmed_allow_apps=frozenset(),
            mask_apps=frozenset({TERMINAL_BUNDLE}),
            default_action=PrivacyAction.MASK_WINDOW,
        )
        restricted = cfg.restricted_to_confirmed()
        assert restricted.allow_apps == frozenset()
        assert restricted.is_masked_app(TERMINAL_BUNDLE)
        assert restricted.default_action == PrivacyAction.MASK_WINDOW


# ---------------------------------------------------------------------------
# U7: scrub-time mask coverage for user-masked classes
# ---------------------------------------------------------------------------


class TestScrubMaskCoverage:
    def test_terminal_class_has_a_mask_strategy(self):
        """Once a user can Mask a terminal, the strategy map must answer for it.
        Previously omitted on the premise that the class always TEXT_REDACTs."""
        from screencap.redaction.masking import MaskStrategy, get_mask_strategy

        assert (
            get_mask_strategy(ContextClass.CODE_EDITOR_TERMINAL)
            == MaskStrategy.FULL_WINDOW
        )

    def test_admin_console_class_has_a_mask_strategy(self):
        from screencap.redaction.masking import MaskStrategy, get_mask_strategy

        assert (
            get_mask_strategy(ContextClass.ADMIN_CONSOLE) == MaskStrategy.FULL_WINDOW
        )

    def test_exclude_classes_still_have_no_mask_strategy(self):
        """EXCLUDE-class surfaces are deleted, never masked — still no strategy."""
        from screencap.redaction.masking import get_mask_strategy

        assert get_mask_strategy(ContextClass.PASSWORD_MANAGER) is None
        assert get_mask_strategy(ContextClass.BANKING) is None

    def test_region_selection_includes_a_user_masked_window(self):
        """window_regions_from_geometry selects by action, so a per-app Mask
        window is picked up. Pins the behavior against a future edit to the
        keystroke-named action set it defaults to."""
        from screencap.privacy.classify import DefaultContextClassifier
        from screencap.privacy.mask_primitives import window_regions_from_geometry

        evaluator = _evaluator(mask_apps=[TERMINAL_BUNDLE])
        windows = [
            {
                "bundle_id": TERMINAL_BUNDLE,
                "title": "zsh",
                "x": 0,
                "y": 0,
                "width": 400,
                "height": 300,
            }
        ]
        regions = window_regions_from_geometry(
            windows, 800, 600, 1.0, DefaultContextClassifier(), evaluator
        )
        assert regions, "a user-masked terminal window must produce a mask region"
