"""Tests for window event processing in screencap.engine.processing."""

from __future__ import annotations

from screencap.engine.events import (
    EventType,
    MouseClickEvent,
    MouseButton,
    MouseDownEvent,
    MouseUpEvent,
    WindowSwitchEvent,
)
from screencap.engine.processing import deduplicate_window_events, interleave_window_events


def _win_row(ts, bundle_id="com.apple.finder", window_id="1", title="Finder"):
    """Helper to build a window event dict."""
    return {
        "timestamp": ts,
        "app_bundle_id": bundle_id,
        "title": title,
        "window_id": window_id,
        "left": 0, "top": 0, "width": 800, "height": 600,
    }


class TestDeduplicateWindowEvents:
    """Tests for deduplicate_window_events."""

    def test_empty_input(self):
        assert deduplicate_window_events([]) == []

    def test_single_event(self):
        rows = [_win_row(1.0)]
        result = deduplicate_window_events(rows)
        assert len(result) == 1
        assert isinstance(result[0], WindowSwitchEvent)

    def test_dedup_same_window(self):
        """Multiple events for same (bundle, window_id) → single switch."""
        rows = [
            _win_row(1.0, "com.apple.finder", "1", "Documents"),
            _win_row(2.0, "com.apple.finder", "1", "Downloads"),  # title change only
            _win_row(3.0, "com.apple.finder", "1", "Desktop"),
        ]
        result = deduplicate_window_events(rows)
        assert len(result) == 1
        assert result[0].window_title == "Documents"  # first occurrence

    def test_different_windows_same_app(self):
        """Same app, different window_id → separate switches."""
        rows = [
            _win_row(1.0, "com.apple.finder", "1", "Documents"),
            _win_row(2.0, "com.apple.finder", "2", "Downloads"),
        ]
        result = deduplicate_window_events(rows)
        assert len(result) == 2

    def test_switch_between_apps(self):
        rows = [
            _win_row(1.0, "com.apple.finder", "1"),
            _win_row(2.0, "com.google.Chrome", "2"),
            _win_row(3.0, "com.apple.finder", "1"),  # back to Finder
        ]
        result = deduplicate_window_events(rows)
        assert len(result) == 3

    def test_preserves_timestamp_order(self):
        rows = [
            _win_row(1.0, "com.apple.finder", "1"),
            _win_row(5.0, "com.google.Chrome", "2"),
        ]
        result = deduplicate_window_events(rows)
        assert result[0].timestamp == 1.0
        assert result[1].timestamp == 5.0


class TestInterleaveWindowEvents:
    """Tests for interleave_window_events."""

    def test_empty_both(self):
        assert interleave_window_events([], []) == []

    def test_only_action_events(self):
        click = MouseClickEvent(
            timestamp=1.0, x=100, y=200, button=MouseButton.LEFT,
        )
        result = interleave_window_events([click], [])
        assert len(result) == 1
        assert result[0] is click

    def test_only_window_events(self):
        ws = WindowSwitchEvent(
            timestamp=1.0, app_name="Finder", app_bundle_id="com.apple.finder",
            window_title="Docs", window_id="1", x=0, y=0, width=800, height=600,
        )
        result = interleave_window_events([], [ws])
        assert len(result) == 1
        assert result[0] is ws

    def test_interleave_by_timestamp(self):
        ws1 = WindowSwitchEvent(
            timestamp=1.0, app_name="Finder", app_bundle_id="com.apple.finder",
            window_title="Docs", window_id="1", x=0, y=0, width=800, height=600,
        )
        click = MouseClickEvent(
            timestamp=2.0, x=100, y=200, button=MouseButton.LEFT,
        )
        ws2 = WindowSwitchEvent(
            timestamp=3.0, app_name="Chrome", app_bundle_id="com.google.Chrome",
            window_title="Google", window_id="2", x=0, y=0, width=800, height=600,
        )

        result = interleave_window_events([click], [ws1, ws2])
        assert len(result) == 3
        assert result[0].timestamp == 1.0
        assert isinstance(result[0], WindowSwitchEvent)
        assert result[1].timestamp == 2.0
        assert isinstance(result[1], MouseClickEvent)
        assert result[2].timestamp == 3.0

    def test_window_before_action_at_same_timestamp(self):
        """Window switch at same timestamp as action should come first."""
        ws = WindowSwitchEvent(
            timestamp=1.0, app_name="Finder", app_bundle_id="com.apple.finder",
            window_title="Docs", window_id="1", x=0, y=0, width=800, height=600,
        )
        click = MouseClickEvent(
            timestamp=1.0, x=100, y=200, button=MouseButton.LEFT,
        )
        result = interleave_window_events([click], [ws])
        assert len(result) == 2
        assert isinstance(result[0], WindowSwitchEvent)
        assert isinstance(result[1], MouseClickEvent)
