"""Targeted browser URL extraction via macOS Accessibility API.

Extracts the URL from the browser address bar using a depth-limited AX
tree walk.  Does NOT modify ``_AX_ATTRS`` — this is a targeted query
separate from the general window-state dump.

The extraction is browser-family-specific:
- **Safari**: AXToolbar → AXTextField (description contains "Address")
- **Chromium** (Chrome, Brave, Edge, Arc, …): AXToolbar → AXTextField
  with identifier containing "address" or "omnibox"
- **Firefox**: AXToolbar → AXComboBox / AXTextField for the URL bar

Returns ``None`` on any failure — the caller falls back to
``BROWSER_UNVERIFIED`` classification.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from loguru import logger

# Browser bundle-ID sets (mirrors BROWSER_BUNDLE_IDS in privacy/context.py).
_SAFARI_BUNDLES: frozenset[str] = frozenset({
    "com.apple.Safari",
    "com.apple.SafariTechnologyPreview",
})

_CHROMIUM_BUNDLES: frozenset[str] = frozenset({
    "com.google.Chrome",
    "com.google.Chrome.canary",
    "com.brave.Browser",
    "com.microsoft.edgemac",
    "company.thebrowser.Browser",  # Arc
    "com.operasoftware.Opera",
    "com.vivaldi.Vivaldi",
    "org.chromium.Chromium",
})

_FIREFOX_BUNDLES: frozenset[str] = frozenset({
    "org.mozilla.firefox",
    "org.mozilla.firefoxdeveloperedition",
    "org.waterfoxproject.waterfox",
})

_ALWAYS_PRIVATE_BUNDLES: frozenset[str] = frozenset({
    "org.torproject.torbrowser",
})

_OTHER_BROWSER_BUNDLES: frozenset[str] = frozenset({
    "com.nickvision.nicegx.nicegx",  # Orion
    "org.torproject.torbrowser",
})

ALL_KNOWN_BROWSER_BUNDLES: frozenset[str] = (
    _SAFARI_BUNDLES | _CHROMIUM_BUNDLES | _FIREFOX_BUNDLES | _OTHER_BROWSER_BUNDLES
)

# ---------------------------------------------------------------------------
# AX helpers (lazy-imported to avoid import failure on non-macOS)
# ---------------------------------------------------------------------------

_ApplicationServices: Any = None


def _ensure_ax() -> bool:
    """Lazy-import ApplicationServices.  Returns True if available."""
    global _ApplicationServices
    if _ApplicationServices is not None:
        return True
    if sys.platform != "darwin":
        return False
    try:
        import ApplicationServices as _AS
        _ApplicationServices = _AS
        return True
    except ImportError:
        return False


def _ax_get_attr(element: Any, attr_name: str) -> Any | None:
    """Read a single AX attribute.  Returns None on any failure."""
    err, value = _ApplicationServices.AXUIElementCopyAttributeValue(
        element, attr_name, None,
    )
    if err != 0 or value is None:
        return None
    return value


def _ax_get_children(element: Any) -> list:
    children = _ax_get_attr(element, "AXChildren")
    if children is None:
        return []
    return list(children)


def _ax_get_str(element: Any, attr_name: str) -> str:
    """Read an AX attribute as a string.  Returns '' on failure."""
    val = _ax_get_attr(element, attr_name)
    if val is None:
        return ""
    # Handle NSURL objects
    if hasattr(val, "absoluteString"):
        return str(val.absoluteString())
    return str(val)


# ---------------------------------------------------------------------------
# Incognito / Private Browsing detection
# ---------------------------------------------------------------------------


def is_incognito(bundle_id: str, window_title: str) -> bool:
    """Detect incognito/private-browsing windows from the window title.

    Limitation: relies on English-language title substrings. Non-English
    locales may not be detected (e.g. Chrome shows "(Inkognito)" in German).
    """
    if bundle_id in _CHROMIUM_BUNDLES:
        return "(Incognito)" in window_title or "(Private)" in window_title
    if bundle_id in _SAFARI_BUNDLES:
        return "Private Browsing" in window_title
    if bundle_id in _FIREFOX_BUNDLES:
        return "(Private Browsing)" in window_title
    return False


# ---------------------------------------------------------------------------
# Browser-specific URL extraction strategies
# ---------------------------------------------------------------------------

_MAX_TOOLBAR_DEPTH = 4


def _extract_safari(window: Any) -> str | None:
    """Safari: AXToolbar → AXTextField whose description contains 'address'."""
    for child in _ax_get_children(window):
        if _ax_get_str(child, "AXRole") == "AXToolbar":
            url = _search_for_url_field(child, depth=0)
            if url:
                return url
    return None


def _extract_chromium(window: Any) -> str | None:
    """Chromium: toolbar may be nested under an AXGroup."""
    for child in _ax_get_children(window):
        role = _ax_get_str(child, "AXRole")
        if role == "AXToolbar":
            url = _search_for_url_field(child, depth=0)
            if url:
                return url
        # Chrome sometimes wraps the toolbar in an AXGroup
        if role == "AXGroup":
            for gc in _ax_get_children(child):
                if _ax_get_str(gc, "AXRole") == "AXToolbar":
                    url = _search_for_url_field(gc, depth=0)
                    if url:
                        return url
    return None


def _extract_firefox(window: Any) -> str | None:
    """Firefox: toolbar → AXComboBox / AXTextField for the URL bar."""
    for child in _ax_get_children(window):
        role = _ax_get_str(child, "AXRole")
        if role == "AXToolbar":
            url = _search_for_url_field(child, depth=0)
            if url:
                return url
    # Fallback: walk all top-level children in case toolbar role differs
    for child in _ax_get_children(window):
        url = _search_for_url_field(child, depth=0)
        if url:
            return url
    return None


def _search_for_url_field(element: Any, depth: int) -> str | None:
    """Recursively search an element subtree for the URL text field."""
    if depth > _MAX_TOOLBAR_DEPTH:
        return None

    role = _ax_get_str(element, "AXRole")
    identifier = _ax_get_str(element, "AXIdentifier").lower()
    description = _ax_get_str(element, "AXDescription").lower()

    # Chromium: AXTextField with identifier like "addressAndUrl" or "omnibox"
    if role == "AXTextField" and _matches_url_keywords(identifier):
        return _ax_get_str(element, "AXValue") or None

    # Safari: AXTextField with description "Address and Search"
    if role == "AXTextField" and _matches_url_keywords(description):
        return _ax_get_str(element, "AXValue") or None

    # Chromium/Firefox: AXComboBox with URL-related identifier
    if role == "AXComboBox" and _matches_url_keywords(identifier):
        return _ax_get_str(element, "AXValue") or None

    # Recurse into children
    for child in _ax_get_children(element):
        url = _search_for_url_field(child, depth + 1)
        if url:
            return url

    return None


def _matches_url_keywords(text: str) -> bool:
    """Check if text contains URL-bar-related keywords."""
    return any(kw in text for kw in ("address", "omnibox", "url", "location"))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

# (pid, window_id) -> url_or_none
_url_cache: dict[tuple[int, str], str | None] = {}


def invalidate_url_cache(
    pid: int | None = None, window_id: str | None = None
) -> None:
    """Evict a cache entry, or clear the entire cache if no args given."""
    if pid is not None and window_id is not None:
        _url_cache.pop((pid, window_id), None)
    else:
        _url_cache.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_browser_url(
    pid: int,
    bundle_id: str,
    window_id: str = "",
    window_title: str = "",
) -> str | None:
    """Extract the current URL from a browser window's address bar.

    Uses a targeted AX tree walk (max 4 levels deep) on the browser's
    toolbar subtree.  Results are cached by ``(pid, window_id)`` —
    call :func:`invalidate_url_cache` when the window changes.

    Returns ``None`` when:
    - AX API is unavailable or fails
    - The browser is in incognito / private-browsing mode
    - The address bar element cannot be found
    - Extraction exceeds the timeout

    Parameters
    ----------
    pid:
        Process ID of the browser.
    bundle_id:
        Bundle identifier (e.g. ``com.google.Chrome``).
    window_id:
        macOS CGWindowNumber as a string.
    window_title:
        Window title — used for incognito detection.
    """
    # Always-private browsers (Tor) → never extract
    if bundle_id in _ALWAYS_PRIVATE_BUNDLES:
        return None

    # Incognito → always None
    if is_incognito(bundle_id, window_title):
        return None

    # Cache check
    cache_key = (pid, window_id)
    if cache_key in _url_cache:
        return _url_cache[cache_key]

    # AX API required
    if not _ensure_ax():
        return None

    url: str | None = None
    try:
        url = _extract_url(pid, bundle_id)
    except Exception:
        logger.debug(f"AX URL extraction failed for pid={pid} bundle={bundle_id}")
        url = None

    _url_cache[cache_key] = url
    return url


def _extract_url(pid: int, bundle_id: str) -> str | None:
    """Core extraction: get the focused window and apply browser strategy."""
    from screencap.engine.config import config

    app_ref = _ApplicationServices.AXUIElementCreateApplication(pid)
    _ApplicationServices.AXUIElementSetMessagingTimeout(app_ref, config.AX_ELEMENT_TIMEOUT)

    err, window = _ApplicationServices.AXUIElementCopyAttributeValue(
        app_ref, "AXFocusedWindow", None,
    )
    if err != 0 or window is None:
        return None

    # Strategy 1: AXDocument on the window (Safari sometimes exposes this)
    doc = _ax_get_str(window, "AXDocument")
    if doc and doc.startswith(("http://", "https://")):
        return doc

    # Strategy 2: browser-specific toolbar walk
    if bundle_id in _SAFARI_BUNDLES:
        return _extract_safari(window)
    if bundle_id in _CHROMIUM_BUNDLES:
        return _extract_chromium(window)
    if bundle_id in _FIREFOX_BUNDLES:
        return _extract_firefox(window)

    # Unknown browser — try generic toolbar walk
    return _extract_chromium(window)
