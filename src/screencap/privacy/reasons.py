"""Shared reason codes for privacy audit trail.

Every privacy decision emits a reason code from this module so all layers
use a consistent taxonomy.  Import freely — this module has zero internal
dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass


class ReasonCode:
    # Policy-layer reasons
    POLICY_EXCLUDED_APP = "policy_excluded_app"
    POLICY_MASKED_DOMAIN = "policy_masked_domain"
    POLICY_MASKED_TITLE = "policy_masked_title"
    POLICY_MODE_DEFAULT = "policy_mode_default"

    # Context-classifier reasons
    CONTEXT_EMAIL = "context_email_surface"
    CONTEXT_CHAT = "context_chat_surface"
    CONTEXT_CALENDAR = "context_calendar_surface"
    CONTEXT_VIDEO_CALL = "context_video_call_surface"
    CONTEXT_PASSWORD_MANAGER = "context_password_manager"
    CONTEXT_BANKING = "context_banking"
    CONTEXT_ADMIN_CONSOLE = "context_admin_console"
    CONTEXT_CODE_EDITOR_TERMINAL = "context_code_editor_terminal"
    CONTEXT_BROWSER_UNVERIFIED = "context_browser_unverified"
    CONTEXT_UNKNOWN = "context_unknown"

    # Text-redaction reasons
    TEXT_SECRET = "text_secret"
    TEXT_PII = "text_pii"

    # OCR / image reasons
    OCR_SECRET = "ocr_secret"
    OCR_PII = "ocr_pii"

    # Secure input reasons (capture-time detection)
    SECURE_INPUT_ACTIVE = "secure_input_active"
    SECURE_FIELD_DETECTED = "secure_field_detected"

    # Blocked-app interval reasons
    BLOCKED_APP_EXCLUDE = "blocked_app_exclude"
    BLOCKED_APP_MASK = "blocked_app_mask"

    # Explicit allow
    ALLOWED = "allowed"


@dataclass(frozen=True)
class AuditEntry:
    """Export-safe audit entry for a privacy decision.

    Must NOT contain raw text, normalized text, OCR word lists,
    full titles, domains, or query parameters.
    """

    timestamp: float
    surface: str  # "screenshot", "event", "keystroke", "db_field"
    action: str  # PrivacyAction value
    reason: str  # ReasonCode constant
    context_class: str = ""  # ContextClass value
    evidence_type: str = ""  # "bundle_id", "domain", "title" — category, not content
