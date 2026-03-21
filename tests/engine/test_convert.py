"""Tests for screencap.engine.convert — dict-based event conversion."""

from __future__ import annotations

from screencap.engine.convert import dict_to_action_event, dict_to_window_switch
from screencap.engine.events import (
    EventType,
    KeyDownEvent,
    KeyUpEvent,
    MouseDownEvent,
    MouseMagnifyEvent,
    MouseMoveEvent,
    MouseRotateEvent,
    MouseScrollEvent,
    MouseSmartMagnifyEvent,
    MouseUpEvent,
    WindowSwitchEvent,
)


class TestDictToActionEvent:
    """Test dict_to_action_event conversion."""

    def test_move_event(self):
        row = {"timestamp": 1.0, "name": "move", "mouse_x": 100, "mouse_y": 200}
        event = dict_to_action_event(row)
        assert isinstance(event, MouseMoveEvent)
        assert event.x == 100
        assert event.y == 200

    def test_click_down(self):
        row = {
            "timestamp": 1.0, "name": "click",
            "mouse_x": 50, "mouse_y": 60,
            "mouse_button_name": "left", "mouse_pressed": True,
        }
        event = dict_to_action_event(row)
        assert isinstance(event, MouseDownEvent)
        assert event.button == "left"

    def test_click_down_int_pressed(self):
        """sqlite3 returns booleans as 0/1 integers."""
        row = {
            "timestamp": 1.0, "name": "click",
            "mouse_x": 50, "mouse_y": 60,
            "mouse_button_name": "left", "mouse_pressed": 1,
        }
        event = dict_to_action_event(row)
        assert isinstance(event, MouseDownEvent)

    def test_click_up(self):
        row = {
            "timestamp": 1.0, "name": "click",
            "mouse_x": 50, "mouse_y": 60,
            "mouse_button_name": "left", "mouse_pressed": False,
        }
        event = dict_to_action_event(row)
        assert isinstance(event, MouseUpEvent)

    def test_click_up_int_pressed(self):
        row = {
            "timestamp": 1.0, "name": "click",
            "mouse_x": 50, "mouse_y": 60,
            "mouse_button_name": "left", "mouse_pressed": 0,
        }
        event = dict_to_action_event(row)
        assert isinstance(event, MouseUpEvent)

    def test_click_none_pressed_returns_none(self):
        row = {
            "timestamp": 1.0, "name": "click",
            "mouse_x": 50, "mouse_y": 60,
            "mouse_button_name": "left", "mouse_pressed": None,
        }
        assert dict_to_action_event(row) is None

    def test_scroll_event(self):
        row = {
            "timestamp": 1.0, "name": "scroll",
            "mouse_x": 100, "mouse_y": 200,
            "mouse_dx": 0, "mouse_dy": -3,
        }
        event = dict_to_action_event(row)
        assert isinstance(event, MouseScrollEvent)
        assert event.dy == -3

    def test_press_event(self):
        row = {
            "timestamp": 1.0, "name": "press",
            "key_char": "a", "key_name": None, "key_vk": None,
            "canonical_key_char": "a", "canonical_key_name": None,
            "canonical_key_vk": None,
        }
        event = dict_to_action_event(row)
        assert isinstance(event, KeyDownEvent)
        assert event.key_char == "a"

    def test_release_event(self):
        row = {
            "timestamp": 1.0, "name": "release",
            "key_char": "a", "key_name": None, "key_vk": None,
            "canonical_key_char": "a", "canonical_key_name": None,
            "canonical_key_vk": None,
        }
        event = dict_to_action_event(row)
        assert isinstance(event, KeyUpEvent)

    def test_magnify_event(self):
        row = {"timestamp": 1.0, "name": "magnify", "mouse_x": 100, "mouse_y": 200, "mouse_dx": 0.05}
        event = dict_to_action_event(row)
        assert isinstance(event, MouseMagnifyEvent)
        assert event.magnification == 0.05

    def test_rotate_event(self):
        row = {"timestamp": 1.0, "name": "rotate", "mouse_x": 100, "mouse_y": 200, "mouse_dx": 15.0}
        event = dict_to_action_event(row)
        assert isinstance(event, MouseRotateEvent)
        assert event.rotation == 15.0

    def test_smart_magnify_event(self):
        row = {"timestamp": 1.0, "name": "smart_magnify", "mouse_x": 100, "mouse_y": 200}
        event = dict_to_action_event(row)
        assert isinstance(event, MouseSmartMagnifyEvent)

    def test_unknown_name_returns_none(self):
        row = {"timestamp": 1.0, "name": "unknown_event_type"}
        assert dict_to_action_event(row) is None

    def test_missing_fields_use_defaults(self):
        """Sparse dict (e.g. old DB schema) should not crash."""
        row = {"timestamp": 1.0, "name": "move"}
        event = dict_to_action_event(row)
        assert isinstance(event, MouseMoveEvent)
        assert event.x == 0
        assert event.y == 0

    def test_invalid_button_defaults_to_left(self):
        row = {
            "timestamp": 1.0, "name": "click",
            "mouse_x": 50, "mouse_y": 60,
            "mouse_button_name": "nonexistent_button", "mouse_pressed": True,
        }
        event = dict_to_action_event(row)
        assert isinstance(event, MouseDownEvent)
        assert event.button == "left"


class TestDictToWindowSwitch:
    """Test dict_to_window_switch conversion."""

    def test_basic_conversion(self):
        row = {
            "timestamp": 1.0,
            "app_bundle_id": "com.apple.finder",
            "title": "Documents",
            "window_id": "42",
            "left": 0, "top": 0, "width": 1920, "height": 1080,
        }
        event = dict_to_window_switch(row)
        assert isinstance(event, WindowSwitchEvent)
        assert event.type == EventType.WINDOW_SWITCH
        assert event.app_bundle_id == "com.apple.finder"
        assert event.window_title == "Documents"
        assert event.window_id == "42"
        assert event.app_name == "Finder"

    def test_app_name_derived_from_bundle_id(self):
        row = {
            "timestamp": 1.0,
            "app_bundle_id": "com.google.Chrome",
            "title": "Google", "window_id": "1",
            "left": 0, "top": 0, "width": 800, "height": 600,
        }
        event = dict_to_window_switch(row)
        assert event.app_name == "Chrome"

    def test_missing_bundle_id_uses_title(self):
        row = {
            "timestamp": 1.0,
            "app_bundle_id": None,
            "title": "My App", "window_id": "1",
            "left": 0, "top": 0, "width": 800, "height": 600,
        }
        event = dict_to_window_switch(row)
        assert event.app_name == "My App"

    def test_missing_fields_default(self):
        row = {"timestamp": 1.0}
        event = dict_to_window_switch(row)
        assert event.window_title == ""
        assert event.window_id == ""
        assert event.x == 0
        assert event.domain is None

    def test_domain_extracted_from_browser_url(self):
        """browser_url column → domain hostname on WindowSwitchEvent."""
        row = {
            "timestamp": 1.0,
            "app_bundle_id": "com.google.Chrome",
            "title": "GitHub",
            "window_id": "1",
            "left": 0, "top": 0, "width": 800, "height": 600,
            "browser_url": "https://github.com/Divide-By-0/screencap",
        }
        event = dict_to_window_switch(row)
        assert event.domain == "github.com"

    def test_domain_none_when_no_browser_url(self):
        """Old recordings without browser_url column → domain=None."""
        row = {
            "timestamp": 1.0,
            "app_bundle_id": "com.apple.Safari",
            "title": "Apple",
            "window_id": "2",
            "left": 0, "top": 0, "width": 800, "height": 600,
        }
        event = dict_to_window_switch(row)
        assert event.domain is None

    def test_domain_none_when_browser_url_empty_or_null(self):
        """Empty or NULL browser_url → domain=None."""
        for browser_url in ("", None):
            row = {
                "timestamp": 1.0,
                "app_bundle_id": "com.apple.Safari",
                "title": "Safari",
                "window_id": "3",
                "left": 0, "top": 0, "width": 800, "height": 600,
                "browser_url": browser_url,
            }
            event = dict_to_window_switch(row)
            assert event.domain is None, f"Expected None for browser_url={browser_url!r}"
