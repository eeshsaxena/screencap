"""Tests for the privacy matrix and allow-list precedence.

Unit 7a pinned the matrix entries (CHAT/EMAIL/CALENDAR/VIDEO_CALL under
PrivacyMode.INTERNAL → MASK_WINDOW) and the matrix-strictness floor for
allow_apps entries.

SCR-235 makes a *confirmed* allow-list entry authoritative over the matrix
in every mode: the floor now applies only to legacy (unconfirmed) entries,
user domain/title mask rules outrank any allow, and a confirmed browser's
windows refined to a confirmation-required context keep the matrix action.
These tests pin the matrix entries, the legacy floor, and the new
confirmed-allow precedence so regressions in either direction fail loudly.
"""

from __future__ import annotations

import pytest

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.policy import (
    CONFIRMATION_REQUIRED_CLASSES,
    ContextClass,
    ContextResult,
    FrameMetadata,
    PrivacyConfig,
    PrivacyMode,
    get_matrix_action,
)

pytestmark = pytest.mark.privacy


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


def _evaluator(
    *,
    mode="internal",
    allow_apps=(),
    exclude_apps=(),
    confirmed_allow_apps=(),
    mask_domains=(),
    mask_title_patterns=(),
    app_classes=None,
):
    """Build a DefaultPolicyEvaluator with a minimal config."""
    import re

    from screencap.privacy.policy import DefaultPolicyEvaluator

    cfg = PrivacyConfig(
        mode=PrivacyMode(mode),
        allow_apps=frozenset(allow_apps),
        exclude_apps=frozenset(exclude_apps),
        confirmed_allow_apps=frozenset(confirmed_allow_apps),
        mask_domains=frozenset(mask_domains),
        mask_title_patterns=tuple(re.compile(p) for p in mask_title_patterns),
        app_classes=dict(app_classes or {}),
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
    """Legacy (unconfirmed) allow entries observe the matrix-strictness floor.

    Since SCR-235, new adds through the CLI/UI are *confirmed* (authoritative
    over the matrix); the floor pinned here applies to entries that are in
    allow_apps but NOT confirmed_allow_apps — pre-SCR-235 configs, wizard
    silent auto-allows, and hand edits. Those must not silently bypass the
    matrix.

    Documented precedence (policy.py DefaultPolicyEvaluator):
      exclude_apps > mask_domains/mask_title_patterns > confirmed allow
      (browser carve-out) > legacy-allow floor > matrix
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


# ---------------------------------------------------------------------------
# SCR-235: confirmed allow-list entries are authoritative over the matrix
# ---------------------------------------------------------------------------


def _confirmed(bundle_id, **kwargs):
    """Evaluator where bundle_id is a confirmed allow entry."""
    return _evaluator(
        allow_apps=[bundle_id],
        confirmed_allow_apps=[bundle_id],
        **kwargs,
    )


def _eval_direct(evaluator, bundle_id, context_class, title="Whatever", domain=None):
    """Evaluate with an explicit (pre-refined) context, bypassing the classifier."""
    metadata = FrameMetadata(bundle_id=bundle_id, window_title=title, domain=domain)
    context = ContextResult(context_class=context_class, confidence="test", evidence="test")
    return evaluator.evaluate(context, metadata)


class TestConfirmedAllowIsAuthoritative:
    """A confirmed allow beats every matrix action in every mode (R1)."""

    @pytest.mark.parametrize("mode", ["internal", "public"])
    def test_confirmed_chat_allows_in_every_mode(self, mode):
        evaluator = _confirmed("com.tinyspeck.slackmacgap", mode=mode)
        decision = _eval_for(evaluator, "com.tinyspeck.slackmacgap")
        assert decision.action == PrivacyAction.ALLOW

    @pytest.mark.parametrize("mode", ["internal", "public"])
    def test_confirmed_email_allows_in_every_mode(self, mode):
        """Covers AE1 (policy half): confirmed Mail under public → ALLOW."""
        evaluator = _confirmed("com.apple.mail", mode=mode)
        decision = _eval_for(evaluator, "com.apple.mail")
        assert decision.action == PrivacyAction.ALLOW

    @pytest.mark.parametrize("mode", ["internal", "public"])
    def test_confirmed_password_manager_allows(self, mode):
        """Covers AE2 (policy half): a confirmed EXCLUDE-class app is ALLOW."""
        evaluator = _confirmed("com.1password.1password", mode=mode)
        decision = _eval_for(evaluator, "com.1password.1password")
        assert decision.action == PrivacyAction.ALLOW

    def test_confirmed_admin_console_allows_under_public(self):
        """TEXT_REDACT-class contexts are overridable too."""
        evaluator = _confirmed("com.electron.dockerdesktop", mode="public")
        decision = _eval_for(evaluator, "com.electron.dockerdesktop")
        assert decision.action == PrivacyAction.ALLOW

    def test_exclude_beats_confirmed_allow(self):
        """Covers AE6: explicit deny wins over a confirmed allow (R3)."""
        evaluator = _evaluator(
            allow_apps=["com.tinyspeck.slackmacgap"],
            confirmed_allow_apps=["com.tinyspeck.slackmacgap"],
            exclude_apps=["com.tinyspeck.slackmacgap"],
        )
        decision = _eval_for(evaluator, "com.tinyspeck.slackmacgap")
        assert decision.action == PrivacyAction.EXCLUDE

    def test_orphaned_confirmed_entry_is_inert(self):
        """A confirmed entry not present in allow_apps behaves as unlisted (KTD1)."""
        evaluator = _evaluator(
            mode="internal",
            confirmed_allow_apps=["com.tinyspeck.slackmacgap"],
        )
        decision = _eval_for(evaluator, "com.tinyspeck.slackmacgap")
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_case_variant_membership_still_authoritative(self):
        """Covers KTD7: casing differences between the two lists don't drop authority."""
        evaluator = _evaluator(
            mode="public",
            allow_apps=["com.apple.mail"],
            confirmed_allow_apps=["COM.APPLE.MAIL"],
        )
        decision = _eval_for(evaluator, "com.apple.mail")
        assert decision.action == PrivacyAction.ALLOW

    def test_case_variant_probe_still_authoritative(self):
        """OS-reported casing differences don't drop authority either."""
        evaluator = _confirmed("com.apple.mail", mode="public")
        decision = _eval_for(evaluator, "com.Apple.Mail")
        assert decision.action == PrivacyAction.ALLOW

    def test_legacy_entry_still_floor_bound(self):
        """Covers AE4: an allow entry without confirmation keeps the floor (R5)."""
        evaluator = _evaluator(
            mode="internal",
            allow_apps=["com.tinyspeck.slackmacgap"],
        )
        decision = _eval_for(evaluator, "com.tinyspeck.slackmacgap")
        assert decision.action == PrivacyAction.MASK_WINDOW


class TestConfirmedBrowserCarveOut:
    """R12: a confirmed browser's windows refined to a confirmation-required
    context keep the matrix action; other refinements follow the allow."""

    def test_stock_browser_refined_to_banking_public(self):
        """Covers AE7: stock browser (BROWSER_BUNDLE_IDS, no app_classes entry)."""
        evaluator = _confirmed("com.apple.Safari", mode="public")
        decision = _eval_direct(evaluator, "com.apple.Safari", ContextClass.BANKING)
        assert decision.action == PrivacyAction.EXCLUDE

    def test_stock_browser_refined_to_banking_internal(self):
        """Covers AE7: matrix action for the current mode (MASK_WINDOW at internal)."""
        evaluator = _confirmed("com.apple.Safari", mode="internal")
        decision = _eval_direct(evaluator, "com.apple.Safari", ContextClass.BANKING)
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_stock_browser_refined_to_auth_flow_public(self):
        evaluator = _confirmed("com.apple.Safari", mode="public")
        decision = _eval_direct(evaluator, "com.apple.Safari", ContextClass.AUTH_FLOW)
        assert decision.action == PrivacyAction.EXCLUDE

    def test_tagged_browser_refined_to_banking_public(self):
        """The carve-out also fires for app_classes-tagged browsers."""
        evaluator = _evaluator(
            mode="public",
            allow_apps=["com.example.custombrowser"],
            confirmed_allow_apps=["com.example.custombrowser"],
            app_classes={"com.example.custombrowser": ContextClass.BROWSER_UNVERIFIED},
        )
        decision = _eval_direct(
            evaluator, "com.example.custombrowser", ContextClass.BANKING
        )
        assert decision.action == PrivacyAction.EXCLUDE

    def test_confirmed_browser_refined_to_email_allows(self):
        """Mask-class refinements (webmail) follow the confirmed allow."""
        evaluator = _confirmed("com.apple.Safari", mode="public")
        decision = _eval_direct(evaluator, "com.apple.Safari", ContextClass.EMAIL)
        assert decision.action == PrivacyAction.ALLOW

    def test_confirmed_browser_benign_page_allows(self):
        """Unrefined browser context (no rule match) → ALLOW."""
        evaluator = _confirmed("com.apple.Safari", mode="public")
        decision = _eval_direct(
            evaluator, "com.apple.Safari", ContextClass.BROWSER_UNVERIFIED
        )
        assert decision.action == PrivacyAction.ALLOW

    def test_carve_out_holds_for_case_variant_browser_bundle(self):
        """Covers the R12 case-asymmetry regression: confirmed membership is
        case-insensitive, so the browser test must be too — a lowercase-cased
        Safari event must not skip the carve-out and leak ALLOW on a
        banking page."""
        evaluator = _confirmed("com.apple.safari", mode="public")
        decision = _eval_direct(evaluator, "com.apple.safari", ContextClass.BANKING)
        assert decision.action == PrivacyAction.EXCLUDE

    def test_carve_out_does_not_fire_for_non_browsers(self):
        """A confirmed password manager's own windows classify
        PASSWORD_MANAGER — the carve-out must not re-exclude them (R1)."""
        evaluator = _confirmed("com.1password.1password", mode="public")
        decision = _eval_direct(
            evaluator, "com.1password.1password", ContextClass.PASSWORD_MANAGER
        )
        assert decision.action == PrivacyAction.ALLOW


class TestMaskRulesPrecedeAllows:
    """User domain/title mask rules outrank any allow (R4), including legacy
    entries (the owned R5 tightening)."""

    def test_mask_domain_masks_inside_confirmed_browser(self):
        """Covers AE5: a mask_domains rule still masks in a confirmed browser."""
        evaluator = _confirmed("com.apple.Safari", mode="internal", mask_domains=["mybank.com"])
        decision = _eval_direct(
            evaluator,
            "com.apple.Safari",
            ContextClass.BROWSER_UNVERIFIED,
            domain="mybank.com",
        )
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_mask_title_masks_inside_confirmed_app(self):
        """R4 for non-browser apps: title rules apply inside a confirmed app."""
        evaluator = _confirmed(
            "com.tinyspeck.slackmacgap", mode="internal", mask_title_patterns=[r"(?i)payroll"]
        )
        decision = _eval_for(evaluator, "com.tinyspeck.slackmacgap", title="Payroll review")
        assert decision.action == PrivacyAction.MASK_WINDOW

    def test_mask_title_masks_legacy_allow_in_non_blocking_context(self):
        """The owned R5 delta: a legacy allow that returns ALLOW today
        (BROWSER_UNVERIFIED at internal) now yields to a matching mask rule."""
        evaluator = _evaluator(
            mode="internal",
            allow_apps=["com.openai.chat"],
            mask_title_patterns=[r"(?i)secret"],
        )
        decision = _eval_for(evaluator, "com.openai.chat", title="secret roadmap")
        assert decision.action == PrivacyAction.MASK_WINDOW


class TestConfirmationRequiredClasses:
    """KTD2: the confirmation-required set derives from the matrix."""

    def test_set_is_exactly_the_exclude_anywhere_classes(self):
        assert CONFIRMATION_REQUIRED_CLASSES == frozenset(
            {
                ContextClass.PASSWORD_MANAGER,
                ContextClass.BANKING,
                ContextClass.AUTH_FLOW,
                ContextClass.PAYMENT_FLOW,
            }
        )
