"""RegexDetector — custom patterns for edge cases."""

from __future__ import annotations

import re

from screencap.redaction.engine import Detection, EntityType

# Luhn checksum for credit card validation
def _luhn_check(number: str) -> bool:
    """Validate a credit card number using the Luhn algorithm."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    reverse = digits[::-1]
    for i, d in enumerate(reverse):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[re.Pattern[str], str, float]] = [
    # Password assignments: password=xxx, pwd: xxx, PASS=xxx
    # Capture the value (group 1) — we redact the whole match
    (
        re.compile(
            r"""(?i)(?:password|passwd|pwd|pass)\s*[=:]\s*(\S+)""",
        ),
        EntityType.PASSWORD,
        0.9,
    ),
    # Connection strings: postgresql://, mysql://, mongodb://, redis://, etc.
    (
        re.compile(
            r"""(?:postgresql|postgres|mysql|mongodb(?:\+srv)?|redis|rediss|amqp|amqps|mssql)://\S+""",
        ),
        EntityType.CONNECTION_STRING,
        0.95,
    ),
    # JWT tokens: eyJ followed by base64url.base64url.base64url
    (
        re.compile(
            r"""eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}""",
        ),
        EntityType.JWT,
        0.95,
    ),
    # Credit cards with spaces or dashes (Luhn validated in detect())
    (
        re.compile(
            r"""\b(\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4})\b""",
        ),
        EntityType.CREDIT_CARD,
        0.85,
    ),
    # Basic auth URLs: https://user:password@host
    (
        re.compile(
            r"""https?://[^:@\s]+:[^@\s]+@[^\s]+""",
        ),
        EntityType.PASSWORD,
        0.9,
    ),
    # Bearer tokens: Bearer eyJ... or Authorization: Bearer ...
    (
        re.compile(
            r"""(?i)(?:authorization:\s*)?bearer\s+([A-Za-z0-9_.\-]+)""",
        ),
        EntityType.API_KEY,
        0.9,
    ),
    # SSN: 3 digits - 2 digits - 4 digits (with context to reduce false positives)
    (
        re.compile(
            r"""(?i)(?:ssn|social[.\s_-]security|social\s+security)\s*(?:number)?[\s:=#]*(\d{3}-\d{2}-\d{4})""",
        ),
        EntityType.SSN,
        0.9,
    ),
    # Stripe publishable keys (sk_live/sk_test already covered by detect-secrets).
    # Uses \S instead of [A-Za-z0-9] to tolerate OCR artifacts (e.g. Ø for 0).
    (
        re.compile(r"""\bpk_(?:live|test)_\S{20,}"""),
        EntityType.API_KEY,
        0.9,
    ),
    # Generic secret/key/token assignments with unquoted values.
    # Excludes quoted values (handled by detect-secrets KeywordDetector)
    # and file paths (start with / or ~) to reduce false positives.
    # Allows optional whitespace after _ before keyword (OCR: JWT_ SECRET).
    # Value class includes @ to tolerate OCR digit→@ substitution.
    (
        re.compile(
            r"""(?i)\b\w+_\s*(?:SECRET|KEY|TOKEN)\s*=\s*"""
            r"""(?!["'/~])"""
            r"""([A-Za-z0-9_\-+/.@]{24,})""",
        ),
        EntityType.API_KEY,
        0.85,
    ),
]


class RegexDetector:
    """Custom regex-based detector for edge cases."""

    def detect(self, text: str) -> list[Detection]:
        detections: list[Detection] = []

        for pattern, entity_type, score in _PATTERNS:
            for match in pattern.finditer(text):
                start = match.start()
                end = match.end()

                # Luhn validation for credit cards
                if entity_type == EntityType.CREDIT_CARD:
                    card_text = match.group(1) if match.lastindex else match.group(0)
                    if not _luhn_check(card_text):
                        continue

                detections.append(
                    Detection(
                        entity_type=entity_type,
                        start=start,
                        end=end,
                        score=score,
                        source="regex",
                    )
                )

        return detections
