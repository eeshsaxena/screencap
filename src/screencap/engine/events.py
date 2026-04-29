"""Event schemas for GUI interaction capture.

This module defines Pydantic models for all event types captured during
GUI interaction recording. Events are designed to follow the legacy
battle-tested implementation.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, computed_field


class EventType(str, Enum):
    """Event type identifiers."""

    # Mouse events (raw)
    MOUSE_MOVE = "mouse.move"
    MOUSE_DOWN = "mouse.down"
    MOUSE_UP = "mouse.up"
    MOUSE_SCROLL = "mouse.scroll"
    MOUSE_MAGNIFY = "mouse.magnify"
    MOUSE_ROTATE = "mouse.rotate"
    MOUSE_SMART_MAGNIFY = "mouse.smart_magnify"

    # Keyboard events (raw)
    KEY_DOWN = "key.down"
    KEY_UP = "key.up"

    # Screen events
    SCREEN_FRAME = "screen.frame"

    # Audio events
    AUDIO_CHUNK = "audio.chunk"

    # Window events
    WINDOW_STATE = "window.state"
    WINDOW_SWITCH = "window.switch"

    # Derived events (from post-processing)
    MOUSE_CLICK = "mouse.click"
    MOUSE_SINGLECLICK = "mouse.singleclick"
    MOUSE_DOUBLECLICK = "mouse.doubleclick"
    MOUSE_DRAG = "mouse.drag"
    KEY_TYPE = "key.type"
    KEY_SHORTCUT = "key.shortcut"
    KEY_SPECIAL = "key.special"

    # Network events (V1 - metadata-only schema)
    NETWORK_REQUEST = "network.request"
    NETWORK_RESPONSE = "network.response"
    NETWORK_WS_UPGRADE = "network.ws_upgrade"
    NETWORK_WS_FRAME = "network.ws_frame"
    NETWORK_DROP_BURST = "network.drop_burst"


class MouseButton(str, Enum):
    """Mouse button names."""

    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class BaseEvent(BaseModel):
    """Base class for all events.

    All events have a timestamp and type. This mirrors the legacy
    Event namedtuple: Event = namedtuple("Event", ("timestamp", "type", "data"))
    """

    timestamp: float = Field(description="Unix timestamp in seconds (float for sub-ms precision)")
    type: EventType = Field(description="Event type identifier")

    model_config = {"use_enum_values": True}


# =============================================================================
# Mouse Events
# =============================================================================


class MouseMoveEvent(BaseEvent):
    """Mouse cursor movement event.

    Corresponds to legacy ActionEvent with name="move".
    """

    type: Literal[EventType.MOUSE_MOVE] = EventType.MOUSE_MOVE
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    pressure: float | None = Field(default=None, description="Pressure 0.0-1.0, None if unsupported")
    modifier_flags: int | None = Field(default=None, description="Active modifier bitmask from CGEventGetFlags")
    path: list[tuple[float, float]] = Field(
        default_factory=list,
        description="All (x, y) waypoints. Single-element for unmerged moves, "
                    "multi-element when consecutive moves are merged.",
    )
    last_timestamp: float | None = Field(
        default=None,
        description=(
            "End timestamp for merged consecutive moves. None for unmerged moves "
            "(timestamp is both start and end). Set by merge_consecutive_mouse_move_events "
            "when buffer length > 1. Used by the scrub layer to detect blocked-interval "
            "intersection across the merged span."
        ),
    )


class MouseDownEvent(BaseEvent):
    """Mouse button press event.

    Corresponds to legacy ActionEvent with name="click" and mouse_pressed=True.
    """

    type: Literal[EventType.MOUSE_DOWN] = EventType.MOUSE_DOWN
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: MouseButton = Field(description="Mouse button name")
    pressure: float | None = Field(default=None, description="Pressure 0.0-1.0, None if unsupported")
    modifier_flags: int | None = Field(default=None, description="Active modifier bitmask from CGEventGetFlags")


class MouseUpEvent(BaseEvent):
    """Mouse button release event.

    Corresponds to legacy ActionEvent with name="click" and mouse_pressed=False.
    """

    type: Literal[EventType.MOUSE_UP] = EventType.MOUSE_UP
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: MouseButton = Field(description="Mouse button name")
    pressure: float | None = Field(default=None, description="Pressure 0.0-1.0, None if unsupported")
    modifier_flags: int | None = Field(default=None, description="Active modifier bitmask from CGEventGetFlags")


class MouseScrollEvent(BaseEvent):
    """Mouse scroll wheel event.

    Corresponds to legacy ActionEvent with name="scroll".
    """

    type: Literal[EventType.MOUSE_SCROLL] = EventType.MOUSE_SCROLL
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    dx: float = Field(description="Horizontal scroll delta")
    dy: float = Field(description="Vertical scroll delta")
    modifier_flags: int | None = Field(default=None, description="Active modifier bitmask from CGEventGetFlags")
    scroll_phase: int | None = Field(default=None, description="Scroll phase (1=began, 2=changed, 128=ended)")
    momentum_phase: int | None = Field(default=None, description="Momentum phase (0=none, 1=begin, 2=continue, 3=end)")
    is_continuous: bool | None = Field(default=None, description="True=trackpad continuous, False=mouse wheel discrete")


class MouseMagnifyEvent(BaseEvent):
    """Trackpad pinch-to-zoom gesture event.

    Corresponds to macOS NSEventTypeMagnify (type 30).
    magnification is a per-event delta (e.g., 0.05 = 5% zoom increase).
    """

    type: Literal[EventType.MOUSE_MAGNIFY] = EventType.MOUSE_MAGNIFY
    x: float = Field(description="Cursor X position in pixels")
    y: float = Field(description="Cursor Y position in pixels")
    magnification: float = Field(description="Magnification delta per event")


class MouseRotateEvent(BaseEvent):
    """Trackpad two-finger rotation gesture event.

    Corresponds to macOS NSEventTypeRotate (type 18).
    rotation is in degrees, positive = counter-clockwise.
    """

    type: Literal[EventType.MOUSE_ROTATE] = EventType.MOUSE_ROTATE
    x: float = Field(description="Cursor X position in pixels")
    y: float = Field(description="Cursor Y position in pixels")
    rotation: float = Field(description="Rotation in degrees, positive = CCW")


class MouseSmartMagnifyEvent(BaseEvent):
    """Two-finger double-tap zoom toggle (macOS SmartMagnify).

    Corresponds to macOS NSEventTypeSmartMagnify (type 32).
    This is a discrete toggle event - it zooms to fit a region
    then zooms back out. No delta or direction is available.
    """

    type: Literal[EventType.MOUSE_SMART_MAGNIFY] = EventType.MOUSE_SMART_MAGNIFY
    x: float = Field(description="Cursor X position in pixels")
    y: float = Field(description="Cursor Y position in pixels")


# =============================================================================
# Keyboard Events
# =============================================================================


class KeyDownEvent(BaseEvent):
    """Keyboard key press event.

    Corresponds to legacy ActionEvent with name="press".
    """

    type: Literal[EventType.KEY_DOWN] = EventType.KEY_DOWN
    key_name: str | None = Field(default=None, description="Key name (e.g., 'shift', 'ctrl')")
    key_char: str | None = Field(default=None, description="Character typed (e.g., 'a', '1')")
    key_vk: str | None = Field(default=None, description="Virtual key code")
    canonical_key_name: str | None = Field(default=None, description="Canonical key name")
    canonical_key_char: str | None = Field(default=None, description="Canonical character")
    canonical_key_vk: str | None = Field(default=None, description="Canonical virtual key code")


class KeyUpEvent(BaseEvent):
    """Keyboard key release event.

    Corresponds to legacy ActionEvent with name="release".
    """

    type: Literal[EventType.KEY_UP] = EventType.KEY_UP
    key_name: str | None = Field(default=None, description="Key name")
    key_char: str | None = Field(default=None, description="Character")
    key_vk: str | None = Field(default=None, description="Virtual key code")
    canonical_key_name: str | None = Field(default=None, description="Canonical key name")
    canonical_key_char: str | None = Field(default=None, description="Canonical character")
    canonical_key_vk: str | None = Field(default=None, description="Canonical virtual key code")


# =============================================================================
# Screen Events
# =============================================================================


class ScreenFrameEvent(BaseEvent):
    """Screen capture event.

    References a frame in the video file or a screenshot image.
    """

    type: Literal[EventType.SCREEN_FRAME] = EventType.SCREEN_FRAME
    video_timestamp: float | None = Field(
        default=None, description="Timestamp within video file (seconds)"
    )
    image_path: str | None = Field(default=None, description="Path to screenshot image file")
    width: int = Field(description="Frame width in pixels")
    height: int = Field(description="Frame height in pixels")


# =============================================================================
# Audio Events
# =============================================================================


class AudioChunkEvent(BaseEvent):
    """Audio capture event.

    References a segment of the audio recording.
    """

    type: Literal[EventType.AUDIO_CHUNK] = EventType.AUDIO_CHUNK
    start_time: float = Field(description="Start time within audio file (seconds)")
    end_time: float = Field(description="End time within audio file (seconds)")
    transcription: str | None = Field(default=None, description="Transcribed text for this chunk")


# =============================================================================
# Window Events
# =============================================================================


class WindowStateEvent(BaseEvent):
    """Window state and accessibility tree data.

    Captures the active window's geometry, title, and optional
    accessibility tree state for UI element grounding.
    """

    type: Literal[EventType.WINDOW_STATE] = EventType.WINDOW_STATE
    title: str = Field(description="Window title")
    left: int = Field(description="Window left position in pixels")
    top: int = Field(description="Window top position in pixels")
    width: int = Field(description="Window width in pixels")
    height: int = Field(description="Window height in pixels")
    window_id: str | int = Field(description="Platform-specific window ID")
    state: dict | None = Field(default=None, description="Accessibility tree data")
    meta: dict | None = Field(default=None, description="Window metadata")


class WindowSwitchEvent(BaseEvent):
    """Standalone event emitted when the active app or window changes.

    Deduplicated by (app_bundle_id, window_id) - captures actual window
    switches, ignores title-only changes. Used in events.jsonl export.
    """

    type: Literal[EventType.WINDOW_SWITCH] = EventType.WINDOW_SWITCH
    app_name: str = Field(description="Human-readable app name")
    app_bundle_id: str | None = Field(default=None, description="e.g. com.apple.finder")
    window_title: str = Field(description="Full or redacted window title")
    window_id: str = Field(description="macOS CGWindowNumber")
    x: int = Field(description="Window left position")
    y: int = Field(description="Window top position")
    width: int = Field(description="Window width in pixels")
    height: int = Field(description="Window height in pixels")
    domain: str | None = Field(default=None, description="Hostname from browser URL")


# =============================================================================
# Derived Events (from post-processing)
# =============================================================================


class MouseClickEvent(BaseEvent):
    """Combined mouse click event (down + up).

    Corresponds to legacy ActionEvent with name="singleclick".
    Created by merge_consecutive_mouse_click_events().
    """

    type: Literal[EventType.MOUSE_SINGLECLICK] = EventType.MOUSE_SINGLECLICK
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: MouseButton = Field(description="Mouse button name")
    pressure: float | None = Field(default=None, description="Pressure from down event, 0.0-1.0")
    modifier_flags: int | None = Field(default=None, description="Modifier flags from down event")
    children: list[MouseDownEvent | MouseUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


class MouseDoubleClickEvent(BaseEvent):
    """Double click event.

    Corresponds to legacy ActionEvent with name="doubleclick".
    Created by merge_consecutive_mouse_click_events().
    """

    type: Literal[EventType.MOUSE_DOUBLECLICK] = EventType.MOUSE_DOUBLECLICK
    x: float = Field(description="Mouse X position in pixels")
    y: float = Field(description="Mouse Y position in pixels")
    button: MouseButton = Field(description="Mouse button name")
    pressure: float | None = Field(default=None, description="Pressure from first down event, 0.0-1.0")
    modifier_flags: int | None = Field(default=None, description="Modifier flags from first down event")
    children: list[MouseDownEvent | MouseUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


class MouseDragEvent(BaseEvent):
    """Mouse drag event (down + moves + up).

    Uses x/y for start position and dx/dy for displacement (like MouseScrollEvent).
    End position can be computed as (x + dx, y + dy).
    Pressure is always None at parent level; children preserve individual pressures.
    """

    type: Literal[EventType.MOUSE_DRAG] = EventType.MOUSE_DRAG
    x: float = Field(description="Starting X position in pixels")
    y: float = Field(description="Starting Y position in pixels")
    dx: float = Field(description="Horizontal displacement (end_x - start_x)")
    dy: float = Field(description="Vertical displacement (end_y - start_y)")
    button: MouseButton = Field(description="Mouse button name")
    pressure: float | None = Field(default=None, description="Always None; children have individual pressures")
    children: list[
        MouseDownEvent | MouseMoveEvent | MouseUpEvent
        | MouseScrollEvent | KeyDownEvent | KeyUpEvent | KeyTypeEvent
        | KeyShortcutEvent | SpecialKeyEvent
        | MouseMagnifyEvent | MouseRotateEvent | MouseSmartMagnifyEvent
    ] = Field(
        default_factory=list, description="Child events that were merged"
    )


class KeyTypeEvent(BaseEvent):
    """Sequence of typed characters.

    Corresponds to legacy ActionEvent with name="type".
    Created by merge_consecutive_keyboard_events().
    """

    type: Literal[EventType.KEY_TYPE] = EventType.KEY_TYPE
    text: str = Field(description="The typed text")
    children: list[KeyDownEvent | KeyUpEvent] = Field(
        default_factory=list, description="Child events that were merged"
    )


class KeyShortcutEvent(BaseEvent):
    """Keyboard shortcut (modifier + key combination).

    Created by detect_key_shortcuts() when a KeyTypeEvent
    contains modifier keys combined with a non-printable regular key,
    or Ctrl/Alt/Cmd combined with any key.

    Example: Ctrl+z -> keys=["ctrl", "z"], text="Ctrl+z"
    """

    type: Literal[EventType.KEY_SHORTCUT] = EventType.KEY_SHORTCUT
    keys: list[str] = Field(
        description="Canonically ordered key names, e.g. ['ctrl', 'z']"
    )
    children: list[KeyDownEvent | KeyUpEvent] = Field(
        default_factory=list,
        description="Child events that were merged",
    )

    @computed_field
    @property
    def text(self) -> str:
        """Human-readable combo, e.g. 'Ctrl+z'."""
        return "+".join(k.title() for k in self.keys[:-1]) + "+" + self.keys[-1]


class SpecialKeyEvent(BaseEvent):
    """Non-printable, non-modifier key event (media, function, etc.).

    Created by merge_consecutive_keyboard_events() when adjacent
    key-down/key-up pairs are detected for special keys.

    Example: Play/Pause -> key_name="media_play_pause", text="Play/Pause"
    Example: F5 -> key_name="f5", text="F5"
    """

    type: Literal[EventType.KEY_SPECIAL] = EventType.KEY_SPECIAL
    key_name: str = Field(description="Canonical key name, e.g. 'media_play_pause', 'f5'")
    children: list[KeyDownEvent | KeyUpEvent] = Field(default_factory=list)

    @computed_field
    @property
    def text(self) -> str:
        """Human-readable display name."""
        if self.key_name.startswith("media_"):
            return self.key_name.removeprefix("media_").replace("_", "/").title()
        if self.key_name.startswith("brightness_"):
            return self.key_name.replace("_", " ").title()
        return self.key_name.upper()


# =============================================================================
# Network Events (V1 - metadata-only schema)
# =============================================================================
#
# Single Pydantic class per kind. NO body_ciphertext / body_nonce / body_aad /
# body_text fields in V1 - those are V1.5 (which introduces a dual capture/export
# class family to keep ciphertext from leaking to JSONL by construction).
#
# body_sha256_hex is a lowercase hex string (NOT raw bytes) - Pydantic 2's
# default JSON serializer UTF-8-decodes raw bytes which fails for binary
# digests. The DB column stays LargeBinary(32); convert.dict_to_network_event
# calls .hex() to populate this field.
#
# headers is a list of (name, value) tuples (NOT a dict) - preserves multi-value
# headers like duplicate Set-Cookie which a JSON dict cannot represent.


class NetworkRequestEvent(BaseEvent):
    """HTTP request observed by the system proxy.

    Metadata only in V1: no body bytes are retained - the addon hashes
    each body and discards the bytes. body_size is the observed Content-Length
    (or counted streamed bytes); body_sha256_hex is the hex digest if the
    body was hashed; otherwise both are None.
    """

    type: Literal[EventType.NETWORK_REQUEST] = EventType.NETWORK_REQUEST
    timestamp_ns: int = Field(description="High-precision sort key (time.time_ns())")
    flow_id: str = Field(description="mitmproxy flow id (correlates request/response/ws frames)")
    method: str = Field(description="HTTP method, e.g. 'GET', 'POST'")
    url: str = Field(description="Full request URL")
    host: str = Field(description="Request host (lowercase)")
    headers: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Ordered name/value pairs (preserves multi-value headers)",
    )
    body_size: int | None = Field(
        default=None,
        description="Observed body size in bytes (None if unknown or no body)",
    )
    body_sha256_hex: str | None = Field(
        default=None,
        description="Lowercase hex of SHA-256(body), or None if no body / not hashed",
    )
    content_type: str | None = Field(default=None, description="Content-Type header value")
    http_version: str | None = Field(default=None, description="HTTP/1.1 / HTTP/2 / HTTP/3")


class NetworkResponseEvent(BaseEvent):
    """HTTP response observed by the system proxy."""

    type: Literal[EventType.NETWORK_RESPONSE] = EventType.NETWORK_RESPONSE
    timestamp_ns: int = Field(description="High-precision sort key (time.time_ns())")
    flow_id: str = Field(description="mitmproxy flow id (correlates with request)")
    host: str = Field(description="Request host (lowercase)")
    status: int = Field(description="HTTP status code")
    headers: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Ordered name/value pairs (preserves multi-value headers)",
    )
    body_size: int | None = Field(
        default=None,
        description="Observed body size in bytes (None if unknown or no body)",
    )
    body_sha256_hex: str | None = Field(
        default=None,
        description="Lowercase hex of SHA-256(body), or None if no body / not hashed",
    )
    content_type: str | None = Field(default=None, description="Content-Type header value")
    http_version: str | None = Field(default=None, description="HTTP/1.1 / HTTP/2 / HTTP/3")


class NetworkWebSocketUpgradeEvent(BaseEvent):
    """WebSocket upgrade (HTTP 101 Switching Protocols).

    `headers` carries the response headers (the 101 response). The request
    headers are stored in `details_json["request_headers"]` because this is
    the only event kind that needs both header sets in a single row.
    """

    type: Literal[EventType.NETWORK_WS_UPGRADE] = EventType.NETWORK_WS_UPGRADE
    timestamp_ns: int = Field(description="High-precision sort key (time.time_ns())")
    flow_id: str = Field(description="mitmproxy flow id")
    url: str = Field(description="WebSocket URL (ws:// or wss://)")
    host: str = Field(description="Host (lowercase)")
    status: int = Field(default=101, description="Upgrade status code (101)")
    headers: list[tuple[str, str]] = Field(
        default_factory=list,
        description="Response (101) headers",
    )
    http_version: str | None = Field(default=None, description="HTTP version of the upgrade")
    details_json: dict | None = Field(
        default=None,
        description="Carries request_headers for the upgrade: "
                    "{'request_headers': [[name, value], ...]}",
    )


class NetworkWebSocketFrameEvent(BaseEvent):
    """A single WebSocket frame (one event per frame).

    `direction` is "sent" (client to server) or "received" (server to client).
    `frame_type` is "text" or "binary" (control frames are not emitted as
    events in V1).
    """

    type: Literal[EventType.NETWORK_WS_FRAME] = EventType.NETWORK_WS_FRAME
    timestamp_ns: int = Field(description="High-precision sort key (time.time_ns())")
    flow_id: str = Field(description="mitmproxy flow id (correlates with upgrade)")
    host: str = Field(description="Host (lowercase)")
    direction: Literal["sent", "received"] = Field(
        description="'sent' = client->server, 'received' = server->client",
    )
    frame_type: Literal["text", "binary"] = Field(description="WebSocket frame type")
    body_size: int | None = Field(
        default=None,
        description="Observed frame payload size in bytes",
    )
    body_sha256_hex: str | None = Field(
        default=None,
        description="Lowercase hex of SHA-256(payload), or None if not hashed",
    )


class NetworkDropBurstEvent(BaseEvent):
    """A burst of network events were dropped due to backpressure.

    Emitted by either the addon (proxy -> reader queue full) or the
    reader thread (reader -> writer queue full). `flow_id` is None
    because a drop burst is not tied to a single flow.

    `details_json` carries the full payload:
        {
            "dropped_count": int,
            "hosts_affected": [str, ...],
            "source": "addon" | "reader",
        }
    """

    type: Literal[EventType.NETWORK_DROP_BURST] = EventType.NETWORK_DROP_BURST
    timestamp_ns: int = Field(description="High-precision sort key (time.time_ns())")
    details_json: dict = Field(
        description="{'dropped_count': int, 'hosts_affected': [...], "
                    "'source': 'addon'|'reader'}",
    )


# =============================================================================
# Network sideband (control-only - NOT a BaseEvent)
# =============================================================================


class NetworkPinFailureEvent(BaseModel):
    """Control-only sideband message for cert-pinning failures.

    NOT a BaseEvent - does not have a `type` field, is not in EventType,
    is not in EVENT_TYPE_MAP, is not persisted to network_event, and never
    crosses an export boundary. Defined here purely for type-safety on
    the proxy -> reader thread channel so the reader can `isinstance` check
    before dispatching.

    The `TestEventTypeMap` at tests/engine/test_storage.py walks every
    concrete BaseEvent subclass and requires registration in
    EVENT_TYPE_MAP. Subclassing BaseEvent here would break that test -
    a guard test in tests/engine/test_events.py asserts this stays a
    bare BaseModel.
    """

    timestamp: float = Field(description="Unix timestamp of failure")
    timestamp_ns: int = Field(description="High-precision sort key")
    host: str = Field(description="Host that failed pinning")
    reason: str = Field(description="Short human-readable reason")


# =============================================================================
# Union type for all events
# =============================================================================

ActionEvent = (
    MouseMoveEvent
    | MouseDownEvent
    | MouseUpEvent
    | MouseScrollEvent
    | MouseMagnifyEvent
    | MouseRotateEvent
    | MouseSmartMagnifyEvent
    | KeyDownEvent
    | KeyUpEvent
    | MouseClickEvent
    | MouseDoubleClickEvent
    | MouseDragEvent
    | KeyTypeEvent
    | KeyShortcutEvent
    | SpecialKeyEvent
)

ScreenEvent = ScreenFrameEvent

AudioEvent = AudioChunkEvent

WindowEvent = WindowStateEvent | WindowSwitchEvent

NetworkEvent = (
    NetworkRequestEvent
    | NetworkResponseEvent
    | NetworkWebSocketUpgradeEvent
    | NetworkWebSocketFrameEvent
    | NetworkDropBurstEvent
)

Event = ActionEvent | ScreenEvent | AudioEvent | WindowEvent | NetworkEvent


# =============================================================================
# Event-type registry
# =============================================================================
#
# Canonical mapping from event ``type`` string discriminators to their Pydantic
# event classes. Test suite locks completeness via ``TestEventTypeMap`` so any
# new ``BaseEvent`` subclass added without a registry entry fails loudly
# instead of silently dropping on deserialization.

EVENT_TYPE_MAP: dict[str, type[Event]] = {
    EventType.MOUSE_MOVE.value: MouseMoveEvent,
    EventType.MOUSE_DOWN.value: MouseDownEvent,
    EventType.MOUSE_UP.value: MouseUpEvent,
    EventType.MOUSE_SCROLL.value: MouseScrollEvent,
    EventType.MOUSE_MAGNIFY.value: MouseMagnifyEvent,
    EventType.MOUSE_ROTATE.value: MouseRotateEvent,
    EventType.MOUSE_SMART_MAGNIFY.value: MouseSmartMagnifyEvent,
    EventType.KEY_DOWN.value: KeyDownEvent,
    EventType.KEY_UP.value: KeyUpEvent,
    EventType.SCREEN_FRAME.value: ScreenFrameEvent,
    EventType.AUDIO_CHUNK.value: AudioChunkEvent,
    EventType.MOUSE_SINGLECLICK.value: MouseClickEvent,
    EventType.MOUSE_DOUBLECLICK.value: MouseDoubleClickEvent,
    EventType.MOUSE_DRAG.value: MouseDragEvent,
    EventType.KEY_TYPE.value: KeyTypeEvent,
    EventType.KEY_SHORTCUT.value: KeyShortcutEvent,
    EventType.KEY_SPECIAL.value: SpecialKeyEvent,
    EventType.WINDOW_STATE.value: WindowStateEvent,
    EventType.WINDOW_SWITCH.value: WindowSwitchEvent,
    EventType.NETWORK_REQUEST.value: NetworkRequestEvent,
    EventType.NETWORK_RESPONSE.value: NetworkResponseEvent,
    EventType.NETWORK_WS_UPGRADE.value: NetworkWebSocketUpgradeEvent,
    EventType.NETWORK_WS_FRAME.value: NetworkWebSocketFrameEvent,
    EventType.NETWORK_DROP_BURST.value: NetworkDropBurstEvent,
}
