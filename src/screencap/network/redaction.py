"""Capture-time redaction primitives (R10 — single source of truth).

These frozensets and helper functions are imported by the Unit 4 capture
addon (and later by V1.5's ``NetworkScrubPipeline``). Centralizing the
denylists here keeps the lists in lockstep across capture-time and
post-capture scrubbing.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Header denylists
# ---------------------------------------------------------------------------

# Always replaced with ``[REDACTED:auth-header]``. Stored lowercase so the
# match site can do a single ``name.lower() in AUTH_HEADER_NAMES`` check.
AUTH_HEADER_NAMES: frozenset[str] = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-hub-signature",
    "x-hub-signature-256",
    "x-webhook-secret",
    "x-accesskey",
})

# Glob suffix patterns matched case-insensitively against the header name.
# ``*-api-key`` matches any header ending in ``-api-key`` (e.g.
# ``X-API-Key``, ``CF-API-Key``). Any header matching one of these
# patterns is treated as auth and redacted.
AUTH_HEADER_PATTERNS: tuple[str, ...] = (
    "*-api-key",
    "*-token",
    "*-secret",
)

# Replaced with ``[REDACTED:sensitive-header]`` — these aren't auth per
# se but they leak referrer/origin context, CSRF tokens, or client-IP
# info that we don't want flowing to a remote analyzer.
SENSITIVE_HEADER_NAMES: frozenset[str] = frozenset({
    "referer",
    "origin",
    "x-csrf-token",
    "x-xsrf-token",
    "x-auth-token",
    "x-session-id",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-real-ip",
    "true-client-ip",
})

# ---------------------------------------------------------------------------
# Query-param denylist
# ---------------------------------------------------------------------------

# Lowercase. Values for any matching parameter name are replaced with
# ``[REDACTED:query-param]``. The original parameter NAME and ordering
# are preserved.
SENSITIVE_QUERY_PARAM_NAMES: frozenset[str] = frozenset({
    "access_token",
    "id_token",
    "refresh_token",
    "token",
    "api_key",
    "apikey",
    "client_secret",
    "code",
    "signature",
    "sig",
    "session",
    "auth",
    "password",
    "pwd",
    "x-amz-signature",
    "x-amz-security-token",
    "awsaccesskeyid",
    "awssecretkey",
    "state",
    "nonce",
    "_token",
    "csrf",
    "_csrf",
    "xsrf-token",
    "assertion",
    "session_state",
    "id_token_hint",
    "client_assertion",
})


# ---------------------------------------------------------------------------
# Compiled glob → regex for AUTH_HEADER_PATTERNS
# ---------------------------------------------------------------------------


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a simple ``*name`` glob to an anchored, case-insensitive
    regex. Only ``*`` is supported (no ``?`` or character classes).

    Leading ``*-`` is a special form: ``*-api-key`` matches both
    ``X-API-Key`` (prefix + dash) and bare ``api-key`` (no prefix). This
    mirrors how the brainstorm specifies the patterns — the ``-`` is
    intended to be the optional vendor separator, not a hard requirement.
    """
    if pattern.startswith("*-"):
        # Make the dash optional: pattern '*-api-key' becomes regex
        # '^(.*-)?api-key$' so bare 'api-key' also matches.
        suffix = pattern[2:]
        escaped = re.escape(suffix)
        return re.compile(rf"^(.*-)?{escaped}$", re.IGNORECASE)

    parts = pattern.split("*")
    escaped = ".*".join(re.escape(p) for p in parts)
    return re.compile(rf"^{escaped}$", re.IGNORECASE)


_AUTH_HEADER_PATTERN_RES: tuple[re.Pattern[str], ...] = tuple(
    _glob_to_regex(p) for p in AUTH_HEADER_PATTERNS
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_auth_header(name: str) -> bool:
    """Return ``True`` iff ``name`` matches an auth-header denylist entry.

    Matches case-insensitively against :data:`AUTH_HEADER_NAMES` (exact)
    and :data:`AUTH_HEADER_PATTERNS` (glob suffix).
    """
    lowered = name.lower()
    if lowered in AUTH_HEADER_NAMES:
        return True
    return any(rx.match(name) for rx in _AUTH_HEADER_PATTERN_RES)


def is_sensitive_header(name: str) -> bool:
    """Return ``True`` iff ``name`` is in :data:`SENSITIVE_HEADER_NAMES`."""
    return name.lower() in SENSITIVE_HEADER_NAMES


def redact_headers(
    headers: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Return a new header list with auth/sensitive values redacted.

    For each ``(name, value)`` tuple:
        - If :func:`is_auth_header` matches, replace with
          ``[REDACTED:auth-header]``.
        - Else if :func:`is_sensitive_header` matches, replace with
          ``[REDACTED:sensitive-header]``.
        - Otherwise, keep the value verbatim.

    Multi-valued headers (e.g. multiple ``Set-Cookie`` lines) are
    preserved — each entry is handled independently and the list shape
    is unchanged.
    """
    result: list[tuple[str, str]] = []
    for name, value in headers:
        if is_auth_header(name):
            result.append((name, "[REDACTED:auth-header]"))
        elif is_sensitive_header(name):
            result.append((name, "[REDACTED:sensitive-header]"))
        else:
            result.append((name, value))
    return result


def redact_url_query(url: str) -> str:
    """Redact sensitive query-parameter values in ``url``.

    Parses the query string with :func:`urllib.parse.parse_qsl`
    (``keep_blank_values=True``), replaces values for keys whose
    lowercase form is in :data:`SENSITIVE_QUERY_PARAM_NAMES` with
    ``[REDACTED:query-param]``, and re-encodes. Parameter names and
    ordering are preserved; duplicate keys are kept independently.

    URLs with no query string are returned unchanged.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url

    pairs = parse_qsl(parts.query, keep_blank_values=True)
    redacted: list[tuple[str, str]] = []
    for key, value in pairs:
        if key.lower() in SENSITIVE_QUERY_PARAM_NAMES:
            redacted.append((key, "[REDACTED:query-param]"))
        else:
            redacted.append((key, value))

    # ``safe="="`` keeps ``[REDACTED:query-param]`` legible (urlencode
    # would otherwise percent-encode the ``[``, ``]``, and ``:`` chars).
    new_query = urlencode(redacted, safe="[]:=-")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )
