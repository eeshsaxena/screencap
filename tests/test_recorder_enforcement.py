"""Tests for capture-time privacy enforcement (RecorderPrivacyFilter)."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.policy import PrivacyConfig, PrivacyMode
from screencap.privacy.recorder_enforcement import (
    KEYSTROKE_CONTENT_FIELDS,
    RecorderPrivacyFilter,
)


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
        f = RecorderPrivacyFilter(config, transition_hold_seconds=0.0, secure_input_fn=None)

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
        f = RecorderPrivacyFilter(config, transition_hold_seconds=0.0, secure_input_fn=None)

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
        f = RecorderPrivacyFilter(config, transition_hold_seconds=hold, secure_input_fn=None)

        # Enter blocked app
        f.on_window_event({
            "app_bundle_id": "com.1password.1password",
            "title": "1Password",
        })
        assert f.is_screen_allowed() is False

        # Switch to allowed app — hold should activate
        now = time.monotonic()
        with patch("screencap.privacy.recorder_enforcement.time") as mock_time:
            mock_time.monotonic.return_value = now
            f.on_window_event({
                "app_bundle_id": "com.microsoft.VSCode",
                "title": "main.py — project",
            })

            # During hold period: still blocked
            mock_time.monotonic.return_value = now + 0.5
            assert f.is_screen_allowed() is False

            # After hold period: allowed
            mock_time.monotonic.return_value = now + hold + 0.1
            assert f.is_screen_allowed() is True

    def test_transition_hold_works_with_unix_event_timestamps(self):
        """Regression: is_screen_allowed() must use monotonic clock
        internally even when callers pass Unix event timestamps (~1.7e9).
        Previously the hold deadline (monotonic-based) was compared against
        the caller's Unix timestamp, making the hold always expire."""
        hold = 1.0
        config = _make_config(
            exclude_apps=frozenset({"com.1password.1password"}),
        )
        f = RecorderPrivacyFilter(config, transition_hold_seconds=hold, secure_input_fn=None)

        # Enter blocked app
        f.on_window_event({
            "app_bundle_id": "com.1password.1password",
            "title": "1Password",
        })

        mono_now = 500.0  # monotonic ~500s since boot
        unix_now = 1.7e9  # Unix timestamp

        with patch("screencap.privacy.recorder_enforcement.time") as mock_time:
            mock_time.monotonic.return_value = mono_now
            f.on_window_event({
                "app_bundle_id": "com.microsoft.VSCode",
                "title": "main.py — project",
            })

            # During hold: blocked regardless of what timestamp is passed
            mock_time.monotonic.return_value = mono_now + 0.5
            assert f.is_screen_allowed(unix_now) is False

            # After hold: allowed
            mock_time.monotonic.return_value = mono_now + hold + 0.1
            assert f.is_screen_allowed(unix_now) is True

    def test_switch_to_blocked_app_blocks_immediately(self):
        """Switching from an allowed to a blocked app blocks capture
        with no delay."""
        config = _make_config(
            exclude_apps=frozenset({"com.1password.1password"}),
        )
        f = RecorderPrivacyFilter(config, transition_hold_seconds=1.0, secure_input_fn=None)

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
        f = RecorderPrivacyFilter(config, transition_hold_seconds=0.0, secure_input_fn=None)

        f.on_window_event({
            "app_bundle_id": "com.unknown.app",
            "title": "Inbox — Webmail",
        })

        assert f.is_screen_allowed() is False


class TestSecureInputDetection:
    """Tests for Layer 0: CGSIsSecureEventInputSet detection."""

    def test_secure_input_active_blocks_capture(self):
        """When secure input fn returns True, capture is blocked."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=lambda: True
        )

        assert f.is_screen_allowed() is False

    def test_secure_input_none_allows_capture(self):
        """When secure input fn is None (unavailable), capture is allowed."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None
        )

        assert f.is_screen_allowed() is True

    def test_secure_input_hold_after_deactivation(self):
        """After secure input deactivates, hold period applies."""
        hold = 1.0
        secure_active = [True]  # Mutable so we can toggle
        config = _make_config()

        f = RecorderPrivacyFilter(
            config,
            transition_hold_seconds=hold,
            secure_input_fn=lambda: secure_active[0],
        )

        now = time.monotonic()
        with patch("screencap.privacy.recorder_enforcement.time") as mock_time:
            # Secure input active — blocked
            mock_time.monotonic.return_value = now
            assert f.is_screen_allowed() is False

            # Deactivate secure input
            secure_active[0] = False

            # During hold: still blocked (hold timer set from previous call)
            mock_time.monotonic.return_value = now + 0.5
            assert f.is_screen_allowed() is False

            # After hold: allowed
            mock_time.monotonic.return_value = now + hold + 0.1
            assert f.is_screen_allowed() is True

    def test_secure_input_exception_degrades_gracefully(self):
        """If secure_input_fn raises, capture is not affected."""
        config = _make_config()

        def boom():
            raise RuntimeError("ctypes segfault")

        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=boom
        )

        # Should not raise, should allow capture
        assert f.is_screen_allowed() is True


class TestAXSecureTextField:
    """Tests for Layer 1: AXSecureTextField detection."""

    @pytest.mark.parametrize("ax_key", ["AXRole", "AXSubrole"])
    def test_secure_text_field_blocks(self, ax_key):
        """AXSecureTextField in AXRole or AXSubrole triggers blocking.

        AXSubrole variant is used by Chrome/Electron.
        """
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=1.0, secure_input_fn=None
        )

        now = time.monotonic()
        with patch("screencap.privacy.recorder_enforcement.time") as mock_time:
            mock_time.monotonic.return_value = now
            f.on_action_event({
                "element_state": {ax_key: "AXSecureTextField"},
            })
            assert f.is_screen_allowed() is False

    def test_normal_text_field_allows(self):
        """A regular AXTextField does not trigger blocking."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None
        )

        f.on_action_event({
            "element_state": {"AXRole": "AXTextField"},
        })

        assert f.is_screen_allowed() is True

    def test_secure_field_hold_timer(self):
        """Secure field blocking uses hold timer after deactivation."""
        hold = 1.0
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=hold, secure_input_fn=None
        )

        now = time.monotonic()
        with patch("screencap.privacy.recorder_enforcement.time") as mock_time:
            mock_time.monotonic.return_value = now
            f.on_action_event({
                "element_state": {"AXRole": "AXSecureTextField"},
            })

            # Immediately after: blocked
            assert f.is_screen_allowed() is False

            # During hold period: still blocked
            mock_time.monotonic.return_value = now + 0.5
            assert f.is_screen_allowed() is False

            # After hold period: allowed
            mock_time.monotonic.return_value = now + hold + 0.1
            assert f.is_screen_allowed() is True


class TestMultiReasonComposition:
    """Tests for independent blocking sources composing via OR logic."""

    def test_secure_input_blocks_even_when_app_allowed(self):
        """Secure input blocks even for a normally-allowed app."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=lambda: True
        )

        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py — project",
        })

        assert f.is_screen_allowed() is False

    def test_one_reason_clears_other_still_blocks(self):
        """When one reason clears but another is still active, stays blocked."""
        secure_active = [True]
        config = _make_config(
            exclude_apps=frozenset({"com.1password.1password"}),
        )
        f = RecorderPrivacyFilter(
            config,
            transition_hold_seconds=0.0,
            secure_input_fn=lambda: secure_active[0],
        )

        # Both reasons active
        f.on_window_event({
            "app_bundle_id": "com.1password.1password",
            "title": "1Password",
        })
        assert f.is_screen_allowed() is False

        # Clear secure input, but app policy still blocks
        secure_active[0] = False
        assert f.is_screen_allowed() is False

    def test_all_reasons_clear_allows(self):
        """When all blocking reasons clear, capture is allowed."""
        secure_active = [True]
        config = _make_config()
        f = RecorderPrivacyFilter(
            config,
            transition_hold_seconds=0.0,
            secure_input_fn=lambda: secure_active[0],
        )

        # Secure input blocks
        assert f.is_screen_allowed() is False

        # Clear secure input
        secure_active[0] = False
        assert f.is_screen_allowed() is True


class TestKeystrokeBlocking:
    """Tests for capture-time keystroke content nulling."""

    def test_null_keystroke_content_nulls_all_fields_and_preserves_metadata(self):
        """null_keystroke_content nulls all key content fields while
        preserving structural metadata (name, timestamp)."""
        data = {
            "name": "key.down",
            "key_char": "p",
            "key_name": "p",
            "key_vk": 35,
            "canonical_key_char": "p",
            "canonical_key_name": "p",
            "canonical_key_vk": 35,
            "text": "p",
            "element_state": {"AXRole": "AXTextField"},
            "active_segment_description": "seg",
            "available_segment_descriptions": ["seg1"],
            "timestamp": 1234567890.0,
        }

        RecorderPrivacyFilter.null_keystroke_content(data)

        # Content fields nulled
        for field in KEYSTROKE_CONTENT_FIELDS:
            assert data[field] is None, f"{field} should be None"

        # Structural metadata preserved
        assert data["name"] == "key.down"
        assert data["timestamp"] == 1234567890.0

    def test_blocked_filter_gates_keystroke_nulling(self):
        """Integration: when filter is blocked and caller follows the
        is_screen_allowed() → null_keystroke_content() protocol,
        key content is nulled; when allowed, it's preserved.

        This mirrors the process_events() integration in sc_engine:
            if not screen_filter.is_screen_allowed():
                screen_filter.null_keystroke_content(event.data)
        """
        config = _make_config(
            exclude_apps=frozenset({"com.1password.1password"}),
        )
        f = RecorderPrivacyFilter(config, transition_hold_seconds=0.0, secure_input_fn=None)

        def make_key_event():
            return {
                "name": "key.down",
                "key_char": "p",
                "key_name": "p",
                "key_vk": 35,
                "canonical_key_char": "p",
                "canonical_key_name": "p",
                "canonical_key_vk": 35,
                "text": "p",
                "element_state": {"AXRole": "AXTextField"},
                "active_segment_description": "seg",
                "available_segment_descriptions": ["seg1"],
            }

        # Blocked app → keystrokes nulled
        f.on_window_event({
            "app_bundle_id": "com.1password.1password",
            "title": "1Password",
        })
        blocked_data = make_key_event()
        if not f.is_screen_allowed():
            f.null_keystroke_content(blocked_data)

        for field in KEYSTROKE_CONTENT_FIELDS:
            assert blocked_data[field] is None

        # Allowed app → keystrokes preserved
        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py",
        })
        # Wait for hold to expire (hold=0.0 so immediate)
        allowed_data = make_key_event()
        if not f.is_screen_allowed():
            f.null_keystroke_content(allowed_data)

        assert allowed_data["key_char"] == "p"
        assert allowed_data["key_vk"] == 35

    def test_null_keystroke_content_nulls_text_on_composite_event(self):
        """key.type composite events carry a 'text' field but no key_char/
        key_name. null_keystroke_content must null 'text' and be harmless
        for absent key fields — this is what makes the broader capture-time
        nulling (all action types, not just key.down/up) safe."""
        data = {
            "name": "key.type",
            "text": "hunter2",
            "timestamp": 1234567890.0,
        }

        RecorderPrivacyFilter.null_keystroke_content(data)

        assert data["text"] is None
        assert data["name"] == "key.type"
        assert data["timestamp"] == 1234567890.0
