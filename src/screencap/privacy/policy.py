"""Policy engine for privacy v3.

Owns:
- PrivacyMode and ContextClass enums
- user privacy config parsing
- the (context_class, privacy_mode) -> action matrix
- policy precedence and evaluation
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Protocol

# Transition hold: suppress capture for this many seconds after switching
# away from a blocked app.  Covers macOS Cmd+Tab animation (200-350ms).
DEFAULT_TRANSITION_HOLD_SECONDS: float = 1.0

from screencap.privacy.actions import ActionDecision, PrivacyAction, stricter
from screencap.privacy.reasons import ReasonCode


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PrivacyMode(Enum):
    PUBLIC = "public"
    SHARED = "shared"
    INTERNAL = "internal"


_MODE_STRICTNESS: dict[PrivacyMode, int] = {
    PrivacyMode.PUBLIC: 0,
    PrivacyMode.SHARED: 1,
    PrivacyMode.INTERNAL: 2,
}


class ContextClass(Enum):
    PASSWORD_MANAGER = "password_manager"
    BANKING = "banking"
    EMAIL = "email"
    CHAT = "chat"
    CALENDAR = "calendar"
    VIDEO_CALL = "video_call"
    BROWSER_UNVERIFIED = "browser_unverified"
    CODE_EDITOR_TERMINAL = "code_editor_terminal"
    ADMIN_CONSOLE = "admin_console"
    AUTH_FLOW = "auth_flow"
    PAYMENT_FLOW = "payment_flow"
    CLOUD_STORAGE = "cloud_storage"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Action matrix
# ---------------------------------------------------------------------------

# Explicit mapping for every (ContextClass, PrivacyMode) pair.
# Shared is architecture-supported but deferred from implementation;
# we still define its matrix entries for completeness.

_ACTION_MATRIX: dict[tuple[ContextClass, PrivacyMode], PrivacyAction] = {
    # password_manager
    (ContextClass.PASSWORD_MANAGER, PrivacyMode.PUBLIC): PrivacyAction.EXCLUDE,
    (ContextClass.PASSWORD_MANAGER, PrivacyMode.SHARED): PrivacyAction.EXCLUDE,
    (ContextClass.PASSWORD_MANAGER, PrivacyMode.INTERNAL): PrivacyAction.EXCLUDE,
    # banking
    (ContextClass.BANKING, PrivacyMode.PUBLIC): PrivacyAction.EXCLUDE,
    (ContextClass.BANKING, PrivacyMode.SHARED): PrivacyAction.EXCLUDE,
    (ContextClass.BANKING, PrivacyMode.INTERNAL): PrivacyAction.MASK_WINDOW,
    # email / chat / calendar / video_call under INTERNAL → MASK_WINDOW.
    # Privacy-improving change vs. the prior TEXT_REDACT default: window
    # contents are blurred end-to-end (video frames blocked, keystrokes
    # nulled, screenshots masked at scrub time) instead of relying on
    # post-capture text scrubbing for personal/workplace conversations.
    # Aligns with the friend-onboarding "safe defaults" intent — see
    # docs/brainstorms/2026-04-27-native-macos-ui-v1-requirements.md (R20)
    # and the v1 plan's Unit 7a discussion of the workflow impact.
    # email
    (ContextClass.EMAIL, PrivacyMode.PUBLIC): PrivacyAction.MASK_WINDOW,
    (ContextClass.EMAIL, PrivacyMode.SHARED): PrivacyAction.MASK_REGION,
    (ContextClass.EMAIL, PrivacyMode.INTERNAL): PrivacyAction.MASK_WINDOW,
    # chat
    (ContextClass.CHAT, PrivacyMode.PUBLIC): PrivacyAction.MASK_WINDOW,
    (ContextClass.CHAT, PrivacyMode.SHARED): PrivacyAction.MASK_REGION,
    (ContextClass.CHAT, PrivacyMode.INTERNAL): PrivacyAction.MASK_WINDOW,
    # calendar
    (ContextClass.CALENDAR, PrivacyMode.PUBLIC): PrivacyAction.MASK_WINDOW,
    (ContextClass.CALENDAR, PrivacyMode.SHARED): PrivacyAction.MASK_REGION,
    (ContextClass.CALENDAR, PrivacyMode.INTERNAL): PrivacyAction.MASK_WINDOW,
    # video_call
    (ContextClass.VIDEO_CALL, PrivacyMode.PUBLIC): PrivacyAction.MASK_WINDOW,
    (ContextClass.VIDEO_CALL, PrivacyMode.SHARED): PrivacyAction.MASK_REGION,
    (ContextClass.VIDEO_CALL, PrivacyMode.INTERNAL): PrivacyAction.MASK_WINDOW,
    # browser_unverified
    (ContextClass.BROWSER_UNVERIFIED, PrivacyMode.PUBLIC): PrivacyAction.MASK_WINDOW,
    (ContextClass.BROWSER_UNVERIFIED, PrivacyMode.SHARED): PrivacyAction.OCR_FALLBACK,
    (ContextClass.BROWSER_UNVERIFIED, PrivacyMode.INTERNAL): PrivacyAction.ALLOW,
    # code_editor_terminal
    (ContextClass.CODE_EDITOR_TERMINAL, PrivacyMode.PUBLIC): PrivacyAction.TEXT_REDACT,
    (ContextClass.CODE_EDITOR_TERMINAL, PrivacyMode.SHARED): PrivacyAction.TEXT_REDACT,
    (ContextClass.CODE_EDITOR_TERMINAL, PrivacyMode.INTERNAL): PrivacyAction.ALLOW,
    # admin_console
    (ContextClass.ADMIN_CONSOLE, PrivacyMode.PUBLIC): PrivacyAction.TEXT_REDACT,
    (ContextClass.ADMIN_CONSOLE, PrivacyMode.SHARED): PrivacyAction.TEXT_REDACT,
    (ContextClass.ADMIN_CONSOLE, PrivacyMode.INTERNAL): PrivacyAction.ALLOW,
    # auth_flow — login/SSO pages get maximum protection
    (ContextClass.AUTH_FLOW, PrivacyMode.PUBLIC): PrivacyAction.EXCLUDE,
    (ContextClass.AUTH_FLOW, PrivacyMode.SHARED): PrivacyAction.EXCLUDE,
    (ContextClass.AUTH_FLOW, PrivacyMode.INTERNAL): PrivacyAction.MASK_WINDOW,
    # payment_flow — checkout/billing pages
    (ContextClass.PAYMENT_FLOW, PrivacyMode.PUBLIC): PrivacyAction.EXCLUDE,
    (ContextClass.PAYMENT_FLOW, PrivacyMode.SHARED): PrivacyAction.EXCLUDE,
    (ContextClass.PAYMENT_FLOW, PrivacyMode.INTERNAL): PrivacyAction.MASK_WINDOW,
    # cloud_storage
    (ContextClass.CLOUD_STORAGE, PrivacyMode.PUBLIC): PrivacyAction.MASK_WINDOW,
    (ContextClass.CLOUD_STORAGE, PrivacyMode.SHARED): PrivacyAction.MASK_REGION,
    (ContextClass.CLOUD_STORAGE, PrivacyMode.INTERNAL): PrivacyAction.ALLOW,
    # unknown — allow by default, sensitive apps have explicit classes
    (ContextClass.UNKNOWN, PrivacyMode.PUBLIC): PrivacyAction.ALLOW,
    (ContextClass.UNKNOWN, PrivacyMode.SHARED): PrivacyAction.ALLOW,
    (ContextClass.UNKNOWN, PrivacyMode.INTERNAL): PrivacyAction.ALLOW,
}


def get_matrix_action(
    context: ContextClass, mode: PrivacyMode
) -> PrivacyAction:
    """Look up the base action for a (context, mode) pair.

    Raises KeyError if the pair is missing — this should never happen
    because the matrix is validated at import time.
    """
    return _ACTION_MATRIX[(context, mode)]


def _validate_matrix() -> None:
    """Verify every (ContextClass, PrivacyMode) pair is covered."""
    for ctx in ContextClass:
        for mode in PrivacyMode:
            if (ctx, mode) not in _ACTION_MATRIX:
                raise RuntimeError(
                    f"Action matrix missing entry: ({ctx.value}, {mode.value})"
                )


_validate_matrix()


# Classes whose matrix row contains EXCLUDE in any mode. Allow-listing one of
# these requires an explicit confirmation (SCR-235): the CLI gate, the setup
# wizard, and the `apps` payload all consume this single matrix-derived set so
# the definition cannot drift across surfaces.
CONFIRMATION_REQUIRED_CLASSES: frozenset[ContextClass] = frozenset(
    ctx
    for (ctx, _mode), action in _ACTION_MATRIX.items()
    if action is PrivacyAction.EXCLUDE
)


def requires_confirmed_allow(context_class: ContextClass) -> bool:
    """True when allow-listing this class needs an explicit confirmation."""
    return context_class in CONFIRMATION_REQUIRED_CLASSES


# Known browser bundle IDs. Lives here (the leaf module) because both the
# classifier and the evaluator's R12 browser carve-out must share one browser
# definition; classify.py re-exports it for existing importers.
BROWSER_BUNDLE_IDS: frozenset[str] = frozenset({
    "com.apple.Safari",
    "com.google.Chrome",
    "org.mozilla.firefox",
    "com.microsoft.edgemac",
    "com.brave.Browser",
    "com.operasoftware.Opera",
    "com.vivaldi.Vivaldi",
    "company.thebrowser.Browser",  # Arc
    "org.chromium.Chromium",
    "com.nickvision.nicegx.nicegx",  # Orion
    "org.waterfoxproject.waterfox",
    "org.torproject.torbrowser",
})

# Case-normalized membership set: bundle-id probes are case-insensitive
# wherever membership decides policy (SCR-235). Every consumer that asks
# "is this a browser?" must probe this set with a lowered id — a case-exact
# probe beside case-insensitive allow membership is the asymmetry that let
# a confirmed browser dodge the R12 carve-out.
BROWSER_BUNDLE_IDS_LOWER: frozenset[str] = frozenset(
    b.lower() for b in BROWSER_BUNDLE_IDS
)


# ---------------------------------------------------------------------------
# Privacy config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrivacyConfig:
    """Parsed [privacy] section from config.toml."""

    mode: PrivacyMode = PrivacyMode.INTERNAL
    exclude_apps: frozenset[str] = field(default_factory=frozenset)
    allow_apps: frozenset[str] = field(default_factory=frozenset)
    confirmed_allow_apps: frozenset[str] = field(default_factory=frozenset)
    mask_domains: frozenset[str] = field(default_factory=frozenset)
    mask_title_patterns: tuple[re.Pattern[str], ...] = ()
    app_classes: dict[str, ContextClass] = field(default_factory=dict)

    def is_excluded_app(self, bundle_id: str) -> bool:
        return bundle_id.lower() in self.exclude_apps

    def is_allowed_app(self, bundle_id: str) -> bool:
        return bundle_id.lower() in self.allow_apps

    def is_confirmed_allowed_app(self, bundle_id: str) -> bool:
        """True when the entry is authoritative (SCR-235): present in BOTH
        allow_apps and confirmed_allow_apps. An orphaned confirmed entry
        (hand-edited config) is inert."""
        bid = bundle_id.lower()
        return bid in self.confirmed_allow_apps and bid in self.allow_apps

    def app_class_for(self, bundle_id: str) -> ContextClass | None:
        """Case-normalized app_classes lookup."""
        return self.app_classes.get(bundle_id.lower())

    def restricted_to_confirmed(self) -> "PrivacyConfig":
        """Cloud-posture shaping (SCR-235 KTD5): keep only confirmed allow
        entries. Both PUBLIC-forcing cloud surfaces (the cloud window filter
        and the background-window masking evaluator) use this one helper so
        they agree: confirmed entries survive into cloud posture, legacy
        (unconfirmed) entries do not. Tightening knobs (exclude_apps,
        mask_domains, mask_title_patterns) are untouched."""
        return replace(self, allow_apps=self.allow_apps & self.confirmed_allow_apps)

    def is_masked_domain(self, domain: str) -> bool:
        domain = domain.lower()
        return any(domain == d or domain.endswith("." + d) for d in self.mask_domains)

    def matches_title_pattern(self, title: str) -> str | None:
        """Return the matching pattern string, or None."""
        for pat in self.mask_title_patterns:
            if pat.search(title):
                return pat.pattern
        return None

    def __post_init__(self) -> None:
        # Bundle-id membership is case-normalized (SCR-235): macOS treats
        # CFBundleIdentifiers case-insensitively, and confirmed-allow
        # authority rests on two-list membership that must not silently
        # break on a casing mismatch. Idempotent — safe to re-run on
        # unpickle and dataclasses.replace().
        for name in ("exclude_apps", "allow_apps", "confirmed_allow_apps"):
            object.__setattr__(
                self, name, frozenset(b.lower() for b in getattr(self, name))
            )
        if isinstance(self.app_classes, (dict, MappingProxyType)):
            object.__setattr__(
                self,
                "app_classes",
                MappingProxyType({k.lower(): v for k, v in self.app_classes.items()}),
            )

    def __getstate__(self) -> dict[str, object]:
        # MappingProxyType is not picklable; convert to plain dict for transport.
        # __post_init__ on the unpickled instance re-wraps it.
        state = self.__dict__.copy()
        if isinstance(state.get("app_classes"), MappingProxyType):
            state["app_classes"] = dict(state["app_classes"])
        # mask_title_patterns: re.Pattern is picklable in 3.7+; leave as-is.
        return state

    def __setstate__(self, state: dict[str, object]) -> None:
        for key, value in state.items():
            object.__setattr__(self, key, value)
        self.__post_init__()


class InvalidPrivacyConfigError(Exception):
    """Raised when the [privacy] config section is malformed."""


def parse_privacy_config(toml_dict: dict) -> PrivacyConfig:
    """Parse the [privacy] section of a TOML config dict.

    Args:
        toml_dict: The full parsed TOML dict (not just the privacy section).

    Returns:
        PrivacyConfig with validated settings.

    Raises:
        InvalidPrivacyConfigError: On malformed values.
    """
    section = toml_dict.get("privacy", {})
    if not isinstance(section, dict):
        raise InvalidPrivacyConfigError(
            f"[privacy] must be a table, got {type(section).__name__}"
        )

    # mode — env var can tighten but never loosen
    config_mode_str = section.get("mode", "internal")
    if not isinstance(config_mode_str, str):
        raise InvalidPrivacyConfigError(
            f"privacy.mode must be a string, got {type(config_mode_str).__name__}"
        )
    env_mode_str = os.environ.get("SCREENCAP_PRIVACY_MODE")
    mode_str = env_mode_str or config_mode_str
    try:
        mode = PrivacyMode(mode_str.lower())
    except ValueError:
        valid = ", ".join(m.value for m in PrivacyMode)
        raise InvalidPrivacyConfigError(
            f"Invalid privacy.mode={mode_str!r}. Must be one of: {valid}"
        )

    if env_mode_str:
        try:
            config_mode = PrivacyMode(config_mode_str.lower())
        except ValueError:
            config_mode = PrivacyMode.INTERNAL
        if _MODE_STRICTNESS[mode] > _MODE_STRICTNESS[config_mode]:
            mode = config_mode

    if mode == PrivacyMode.SHARED:
        raise InvalidPrivacyConfigError(
            "privacy.mode='shared' is not yet enforced (MASK_REGION is not "
            "implemented). Use 'public' or 'internal' until region masking "
            "is available."
        )

    # exclude_apps
    raw_apps = section.get("exclude_apps", [])
    if not isinstance(raw_apps, list):
        raise InvalidPrivacyConfigError(
            f"privacy.exclude_apps must be a list, got {type(raw_apps).__name__}"
        )
    for i, app in enumerate(raw_apps):
        if not isinstance(app, str):
            raise InvalidPrivacyConfigError(
                f"privacy.exclude_apps[{i}] must be a string, got {type(app).__name__}"
            )
    exclude_apps = frozenset(raw_apps)

    # allow_apps
    raw_allow = section.get("allow_apps", [])
    if not isinstance(raw_allow, list):
        raise InvalidPrivacyConfigError(
            f"privacy.allow_apps must be a list, got {type(raw_allow).__name__}"
        )
    for i, app in enumerate(raw_allow):
        if not isinstance(app, str):
            raise InvalidPrivacyConfigError(
                f"privacy.allow_apps[{i}] must be a string, got {type(app).__name__}"
            )
    allow_apps = frozenset(raw_allow)

    # confirmed_allow_apps — the subset of allow_apps the user explicitly
    # confirmed (SCR-235); authoritative only when present in both lists.
    raw_confirmed = section.get("confirmed_allow_apps", [])
    if not isinstance(raw_confirmed, list):
        raise InvalidPrivacyConfigError(
            f"privacy.confirmed_allow_apps must be a list, got {type(raw_confirmed).__name__}"
        )
    for i, app in enumerate(raw_confirmed):
        if not isinstance(app, str):
            raise InvalidPrivacyConfigError(
                f"privacy.confirmed_allow_apps[{i}] must be a string, got {type(app).__name__}"
            )
    confirmed_allow_apps = frozenset(raw_confirmed)

    # mask_domains
    raw_domains = section.get("mask_domains", [])
    if not isinstance(raw_domains, list):
        raise InvalidPrivacyConfigError(
            f"privacy.mask_domains must be a list, got {type(raw_domains).__name__}"
        )
    for i, domain in enumerate(raw_domains):
        if not isinstance(domain, str):
            raise InvalidPrivacyConfigError(
                f"privacy.mask_domains[{i}] must be a string, got {type(domain).__name__}"
            )
    mask_domains = frozenset(d.lower() for d in raw_domains)

    # mask_title_patterns
    raw_patterns = section.get("mask_title_patterns", [])
    if not isinstance(raw_patterns, list):
        raise InvalidPrivacyConfigError(
            f"privacy.mask_title_patterns must be a list, got {type(raw_patterns).__name__}"
        )
    compiled: list[re.Pattern[str]] = []
    for i, pat in enumerate(raw_patterns):
        if not isinstance(pat, str):
            raise InvalidPrivacyConfigError(
                f"privacy.mask_title_patterns[{i}] must be a string, got {type(pat).__name__}"
            )
        try:
            compiled.append(re.compile(pat))
        except re.error as exc:
            raise InvalidPrivacyConfigError(
                f"privacy.mask_title_patterns[{i}] is not a valid regex: {exc}"
            )

    # app_classes
    raw_app_classes = section.get("app_classes", {})
    if not isinstance(raw_app_classes, dict):
        raise InvalidPrivacyConfigError(
            f"privacy.app_classes must be a table, got {type(raw_app_classes).__name__}"
        )
    app_classes: dict[str, ContextClass] = {}
    for bid, cls_str in raw_app_classes.items():
        if not isinstance(cls_str, str):
            raise InvalidPrivacyConfigError(
                f"privacy.app_classes.{bid} must be a string, got {type(cls_str).__name__}"
            )
        try:
            app_classes[bid] = ContextClass(cls_str.lower())
        except ValueError:
            valid = ", ".join(c.value for c in ContextClass)
            raise InvalidPrivacyConfigError(
                f"Invalid privacy.app_classes.{bid}={cls_str!r}. "
                f"Must be one of: {valid}"
            )

    return PrivacyConfig(
        mode=mode,
        exclude_apps=exclude_apps,
        allow_apps=allow_apps,
        confirmed_allow_apps=confirmed_allow_apps,
        mask_domains=mask_domains,
        mask_title_patterns=tuple(compiled),
        app_classes=app_classes,
    )


# ---------------------------------------------------------------------------
# Protocol contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameMetadata:
    """Metadata about a frame or data surface for context classification."""

    bundle_id: str = ""
    window_title: str = ""
    domain: str | None = None  # None = unknown/unavailable
    timestamp: float = 0.0
    browser_url: str | None = None  # Full URL for keyword detection


@dataclass(frozen=True)
class ContextResult:
    """Output of a ContextClassifier."""

    context_class: ContextClass
    confidence: str = ""  # e.g. "domain", "bundle_id", "title"
    evidence: str = ""  # human-readable, e.g. the bundle ID that matched


class ContextClassifier(Protocol):
    def classify(self, metadata: FrameMetadata) -> ContextResult: ...


class PolicyEvaluator(Protocol):
    def evaluate(
        self,
        context: ContextResult,
        metadata: FrameMetadata,
        mode: PrivacyMode | None = None,
    ) -> ActionDecision: ...


# ---------------------------------------------------------------------------
# Default evaluator
# ---------------------------------------------------------------------------

# Map ContextClass -> ReasonCode for matrix-driven decisions
_CONTEXT_REASON: dict[ContextClass, str] = {
    ContextClass.PASSWORD_MANAGER: ReasonCode.CONTEXT_PASSWORD_MANAGER,
    ContextClass.BANKING: ReasonCode.CONTEXT_BANKING,
    ContextClass.EMAIL: ReasonCode.CONTEXT_EMAIL,
    ContextClass.CHAT: ReasonCode.CONTEXT_CHAT,
    ContextClass.CALENDAR: ReasonCode.CONTEXT_CALENDAR,
    ContextClass.VIDEO_CALL: ReasonCode.CONTEXT_VIDEO_CALL,
    ContextClass.BROWSER_UNVERIFIED: ReasonCode.CONTEXT_BROWSER_UNVERIFIED,
    ContextClass.CODE_EDITOR_TERMINAL: ReasonCode.CONTEXT_CODE_EDITOR_TERMINAL,
    ContextClass.ADMIN_CONSOLE: ReasonCode.CONTEXT_ADMIN_CONSOLE,
    ContextClass.AUTH_FLOW: ReasonCode.CONTEXT_AUTH_FLOW,
    ContextClass.PAYMENT_FLOW: ReasonCode.CONTEXT_PAYMENT_FLOW,
    ContextClass.CLOUD_STORAGE: ReasonCode.CONTEXT_CLOUD_STORAGE,
    ContextClass.UNKNOWN: ReasonCode.CONTEXT_UNKNOWN,
}


class DefaultPolicyEvaluator:
    """Evaluates policy using config rules + action matrix.

    Precedence (highest to lowest), per SCR-235:
    1. Explicit user denylist (exclude_apps via bundle_id)
    2. User mask rules (mask_domains, mask_title_patterns) — user-authored
       tightening outranks any allow, confirmed or not
    3. Confirmed user allowlist (allow_apps ∩ confirmed_allow_apps) —
       authoritative over the matrix in every mode, except browser windows
       refined to a confirmation-required context (R12)
    4. Legacy (unconfirmed) allowlist — keeps the pre-SCR-235
       matrix-strictness floor
    5. Action matrix lookup (context_class, privacy_mode)
    """

    def __init__(self, config: PrivacyConfig) -> None:
        self._config = config

    @property
    def config(self) -> PrivacyConfig:
        return self._config

    def _is_browser_bundle(self, bundle_id: str) -> bool:
        """The classifier's browser definition (classify.py step 3): a stock
        browser or an app_classes-tagged one. The R12 carve-out must use the
        same definition or it goes dead for stock browsers with no
        app_classes entry. Case-insensitive on both arms — the confirmed
        membership check is case-insensitive, so this must be too, or a
        non-canonically-cased confirmed browser skips the carve-out."""
        return (
            bundle_id.lower() in BROWSER_BUNDLE_IDS_LOWER
            or self._config.app_class_for(bundle_id) == ContextClass.BROWSER_UNVERIFIED
        )

    def evaluate(
        self,
        context: ContextResult,
        metadata: FrameMetadata,
        mode: PrivacyMode | None = None,
    ) -> ActionDecision:
        mode = mode or self._config.mode

        # 1. Explicit app exclusion — beats everything.
        if metadata.bundle_id and self._config.is_excluded_app(metadata.bundle_id):
            return ActionDecision(
                action=PrivacyAction.EXCLUDE,
                reason=ReasonCode.POLICY_EXCLUDED_APP,
                evidence=metadata.bundle_id,
            )

        # 2. User mask rules — finer-grained user-authored rules outrank
        # app-level allows (SCR-235 R4): a domain/title rule the user wrote
        # holds inside apps they allowed. For legacy allows in non-blocking
        # contexts this is a deliberate, strictly-tightening change from the
        # pre-SCR-235 order (allow used to return before these ran).
        if metadata.domain and self._config.is_masked_domain(metadata.domain):
            matrix_action = get_matrix_action(context.context_class, mode)
            forced = stricter(PrivacyAction.MASK_WINDOW, matrix_action)
            return ActionDecision(
                action=forced,
                reason=ReasonCode.POLICY_MASKED_DOMAIN,
                evidence=metadata.domain,
            )
        if metadata.window_title:
            title_match = self._config.matches_title_pattern(metadata.window_title)
            if title_match:
                matrix_action = get_matrix_action(context.context_class, mode)
                forced = stricter(PrivacyAction.MASK_WINDOW, matrix_action)
                return ActionDecision(
                    action=forced,
                    reason=ReasonCode.POLICY_MASKED_TITLE,
                    evidence=f"pattern={title_match}",
                )

        # 3. Confirmed allow — the user's explicit, confirmed choice beats
        # the matrix in every mode (SCR-235 R1). One carve-out (R12): a
        # browser window the classifier refined to a confirmation-required
        # context (banking/auth/payment page) keeps the matrix action —
        # app-level consent does not extend to arbitrary sensitive content
        # a browser can host.
        if metadata.bundle_id and self._config.is_confirmed_allowed_app(
            metadata.bundle_id
        ):
            browser_on_sensitive_page = (
                self._is_browser_bundle(metadata.bundle_id)
                and context.context_class is not ContextClass.BROWSER_UNVERIFIED
                and context.context_class in CONFIRMATION_REQUIRED_CLASSES
            )
            if not browser_on_sensitive_page:
                return ActionDecision(
                    action=PrivacyAction.ALLOW,
                    reason=ReasonCode.POLICY_ALLOWED_APP,
                    evidence=metadata.bundle_id,
                )
            # fall through to step 5 (matrix decides for the refined context)

        # 4. Legacy (unconfirmed) allow — the pre-SCR-235 matrix-strictness
        # floor, preserved verbatim: entries from before Unit 7a (or written
        # outside the confirmation flow) must not silently re-enable raw
        # capture. Confirming via the UI/CLI is the upgrade path.
        elif metadata.bundle_id and self._config.is_allowed_app(metadata.bundle_id):
            matrix_action = get_matrix_action(context.context_class, mode)
            if matrix_action == PrivacyAction.EXCLUDE:
                return ActionDecision(
                    action=PrivacyAction.EXCLUDE,
                    reason=ReasonCode.POLICY_EXCLUDED_APP,
                    evidence=f"matrix override: {context.context_class.value}",
                )
            if matrix_action in (PrivacyAction.MASK_WINDOW, PrivacyAction.TEXT_REDACT):
                # The floor: allow_apps doesn't override these — fall through
                # to step 5 so the user gets the matrix's action.
                pass
            else:
                # Browser with refined context → let the matrix decide
                # (legacy semantics: app_classes-tagged browsers only).
                is_browser = (
                    self._config.app_class_for(metadata.bundle_id)
                    == ContextClass.BROWSER_UNVERIFIED
                )
                if is_browser and context.context_class != ContextClass.BROWSER_UNVERIFIED:
                    pass  # fall through to matrix (step 5)
                else:
                    return ActionDecision(
                        action=PrivacyAction.ALLOW,
                        reason=ReasonCode.POLICY_ALLOWED_APP,
                        evidence=metadata.bundle_id,
                    )

        # 5. Matrix default
        action = get_matrix_action(context.context_class, mode)
        reason = _CONTEXT_REASON.get(
            context.context_class, ReasonCode.POLICY_MODE_DEFAULT
        )
        return ActionDecision(
            action=action,
            reason=reason,
            evidence=context.evidence,
        )
