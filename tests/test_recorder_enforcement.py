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


_ALLOWED_EVENT = {"app_bundle_id": "com.microsoft.VSCode", "title": "main.py"}


class TestRecorderPrivacyFilter:
    def test_starts_blocked_before_first_window_event(self):
        """Filter starts fail-closed until first window event arrives."""
        config = _make_config()
        f = RecorderPrivacyFilter(config, transition_hold_seconds=0.0, secure_input_fn=None)
        assert f.is_screen_allowed() is False

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


class TestFailClosed:
    """Tests for fail_closed() — blocks all capture until next successful window event."""

    def test_fail_closed_blocks_capture(self):
        """After fail_closed(), capture is blocked."""
        config = _make_config()
        f = RecorderPrivacyFilter(config, transition_hold_seconds=0.0, secure_input_fn=None)

        f.on_window_event(_ALLOWED_EVENT)
        assert f.is_screen_allowed() is True

        f.fail_closed()
        assert f.is_screen_allowed() is False

    def test_fail_closed_clears_on_next_window_event(self):
        """A successful on_window_event() after fail_closed() restores capture."""
        config = _make_config()
        f = RecorderPrivacyFilter(config, transition_hold_seconds=0.0, secure_input_fn=None)

        f.fail_closed()
        assert f.is_screen_allowed() is False

        # A new window event clears the fail-closed state
        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py — project",
        })
        assert f.is_screen_allowed() is True

    def test_fail_closed_persists_across_hold_expiry(self):
        """fail_closed() sets hold to infinity — doesn't expire with time."""
        config = _make_config()
        f = RecorderPrivacyFilter(config, transition_hold_seconds=1.0, secure_input_fn=None)

        now = time.monotonic()
        with patch("screencap.privacy.recorder_enforcement.time") as mock_time:
            mock_time.monotonic.return_value = now
            f.fail_closed()
            assert f.is_screen_allowed() is False

            # Even far in the future, still blocked (infinity hold)
            mock_time.monotonic.return_value = now + 9999
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

        f.on_window_event(_ALLOWED_EVENT)
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

        f.on_window_event(_ALLOWED_EVENT)

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

        f.on_window_event(_ALLOWED_EVENT)
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

        f.on_window_event(_ALLOWED_EVENT)
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

        f.on_window_event(_ALLOWED_EVENT)

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

        f.on_window_event(_ALLOWED_EVENT)

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


class TestCloudIntentBlocking:
    """Tests for Phase 2: OCR_FALLBACK apps blocked for cloud-intent."""

    def test_cloud_intent_blocks_ocr_fallback_apps(self):
        """OCR_FALLBACK apps (code editors) are blocked for cloud-intent."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
            cloud_intent=True,
        )

        # VSCode gets OCR_FALLBACK in public mode (code editor)
        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py — project",
        })

        assert f.is_screen_allowed() is False

    def test_non_cloud_allows_ocr_fallback_apps(self):
        """OCR_FALLBACK apps pass through for non-cloud recordings."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
            cloud_intent=False,
        )

        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py — project",
        })

        assert f.is_screen_allowed() is True

    def test_cloud_intent_attribute_exposed(self):
        """cloud_intent attribute is accessible (used by process_events)."""
        config = _make_config()
        f_cloud = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
            cloud_intent=True,
        )
        f_local = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
            cloud_intent=False,
        )
        assert f_cloud.cloud_intent is True
        assert f_local.cloud_intent is False

    def test_cloud_intent_still_blocks_excluded_apps(self):
        """EXCLUDE apps are still blocked in cloud-intent (superset of normal)."""
        config = _make_config(
            exclude_apps=frozenset({"com.1password.1password"}),
        )
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
            cloud_intent=True,
        )

        f.on_window_event({
            "app_bundle_id": "com.1password.1password",
            "title": "1Password",
        })

        assert f.is_screen_allowed() is False


class TestBlockedIntervalTracking:
    """Tests for Phase 6: blocked interval recording."""

    def test_record_and_retrieve_interval(self):
        """Blocked intervals are recorded and retrievable."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )

        f.record_block_start(100.0)
        f.record_block_end(110.0, reason="app_policy")

        intervals = f.get_blocked_intervals(90.0, 120.0)
        assert len(intervals) == 1
        assert intervals[0]["start_ts"] == 100.0
        assert intervals[0]["end_ts"] == 110.0
        assert intervals[0]["reason"] == "app_policy"

    def test_intervals_clipped_to_range(self):
        """Intervals are clipped to the requested time range."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )

        f.record_block_start(95.0)
        f.record_block_end(115.0, reason="app_policy")

        intervals = f.get_blocked_intervals(100.0, 110.0)
        assert len(intervals) == 1
        assert intervals[0]["start_ts"] == 100.0
        assert intervals[0]["end_ts"] == 110.0

    def test_ongoing_block_included(self):
        """An ongoing (unclosed) blocked interval is included in results."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )

        f.record_block_start(105.0)
        # No record_block_end — still blocked

        intervals = f.get_blocked_intervals(100.0, 120.0)
        assert len(intervals) == 1
        assert intervals[0]["start_ts"] == 105.0
        assert intervals[0]["end_ts"] == 120.0

    def test_non_overlapping_interval_excluded(self):
        """Intervals outside the requested range are not returned."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )

        f.record_block_start(50.0)
        f.record_block_end(60.0)

        intervals = f.get_blocked_intervals(100.0, 200.0)
        assert len(intervals) == 0

    def test_duplicate_start_ignored(self):
        """Calling record_block_start twice without end doesn't create duplicates."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )

        f.record_block_start(100.0)
        f.record_block_start(105.0)  # Should be ignored
        f.record_block_end(110.0)

        intervals = f.get_blocked_intervals(90.0, 120.0)
        assert len(intervals) == 1
        assert intervals[0]["start_ts"] == 100.0
