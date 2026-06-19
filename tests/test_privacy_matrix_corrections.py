"""Tests for Unit 7a: privacy matrix correction.

CHAT/EMAIL/CALENDAR/VIDEO_CALL under PrivacyMode.INTERNAL changed from
TEXT_REDACT to MASK_WINDOW so the friend-onboarding default actually
prevents conversation-window contents from being captured (rather than
relying on post-capture text scrubbing).

These tests pin the new matrix entries plus the precedence interactions
(allow_apps × MASK_WINDOW, EXCLUDE-class invariants) so a future regression
that quietly reverts the entries fails loudly.
"""

from __future__ import annotations

import pytest

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.policy import (
    ContextClass,
    PrivacyConfig,
    PrivacyMode,
    get_matrix_action,
)


# ---------------------------------------------------------------------------
# Matrix values pinned (post-Unit-7a)
# ---------------------------------------------------------------------------


CHAT_LIKE = (
    ContextClass.CHAT,
    ContextClass.EMAIL,
    ContextClass.CALENDAR,
    ContextClass.VIDEO_CALL,
)


@pytest.mark.parametrize("ctx", CHAT_LIKE)
def test_chat_like_internal_is_mask_window(ctx):
    """Post-Unit-7a: chat/email/calendar/video-call under internal → MASK_WINDOW."""
    assert get_matrix_action(ctx, PrivacyMode.INTERNAL) == PrivacyAction.MASK_WINDOW


@pytest.mark.parametrize("ctx", CHAT_LIKE)
def test_chat_like_public_unchanged(ctx):
    """Public mode for chat-like contexts is still MASK_WINDOW (unchanged)."""
    assert get_matrix_action(ctx, PrivacyMode.PUBLIC) == PrivacyAction.MASK_WINDOW


@pytest.mark.parametrize("ctx", CHAT_LIKE)
def test_chat_like_shared_unchanged(ctx):
    """Shared mode for chat-like contexts is still MASK_REGION (unchanged)."""
    assert get_matrix_action(ctx, PrivacyMode.SHARED) == PrivacyAction.MASK_REGION


def test_password_manager_still_excluded_under_internal():
    """The matrix correction must NOT loosen the password-manager invariant."""
    assert (
        get_matrix_action(ContextClass.PASSWORD_MANAGER, PrivacyMode.INTERNAL)
        == PrivacyAction.EXCLUDE
    )


def test_banking_internal_still_mask_window():
    """Banking under internal stays MASK_WINDOW (was already correct pre-Unit-7a)."""
    assert (
        get_matrix_action(ContextClass.BANKING, PrivacyMode.INTERNAL)
        == PrivacyAction.MASK_WINDOW
    )


def test_unknown_internal_still_allow():
    """UNKNOWN class under internal still ALLOW (sensitive apps need explicit class)."""
    assert (
        get_matrix_action(ContextClass.UNKNOWN, PrivacyMode.INTERNAL)
        == PrivacyAction.ALLOW
    )


# ---------------------------------------------------------------------------
# Precedence interactions: allow_apps × MASK_WINDOW
# ---------------------------------------------------------------------------


def _evaluator(*, mode="internal", allow_apps=(), exclude_apps=()):
    """Build a DefaultPolicyEvaluator with a minimal config."""
    from screencap.privacy.policy import DefaultPolicyEvaluator

    cfg = PrivacyConfig(
        mode=PrivacyMode(mode),
        allow_apps=frozenset(allow_apps),
        exclude_apps=frozenset(exclude_apps),
    )
    return DefaultPolicyEvaluator(cfg)


def _eval_for(evaluator, bundle_id, title="Whatever", domain=None):
    """Classify the bundle and evaluate; returns the ActionDecision."""
    from screencap.privacy.classify import DefaultContextClassifier
    from screencap.privacy.policy import FrameMetadata

    metadata = FrameMetadata(bundle_id=bundle_id, window_title=title, domain=domain)
    context = DefaultContextClassifier().classify(metadata)
    return evaluator.evaluate(context, metadata)


class TestAllowAppsRespectsMatrixFloor:
    """allow_apps observes the matrix-strictness floor at the configured mode.

    The CLI guard (cli._matrix_blocks_allow_for_class) rejects new
    additions of bundles whose matrix action is EXCLUDE / MASK_WINDOW /
    TEXT_REDACT. The runtime evaluator must apply the same floor so an
    EXISTING entry (carried over from before the guard or from a prior
    privacy mode) does not silently bypass the matrix.

    Documented precedence at policy.py:389+:
      exclude_apps > matrix-floor(allow_apps) > mask_domains > mask_title_patterns > matrix
    """

    def test_allow_apps_respects_chat_mask_window_floor_under_internal(self):
        """Pre-existing Slack in allow_apps under internal → MASK_WINDOW
        (matrix wins). Previously this evaluated to ALLOW, defeating
        Unit 7a's tightening for upgrading users."""
        evaluator = _evaluator(
            mode="internal",
            allow_apps=["com.tinyspeck.slackmacgap"],
        )
        decision = _eval_for(evaluator, "com.tinyspeck.slackmacgap")
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_allow_apps_respects_email_mask_window_floor_under_internal(self):
        """Pre-existing Mail.app in allow_apps under internal → MASK_WINDOW."""
        evaluator = _evaluator(
            mode="internal",
            allow_apps=["com.apple.mail"],
        )
        decision = _eval_for(evaluator, "com.apple.mail")
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_allow_apps_cannot_override_password_manager_exclude(self):
        """1Password stays EXCLUDE even when allow-listed (PASSWORD_MANAGER invariant)."""
        evaluator = _evaluator(
            mode="internal",
            allow_apps=["com.1password.1password"],
        )
        decision = _eval_for(evaluator, "com.1password.1password")
        assert decision.action == PrivacyAction.EXCLUDE

    def test_exclude_apps_beats_allow_apps_for_chat(self):
        """Explicit exclude wins over allow even for non-EXCLUDE matrix classes."""
        evaluator = _evaluator(
            mode="internal",
            allow_apps=["com.tinyspeck.slackmacgap"],
            exclude_apps=["com.tinyspeck.slackmacgap"],
        )
        decision = _eval_for(evaluator, "com.tinyspeck.slackmacgap")
        assert decision.action == PrivacyAction.EXCLUDE


# ---------------------------------------------------------------------------
# Concrete bundle IDs that move into MASK_WINDOW under internal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bundle_id", [
    "com.tinyspeck.slackmacgap",       # Slack (CHAT)
    "com.apple.MobileSMS",              # iMessage (CHAT)
    "com.apple.mail",                   # Mail.app (EMAIL)
    "com.apple.iCal",                   # Calendar.app (CALENDAR)
    "us.zoom.xos",                      # Zoom (VIDEO_CALL)
])
def test_known_chat_like_bundles_resolve_to_mask_window_under_internal(bundle_id):
    """Real-world bundle IDs in the CHAT/EMAIL/CALENDAR/VIDEO_CALL classes
    all evaluate to MASK_WINDOW under the default internal mode."""
    evaluator = _evaluator(mode="internal")
    decision = _eval_for(evaluator, bundle_id)
    assert decision.action == PrivacyAction.MASK_WINDOW


# ---------------------------------------------------------------------------
# Runtime allow_apps strictness floor — pre-existing entries must NOT
# silently bypass Unit 7a's matrix tightening.
# ---------------------------------------------------------------------------


class TestAllowAppsStrictnessFloor:
    """The CLI guard rejects new allow_apps additions for matrix-blocked
    classes. The runtime evaluator must apply the same floor to existing
    on-disk entries — otherwise an upgrading user with Slack already in
    allow_apps gets raw capture under internal mode (defeating Unit 7a).
    """

    def test_existing_chat_allow_does_not_bypass_mask_window(self):
        """Pre-existing Slack in allow_apps under internal mode → should
        evaluate to MASK_WINDOW (matrix), NOT ALLOW (allow_apps override).
        """
        evaluator = _evaluator(
            mode="internal",
            allow_apps=["com.tinyspeck.slackmacgap"],
        )
        decision = _eval_for(evaluator, "com.tinyspeck.slackmacgap")
        assert decision.action == PrivacyAction.MASK_WINDOW, (
            f"existing allow_apps must not bypass MASK_WINDOW, got {decision.action}"
        )

    def test_existing_email_allow_does_not_bypass_mask_window(self):
        evaluator = _evaluator(
            mode="internal",
            allow_apps=["com.apple.mail"],
        )
        decision = _eval_for(evaluator, "com.apple.mail")
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_existing_admin_console_allow_does_not_bypass_text_redact(self):
        """ADMIN_CONSOLE under PUBLIC = TEXT_REDACT. allow_apps must not
        bypass that — text-redact is the matrix's explicit decision."""
        evaluator = _evaluator(
            mode="public",
            allow_apps=["com.electron.dockerdesktop"],
        )
        decision = _eval_for(evaluator, "com.electron.dockerdesktop")
        # Under public, ADMIN_CONSOLE → TEXT_REDACT. allow_apps does not
        # override.
        assert decision.action == PrivacyAction.TEXT_REDACT

    def test_browser_unverified_allow_still_works(self):
        """Strictness floor only blocks the matrix-blocked actions
        (EXCLUDE/MASK_WINDOW/TEXT_REDACT). BROWSER_UNVERIFIED under internal
        is ALLOW — allow_apps is a no-op there but evaluation still
        produces ALLOW."""
        evaluator = _evaluator(
            mode="internal",
            allow_apps=["com.openai.chat"],
        )
        decision = _eval_for(evaluator, "com.openai.chat")
        assert decision.action == PrivacyAction.ALLOW

    def test_password_manager_allow_still_excludes(self):
        """Existing behavior preserved: matrix EXCLUDE wins over allow_apps."""
        evaluator = _evaluator(
            mode="internal",
            allow_apps=["com.1password.1password"],
        )
        decision = _eval_for(evaluator, "com.1password.1password")
        assert decision.action == PrivacyAction.EXCLUDE
