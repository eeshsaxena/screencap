"""Tests for event schemas."""


from screencap.engine.events import (
    AudioChunkEvent,
    EventType,
    KeyDownEvent,
    KeyTypeEvent,
    KeyUpEvent,
    MouseButton,
    MouseClickEvent,
    MouseDoubleClickEvent,
    MouseDownEvent,
    MouseDragEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    MouseUpEvent,
    ScreenFrameEvent,
)


class TestMouseEvents:
    """Tests for mouse event types."""

    def test_mouse_move_event(self):
        """Test MouseMoveEvent creation and serialization."""
        event = MouseMoveEvent(
            timestamp=1234567890.123,
            x=100.5,
            y=200.5,
        )
        assert event.type == EventType.MOUSE_MOVE
        assert event.x == 100.5
        assert event.y == 200.5
        assert event.timestamp == 1234567890.123

        # Test serialization
        data = event.model_dump()
        assert data["type"] == "mouse.move"
        assert data["x"] == 100.5

    def test_mouse_down_event(self):
        """Test MouseDownEvent creation."""
        event = MouseDownEvent(
            timestamp=1234567890.123,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        assert event.type == EventType.MOUSE_DOWN
        assert event.button == MouseButton.LEFT

    def test_mouse_up_event(self):
        """Test MouseUpEvent creation."""
        event = MouseUpEvent(
            timestamp=1234567890.123,
            x=100.0,
            y=200.0,
            button=MouseButton.RIGHT,
        )
        assert event.type == EventType.MOUSE_UP
        assert event.button == MouseButton.RIGHT

    def test_mouse_scroll_event(self):
        """Test MouseScrollEvent creation."""
        event = MouseScrollEvent(
            timestamp=1234567890.123,
            x=100.0,
            y=200.0,
            dx=0.0,
            dy=-3.0,
        )
        assert event.type == EventType.MOUSE_SCROLL
        assert event.dy == -3.0

    def test_mouse_click_event(self):
        """Test MouseClickEvent with children."""
        down = MouseDownEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        up = MouseUpEvent(
            timestamp=1.1,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        click = MouseClickEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
            children=[down, up],
        )
        assert click.type == EventType.MOUSE_SINGLECLICK
        assert len(click.children) == 2

    def test_mouse_double_click_event(self):
        """Test MouseDoubleClickEvent."""
        event = MouseDoubleClickEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        assert event.type == EventType.MOUSE_DOUBLECLICK

    def test_mouse_drag_event(self):
        """Test MouseDragEvent."""
        event = MouseDragEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            dx=200.0,  # end_x - start_x = 300 - 100
            dy=200.0,  # end_y - start_y = 400 - 200
            button=MouseButton.LEFT,
        )
        assert event.type == EventType.MOUSE_DRAG
        assert event.x == 100.0
        assert event.dx == 200.0


class TestKeyboardEvents:
    """Tests for keyboard event types."""

    def test_key_down_event(self):
        """Test KeyDownEvent creation."""
        event = KeyDownEvent(
            timestamp=1234567890.123,
            key_char="a",
        )
        assert event.type == EventType.KEY_DOWN
        assert event.key_char == "a"

    def test_key_down_event_with_name(self):
        """Test KeyDownEvent with key name (special key)."""
        event = KeyDownEvent(
            timestamp=1.0,
            key_name="shift",
        )
        assert event.key_name == "shift"
        assert event.key_char is None

    def test_key_up_event(self):
        """Test KeyUpEvent creation."""
        event = KeyUpEvent(
            timestamp=1.0,
            key_char="b",
        )
        assert event.type == EventType.KEY_UP

    def test_key_type_event(self):
        """Test KeyTypeEvent with children."""
        children = [
            KeyDownEvent(timestamp=1.0, key_char="h"),
            KeyUpEvent(timestamp=1.1, key_char="h"),
            KeyDownEvent(timestamp=1.2, key_char="i"),
            KeyUpEvent(timestamp=1.3, key_char="i"),
        ]
        event = KeyTypeEvent(
            timestamp=1.0,
            text="hi",
            children=children,
        )
        assert event.type == EventType.KEY_TYPE
        assert event.text == "hi"
        assert len(event.children) == 4


class TestMediaKeyEvents:
    """Tests for media key event types."""

    def test_key_down_media_play_pause(self):
        """Test KeyDownEvent with media_play_pause round-trips through JSON."""
        event = KeyDownEvent(
            timestamp=1.0,
            key_name="media_play_pause",
        )
        assert event.type == EventType.KEY_DOWN
        assert event.key_name == "media_play_pause"

        # Round-trip through JSON
        data = event.model_dump()
        restored = KeyDownEvent(**data)
        assert restored.key_name == "media_play_pause"
        assert restored.type == EventType.KEY_DOWN

    def test_key_down_media_brightness_up(self):
        """Test KeyDownEvent with media_brightness_up round-trips correctly."""
        event = KeyDownEvent(
            timestamp=1.0,
            key_name="media_brightness_up",
            key_vk="21",
        )
        data = event.model_dump()
        restored = KeyDownEvent(**data)
        assert restored.key_name == "media_brightness_up"
        assert restored.key_vk == "21"

    def test_key_down_media_volume_up(self):
        """Test KeyDownEvent with media_volume_up round-trips correctly."""
        event = KeyDownEvent(
            timestamp=1.0,
            key_name="media_volume_up",
        )
        json_str = event.model_dump_json()
        assert "media_volume_up" in json_str

    def test_key_up_media_play_pause(self):
        """Test KeyUpEvent with media_play_pause round-trips correctly."""
        event = KeyUpEvent(
            timestamp=1.0,
            key_name="media_play_pause",
        )
        assert event.type == EventType.KEY_UP
        data = event.model_dump()
        restored = KeyUpEvent(**data)
        assert restored.key_name == "media_play_pause"

    def test_key_up_media_brightness_down(self):
        """Test KeyUpEvent with media_brightness_down round-trips correctly."""
        event = KeyUpEvent(
            timestamp=1.0,
            key_name="media_brightness_down",
            key_vk="22",
        )
        data = event.model_dump()
        restored = KeyUpEvent(**data)
        assert restored.key_name == "media_brightness_down"
        assert restored.key_vk == "22"


class TestScreenEvents:
    """Tests for screen event types."""

    def test_screen_frame_event_with_video(self):
        """Test ScreenFrameEvent with video timestamp."""
        event = ScreenFrameEvent(
            timestamp=1.0,
            video_timestamp=0.5,
            width=1920,
            height=1080,
        )
        assert event.type == EventType.SCREEN_FRAME
        assert event.video_timestamp == 0.5
        assert event.image_path is None

    def test_screen_frame_event_with_image(self):
        """Test ScreenFrameEvent with image path."""
        event = ScreenFrameEvent(
            timestamp=1.0,
            image_path="/path/to/screenshot.png",
            width=1920,
            height=1080,
        )
        assert event.image_path == "/path/to/screenshot.png"
        assert event.video_timestamp is None


class TestAudioEvents:
    """Tests for audio event types."""

    def test_audio_chunk_event(self):
        """Test AudioChunkEvent creation."""
        event = AudioChunkEvent(
            timestamp=1.0,
            start_time=1.0,
            end_time=31.0,
            transcription="Hello world",
        )
        assert event.type == EventType.AUDIO_CHUNK
        assert event.transcription == "Hello world"
        assert event.end_time - event.start_time == 30.0

    def test_audio_chunk_event_no_transcription(self):
        """Test AudioChunkEvent without transcription."""
        event = AudioChunkEvent(
            timestamp=1.0,
            start_time=1.0,
            end_time=31.0,
        )
        assert event.transcription is None


class TestEventSerialization:
    """Tests for event serialization."""

    def test_event_to_json(self):
        """Test event serialization to JSON."""
        event = MouseMoveEvent(
            timestamp=1234567890.123,
            x=100.5,
            y=200.5,
        )
        json_str = event.model_dump_json()
        assert "mouse.move" in json_str
        assert "100.5" in json_str

    def test_event_from_dict(self):
        """Test event creation from dict."""
        data = {
            "timestamp": 1234567890.123,
            "x": 100.5,
            "y": 200.5,
        }
        event = MouseMoveEvent(**data)
        assert event.x == 100.5

    def test_nested_event_serialization(self):
        """Test serialization of events with children."""
        down = MouseDownEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        up = MouseUpEvent(
            timestamp=1.1,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
        )
        click = MouseClickEvent(
            timestamp=1.0,
            x=100.0,
            y=200.0,
            button=MouseButton.LEFT,
            children=[down, up],
        )

        data = click.model_dump()
        assert len(data["children"]) == 2
        assert data["children"][0]["type"] == "mouse.down"


# =============================================================================
# Network event tests (V1)
# =============================================================================


class TestNetworkRequestEvent:
    """Tests for NetworkRequestEvent - V1 metadata-only HTTP request."""

    def test_basic_request(self):
        from screencap.engine.events import EventType, NetworkRequestEvent

        event = NetworkRequestEvent(
            timestamp=1234567890.123,
            timestamp_ns=1234567890_123_000_000,
            flow_id="flow-1",
            method="GET",
            url="https://example.com/api/v1/users",
            host="example.com",
            headers=[("Accept", "application/json")],
            body_size=None,
            content_type=None,
            http_version="HTTP/1.1",
        )
        assert event.type == EventType.NETWORK_REQUEST
        assert event.method == "GET"
        assert event.host == "example.com"
        assert event.body_sha256_hex is None

    def test_round_trip_json(self):
        """V1: model_dump_json -> model_validate_json must round-trip."""
        from screencap.engine.events import NetworkRequestEvent

        event = NetworkRequestEvent(
            timestamp=1234567890.123,
            timestamp_ns=1234567890_123_000_000,
            flow_id="flow-1",
            method="POST",
            url="https://api.example.com/v1/post",
            host="api.example.com",
            headers=[
                ("Content-Type", "application/json"),
                ("X-Request-Id", "abc-123"),
            ],
            body_size=42,
            body_sha256_hex="a" * 64,
            content_type="application/json",
            http_version="HTTP/1.1",
        )
        json_str = event.model_dump_json()
        restored = NetworkRequestEvent.model_validate_json(json_str)
        assert restored == event
        assert restored.body_sha256_hex == "a" * 64

    def test_multi_value_headers_preserved(self):
        """Edge: duplicate header names (e.g. multiple Set-Cookie) survive."""
        from screencap.engine.events import NetworkRequestEvent

        event = NetworkRequestEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="f",
            method="GET",
            url="https://example.com/",
            host="example.com",
            headers=[
                ("Cookie", "first=1"),
                ("Cookie", "second=2"),
                ("Cookie", "third=3"),
            ],
        )
        json_str = event.model_dump_json()
        restored = NetworkRequestEvent.model_validate_json(json_str)
        assert len(restored.headers) == 3
        assert all(name == "Cookie" for name, _ in restored.headers)
        assert [v for _, v in restored.headers] == ["first=1", "second=2", "third=3"]


class TestNetworkResponseEvent:
    """Tests for NetworkResponseEvent."""

    def test_basic_response(self):
        from screencap.engine.events import EventType, NetworkResponseEvent

        event = NetworkResponseEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-1",
            host="example.com",
            status=200,
            headers=[("Content-Type", "text/html")],
        )
        assert event.type == EventType.NETWORK_RESPONSE
        assert event.status == 200

    def test_round_trip_json(self):
        from screencap.engine.events import NetworkResponseEvent

        event = NetworkResponseEvent(
            timestamp=2.0,
            timestamp_ns=2_000_000_000,
            flow_id="f-2",
            host="api.example.com",
            status=201,
            headers=[
                ("Set-Cookie", "session=abc"),
                ("Set-Cookie", "csrf=def"),
            ],
            body_size=128,
            body_sha256_hex="b" * 64,
            content_type="application/json",
            http_version="HTTP/2",
        )
        restored = NetworkResponseEvent.model_validate_json(event.model_dump_json())
        assert restored == event
        assert len(restored.headers) == 2


class TestNetworkWebSocketUpgradeEvent:
    """Tests for NetworkWebSocketUpgradeEvent."""

    def test_basic_upgrade(self):
        from screencap.engine.events import (
            EventType,
            NetworkWebSocketUpgradeEvent,
        )

        event = NetworkWebSocketUpgradeEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="ws-1",
            url="wss://chat.example.com/socket",
            host="chat.example.com",
            headers=[("Upgrade", "websocket")],
            details_json={
                "request_headers": [
                    ["Sec-WebSocket-Key", "abcd=="],
                    ["Sec-WebSocket-Version", "13"],
                ]
            },
        )
        assert event.type == EventType.NETWORK_WS_UPGRADE
        assert event.status == 101  # default

    def test_round_trip_json_with_request_headers(self):
        from screencap.engine.events import NetworkWebSocketUpgradeEvent

        event = NetworkWebSocketUpgradeEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="ws-1",
            url="wss://example.com/ws",
            host="example.com",
            headers=[("Upgrade", "websocket"), ("Connection", "Upgrade")],
            http_version="HTTP/1.1",
            details_json={
                "request_headers": [["Sec-WebSocket-Key", "x=="]],
            },
        )
        restored = NetworkWebSocketUpgradeEvent.model_validate_json(
            event.model_dump_json()
        )
        assert restored == event
        assert restored.details_json["request_headers"] == [["Sec-WebSocket-Key", "x=="]]


class TestNetworkWebSocketFrameEvent:
    """Tests for NetworkWebSocketFrameEvent."""

    def test_text_sent_frame(self):
        from screencap.engine.events import (
            EventType,
            NetworkWebSocketFrameEvent,
        )

        event = NetworkWebSocketFrameEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="ws-1",
            host="example.com",
            direction="sent",
            frame_type="text",
            body_size=12,
        )
        assert event.type == EventType.NETWORK_WS_FRAME
        assert event.direction == "sent"
        assert event.frame_type == "text"

    def test_binary_received_frame_round_trip(self):
        """Edge: ws_frame with direction/frame_type round-trips through JSON."""
        from screencap.engine.events import NetworkWebSocketFrameEvent

        event = NetworkWebSocketFrameEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="ws-1",
            host="example.com",
            direction="received",
            frame_type="binary",
            body_size=2048,
            body_sha256_hex="c" * 64,
        )
        restored = NetworkWebSocketFrameEvent.model_validate_json(
            event.model_dump_json()
        )
        assert restored == event
        assert restored.direction == "received"
        assert restored.frame_type == "binary"

    def test_invalid_direction_raises(self):
        """Error: invalid direction is rejected by Literal type."""
        import pytest
        from pydantic import ValidationError

        from screencap.engine.events import NetworkWebSocketFrameEvent

        with pytest.raises(ValidationError):
            NetworkWebSocketFrameEvent(
                timestamp=1.0,
                timestamp_ns=1_000_000_000,
                flow_id="ws",
                host="x.com",
                direction="bogus",  # type: ignore[arg-type]
                frame_type="text",
            )

    def test_invalid_frame_type_raises(self):
        """Error: invalid frame_type is rejected."""
        import pytest
        from pydantic import ValidationError

        from screencap.engine.events import NetworkWebSocketFrameEvent

        with pytest.raises(ValidationError):
            NetworkWebSocketFrameEvent(
                timestamp=1.0,
                timestamp_ns=1_000_000_000,
                flow_id="ws",
                host="x.com",
                direction="sent",
                frame_type="control",  # type: ignore[arg-type]
            )


class TestNetworkDropBurstEvent:
    """Tests for NetworkDropBurstEvent."""

    def test_addon_source_round_trip(self):
        from screencap.engine.events import (
            EventType,
            NetworkDropBurstEvent,
        )

        event = NetworkDropBurstEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            details_json={
                "dropped_count": 17,
                "hosts_affected": ["chat.example.com", "api.example.com"],
                "source": "addon",
            },
        )
        assert event.type == EventType.NETWORK_DROP_BURST
        restored = NetworkDropBurstEvent.model_validate_json(event.model_dump_json())
        assert restored == event
        assert restored.details_json["source"] == "addon"
        assert restored.details_json["dropped_count"] == 17

    def test_reader_source_round_trip(self):
        from screencap.engine.events import NetworkDropBurstEvent

        event = NetworkDropBurstEvent(
            timestamp=2.0,
            timestamp_ns=2_000_000_000,
            details_json={
                "dropped_count": 5,
                "hosts_affected": [],
                "source": "reader",
            },
        )
        restored = NetworkDropBurstEvent.model_validate_json(event.model_dump_json())
        assert restored == event


class TestNetworkPinFailureEvent:
    """Tests for the NetworkPinFailureEvent control sideband.

    This class is a bare BaseModel - NOT a BaseEvent. The guard test
    below locks the inheritance so a future "tidying" refactor that
    changed the base class would break TestEventTypeMap loudly here
    rather than silently in test_storage.py.
    """

    def test_not_a_base_event(self):
        """Guard: NetworkPinFailureEvent must NOT subclass BaseEvent.

        Subclassing BaseEvent would make TestEventTypeMap fail because
        there is no EventType.NETWORK_PIN_FAILURE and no EVENT_TYPE_MAP
        entry for this control message - by design.
        """
        from screencap.engine.events import BaseEvent, NetworkPinFailureEvent

        assert not issubclass(NetworkPinFailureEvent, BaseEvent), (
            "NetworkPinFailureEvent must remain a bare pydantic.BaseModel - "
            "it is a control-only sideband, not a recordable event."
        )

    def test_basic_construction(self):
        from screencap.engine.events import NetworkPinFailureEvent

        event = NetworkPinFailureEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            host="bank.example.com",
            reason="cert pin mismatch",
        )
        assert event.host == "bank.example.com"
        assert event.reason == "cert pin mismatch"

    def test_no_type_field(self):
        """NetworkPinFailureEvent has no `type` field - it's not in the registry."""
        from screencap.engine.events import NetworkPinFailureEvent

        assert "type" not in NetworkPinFailureEvent.model_fields


class TestNetworkEventValidation:
    """Error-path tests for network event Pydantic validation."""

    def test_request_rejects_wrong_type_field(self):
        """Error: feeding a request payload to a response class fails."""
        import pytest
        from pydantic import ValidationError

        from screencap.engine.events import NetworkResponseEvent

        # response payload with a wrong `type` literal
        bad = {
            "timestamp": 1.0,
            "timestamp_ns": 1,
            "type": "network.request",  # wrong - response expects network.response
            "flow_id": "f",
            "host": "h",
            "status": 200,
        }
        with pytest.raises(ValidationError):
            NetworkResponseEvent.model_validate(bad)

    def test_drop_burst_requires_details_json(self):
        """Error: NetworkDropBurstEvent.details_json is required."""
        import pytest
        from pydantic import ValidationError

        from screencap.engine.events import NetworkDropBurstEvent

        with pytest.raises(ValidationError):
            NetworkDropBurstEvent(
                timestamp=1.0,
                timestamp_ns=1,
            )
