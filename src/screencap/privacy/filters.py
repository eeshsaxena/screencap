"""Heuristic-based false positive filter.

Each rule is a named function with a docstring explaining WHY it exists,
linked to the specific false positive pattern it addresses.
"""

from __future__ import annotations

import re
import unicodedata

from screencap.privacy import Detection, EntityType


# Month patterns that NER models misclassify as PERSON
_MONTH_PATTERNS = frozenset({
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june",
    "july", "august", "september", "october", "november", "december",
})

# CLI/UI keywords misclassified as PERSON
_UI_KEYWORDS = frozenset({
    "tab", "window", "help", "file", "edit", "view",
    "settings", "preferences", "inbox", "starred", "snoozed",
    "sent", "drafts", "more", "trash", "spam", "archive",
})

# Software/app/tool names that NER models misclassify as PERSON.
# Individual words only (full-span matching via span.strip().lower()).
# Names < 4 chars excluded — already caught by _reject_short_person.
_SOFTWARE_NAMES = frozenset({
    # macOS apps
    "safari", "chrome", "firefox", "slack", "discord", "telegram",
    "whatsapp", "signal", "notion", "figma", "sketch", "xcode",
    "finder", "keynote", "pages", "numbers",
    # Terminals / editors
    "ghostty", "kitty", "alacritty", "iterm", "wezterm", "hyper",
    "vscode", "sublime", "neovim", "emacs",
    # CLI tools
    "homebrew", "cargo", "rustup", "yarn", "pnpm", "pipx", "conda",
    "docker", "kubectl", "kubernetes", "terraform", "ansible", "vagrant",
    "gradle", "maven", "cmake", "webpack", "vite", "pytest", "jest", "ruff",
    # Products / services
    "github", "gitlab", "bitbucket", "jira", "linear", "vercel",
    "netlify", "heroku", "cloudflare", "datadog", "sentry", "grafana",
    "prometheus", "elasticsearch", "redis", "postgres", "mongodb",
    "mysql", "nginx", "apache", "bitwarden",
    # Languages (where name != common person name)
    "python", "golang", "typescript", "javascript", "kotlin", "swift",
    "elixir", "clojure", "haskell", "erlang", "scala", "fortran",
    "cobol", "perl", "rust",
    # Ambiguous but accepted trade-off — tool names far more common than
    # person names in dev screen recordings
    "ruby", "julia", "hugo",
})

_SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")


class HeuristicFilter:
    """Reject known false positive patterns."""

    def filter(self, text: str, detections: list[Detection]) -> list[Detection]:
        return [d for d in detections if not self._is_false_positive(text, d)]

    def _is_false_positive(self, text: str, det: Detection) -> bool:
        span = text[det.start:det.end]

        if det.entity_type == EntityType.PERSON:
            if self._reject_short_person(span):
                return True
            if self._reject_month_person(span):
                return True
            if self._reject_ui_keyword_person(span):
                return True
            if self._reject_app_name_person(span):
                return True

        if det.entity_type == EntityType.ADDRESS:
            if self._reject_unicode_block_address(span):
                return True

        if det.entity_type == EntityType.SSN:
            if self._reject_malformed_ssn(span):
                return True

        if det.entity_type == EntityType.PHONE:
            if self._reject_no_separator_phone(span):
                return True

        return False

    @staticmethod
    def _reject_short_person(span: str) -> bool:
        """Reject PERSON < 4 chars.

        Why: NER models flag "Jr", "Mar", "Tab", "Al" as PERSON.
        These are abbreviations, UI labels, or month names — not people.
        Observed in: Gmail sidebar OCR, terminal output, menu bars.
        """
        return len(span.strip()) < 4

    @staticmethod
    def _reject_month_person(span: str) -> bool:
        """Reject PERSON matching month name patterns.

        Why: "Mar", "Jan", "May" detected as PERSON in calendar UIs
        and date output (ls -la, git log).
        """
        return span.strip().lower() in _MONTH_PATTERNS

    @staticmethod
    def _reject_ui_keyword_person(span: str) -> bool:
        """Reject PERSON matching common UI/CLI keywords.

        Why: "Tab", "Window", "Inbox", "Starred" flagged as PERSON
        in accessibility text from menu bars and sidebars.
        """
        return span.strip().lower() in _UI_KEYWORDS

    @staticmethod
    def _reject_app_name_person(span: str) -> bool:
        """Reject PERSON matching known software/app/tool names.

        Why: GLiNER flags capitalized app names (Ghostty, Homebrew, Docker)
        as PERSON. These appear constantly in window titles, terminal output,
        and accessibility text during dev screen recordings.
        """
        return span.strip().lower() in _SOFTWARE_NAMES

    @staticmethod
    def _reject_unicode_block_address(span: str) -> bool:
        """Reject ADDRESS containing only Unicode box-drawing chars.

        Why: TUI box borders detected as ADDRESS.
        """
        stripped = span.strip()
        if not stripped:
            return True
        non_space = [c for c in stripped if not c.isspace()]
        if not non_space:
            return True
        return all(
            unicodedata.category(c) in ("So", "Sm", "Sk", "Sc")
            or c in "─│┌┐└┘├┤┬┴┼╔╗╚╝║═"
            for c in non_space
        )

    @staticmethod
    def _reject_malformed_ssn(span: str) -> bool:
        """Reject SSN not containing NNN-NN-NNNN pattern.

        Why: PIDs, port numbers, version strings, git hashes (12345, 5432, 3.12, 34d0845)
        flagged as SSN in terminal output. NER models sometimes include
        context prefix (e.g., "SSN: 123-45-6789"), so we search for the
        pattern anywhere in the span rather than requiring an exact match.
        """
        return not _SSN_RE.search(span)

    @staticmethod
    def _reject_no_separator_phone(span: str) -> bool:
        """Reject PHONE with no separators (dashes, dots, spaces, parens).

        Why: Long numeric sequences in terminal output (PIDs, addresses)
        flagged as PHONE. Real phone numbers have formatting.
        """
        stripped = span.strip()
        return not any(c in stripped for c in "-.()+/ ")
