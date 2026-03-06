"""Tests for capture-time privacy enforcement (RecorderPrivacyFilter)."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.policy import PrivacyConfig, PrivacyMode
from screencap.privacy.recorder_enforcement import RecorderPrivacyFilter


def _make_config(**kwargs) -> PrivacyConfig:
    """Build a PrivacyConfig with sensible test defaults."""
    defaults = dict(mode=PrivacyMode.PUBLIC)
    defaults.update(kwargs)
    return PrivacyConfig(**defaults)


class TestRecorderPrivacyFilter:
    def test_blocked_app_suppresses_capture(self):
        """An excluded app in the policy blocks screen capture."""
        config = _make_config(
            exclude_apps=frozenset({"com.1password.1password"}),
        )
        f = RecorderPrivacyFilter(config, transition_hold_seconds=0.0)

        f.on_window_event({
            "app_bundle_id": "com.1password.1password",
            "title": "1Password",
        })

        assert f.is_screen_allowed() is False

    def test_allowed_app_permits_capture(self):
        """A non-blocked app in public mode allows screen capture."""
        config = _make_config(
            exclude_apps=frozenset({"com.1password.1password"}),
        )
        f = RecorderPrivacyFilter(config, transition_hold_seconds=0.0)

        # VS Code is a code editor — OCR_FALLBACK in public, not blocked
        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py — project",
        })

        assert f.is_screen_allowed() is True

    def test_transition_hold_blocks_after_leaving_blocked_app(self):
        """After switching from a blocked to allowed app, capture is
        suppressed for the hold duration, then resumes."""
        hold = 1.0
        config = _make_config(
            exclude_apps=frozenset({"com.1password.1password"}),
        )
        f = RecorderPrivacyFilter(config, transition_hold_seconds=hold)

        # Enter blocked app
        f.on_window_event({
            "app_bundle_id": "com.1password.1password",
            "title": "1Password",
        })
        assert f.is_screen_allowed() is False

        # Switch to allowed app — hold should activate
        now = time.time()
        with patch("screencap.privacy.recorder_enforcement.time") as mock_time:
            # on_window_event uses time.time() internally
            mock_time.time.return_value = now
            f.on_window_event({
                "app_bundle_id": "com.microsoft.VSCode",
                "title": "main.py — project",
            })

            # During hold period: still blocked
            mock_time.time.return_value = now + 0.5
            assert f.is_screen_allowed(now + 0.5) is False

            # After hold period: allowed
            assert f.is_screen_allowed(now + hold + 0.1) is True

    def test_switch_to_blocked_app_blocks_immediately(self):
        """Switching from an allowed to a blocked app blocks capture
        with no delay."""
        config = _make_config(
            exclude_apps=frozenset({"com.1password.1password"}),
        )
        f = RecorderPrivacyFilter(config, transition_hold_seconds=1.0)

        # Start with allowed app
        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py — project",
        })
        assert f.is_screen_allowed() is True

        # Switch to blocked app — immediate block
        f.on_window_event({
            "app_bundle_id": "com.1password.1password",
            "title": "1Password",
        })
        assert f.is_screen_allowed() is False

    def test_title_pattern_blocks_capture(self):
        """A window matching a mask_title_patterns rule triggers block."""
        import re

        config = _make_config(
            mask_title_patterns=(re.compile(r"(?i)\binbox\b"),),
        )
        f = RecorderPrivacyFilter(config, transition_hold_seconds=0.0)

        # Unknown app with title matching "inbox" pattern.
        # In public mode, unknown context → MASK_WINDOW (blocked).
        # The title pattern also forces MASK_WINDOW.
        f.on_window_event({
            "app_bundle_id": "com.unknown.app",
            "title": "Inbox — Webmail",
        })

        assert f.is_screen_allowed() is False
