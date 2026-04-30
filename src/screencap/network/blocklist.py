"""Network blocklist: HTTPS CONNECT gating + HTTP request gating.

The host-blocking logic is a free function consumed at two sites:
1. :func:`build_ignore_hosts_regex` — produces regex strings for
   mitmproxy's ``ignore_hosts`` option (HTTPS CONNECT level — these
   hosts never get TLS-intercepted).
2. The capture addon's request hooks (HTTP gating — see Unit 4).

It is **not** a method on :class:`RecorderPrivacyFilter`; that class is
screen / keystroke only.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from screencap.network.config import NetworkConfig
    from screencap.privacy.policy import PrivacyConfig


# Curated default blocklist for sensitive third-party properties.
#
# Inclusion criteria:
#   - Financial services (retail banking)
#   - Password managers
#   - Auth providers (OAuth/SAML/SSO endpoints)
#   - Payment processors (auth surface, not the data API per se)
#   - Healthcare patient portals
#
# Notes on edge cases:
#   - ``stripe.com`` is included because the auth + checkout flow lives
#     at the bare apex; the public REST data surface is at
#     ``api.stripe.com`` — users can opt back in via
#     ``override_default_blocklist`` if they need the API.
#   - All entries are stored as suffix patterns. ``foo.com`` matches
#     ``foo.com`` and ``*.foo.com`` (per :func:`is_host_blocked`).
DEFAULT_BLOCKLIST: frozenset[str] = frozenset({
    # Password managers
    "1password.com",
    "1password.ca",
    "vault.bitwarden.com",
    "bitwarden.com",
    "lastpass.com",
    # Retail banking
    "chase.com",
    "bankofamerica.com",
    "wellsfargo.com",
    "capitalone.com",
    "hsbc.com",
    # Financial data / aggregators
    "plaid.com",
    # Payments (auth surface — see note above)
    "stripe.com",
    # Identity providers / SSO
    "okta.com",
    "auth0.com",
    "accounts.google.com",
    "login.microsoftonline.com",
})


# Curated default body-capture allowlist (V1.5).
#
# Inclusion criteria:
#   - Knowledge-work productivity, developer tools, or AI tools.
#   - Provides API responses materially useful for grounding model behavior.
#   - Does NOT overlap with :data:`DEFAULT_BLOCKLIST` (auth/banking/password-
#     managers always win regardless of any allowlist match — see
#     :func:`is_host_in_capture_bodies_for` for the enforcement site).
#
# Notes:
#   - This list is reviewed each major release alongside :data:`DEFAULT_BLOCKLIST`.
#   - Users can extend via ``[network] capture_bodies_for`` (UNION semantics)
#     or replace entirely via ``override_default_capture_bodies_for = true``.
#   - All entries are stored as suffix patterns (bare or ``*.``-prefixed),
#     matched via :func:`_matches_suffix` — same suffix-only invariant as
#     :data:`DEFAULT_BLOCKLIST` to prevent substring-bypass attacks.
#   - Microsoft 365 entry is the conservative subset (``*.office.com`` only,
#     not the much broader ``*.microsoft.com``) per the V1.5 ticket's
#     "Pre-V1.5 decisions (locked 2026-04-29)" block.
#
# See ``docs/tickets/medium-2026-04-27-feat-network-logging-v1.5-bodies.md``
# for the locked-decisions block backing the contents below.
DEFAULT_CAPTURE_BODIES_FOR: frozenset[str] = frozenset({
    # Developer tools
    "*.github.com",
    "api.github.com",
    "api.linear.app",
    "*.linear.app",
    "*.atlassian.net",
    "*.atlassian.com",
    # Productivity
    "*.notion.so",
    "*.notion.com",
    "docs.google.com",
    "sheets.google.com",
    "slides.google.com",
    "drive.google.com",
    "*.slack.com",
    "*.figma.com",
    # AI tools
    "chat.openai.com",
    "chatgpt.com",
    "api.openai.com",
    "claude.ai",
    "*.anthropic.com",
    # Microsoft 365 (subset — no broad *.microsoft.com)
    "*.office.com",
})


def is_ip_literal(host: str) -> bool:
    """Return ``True`` iff ``host`` is an IPv4 or IPv6 literal.

    Accepts the bracketed form for IPv6 (``[::1]``) as well as the bare
    form (``::1``). Returns ``False`` (rather than raising) on empty
    input or any host that doesn't parse as an IP literal.
    """
    if not host:
        return False
    candidate = host.strip("[]")
    if not candidate:
        return False
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def _matches_suffix(host: str, entry: str) -> bool:
    """Suffix-match a host against a blocklist entry.

    Wildcard form (``*.foo.com``) and bare form (``foo.com``) are
    treated identically: the host must equal the bare apex or end with
    ``"." + apex``. **Never** uses substring containment — that's a
    privacy bypass (e.g., ``foo.com.evil.com`` would erroneously match).
    """
    bare = entry.lower()
    if bare.startswith("*."):
        bare = bare[2:]
    host = host.lower()
    return host == bare or host.endswith("." + bare)


def is_host_blocked(
    host: str,
    privacy_config: "PrivacyConfig",
    network_config: "NetworkConfig",
) -> bool:
    """Return ``True`` iff network capture for ``host`` should be skipped.

    Combines four sources:
        1. ``privacy_config.mask_domains`` (suffix match via
           :meth:`PrivacyConfig.is_masked_domain`).
        2. ``network_config.extra_blocklist`` (suffix match).
        3. :data:`DEFAULT_BLOCKLIST` — applied unless
           ``network_config.override_default_blocklist`` is True.
        4. IP literals (always blocked).
    """
    if not host:
        return False

    if is_ip_literal(host):
        return True

    if privacy_config.is_masked_domain(host):
        return True

    for entry in network_config.extra_blocklist:
        if _matches_suffix(host, entry):
            return True

    if not network_config.override_default_blocklist:
        for entry in DEFAULT_BLOCKLIST:
            if _matches_suffix(host, entry):
                return True

    return False


def effective_capture_bodies_for(
    network_config: "NetworkConfig",
) -> frozenset[str]:
    """Return the effective body-capture allowlist for ``network_config``.

    Pre-flight uses this to detect "empty allowlist" — i.e. metadata-only
    capture for all hosts — and emit a warning. The capture addon
    consults the same set at request time via :func:`is_host_in_capture_bodies_for`.

    Semantics:
        * ``override_default_capture_bodies_for=False`` (default): user's
          ``capture_bodies_for`` UNIONED with :data:`DEFAULT_CAPTURE_BODIES_FOR`.
        * ``override_default_capture_bodies_for=True``: user's list ONLY
          (may be empty — that is an explicit "metadata-only" posture).
    """
    if network_config.override_default_capture_bodies_for:
        return network_config.capture_bodies_for
    return network_config.capture_bodies_for | DEFAULT_CAPTURE_BODIES_FOR


def is_host_in_capture_bodies_for(
    host: str,
    privacy_config: "PrivacyConfig",
    network_config: "NetworkConfig",
) -> bool:
    """Return True iff body bytes for ``host`` should be retained (encrypted).

    **Fail-closed semantics:** body capture is OFF unless the host explicitly
    matches the effective allowlist. The blocklist always wins on overlap —
    a host covered by :func:`is_host_blocked` returns False here even if
    the user added it to ``capture_bodies_for`` (auth/banking/password-managers
    can never be body-captured).

    The effective allowlist is computed by :func:`effective_capture_bodies_for`.
    Suffix-match semantics mirror :func:`_matches_suffix`: bare or
    ``*.``-prefixed entries match the apex or any subdomain; never substring
    containment (the privacy-bypass pattern that suffix-match prevents).
    """
    if not host:
        return False
    if is_host_blocked(host, privacy_config, network_config):
        return False
    for entry in effective_capture_bodies_for(network_config):
        if _matches_suffix(host, entry):
            return True
    return False


def build_ignore_hosts_regex(
    privacy_config: "PrivacyConfig",
    network_config: "NetworkConfig",
) -> list[str]:
    """Build mitmproxy ``ignore_hosts`` regex list from blocklist sources.

    Combines ``privacy_config.mask_domains`` ∪
    ``network_config.extra_blocklist`` ∪ (:data:`DEFAULT_BLOCKLIST`
    unless overridden), produces a per-host suffix-anchored regex.
    The regex matches any port (not just 443) so non-standard HTTPS
    deployments are also bypassed.

    Returned strings are intended for ``re.compile(..., re.IGNORECASE)``.

    **No IP-literal anchors here.** mitmproxy's ``ignore_hosts`` matches
    against ``server.peername`` (the resolved IP after DNS) AND
    ``server.address`` (the original hostname). Including an IPv4 or
    IPv6 anchor in this list causes EVERY connection to be tunneled --
    every connection has a resolved peername, and a regex like
    ``^\\d+\\.\\d+\\.\\d+\\.\\d+:\\d+$`` matches that peername. The
    addon's :func:`is_host_blocked` first-check at ``request()`` time
    is the right place to gate IP-literal CONNECTs (it sees the original
    ``flow.request.host`` -- the literal IP only when the user typed
    one, not the resolved IP for a hostname).
    """
    entries: set[str] = set()
    entries.update(privacy_config.mask_domains)
    entries.update(network_config.extra_blocklist)
    if not network_config.override_default_blocklist:
        entries.update(DEFAULT_BLOCKLIST)

    patterns: list[str] = []
    for entry in sorted(entries):
        bare = entry.lower()
        if bare.startswith("*."):
            bare = bare[2:]
        if not bare:
            continue
        escaped = re.escape(bare)
        # ^(.+\.)?{escaped}:\d+$ matches the bare apex and any subdomain
        # at any port. \d+ keeps the regex flexible across non-443
        # deployments.
        patterns.append(rf"^(.+\.)?{escaped}:\d+$")

    return patterns
