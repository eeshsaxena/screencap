"""Event-type registry for screencap engine events.

The legacy ``capture.db`` storage layer has been removed; this module now
exposes only ``EVENT_TYPE_MAP``, which maps event ``type`` strings to their
corresponding Pydantic event classes. The map is kept (and exercised by
``TestEventTypeMap``) so that any new ``BaseEvent`` subclass added without a
registry entry is caught by the test suite rather than silently dropped on
deserialization.
"""

from __future__ import annotations

from screencap.engine.events import (
    AudioChunkEvent,
    Event,
    EventType,
    KeyDownEvent,
    KeyShortcutEvent,
    KeyTypeEvent,
    KeyUpEvent,
    MouseClickEvent,
    MouseDoubleClickEvent,
    MouseDownEvent,
    MouseDragEvent,
    MouseMagnifyEvent,
    MouseMoveEvent,
    MouseRotateEvent,
    MouseScrollEvent,
    MouseSmartMagnifyEvent,
    MouseUpEvent,
    ScreenFrameEvent,
    SpecialKeyEvent,
    WindowStateEvent,
    WindowSwitchEvent,
)


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
}


__all__ = ["EVENT_TYPE_MAP"]
