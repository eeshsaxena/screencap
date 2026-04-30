"""Tests for event schemas.

Construction + ``model_dump`` round-trips for the simple event classes
(MouseMoveEvent, KeyDownEvent, ScreenFrameEvent, AudioChunkEvent, etc.)
are not tested here — pydantic's BaseModel guarantees field assignment,
type-discriminator inclusion, and JSON round-tripping for free. Tests
that just constructed an event and checked its fields were removed in
the test-audit pass; if a future event ever gains a custom serializer,
validator, or computed field, add a targeted test rather than restoring
the full round-trip suite.

This file keeps the *non-trivial* contracts that pydantic does not
guarantee: network event validation rules, required-field constraints,
multi-value header preservation across JSON round-trips for V1 wire
format stability, and the ``NetworkPinFailureEvent`` non-BaseEvent
regression guard.
"""


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


# =============================================================================
# V1.5 capture-side body-encryption tests
# =============================================================================
#
# Capture-side network events flow proxy -> reader -> writer -> DB via
# `multiprocessing.Queue` (pickle), NEVER through JSONL. The V1.5 ciphertext
# fields (`body_ciphertext`/`body_nonce`/`body_aad`) are raw `bytes` and
# round-trip via pickle. Each test below also covers the metadata-only
# (`body_ciphertext=None`) path so V1 callers continue to work unchanged.


class TestNetworkRequestEventV15:
    """V1.5 body-encryption fields on the capture-side NetworkRequestEvent."""

    def test_ciphertext_pickle_round_trip(self):
        import pickle

        from screencap.engine.events import NetworkRequestEvent

        ciphertext = b"\xde\xad\xbe\xef" * 4
        nonce = b"\x00" * 12
        aad = b"recording_id=1|flow=abc"
        event = NetworkRequestEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-1",
            method="POST",
            url="https://example.com/api",
            host="example.com",
            body_size=16,
            body_sha256_hex="a" * 64,
            body_ciphertext=ciphertext,
            body_nonce=nonce,
            body_aad=aad,
        )
        restored = pickle.loads(pickle.dumps(event))
        assert restored == event
        assert restored.body_ciphertext == ciphertext
        assert restored.body_nonce == nonce
        assert restored.body_aad == aad

    def test_metadata_only_pickle_round_trip(self):
        """Edge: ciphertext=None still round-trips (V1 / metadata-only path)."""
        import pickle

        from screencap.engine.events import NetworkRequestEvent

        event = NetworkRequestEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-1",
            method="GET",
            url="https://example.com/",
            host="example.com",
            body_size=None,
        )
        restored = pickle.loads(pickle.dumps(event))
        assert restored == event
        assert restored.body_ciphertext is None
        assert restored.body_nonce is None
        assert restored.body_aad is None


class TestNetworkResponseEventV15:
    """V1.5 body-encryption fields on the capture-side NetworkResponseEvent."""

    def test_ciphertext_pickle_round_trip(self):
        import pickle

        from screencap.engine.events import NetworkResponseEvent

        ciphertext = b"\x01\x02\x03\x04" * 8
        nonce = b"\x11" * 12
        aad = b"recording_id=2|flow=def"
        event = NetworkResponseEvent(
            timestamp=2.0,
            timestamp_ns=2_000_000_000,
            flow_id="flow-2",
            host="api.example.com",
            status=200,
            body_size=32,
            body_ciphertext=ciphertext,
            body_nonce=nonce,
            body_aad=aad,
        )
        restored = pickle.loads(pickle.dumps(event))
        assert restored == event
        assert restored.body_ciphertext == ciphertext

    def test_metadata_only_pickle_round_trip(self):
        import pickle

        from screencap.engine.events import NetworkResponseEvent

        event = NetworkResponseEvent(
            timestamp=2.0,
            timestamp_ns=2_000_000_000,
            flow_id="flow-2",
            host="example.com",
            status=204,
        )
        restored = pickle.loads(pickle.dumps(event))
        assert restored == event
        assert restored.body_ciphertext is None


class TestNetworkWebSocketUpgradeEventV15:
    """V1.5 body-encryption fields on the capture-side WS upgrade event."""

    def test_ciphertext_pickle_round_trip(self):
        import pickle

        from screencap.engine.events import NetworkWebSocketUpgradeEvent

        ciphertext = b"\xab\xcd" * 16
        nonce = b"\x22" * 12
        aad = b"recording_id=3|flow=ws"
        event = NetworkWebSocketUpgradeEvent(
            timestamp=3.0,
            timestamp_ns=3_000_000_000,
            flow_id="ws-1",
            url="wss://chat.example.com/socket",
            host="chat.example.com",
            details_json={"request_headers": [["Sec-WebSocket-Key", "abc=="]]},
            body_ciphertext=ciphertext,
            body_nonce=nonce,
            body_aad=aad,
        )
        restored = pickle.loads(pickle.dumps(event))
        assert restored == event
        assert restored.body_ciphertext == ciphertext

    def test_metadata_only_pickle_round_trip(self):
        import pickle

        from screencap.engine.events import NetworkWebSocketUpgradeEvent

        event = NetworkWebSocketUpgradeEvent(
            timestamp=3.0,
            timestamp_ns=3_000_000_000,
            flow_id="ws-1",
            url="wss://example.com/ws",
            host="example.com",
        )
        restored = pickle.loads(pickle.dumps(event))
        assert restored == event
        assert restored.body_ciphertext is None


class TestNetworkWebSocketFrameEventV15:
    """V1.5 body-encryption fields on the capture-side WS frame event."""

    def test_ciphertext_pickle_round_trip(self):
        import pickle

        from screencap.engine.events import NetworkWebSocketFrameEvent

        ciphertext = b"\xff\xee\xdd\xcc" * 4
        nonce = b"\x33" * 12
        aad = b"recording_id=4|flow=ws_frame"
        event = NetworkWebSocketFrameEvent(
            timestamp=4.0,
            timestamp_ns=4_000_000_000,
            flow_id="ws-1",
            host="chat.example.com",
            direction="received",
            frame_type="text",
            body_size=16,
            body_ciphertext=ciphertext,
            body_nonce=nonce,
            body_aad=aad,
        )
        restored = pickle.loads(pickle.dumps(event))
        assert restored == event
        assert restored.body_ciphertext == ciphertext

    def test_metadata_only_pickle_round_trip(self):
        import pickle

        from screencap.engine.events import NetworkWebSocketFrameEvent

        event = NetworkWebSocketFrameEvent(
            timestamp=4.0,
            timestamp_ns=4_000_000_000,
            flow_id="ws-1",
            host="example.com",
            direction="sent",
            frame_type="binary",
        )
        restored = pickle.loads(pickle.dumps(event))
        assert restored == event
        assert restored.body_ciphertext is None


# =============================================================================
# V1.5 export-side parallel family tests
# =============================================================================
#
# Export-side classes are bare BaseModel (NOT BaseEvent) by design - this
# keeps them out of EVENT_TYPE_MAP (which the capture-side classes already
# occupy) and out of the TestEventTypeMap walk. They carry `body_text` (str)
# instead of ciphertext, are constructed by NetworkScrubPipeline at export
# time, and round-trip through JSON for events.jsonl emission.


class TestNetworkRequestExportEvent:
    """V1.5 export-side request event (bare BaseModel, body_text plaintext)."""

    def test_not_a_base_event(self):
        """Guard: must NOT subclass BaseEvent (mirrors NetworkPinFailureEvent)."""
        from screencap.engine.events import BaseEvent, NetworkRequestExportEvent

        assert not issubclass(NetworkRequestExportEvent, BaseEvent), (
            "NetworkRequestExportEvent must remain a bare BaseModel - "
            "subclassing BaseEvent would collide with NetworkRequestEvent "
            "in EVENT_TYPE_MAP and break TestEventTypeMap."
        )

    def test_no_body_ciphertext_field(self):
        """Safety contract: export-side class never carries ciphertext."""
        from screencap.engine.events import NetworkRequestExportEvent

        assert "body_ciphertext" not in NetworkRequestExportEvent.model_fields
        assert "body_nonce" not in NetworkRequestExportEvent.model_fields
        assert "body_aad" not in NetworkRequestExportEvent.model_fields
        assert "body_text" in NetworkRequestExportEvent.model_fields

    def test_json_round_trip(self):
        from screencap.engine.events import EventType, NetworkRequestExportEvent

        event = NetworkRequestExportEvent(
            timestamp=1.0,
            timestamp_ns=1_000_000_000,
            flow_id="flow-1",
            method="POST",
            url="https://example.com/api",
            host="example.com",
            headers=[("Content-Type", "application/json")],
            body_size=11,
            body_sha256_hex="a" * 64,
            content_type="application/json",
            http_version="HTTP/1.1",
            body_text="hello world",
        )
        restored = NetworkRequestExportEvent.model_validate_json(
            event.model_dump_json()
        )
        assert restored == event
        assert restored.body_text == "hello world"
        assert restored.type == EventType.NETWORK_REQUEST


class TestNetworkResponseExportEvent:
    """V1.5 export-side response event."""

    def test_not_a_base_event(self):
        from screencap.engine.events import BaseEvent, NetworkResponseExportEvent

        assert not issubclass(NetworkResponseExportEvent, BaseEvent)

    def test_no_body_ciphertext_field(self):
        from screencap.engine.events import NetworkResponseExportEvent

        assert "body_ciphertext" not in NetworkResponseExportEvent.model_fields
        assert "body_text" in NetworkResponseExportEvent.model_fields

    def test_json_round_trip(self):
        from screencap.engine.events import EventType, NetworkResponseExportEvent

        event = NetworkResponseExportEvent(
            timestamp=2.0,
            timestamp_ns=2_000_000_000,
            flow_id="flow-2",
            host="api.example.com",
            status=200,
            headers=[("Content-Type", "text/html")],
            body_text="hello",
        )
        restored = NetworkResponseExportEvent.model_validate_json(
            event.model_dump_json()
        )
        assert restored == event
        assert restored.body_text == "hello"
        assert restored.type == EventType.NETWORK_RESPONSE


class TestNetworkWebSocketUpgradeExportEvent:
    """V1.5 export-side WS upgrade event."""

    def test_not_a_base_event(self):
        from screencap.engine.events import (
            BaseEvent,
            NetworkWebSocketUpgradeExportEvent,
        )

        assert not issubclass(NetworkWebSocketUpgradeExportEvent, BaseEvent)

    def test_no_body_ciphertext_field(self):
        from screencap.engine.events import NetworkWebSocketUpgradeExportEvent

        assert (
            "body_ciphertext" not in NetworkWebSocketUpgradeExportEvent.model_fields
        )
        assert "body_text" in NetworkWebSocketUpgradeExportEvent.model_fields

    def test_json_round_trip(self):
        from screencap.engine.events import (
            EventType,
            NetworkWebSocketUpgradeExportEvent,
        )

        event = NetworkWebSocketUpgradeExportEvent(
            timestamp=3.0,
            timestamp_ns=3_000_000_000,
            flow_id="ws-1",
            url="wss://chat.example.com/socket",
            host="chat.example.com",
            headers=[("Upgrade", "websocket")],
            details_json={"request_headers": [["Sec-WebSocket-Key", "x=="]]},
            body_text="hello",
        )
        restored = NetworkWebSocketUpgradeExportEvent.model_validate_json(
            event.model_dump_json()
        )
        assert restored == event
        assert restored.body_text == "hello"
        assert restored.type == EventType.NETWORK_WS_UPGRADE


class TestNetworkWebSocketFrameExportEvent:
    """V1.5 export-side WS frame event."""

    def test_not_a_base_event(self):
        from screencap.engine.events import (
            BaseEvent,
            NetworkWebSocketFrameExportEvent,
        )

        assert not issubclass(NetworkWebSocketFrameExportEvent, BaseEvent)

    def test_no_body_ciphertext_field(self):
        from screencap.engine.events import NetworkWebSocketFrameExportEvent

        assert (
            "body_ciphertext" not in NetworkWebSocketFrameExportEvent.model_fields
        )
        assert "body_text" in NetworkWebSocketFrameExportEvent.model_fields

    def test_json_round_trip(self):
        from screencap.engine.events import (
            EventType,
            NetworkWebSocketFrameExportEvent,
        )

        event = NetworkWebSocketFrameExportEvent(
            timestamp=4.0,
            timestamp_ns=4_000_000_000,
            flow_id="ws-1",
            host="chat.example.com",
            direction="received",
            frame_type="text",
            body_size=11,
            body_text="hello world",
        )
        restored = NetworkWebSocketFrameExportEvent.model_validate_json(
            event.model_dump_json()
        )
        assert restored == event
        assert restored.body_text == "hello world"
        assert restored.type == EventType.NETWORK_WS_FRAME


class TestNetworkExportEventsNotInRegistry:
    """Locks export-side classes OUT of EVENT_TYPE_MAP.

    Capture-side classes (NetworkRequestEvent etc.) are the registered
    `type` discriminators in EVENT_TYPE_MAP. Export-side classes share
    the same `type` literal value but must NEVER be in the map - they
    are bare BaseModel so they pickup at construction time only when
    NetworkScrubPipeline explicitly instantiates them.
    """

    def test_export_classes_not_in_event_type_map(self):
        from screencap.engine.events import (
            EVENT_TYPE_MAP,
            EventType,
            NetworkRequestEvent,
            NetworkRequestExportEvent,
            NetworkResponseEvent,
            NetworkResponseExportEvent,
            NetworkWebSocketFrameEvent,
            NetworkWebSocketFrameExportEvent,
            NetworkWebSocketUpgradeEvent,
            NetworkWebSocketUpgradeExportEvent,
        )

        # Capture-side classes ARE registered
        assert EVENT_TYPE_MAP[EventType.NETWORK_REQUEST.value] is NetworkRequestEvent
        assert EVENT_TYPE_MAP[EventType.NETWORK_RESPONSE.value] is NetworkResponseEvent
        assert (
            EVENT_TYPE_MAP[EventType.NETWORK_WS_UPGRADE.value]
            is NetworkWebSocketUpgradeEvent
        )
        assert (
            EVENT_TYPE_MAP[EventType.NETWORK_WS_FRAME.value]
            is NetworkWebSocketFrameEvent
        )
        # Export-side classes are NOT registered (and must not collide)
        for export_cls in (
            NetworkRequestExportEvent,
            NetworkResponseExportEvent,
            NetworkWebSocketUpgradeExportEvent,
            NetworkWebSocketFrameExportEvent,
        ):
            assert export_cls not in EVENT_TYPE_MAP.values(), (
                f"{export_cls.__name__} must NOT be in EVENT_TYPE_MAP - "
                "would collide with the matching capture-side class."
            )
