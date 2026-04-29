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


# =============================================================================
# Network event conversion tests (V1)
# =============================================================================


class TestBytesToHexHelpers:
    """Hex round-trip helpers shared between V1 and V1.5 paths."""

    def test_bytes_to_hex_none(self):
        from screencap.engine.convert import bytes_to_hex

        assert bytes_to_hex(None) is None

    def test_bytes_to_hex_round_trip(self):
        from screencap.engine.convert import bytes_to_hex, hex_to_bytes

        digest = b"\x01\x02\x03\x04" + b"\x00" * 28
        assert len(digest) == 32
        h = bytes_to_hex(digest)
        assert isinstance(h, str)
        assert h == h.lower()
        assert h == "01020304" + "00" * 28
        assert hex_to_bytes(h) == digest

    def test_hex_to_bytes_none_and_empty(self):
        from screencap.engine.convert import hex_to_bytes

        assert hex_to_bytes(None) is None
        assert hex_to_bytes("") is None


class TestDictToNetworkEvent:
    """Test dict_to_network_event for each kind."""

    def test_request_kind(self):
        import json

        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkRequestEvent

        digest = b"\xaa" * 32
        row = {
            "kind": "request",
            "timestamp": 1.5,
            "timestamp_ns": 1_500_000_000,
            "flow_id": "flow-1",
            "method": "POST",
            "url": "https://example.com/api",
            "host": "example.com",
            "headers_json": json.dumps([
                ["Content-Type", "application/json"],
                ["X-Trace", "abc"],
            ]),
            "body_size": 42,
            "body_sha256": digest,
            "content_type": "application/json",
            "http_version": "HTTP/1.1",
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkRequestEvent)
        assert event.method == "POST"
        assert event.host == "example.com"
        assert event.body_size == 42
        assert event.body_sha256_hex == "aa" * 32
        assert event.headers == [("Content-Type", "application/json"), ("X-Trace", "abc")]

    def test_response_kind(self):
        import json

        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkResponseEvent

        row = {
            "kind": "response",
            "timestamp": 2.0,
            "timestamp_ns": 2_000_000_000,
            "flow_id": "flow-1",
            "host": "example.com",
            "status": 200,
            "headers_json": json.dumps([["Content-Type", "text/html"]]),
            "body_size": 1024,
            "body_sha256": None,
            "content_type": "text/html",
            "http_version": "HTTP/2",
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkResponseEvent)
        assert event.status == 200
        assert event.body_sha256_hex is None
        assert event.http_version == "HTTP/2"

    def test_ws_upgrade_kind(self):
        import json

        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkWebSocketUpgradeEvent

        row = {
            "kind": "ws_upgrade",
            "timestamp": 3.0,
            "timestamp_ns": 3_000_000_000,
            "flow_id": "ws-1",
            "url": "wss://chat.example.com/socket",
            "host": "chat.example.com",
            "status": 101,
            "headers_json": json.dumps([
                ["Upgrade", "websocket"],
                ["Connection", "Upgrade"],
            ]),
            "http_version": "HTTP/1.1",
            "details_json": json.dumps({
                "request_headers": [
                    ["Sec-WebSocket-Key", "abc=="],
                ],
            }),
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkWebSocketUpgradeEvent)
        assert event.url == "wss://chat.example.com/socket"
        assert event.details_json == {
            "request_headers": [["Sec-WebSocket-Key", "abc=="]],
        }
        assert len(event.headers) == 2

    def test_ws_frame_kind(self):
        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkWebSocketFrameEvent

        digest = b"\xbb" * 32
        row = {
            "kind": "ws_frame",
            "timestamp": 4.0,
            "timestamp_ns": 4_000_000_000,
            "flow_id": "ws-1",
            "host": "chat.example.com",
            "direction": "received",
            "frame_type": "binary",
            "body_size": 256,
            "body_sha256": digest,
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkWebSocketFrameEvent)
        assert event.direction == "received"
        assert event.frame_type == "binary"
        assert event.body_sha256_hex == "bb" * 32

    def test_drop_burst_kind(self):
        import json

        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkDropBurstEvent

        row = {
            "kind": "drop_burst",
            "timestamp": 5.0,
            "timestamp_ns": 5_000_000_000,
            "details_json": json.dumps({
                "dropped_count": 3,
                "hosts_affected": ["a.com", "b.com"],
                "source": "addon",
            }),
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkDropBurstEvent)
        assert event.details_json["dropped_count"] == 3
        assert event.details_json["source"] == "addon"
        assert event.details_json["hosts_affected"] == ["a.com", "b.com"]

    def test_unknown_kind_returns_none(self):
        from screencap.engine.convert import dict_to_network_event

        assert dict_to_network_event({"kind": "no_such_kind"}) is None
        assert dict_to_network_event({"kind": ""}) is None
        assert dict_to_network_event({}) is None

    def test_ws_frame_invalid_direction_returns_none(self):
        """Conversion-layer guard: malformed direction is dropped, not raised."""
        from screencap.engine.convert import dict_to_network_event

        row = {
            "kind": "ws_frame",
            "timestamp": 1.0,
            "timestamp_ns": 1,
            "flow_id": "f",
            "host": "h",
            "direction": "garbage",
            "frame_type": "text",
        }
        assert dict_to_network_event(row) is None

    def test_drop_burst_missing_details_returns_none(self):
        """drop_burst requires details_json - missing => None."""
        from screencap.engine.convert import dict_to_network_event

        assert (
            dict_to_network_event({
                "kind": "drop_burst",
                "timestamp": 1.0,
                "timestamp_ns": 1,
            })
            is None
        )

    def test_already_decoded_headers_pass_through(self):
        """headers_json may arrive as a list (some callers pre-decode)."""
        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkRequestEvent

        row = {
            "kind": "request",
            "timestamp": 1.0,
            "timestamp_ns": 1,
            "flow_id": "f",
            "method": "GET",
            "url": "https://x.com/",
            "host": "x.com",
            "headers_json": [["A", "1"], ["B", "2"]],
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkRequestEvent)
        assert event.headers == [("A", "1"), ("B", "2")]


# =============================================================================
# V1.5 body-encryption conversion tests
# =============================================================================


class TestDictToNetworkEventV15:
    """V1.5 body-encryption fields populate cleanly through the converter."""

    def test_request_with_ciphertext(self):
        """V1.5 row with all three ciphertext fields populated."""
        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkRequestEvent

        ciphertext = b"\xde\xad\xbe\xef" * 4
        nonce = b"\x00" * 12
        aad = b"recording_id=1|flow=abc"
        row = {
            "kind": "request",
            "timestamp": 1.0,
            "timestamp_ns": 1_000_000_000,
            "flow_id": "flow-1",
            "method": "POST",
            "url": "https://example.com/api",
            "host": "example.com",
            "body_size": 16,
            "body_ciphertext": ciphertext,
            "body_nonce": nonce,
            "body_aad": aad,
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkRequestEvent)
        assert event.body_ciphertext == ciphertext
        assert event.body_nonce == nonce
        assert event.body_aad == aad

    def test_response_with_ciphertext(self):
        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkResponseEvent

        ciphertext = b"\x01\x02" * 8
        nonce = b"\x11" * 12
        aad = b"recording_id=2|flow=def"
        row = {
            "kind": "response",
            "timestamp": 2.0,
            "timestamp_ns": 2_000_000_000,
            "flow_id": "flow-2",
            "host": "api.example.com",
            "status": 200,
            "body_ciphertext": ciphertext,
            "body_nonce": nonce,
            "body_aad": aad,
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkResponseEvent)
        assert event.body_ciphertext == ciphertext

    def test_ws_upgrade_with_ciphertext(self):
        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkWebSocketUpgradeEvent

        ciphertext = b"\xab" * 32
        nonce = b"\x22" * 12
        aad = b"recording_id=3|flow=ws"
        row = {
            "kind": "ws_upgrade",
            "timestamp": 3.0,
            "timestamp_ns": 3_000_000_000,
            "flow_id": "ws-1",
            "url": "wss://chat.example.com/socket",
            "host": "chat.example.com",
            "status": 101,
            "body_ciphertext": ciphertext,
            "body_nonce": nonce,
            "body_aad": aad,
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkWebSocketUpgradeEvent)
        assert event.body_ciphertext == ciphertext

    def test_ws_frame_with_ciphertext(self):
        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkWebSocketFrameEvent

        ciphertext = b"\xff" * 16
        nonce = b"\x33" * 12
        aad = b"recording_id=4|flow=ws_frame"
        row = {
            "kind": "ws_frame",
            "timestamp": 4.0,
            "timestamp_ns": 4_000_000_000,
            "flow_id": "ws-1",
            "host": "chat.example.com",
            "direction": "received",
            "frame_type": "text",
            "body_ciphertext": ciphertext,
            "body_nonce": nonce,
            "body_aad": aad,
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkWebSocketFrameEvent)
        assert event.body_ciphertext == ciphertext

    def test_v1_row_without_ciphertext_columns(self):
        """Backward-compat: V1 rows (no ciphertext columns) convert with body fields = None."""
        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkRequestEvent

        # No body_ciphertext / body_nonce / body_aad keys at all
        row = {
            "kind": "request",
            "timestamp": 1.0,
            "timestamp_ns": 1_000_000_000,
            "flow_id": "f",
            "method": "GET",
            "url": "https://x.com/",
            "host": "x.com",
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkRequestEvent)
        assert event.body_ciphertext is None
        assert event.body_nonce is None
        assert event.body_aad is None

    def test_explicit_none_ciphertext(self):
        """Explicit None values produce None on the Pydantic event."""
        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkRequestEvent

        row = {
            "kind": "request",
            "timestamp": 1.0,
            "timestamp_ns": 1_000_000_000,
            "flow_id": "f",
            "method": "GET",
            "url": "https://x.com/",
            "host": "x.com",
            "body_ciphertext": None,
            "body_nonce": None,
            "body_aad": None,
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkRequestEvent)
        assert event.body_ciphertext is None

    def test_memoryview_ciphertext_coerced_to_bytes(self):
        """sqlite3 may surface BLOB columns as memoryview - convert to bytes."""
        from screencap.engine.convert import dict_to_network_event
        from screencap.engine.events import NetworkRequestEvent

        raw = b"\x01\x02\x03"
        row = {
            "kind": "request",
            "timestamp": 1.0,
            "timestamp_ns": 1_000_000_000,
            "flow_id": "f",
            "method": "GET",
            "url": "https://x.com/",
            "host": "x.com",
            "body_ciphertext": memoryview(raw),
            "body_nonce": memoryview(b"\x00" * 12),
            "body_aad": memoryview(b"a"),
        }
        event = dict_to_network_event(row)
        assert isinstance(event, NetworkRequestEvent)
        assert isinstance(event.body_ciphertext, bytes)
        assert event.body_ciphertext == raw
