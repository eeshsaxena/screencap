"""Tests for sc_engine.window.ax_browser_url — browser URL extraction."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sc_engine.window.ax_browser_url import (
    _CHROMIUM_BUNDLES,
    _FIREFOX_BUNDLES,
    _SAFARI_BUNDLES,
    extract_browser_url,
    invalidate_url_cache,
    is_incognito,
)


# ---------------------------------------------------------------------------
# Helpers to build mock AX elements
# ---------------------------------------------------------------------------


def _make_element(
    role: str = "",
    identifier: str = "",
    description: str = "",
    value: str = "",
    children: list | None = None,
) -> MagicMock:
    """Create a mock AX element with the given attributes."""

    def _copy_attr(element, attr_name, _):
        attrs = {
            "AXRole": role,
            "AXIdentifier": identifier,
            "AXDescription": description,
            "AXValue": value,
            "AXChildren": children or [],
            "AXDocument": None,
            "AXFocusedWindow": None,
        }
        val = attrs.get(attr_name)
        if val is None:
            return (-25212, None)  # kAXErrorAttributeUnsupported
        return (0, val)

    el = MagicMock()
    el.__ax_attrs__ = {
        "AXRole": role,
        "AXIdentifier": identifier,
        "AXDescription": description,
        "AXValue": value,
    }
    return el


def _mock_ax_get(element_map: dict):
    """Return a side_effect function for AXUIElementCopyAttributeValue.

    element_map: {element_id: {attr_name: value}}
    """

    def _copy_attr(element, attr_name, _none):
        attrs = element.__ax_attrs__
        if attr_name == "AXChildren":
            children = getattr(element, "_children", [])
            return (0, children) if children else (-25212, None)
        val = attrs.get(attr_name)
        if val is None:
            return (-25212, None)
        return (0, val)

    return _copy_attr


def _build_browser_tree(
    url_value: str = "https://github.com",
    url_identifier: str = "WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD",
    url_description: str = "smart search field",
) -> MagicMock:
    """Build a mock Safari-like AX tree: Window → Toolbar → Group → TextField."""
    # URL text field
    url_field = MagicMock()
    url_field.__ax_attrs__ = {
        "AXRole": "AXTextField",
        "AXIdentifier": url_identifier,
        "AXDescription": url_description,
        "AXValue": url_value,
    }
    url_field._children = []

    # Toolbar group containing the URL field
    toolbar_group = MagicMock()
    toolbar_group.__ax_attrs__ = {
        "AXRole": "AXGroup",
        "AXIdentifier": "",
        "AXDescription": "",
        "AXValue": "",
    }
    toolbar_group._children = [url_field]

    # Toolbar
    toolbar = MagicMock()
    toolbar.__ax_attrs__ = {
        "AXRole": "AXToolbar",
        "AXIdentifier": "",
        "AXDescription": "",
        "AXValue": "",
    }
    toolbar._children = [toolbar_group]

    # Window
    window = MagicMock()
    window.__ax_attrs__ = {
        "AXRole": "AXWindow",
        "AXIdentifier": "",
        "AXDescription": "",
        "AXValue": "",
        "AXTitle": "GitHub",
        "AXDocument": None,
    }
    window._children = [toolbar]

    return window


def _patch_ax(window: MagicMock):
    """Create patches for ApplicationServices functions.

    Returns a dict of patch context managers.
    """

    def _copy_attr(element, attr_name, _none):
        # Check for special window-level attributes
        if attr_name == "AXFocusedWindow":
            return (0, window)
        if attr_name == "AXChildren":
            children = getattr(element, "_children", [])
            return (0, children) if children else (0, [])
        attrs = getattr(element, "__ax_attrs__", {})
        val = attrs.get(attr_name)
        if val is None:
            return (-25212, None)
        return (0, val)

    mock_as = MagicMock()
    mock_as.AXUIElementCreateApplication.return_value = MagicMock()
    mock_as.AXUIElementSetMessagingTimeout.return_value = None
    mock_as.AXUIElementCopyAttributeValue.side_effect = _copy_attr

    return mock_as


# ---------------------------------------------------------------------------
# Incognito detection
# ---------------------------------------------------------------------------


class TestIsIncognito:
    def test_chrome_incognito(self):
        assert is_incognito("com.google.Chrome", "GitHub (Incognito)") is True

    def test_chrome_normal(self):
        assert is_incognito("com.google.Chrome", "GitHub") is False

    def test_safari_private(self):
        assert is_incognito("com.apple.Safari", "GitHub — Private Browsing") is True

    def test_safari_normal(self):
        assert is_incognito("com.apple.Safari", "GitHub") is False

    def test_firefox_private(self):
        assert is_incognito("org.mozilla.firefox", "GitHub (Private Browsing)") is True

    def test_firefox_normal(self):
        assert is_incognito("org.mozilla.firefox", "GitHub") is False

    def test_brave_incognito(self):
        assert is_incognito("com.brave.Browser", "GitHub (Incognito)") is True

    def test_edge_private(self):
        assert is_incognito("com.microsoft.edgemac", "GitHub (Private)") is True

    def test_unknown_browser(self):
        assert is_incognito("com.example.browser", "GitHub (Incognito)") is False


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------


class TestExtractBrowserUrl:
    """Test extract_browser_url with mocked AX API."""

    def setup_method(self):
        """Clear cache before each test."""
        invalidate_url_cache()

    def test_incognito_returns_none(self):
        """Incognito windows always return None, no AX query."""
        result = extract_browser_url(
            pid=123,
            bundle_id="com.google.Chrome",
            window_id="42",
            window_title="GitHub (Incognito)",
        )
        assert result is None

    def test_safari_url_extraction(self):
        """Safari: AXToolbar → AXGroup → AXTextField[WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD]."""
        window = _build_browser_tree(
            url_value="https://github.com",
            url_identifier="WEB_BROWSER_ADDRESS_AND_SEARCH_FIELD",
            url_description="smart search field",
        )
        mock_as = _patch_ax(window)

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            result = extract_browser_url(
                pid=100,
                bundle_id="com.apple.Safari",
                window_id="1",
                window_title="GitHub",
            )

        assert result == "https://github.com"

    def test_chromium_url_extraction(self):
        """Chromium: AXToolbar → AXGroup → AXTextField with 'address' identifier."""
        window = _build_browser_tree(
            url_value="https://docs.python.org",
            url_identifier="addressAndUrl",
            url_description="",
        )
        mock_as = _patch_ax(window)

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            result = extract_browser_url(
                pid=200,
                bundle_id="com.google.Chrome",
                window_id="2",
                window_title="Python Docs",
            )

        assert result == "https://docs.python.org"

    def test_firefox_url_extraction(self):
        """Firefox: toolbar → AXTextField with URL keywords."""
        window = _build_browser_tree(
            url_value="https://mozilla.org",
            url_identifier="urlbar",
            url_description="",
        )
        mock_as = _patch_ax(window)

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            result = extract_browser_url(
                pid=300,
                bundle_id="org.mozilla.firefox",
                window_id="3",
                window_title="Mozilla",
            )

        assert result == "https://mozilla.org"

    def test_empty_url_returns_none(self):
        """Start Page / new tab with empty URL field returns None."""
        window = _build_browser_tree(url_value="")
        mock_as = _patch_ax(window)

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            result = extract_browser_url(
                pid=400,
                bundle_id="com.apple.Safari",
                window_id="4",
                window_title="Start Page",
            )

        assert result is None

    def test_cache_hit(self):
        """Second call with same (pid, window_id) returns cached value."""
        window = _build_browser_tree(url_value="https://github.com")
        mock_as = _patch_ax(window)

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            r1 = extract_browser_url(
                pid=500, bundle_id="com.apple.Safari",
                window_id="5", window_title="GitHub",
            )
            # Mutate the mock to return a different URL — should not be seen
            # Path: window → toolbar → toolbar_group → url_field
            window._children[0]._children[0]._children[0].__ax_attrs__["AXValue"] = "https://other.com"
            r2 = extract_browser_url(
                pid=500, bundle_id="com.apple.Safari",
                window_id="5", window_title="GitHub",
            )

        assert r1 == "https://github.com"
        assert r2 == "https://github.com"  # cached

    def test_cache_eviction(self):
        """After invalidation, a fresh AX query is made."""
        window = _build_browser_tree(url_value="https://github.com")
        mock_as = _patch_ax(window)

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            r1 = extract_browser_url(
                pid=600, bundle_id="com.apple.Safari",
                window_id="6", window_title="GitHub",
            )
            assert r1 == "https://github.com"

            # Simulate URL change (path: window → toolbar → group → url_field)
            window._children[0]._children[0]._children[0].__ax_attrs__["AXValue"] = "https://docs.python.org"
            invalidate_url_cache(pid=600, window_id="6")

            r2 = extract_browser_url(
                pid=600, bundle_id="com.apple.Safari",
                window_id="6", window_title="Python Docs",
            )

        assert r2 == "https://docs.python.org"

    def test_cache_clear_all(self):
        """invalidate_url_cache() with no args clears entire cache."""
        window = _build_browser_tree(url_value="https://github.com")
        mock_as = _patch_ax(window)

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            extract_browser_url(
                pid=700, bundle_id="com.apple.Safari",
                window_id="7", window_title="GitHub",
            )
            invalidate_url_cache()

            # Change URL and re-query — should get fresh value
            window._children[0]._children[0]._children[0].__ax_attrs__["AXValue"] = "https://new.com"
            r = extract_browser_url(
                pid=700, bundle_id="com.apple.Safari",
                window_id="7", window_title="GitHub",
            )

        assert r == "https://new.com"

    def test_ax_failure_returns_none(self):
        """AX API failure returns None instead of raising."""
        mock_as = MagicMock()
        mock_as.AXUIElementCreateApplication.return_value = MagicMock()
        mock_as.AXUIElementSetMessagingTimeout.return_value = None
        # Simulate AXFocusedWindow failure
        mock_as.AXUIElementCopyAttributeValue.return_value = (-25204, None)

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            result = extract_browser_url(
                pid=800,
                bundle_id="com.google.Chrome",
                window_id="8",
                window_title="Chrome",
            )

        assert result is None

    def test_exception_returns_none(self):
        """Unexpected exception during extraction returns None."""
        mock_as = MagicMock()
        mock_as.AXUIElementCreateApplication.side_effect = RuntimeError("boom")

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            result = extract_browser_url(
                pid=900,
                bundle_id="com.google.Chrome",
                window_id="9",
                window_title="Chrome",
            )

        assert result is None

    def test_ax_document_strategy(self):
        """AXDocument on window takes priority (Safari sometimes exposes this)."""
        window = MagicMock()
        window.__ax_attrs__ = {
            "AXRole": "AXWindow",
            "AXIdentifier": "",
            "AXDescription": "",
            "AXValue": "",
            "AXDocument": "https://apple.com/safari",
            "AXTitle": "Apple",
        }
        window._children = []

        mock_as = MagicMock()
        mock_as.AXUIElementCreateApplication.return_value = MagicMock()
        mock_as.AXUIElementSetMessagingTimeout.return_value = None

        def _copy_attr(element, attr_name, _none):
            if attr_name == "AXFocusedWindow":
                return (0, window)
            if attr_name == "AXChildren":
                children = getattr(element, "_children", [])
                return (0, children)
            attrs = getattr(element, "__ax_attrs__", {})
            val = attrs.get(attr_name)
            if val is None:
                return (-25212, None)
            return (0, val)

        mock_as.AXUIElementCopyAttributeValue.side_effect = _copy_attr

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            result = extract_browser_url(
                pid=1000,
                bundle_id="com.apple.Safari",
                window_id="10",
                window_title="Apple",
            )

        assert result == "https://apple.com/safari"

    def test_non_http_ax_document_ignored(self):
        """AXDocument with file:// URL is not treated as browser URL."""
        window = _build_browser_tree(url_value="https://github.com")
        # Override AXDocument to be a file URL
        window.__ax_attrs__["AXDocument"] = "file:///tmp/test.html"
        mock_as = _patch_ax(window)

        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": mock_as},
        ):
            result = extract_browser_url(
                pid=1100,
                bundle_id="com.apple.Safari",
                window_id="11",
                window_title="Test",
            )

        # Should fall through to toolbar strategy since file:// is not http(s)
        assert result == "https://github.com"

    def test_non_macos_returns_none(self):
        """On non-macOS platforms, always returns None."""
        with patch.dict(
            "sc_engine.window.ax_browser_url.__dict__",
            {"_ApplicationServices": None},
        ), patch("sc_engine.window.ax_browser_url._ensure_ax", return_value=False):
            result = extract_browser_url(
                pid=1200,
                bundle_id="com.google.Chrome",
                window_id="12",
                window_title="Chrome",
            )

        assert result is None


# ---------------------------------------------------------------------------
# Bundle ID sets
# ---------------------------------------------------------------------------


class TestBundleSets:
    def test_safari_bundles(self):
        assert "com.apple.Safari" in _SAFARI_BUNDLES

    def test_chromium_bundles(self):
        assert "com.google.Chrome" in _CHROMIUM_BUNDLES
        assert "com.brave.Browser" in _CHROMIUM_BUNDLES
        assert "company.thebrowser.Browser" in _CHROMIUM_BUNDLES

    def test_firefox_bundles(self):
        assert "org.mozilla.firefox" in _FIREFOX_BUNDLES
