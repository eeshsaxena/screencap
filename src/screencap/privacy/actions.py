"""Privacy actions and decision container.

Defines the possible actions the privacy system can take on a frame or
data surface, plus the ActionDecision that carries the final verdict,
reason code, and provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PrivacyAction(Enum):
    """What the privacy system decides to do with a piece of content."""

    EXCLUDE = "exclude"
    MASK_WINDOW = "mask_window"
    MASK_REGION = "mask_region"
    TEXT_REDACT = "text_redact"
    OCR_FALLBACK = "ocr_fallback"
    ALLOW = "allow"


# Ordered from most restrictive to least restrictive.
_ACTION_SEVERITY: dict[PrivacyAction, int] = {
    PrivacyAction.EXCLUDE: 0,
    PrivacyAction.MASK_WINDOW: 1,
    PrivacyAction.MASK_REGION: 2,
    PrivacyAction.TEXT_REDACT: 3,
    PrivacyAction.OCR_FALLBACK: 4,
    PrivacyAction.ALLOW: 5,
}


def stricter(a: PrivacyAction, b: PrivacyAction) -> PrivacyAction:
    """Return whichever action is more restrictive."""
    return a if _ACTION_SEVERITY[a] <= _ACTION_SEVERITY[b] else b


# Actions that block screenshot capture entirely (frame dropped).
# MASK_WINDOW is no longer here — screenshots are captured normally for
# MASK_WINDOW apps and selectively masked at scrub time.
BLOCK_ACTIONS = frozenset({PrivacyAction.EXCLUDE})

# Actions that require keystroke content to be nulled before writing to DB.
# Both EXCLUDE and MASK_WINDOW apps have sensitive content that must not
# be stored in keystroke fields.
KEYSTROKE_NULL_ACTIONS = frozenset({PrivacyAction.EXCLUDE, PrivacyAction.MASK_WINDOW})

# Actions that block video frame capture. Video masking (decode/re-encode)
# is out of scope, so both EXCLUDE and MASK_WINDOW drop video frames.
VIDEO_BLOCK_ACTIONS = frozenset({PrivacyAction.EXCLUDE, PrivacyAction.MASK_WINDOW})


# Keystroke content fields to null when blocking.
# Single source of truth used by both recorder_enforcement (capture-time)
# and scrubber (post-processing).
KEYSTROKE_CONTENT_FIELDS = frozenset({
    "key_char",
    "key_name",
    "key_vk",
    "canonical_key_char",
    "canonical_key_name",
    "canonical_key_vk",
    "text",
    "element_state",
    "active_segment_description",
    "available_segment_descriptions",
})


# String values of actions that indicate exclusion from capture.
# Used by the menu bar UI and override system which operate on string
# action values across IPC boundaries.
EXCLUDED_ACTION_VALUES = frozenset(a.value for a in KEYSTROKE_NULL_ACTIONS)


def make_override_key(bundle_id: str, domain: str | None) -> str:
    """Build a canonical override key for an app or browser tab.

    Native apps: ``"com.microsoft.VSCode"``
    Browser tabs: ``"com.google.Chrome::chase.com"``
    """
    return f"{bundle_id}::{domain}" if domain else bundle_id


def resolve_override(
    overrides: dict[str, str], bundle_id: str, domain: str | None,
) -> str | None:
    """Look up an override, falling back from domain-level to app-level."""
    key = make_override_key(bundle_id, domain)
    action = overrides.get(key)
    if action is None and domain:
        action = overrides.get(bundle_id)
    return action


@dataclass(frozen=True)
class ActionDecision:
    """Result of a policy evaluation."""

    action: PrivacyAction
    reason: str  # ReasonCode constant
    evidence: str = ""  # human-readable provenance (e.g. bundle ID, domain)
