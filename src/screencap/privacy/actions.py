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


# Actions that mean "this app/surface should not be captured".
# Used by both recorder_enforcement (capture-time) and scrubber (post-processing).
BLOCK_ACTIONS = frozenset({PrivacyAction.EXCLUDE, PrivacyAction.MASK_WINDOW})


@dataclass(frozen=True)
class ActionDecision:
    """Result of a policy evaluation."""

    action: PrivacyAction
    reason: str  # ReasonCode constant
    evidence: str = ""  # human-readable provenance (e.g. bundle ID, domain)
