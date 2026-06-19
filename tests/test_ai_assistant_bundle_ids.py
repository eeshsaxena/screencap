"""Tests for Unit 7b: AI assistant desktop apps reclassified as BROWSER_UNVERIFIED.

ChatGPT, Claude, and Perplexity desktop apps are wrappers around browser-style
chat surfaces — content the user typed, like a browser tab — not interpersonal
communication tools. They live in BROWSER_UNVERIFIED so a friend recording
"let me show you my AI tool" is useful under the default internal mode.
"""

from __future__ import annotations

import pytest

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.classify import (
    BUNDLE_ID_MAP,
    DefaultContextClassifier,
)
from screencap.privacy.policy import (
    ContextClass,
    DefaultPolicyEvaluator,
    FrameMetadata,
    PrivacyConfig,
    PrivacyMode,
    get_matrix_action,
)


AI_ASSISTANT_BUNDLES = (
    "com.openai.chat",                  # ChatGPT desktop
    "com.anthropic.claudefordesktop",   # Claude desktop
    "ai.perplexity.mac",                # Perplexity desktop
)


@pytest.mark.parametrize("bundle_id", AI_ASSISTANT_BUNDLES)
def test_bundle_classified_as_browser_unverified(bundle_id):
    """All three AI assistant desktops map to BROWSER_UNVERIFIED in the bundle map."""
    assert BUNDLE_ID_MAP[bundle_id] == ContextClass.BROWSER_UNVERIFIED


@pytest.mark.parametrize("bundle_id", AI_ASSISTANT_BUNDLES)
def test_classifier_returns_browser_unverified(bundle_id):
    """DefaultContextClassifier returns BROWSER_UNVERIFIED for these bundle IDs."""
    classifier = DefaultContextClassifier()
    metadata = FrameMetadata(bundle_id=bundle_id, window_title="ChatGPT")
    result = classifier.classify(metadata)
    assert result.context_class == ContextClass.BROWSER_UNVERIFIED


@pytest.mark.parametrize("bundle_id", AI_ASSISTANT_BUNDLES)
def test_resolves_to_allow_under_internal(bundle_id):
    """Under default internal mode, AI assistant apps evaluate to ALLOW.

    BROWSER_UNVERIFIED + INTERNAL = ALLOW per the matrix; these apps
    surface in friend-onboarding recordings without masking.
    """
    cfg = PrivacyConfig(mode=PrivacyMode.INTERNAL)
    evaluator = DefaultPolicyEvaluator(cfg)
    metadata = FrameMetadata(bundle_id=bundle_id, window_title="ChatGPT")
    classifier = DefaultContextClassifier()
    decision = evaluator.evaluate(classifier.classify(metadata), metadata)
    assert decision.action == PrivacyAction.ALLOW


@pytest.mark.parametrize("bundle_id", AI_ASSISTANT_BUNDLES)
def test_masked_under_public(bundle_id):
    """Under public mode, BROWSER_UNVERIFIED → MASK_WINDOW (correct fallback)."""
    cfg = PrivacyConfig(mode=PrivacyMode.PUBLIC)
    evaluator = DefaultPolicyEvaluator(cfg)
    metadata = FrameMetadata(bundle_id=bundle_id, window_title="ChatGPT")
    classifier = DefaultContextClassifier()
    decision = evaluator.evaluate(classifier.classify(metadata), metadata)
    assert decision.action == PrivacyAction.MASK_WINDOW


def test_browser_unverified_internal_is_allow():
    """Sanity-check the matrix entry the AI assistant test relies on."""
    assert (
        get_matrix_action(ContextClass.BROWSER_UNVERIFIED, PrivacyMode.INTERNAL)
        == PrivacyAction.ALLOW
    )


def test_user_can_explicitly_exclude_an_ai_assistant():
    """exclude_apps still wins over the BROWSER_UNVERIFIED → ALLOW default."""
    cfg = PrivacyConfig(
        mode=PrivacyMode.INTERNAL,
        exclude_apps=frozenset({"com.openai.chat"}),
    )
    evaluator = DefaultPolicyEvaluator(cfg)
    metadata = FrameMetadata(bundle_id="com.openai.chat", window_title="ChatGPT")
    classifier = DefaultContextClassifier()
    decision = evaluator.evaluate(classifier.classify(metadata), metadata)
    assert decision.action == PrivacyAction.EXCLUDE


def test_unmapped_ai_app_falls_through_to_unknown():
    """An AI app not yet in the bundle map classifies as UNKNOWN (still ALLOW
    under internal — friend-onboarding goal preserved without explicit mapping)."""
    cfg = PrivacyConfig(mode=PrivacyMode.INTERNAL)
    evaluator = DefaultPolicyEvaluator(cfg)
    metadata = FrameMetadata(
        bundle_id="com.example.nobody-knows-this-ai", window_title="Some AI"
    )
    classifier = DefaultContextClassifier()
    classification = classifier.classify(metadata)
    # Unclassified bundle → UNKNOWN
    assert classification.context_class == ContextClass.UNKNOWN
    decision = evaluator.evaluate(classification, metadata)
    assert decision.action == PrivacyAction.ALLOW
