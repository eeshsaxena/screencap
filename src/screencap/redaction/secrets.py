"""DetectSecretsDetector — wraps detect-secrets plugins."""

from __future__ import annotations

from detect_secrets.plugins.aws import AWSKeyDetector
from detect_secrets.plugins.basic_auth import BasicAuthDetector
from detect_secrets.plugins.github_token import GitHubTokenDetector
from detect_secrets.plugins.high_entropy_strings import (
    Base64HighEntropyString,
    HexHighEntropyString,
)
from detect_secrets.plugins.jwt import JwtTokenDetector
from detect_secrets.plugins.keyword import KeywordDetector
from detect_secrets.plugins.openai import OpenAIDetector
from detect_secrets.plugins.private_key import PrivateKeyDetector
from detect_secrets.plugins.sendgrid import SendGridDetector
from detect_secrets.plugins.slack import SlackDetector
from detect_secrets.plugins.stripe import StripeDetector

from screencap.redaction.engine import Detection, EntityType

# Map detect-secrets plugin types to EntityType constants
_TYPE_MAP: dict[str, str] = {
    "AWS Access Key": EntityType.API_KEY,
    "GitHub Token": EntityType.API_KEY,
    "OpenAI Token": EntityType.API_KEY,
    "Stripe Access Key": EntityType.API_KEY,
    "Slack Token": EntityType.API_KEY,
    "SendGrid API Key": EntityType.API_KEY,
    "Private Key": EntityType.PRIVATE_KEY,
    "JSON Web Token": EntityType.JWT,
    "Secret Keyword": EntityType.PASSWORD,
    "Basic Auth Credentials": EntityType.PASSWORD,
}

# Context words for entropy detector post-filtering
_CONTEXT_WORDS = frozenset({
    "key", "secret", "token", "password", "api", "auth",
    "credential", "private", "access", "bearer",
})

# Entropy plugin class names for identification
_ENTROPY_PLUGINS = frozenset({
    "Base64HighEntropyString",
    "HexHighEntropyString",
})


def _has_secret_context(text: str, match_start: int, match_end: int) -> bool:
    """Check if a 50-char window around the match contains a context word."""
    window_start = max(0, match_start - 50)
    window_end = min(len(text), match_end + 50)
    window = text[window_start:window_end].lower()
    return any(word in window for word in _CONTEXT_WORDS)


class DetectSecretsDetector:
    """Wraps detect-secrets plugins for secret detection."""

    def __init__(self) -> None:
        self._plugins = [
            AWSKeyDetector(),
            GitHubTokenDetector(),
            OpenAIDetector(),
            StripeDetector(),
            SlackDetector(),
            SendGridDetector(),
            PrivateKeyDetector(),
            JwtTokenDetector(),
            BasicAuthDetector(),
            KeywordDetector(),
            Base64HighEntropyString(limit=4.5),
            HexHighEntropyString(limit=3.0),
        ]

    def detect(self, text: str) -> list[Detection]:
        # No meaningful secret fits in < 10 chars
        if len(text) < 10:
            return []

        detections: list[Detection] = []

        # Split into lines preserving line endings for correct offset tracking
        lines = text.splitlines(True)
        line_offsets: list[int] = []
        offset = 0
        for line in lines:
            line_offsets.append(offset)
            offset += len(line)

        for plugin in self._plugins:
            plugin_name = type(plugin).__name__
            is_entropy = plugin_name in _ENTROPY_PLUGINS

            for line_num, line in enumerate(lines):
                # Strip line ending for analysis but track the full line for offsets
                line_text = line.rstrip("\n\r\u2028\u2029")
                if not line_text:
                    continue

                results = plugin.analyze_line(
                    filename="text",
                    line=line_text,
                    line_number=line_num,
                )

                for potential_secret in results:
                    secret_value = potential_secret.secret_value
                    if not secret_value:
                        continue

                    # Map type
                    entity_type = _TYPE_MAP.get(
                        potential_secret.type, EntityType.SECRET
                    )

                    # Find all occurrences of the secret value on this line
                    base_offset = line_offsets[line_num]
                    search_start = 0
                    while True:
                        pos = line_text.find(secret_value, search_start)
                        if pos == -1:
                            break

                        abs_start = base_offset + pos
                        abs_end = abs_start + len(secret_value)

                        # Entropy post-filter: require context words nearby
                        if is_entropy and not _has_secret_context(
                            text, abs_start, abs_end
                        ):
                            search_start = pos + 1
                            continue

                        detections.append(
                            Detection(
                                entity_type=entity_type,
                                start=abs_start,
                                end=abs_end,
                                score=0.9,
                                source="secrets",
                            )
                        )
                        search_start = pos + 1

        return detections
