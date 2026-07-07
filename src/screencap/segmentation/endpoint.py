"""Bring-your-own endpoint classification — the privacy boundary (U7, KTD8).

A user-configured model server is treated as **on-device** (day-split allowed,
nothing leaves the Mac) only when its URL is a **loopback literal**; everything
else is a **cloud** provider (per-task consent-gated, day-split never). Because a
LOCAL endpoint joins the on-device chain with no consent prompt, this classifier
is the single control separating "trusted local" from "cloud" — so it classifies
the *resolved connection*, not a URL string that a name or an encoding trick could
disguise (KTD8):

- only ``localhost`` (the exact hostname) and loopback **IP literals**
  (``127.0.0.0/8``, ``::1``) are LOCAL — a DNS name that merely *resolves* to
  loopback is REMOTE (resolution is TOCTOU);
- decimal/octal/hex integer forms, IPv4-mapped IPv6, ``0.0.0.0``, userinfo tricks
  (``http://127.0.0.1@evil.com/``) and non-``http(s)`` schemes are all REMOTE;
- anything unparseable is REMOTE (fail-safe).

The connect-time half (redirects disabled, ``localhost`` pinned to ``127.0.0.1``)
lives in the provider (:mod:`screencap.segmentation.providers.local_server`).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

LOCAL = "LOCAL"
REMOTE = "REMOTE"


def classify_endpoint(url: str | None) -> str:
    """Return :data:`LOCAL` for a loopback-literal endpoint, else :data:`REMOTE`.

    Fail-safe: any parse failure, non-``http(s)`` scheme, missing host, DNS name,
    or non-bare loopback form classifies REMOTE. See the module docstring for the
    full rule and rationale (KTD8).
    """
    if not url or not isinstance(url, str):
        return REMOTE
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return REMOTE

    if parsed.scheme not in ("http", "https"):
        return REMOTE

    try:
        host = parsed.hostname  # userinfo excluded; IPv6 brackets stripped; lowercased
    except ValueError:
        return REMOTE
    if not host:
        return REMOTE

    if host == "localhost":
        return LOCAL

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A DNS name — REMOTE even if it might resolve to loopback (TOCTOU).
        return REMOTE

    # Reject IPv4-mapped IPv6 (``::ffff:127.0.0.1``) and other non-bare forms
    # conservatively — only bare loopback literals are LOCAL.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return REMOTE

    return LOCAL if ip.is_loopback else REMOTE
