"""Event processing pipeline for merging raw events into higher-level actions.

This module ports legacy event processing functions to work with
the screencap engine Pydantic event models.
"""

from __future__ import annotations

import os
import re
from typing import Any, TypeVar

from screencap.engine.events import (
    ActionEvent,
    BaseEvent,
    Event,
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
    SpecialKeyEvent,
    WindowSwitchEvent,
)

# Type variable for event types
E = TypeVar("E", bound=Event)

# =============================================================================
# Configuration
# =============================================================================

# Thresholds for event merging
MOUSE_MOVE_MERGE_DISTANCE_THRESHOLD = 1  # pixels
MOUSE_MOVE_MERGE_MIN_IDX_DELTA = 5  # minimum events between groups
DOUBLE_CLICK_INTERVAL_SECONDS = 0.5  # default double-click interval
DOUBLE_CLICK_DISTANCE_PIXELS = 5.0  # default double-click distance
KEY_TYPE_MERGE_INTERVAL_SECONDS = 0.5  # merge adjacent KeyTypeEvents within this interval

# Drag distance threshold (pixels). Down/up pairs within this distance are clicks, beyond are drags.
# Default 3px matches macOS HIG and precision graphics apps (Inkscape=4px, Windows=4px).
# Override via SCREENCAP_DRAG_THRESHOLD env var.
DRAG_DISTANCE_THRESHOLD = float(os.environ.get("SCREENCAP_DRAG_THRESHOLD", "3.0"))


# =============================================================================
# Event Processing Functions
# =============================================================================


def process_events(
    events: list[ActionEvent],
    double_click_interval: float = DOUBLE_CLICK_INTERVAL_SECONDS,
    double_click_distance: float = DOUBLE_CLICK_DISTANCE_PIXELS,
    key_type_merge_interval: float = KEY_TYPE_MERGE_INTERVAL_SECONDS,
) -> list[ActionEvent]:
    """Process raw events through the full pipeline.

    Applies all processing functions in sequence:
    1. Remove invalid keyboard events
    2. Remove redundant mouse move events
    3. Merge consecutive keyboard events → KeyTypeEvent
    3.5. Detect keyboard shortcuts → KeyShortcutEvent
    3.75. Merge sequential key type events → word-level KeyTypeEvent
    4. Merge consecutive mouse move events
    5. Merge consecutive mouse scroll events
    6. Merge consecutive mouse magnify events
    7. Merge consecutive mouse rotate events
    8. Merge consecutive mouse click events → MouseClickEvent/MouseDoubleClickEvent
    9. Detect drag events → MouseDragEvent

    Args:
        events: Raw action events.
        double_click_interval: Time threshold for double-click detection (seconds).
        double_click_distance: Distance threshold for double-click detection (pixels).
        key_type_merge_interval: Time threshold for merging sequential KeyTypeEvents (seconds).

    Returns:
        Processed events with merged actions.
    """
    # Apply processing pipeline
    events = remove_invalid_keyboard_events(events)
    events = remove_redundant_mouse_move_events(events)
    events = merge_consecutive_keyboard_events(events)
    events = detect_key_shortcuts(events)
    events = merge_sequential_key_type_events(events, interval=key_type_merge_interval)
    events = merge_consecutive_mouse_move_events(events)
    events = merge_consecutive_mouse_scroll_events(events)
    events = merge_consecutive_mouse_magnify_events(events)
    events = merge_consecutive_mouse_rotate_events(events)
    events = merge_consecutive_mouse_click_events(
        events,
        double_click_interval=double_click_interval,
        double_click_distance=double_click_distance,
    )
    events = detect_drag_events(events)

    return events


def remove_invalid_keyboard_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Remove invalid keyboard events (e.g., invalid key codes).

    Args:
        events: List of events.

    Returns:
        Filtered list of events.
    """
    valid_events = []
    for event in events:
        if isinstance(event, (KeyDownEvent, KeyUpEvent)):
            # Filter out events with no key information
            if not any([event.key_name, event.key_char, event.key_vk]):
                continue
        valid_events.append(event)
    return valid_events


def remove_redundant_mouse_move_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Remove mouse move events that don't change position.

    Args:
        events: List of events.

    Returns:
        Filtered list with redundant moves removed.
    """

    def is_same_position(e1: MouseMoveEvent, e2: MouseMoveEvent) -> bool:
        # Holding a stylus still while varying pressure is NOT redundant
        return e1.x == e2.x and e1.y == e2.y and e1.pressure == e2.pressure and e1.modifier_flags == e2.modifier_flags

    result = []
    prev_move: MouseMoveEvent | None = None

    for event in events:
        if isinstance(event, MouseMoveEvent):
            if prev_move is not None and is_same_position(prev_move, event):
                # Skip redundant move
                continue
            prev_move = event
        result.append(event)

    return result


_MODIFIER_NAMES = {
    "shift", "shift_l", "shift_r",
    "ctrl", "ctrl_l", "ctrl_r",
    "alt", "alt_l", "alt_r", "alt_gr",
    "cmd", "cmd_l", "cmd_r",
}

_FUNCTION_KEY_RE = re.compile(r"^f\d+$")

# Prefixes/patterns that identify truly special (non-editing) keys.
_SPECIAL_KEY_PREFIXES = ("media_", "brightness_")
_SPECIAL_KEY_NAMES = {
    "eject", "fn", "insert", "print_screen", "scroll_lock", "pause",
    "num_lock", "menu",
}


def _is_special_key(event: KeyDownEvent | KeyUpEvent) -> bool:
    """Media key, function key, or system key (NOT common editing keys).

    Positively matches only:
    - Media keys: key_name starts with "media_" or "brightness_"
    - Function keys: key_name matches f1..f20
    - System keys: eject, fn, insert, print_screen, etc.

    Does NOT match common editing/navigation keys like space, backspace,
    enter, tab, esc, arrows, delete, home, end, page_up, page_down.
    """
    if not event.key_name or event.key_char is not None:
        return False
    canonical = (event.canonical_key_name or event.key_name).lower()
    if any(canonical.startswith(p) for p in _SPECIAL_KEY_PREFIXES):
        return True
    if _FUNCTION_KEY_RE.match(canonical):
        return True
    return canonical in _SPECIAL_KEY_NAMES


def merge_consecutive_keyboard_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Merge consecutive keyboard events into KeyTypeEvent or SpecialKeyEvent.

    Groups key press/release sequences into typed text.
    Special keys (media, function, non-printable non-modifier) are wrapped
    into SpecialKeyEvent when their down+up are immediately adjacent.

    Non-keyboard events (mouse moves, clicks, scrolls) are emitted inline.
    However, the keyboard buffer is only flushed when no keys are physically
    held (pressed_keys is empty). This preserves shortcuts like Cmd+C even
    when mouse events interleave between the modifier key-down and the
    letter key-down.

    Args:
        events: List of events.

    Returns:
        Events with keyboard sequences merged into KeyTypeEvent/SpecialKeyEvent.
    """
    result = []
    keyboard_buffer: list[KeyDownEvent | KeyUpEvent] = []
    pressed_keys: set[str] = set()

    def flush_buffer() -> None:
        """Convert buffer to KeyTypeEvent and add to result."""
        if not keyboard_buffer:
            return

        # Extract typed characters from press events
        chars = []
        for event in keyboard_buffer:
            if isinstance(event, KeyDownEvent) and event.key_char:
                chars.append(event.key_char)

        text = "".join(chars)
        if text or keyboard_buffer:
            first_event = keyboard_buffer[0]
            type_event = KeyTypeEvent(
                timestamp=first_event.timestamp,
                text=text,
                children=list(keyboard_buffer),
            )
            result.append(type_event)

        keyboard_buffer.clear()
        pressed_keys.clear()

    # Pending special key down waiting for an adjacent up to pair with
    pending_special_down: KeyDownEvent | None = None

    def emit_pending_special_down() -> None:
        """Emit pending special key down as a raw event (no paired up found)."""
        nonlocal pending_special_down
        if pending_special_down is not None:
            result.append(pending_special_down)
            pending_special_down = None

    for event in events:
        if isinstance(event, KeyDownEvent) and _is_special_key(event):
            if pressed_keys:
                # Modifiers are held — let the special key go into the buffer
                # so it becomes part of a KeyTypeEvent → KeyShortcutEvent
                keyboard_buffer.append(event)
                key_id = event.key_name or event.key_char or event.key_vk or ""
                pressed_keys.add(key_id)
            else:
                # No modifiers held — flush buffer, start pairing
                flush_buffer()
                emit_pending_special_down()
                pending_special_down = event

        elif isinstance(event, KeyUpEvent) and _is_special_key(event):
            if pressed_keys:
                # Modifiers held — let the up go into the buffer
                key_id = event.key_name or event.key_char or event.key_vk or ""
                pressed_keys.discard(key_id)
                keyboard_buffer.append(event)
                if not pressed_keys:
                    flush_buffer()
            elif pending_special_down is not None:
                # Check if this up matches the pending down
                down_id = pending_special_down.key_name or pending_special_down.key_char or pending_special_down.key_vk or ""
                up_id = event.key_name or event.key_char or event.key_vk or ""
                if down_id == up_id:
                    # Adjacent pair — wrap into SpecialKeyEvent
                    key_name = pending_special_down.canonical_key_name or pending_special_down.key_name or ""
                    result.append(SpecialKeyEvent(
                        timestamp=pending_special_down.timestamp,
                        key_name=key_name,
                        children=[pending_special_down, event],
                    ))
                    pending_special_down = None
                else:
                    # Mismatched — emit both as raw
                    emit_pending_special_down()
                    result.append(event)
            else:
                # Orphan up — emit as raw
                result.append(event)

        elif isinstance(event, KeyDownEvent):
            emit_pending_special_down()
            key_id = event.key_name or event.key_char or event.key_vk or ""
            pressed_keys.add(key_id)
            keyboard_buffer.append(event)

        elif isinstance(event, KeyUpEvent):
            emit_pending_special_down()
            key_id = event.key_name or event.key_char or event.key_vk or ""
            pressed_keys.discard(key_id)
            keyboard_buffer.append(event)

            # If no keys pressed, flush the buffer
            if not pressed_keys:
                flush_buffer()
        else:
            # Non-keyboard event breaks special key adjacency
            emit_pending_special_down()
            # Only flush keyboard buffer if no keys are currently held
            # (preserves shortcuts across interleaved mouse events)
            if not pressed_keys:
                flush_buffer()
            result.append(event)

    # Flush any remaining state
    emit_pending_special_down()
    flush_buffer()

    return result


# =============================================================================
# Keyboard Shortcut Detection
# =============================================================================

# Modifier key variants → canonical name
MODIFIER_MAP = {
    "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
    "cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd",
}

# Canonical modifier → sort position
MODIFIER_ORDER = {"ctrl": 0, "alt": 1, "shift": 2, "cmd": 3}


def detect_key_shortcuts(events: list[ActionEvent]) -> list[ActionEvent]:
    """Convert KeyTypeEvent with modifiers into KeyShortcutEvent.

    Args:
        events: List of events (should already have keyboard events merged).

    Returns:
        Events with shortcuts converted from KeyTypeEvent to KeyShortcutEvent.
    """
    result = []
    for event in events:
        if isinstance(event, KeyTypeEvent):
            shortcut = _try_convert_to_shortcut(event)
            result.append(shortcut if shortcut else event)
        else:
            result.append(event)
    return result


def _try_convert_to_shortcut(event: KeyTypeEvent) -> KeyShortcutEvent | None:
    """Check if a KeyTypeEvent is actually a keyboard shortcut.

    Args:
        event: A KeyTypeEvent to check.

    Returns:
        KeyShortcutEvent if it's a shortcut, None otherwise.
    """
    modifiers_found: set[str] = set()
    regular_key: str | None = None

    for child in event.children:
        if not isinstance(child, KeyDownEvent):
            continue
        key_id = child.key_name or child.key_char or child.key_vk or ""
        if key_id in MODIFIER_MAP:
            modifiers_found.add(MODIFIER_MAP[key_id])
        elif regular_key is None:
            regular_key = key_id

    if not modifiers_found or regular_key is None:
        return None  # No modifiers, or modifier-only combo

    # Shift+printable = typing, not shortcut
    # (unless Ctrl/Alt/Cmd is also held)
    if not (modifiers_found - {"shift"}):
        # Only Shift — check if the regular key is printable
        if len(regular_key) == 1 and regular_key.isprintable():
            return None  # Shift+A = typing "A"

    # Build canonical keys list
    sorted_mods = sorted(modifiers_found, key=lambda m: MODIFIER_ORDER.get(m, 99))
    keys = sorted_mods + [regular_key]

    return KeyShortcutEvent(
        timestamp=event.timestamp,
        keys=keys,
        children=list(event.children),
    )


def merge_sequential_key_type_events(
    events: list[ActionEvent],
    interval: float = KEY_TYPE_MERGE_INTERVAL_SECONDS,
) -> list[ActionEvent]:
    """Merge consecutive KeyTypeEvents into word-level events.

    Groups sequential KeyTypeEvents whose inter-event gap is below
    `interval` seconds. Flushes on word boundaries (space, Enter),
    non-printable keys (Backspace, Tab, etc.), non-KeyTypeEvents,
    and time gaps exceeding the threshold.

    Args:
        events: List of action events (post shortcut-detection).
        interval: Maximum gap in seconds between KeyTypeEvents to merge.

    Returns:
        New list with consecutive KeyTypeEvents merged into word-level events.
    """
    result: list[ActionEvent] = []
    buffer: list[KeyTypeEvent] = []

    def flush_buffer() -> None:
        if not buffer:
            return
        if len(buffer) == 1:
            result.append(buffer[0])
        else:
            text = "".join(e.text for e in buffer)
            children = []
            for e in buffer:
                children.extend(e.children)
            result.append(KeyTypeEvent(
                timestamp=buffer[0].timestamp,
                text=text,
                children=children,
            ))
        buffer.clear()

    def _is_whitespace(event: KeyTypeEvent) -> bool:
        """Whitespace text (space, newline, tab, etc.) — flush buffer, emit standalone."""
        return bool(event.text) and event.text.strip() == ""

    def _is_nonprintable(event: KeyTypeEvent) -> bool:
        """Empty text with a non-modifier key_name (Backspace, Enter, Tab, Escape, etc.)."""
        if event.text:
            return False
        for child in event.children:
            if isinstance(child, KeyDownEvent) and child.key_name:
                return True
        return False

    for event in events:
        if not isinstance(event, KeyTypeEvent):
            flush_buffer()
            result.append(event)
            continue

        # Whitespace boundaries flush and are emitted standalone
        if _is_whitespace(event):
            flush_buffer()
            result.append(event)
            continue

        # Non-printable keys flush and are emitted standalone
        if _is_nonprintable(event):
            flush_buffer()
            result.append(event)
            continue

        # Time gap check
        if buffer and (event.timestamp - buffer[-1].timestamp) >= interval:
            flush_buffer()

        buffer.append(event)

    flush_buffer()
    return result


def merge_consecutive_mouse_move_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Merge consecutive mouse move events.

    Reduces move event density while preserving important positions.

    Args:
        events: List of events.

    Returns:
        Events with mouse moves merged.
    """
    result = []
    move_buffer: list[MouseMoveEvent] = []

    def flush_buffer() -> None:
        """Merge move buffer and add to result."""
        if not move_buffer:
            return

        if len(move_buffer) == 1:
            ev = move_buffer[0]
            # Always populate path, even for single moves
            if not ev.path:
                ev = ev.model_copy(update={"path": [(ev.x, ev.y)]})
            result.append(ev)
        else:
            # Create merged move event with final position and last pressure.
            # ``last_timestamp`` carries the end of the merged span so the
            # scrub layer can detect blocked-interval intersection across the
            # full [first, last] window (a run of moves around a window
            # switch into a MASK_WINDOW app would otherwise leak coordinates
            # via ``timestamp`` alone — pre-fix, the start timestamp lands
            # before the blocked interval and slips past the point-lookup
            # ``find_blocked_interval`` check).
            first = move_buffer[0]
            last = move_buffer[-1]
            path = [(m.x, m.y) for m in move_buffer]
            merged = MouseMoveEvent(
                timestamp=first.timestamp,
                last_timestamp=last.timestamp,
                x=last.x,
                y=last.y,
                pressure=last.pressure,
                modifier_flags=last.modifier_flags,
                path=path,
            )
            result.append(merged)

        move_buffer.clear()

    for event in events:
        if isinstance(event, MouseMoveEvent):
            move_buffer.append(event)
        else:
            flush_buffer()
            result.append(event)

    flush_buffer()

    return result


def merge_consecutive_mouse_scroll_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Merge consecutive mouse scroll events.

    Combines scroll deltas from consecutive scroll events.

    Args:
        events: List of events.

    Returns:
        Events with scrolls merged.
    """
    result = []
    scroll_buffer: list[MouseScrollEvent] = []

    def flush_buffer() -> None:
        """Merge scroll buffer and add to result."""
        if not scroll_buffer:
            return

        if len(scroll_buffer) == 1:
            result.append(scroll_buffer[0])
        else:
            # Sum scroll deltas
            first = scroll_buffer[0]
            total_dx = sum(e.dx for e in scroll_buffer)
            total_dy = sum(e.dy for e in scroll_buffer)
            merged = MouseScrollEvent(
                timestamp=first.timestamp,
                x=first.x,
                y=first.y,
                dx=total_dx,
                dy=total_dy,
                modifier_flags=first.modifier_flags,
                scroll_phase=first.scroll_phase,
                momentum_phase=first.momentum_phase,
                is_continuous=first.is_continuous,
            )
            result.append(merged)

        scroll_buffer.clear()

    for event in events:
        if isinstance(event, MouseScrollEvent):
            scroll_buffer.append(event)
        else:
            flush_buffer()
            result.append(event)

    flush_buffer()

    return result


def merge_consecutive_mouse_magnify_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Merge consecutive mouse magnify events.

    Sums magnification deltas from consecutive pinch-to-zoom events.

    Args:
        events: List of events.

    Returns:
        Events with magnify gestures merged.
    """
    result = []
    magnify_buffer: list[MouseMagnifyEvent] = []

    def flush_buffer() -> None:
        if not magnify_buffer:
            return
        if len(magnify_buffer) == 1:
            result.append(magnify_buffer[0])
        else:
            first = magnify_buffer[0]
            total_mag = sum(e.magnification for e in magnify_buffer)
            merged = MouseMagnifyEvent(
                timestamp=first.timestamp,
                x=first.x,
                y=first.y,
                magnification=total_mag,
            )
            result.append(merged)
        magnify_buffer.clear()

    for event in events:
        if isinstance(event, MouseMagnifyEvent):
            magnify_buffer.append(event)
        else:
            flush_buffer()
            result.append(event)

    flush_buffer()
    return result


def merge_consecutive_mouse_rotate_events(events: list[ActionEvent]) -> list[ActionEvent]:
    """Merge consecutive mouse rotate events.

    Sums rotation deltas from consecutive two-finger rotation events.

    Args:
        events: List of events.

    Returns:
        Events with rotate gestures merged.
    """
    result = []
    rotate_buffer: list[MouseRotateEvent] = []

    def flush_buffer() -> None:
        if not rotate_buffer:
            return
        if len(rotate_buffer) == 1:
            result.append(rotate_buffer[0])
        else:
            first = rotate_buffer[0]
            total_rot = sum(e.rotation for e in rotate_buffer)
            merged = MouseRotateEvent(
                timestamp=first.timestamp,
                x=first.x,
                y=first.y,
                rotation=total_rot,
            )
            result.append(merged)
        rotate_buffer.clear()

    for event in events:
        if isinstance(event, MouseRotateEvent):
            rotate_buffer.append(event)
        else:
            flush_buffer()
            result.append(event)

    flush_buffer()
    return result


def merge_consecutive_mouse_click_events(
    events: list[ActionEvent],
    double_click_interval: float = DOUBLE_CLICK_INTERVAL_SECONDS,
    double_click_distance: float = DOUBLE_CLICK_DISTANCE_PIXELS,
    drag_distance_threshold: float = DRAG_DISTANCE_THRESHOLD,
) -> list[ActionEvent]:
    """Merge mouse down/up events into click events.

    Uses timestamp mapping approach to match down/up events
    even when other events occur between them.

    Detects single clicks and double clicks based on timing and distance.
    Does NOT merge if the down→up distance exceeds drag_distance_threshold
    (those will be handled by detect_drag_events).

    Args:
        events: List of events.
        double_click_interval: Time threshold for double-click (seconds).
        double_click_distance: Distance threshold for double-click (pixels).
        drag_distance_threshold: If down→up distance exceeds this, don't merge (pixels).

    Returns:
        Events with clicks merged into MouseClickEvent/MouseDoubleClickEvent.
    """
    def calculate_distance(x1: float, y1: float, x2: float, y2: float) -> float:
        return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

    # Build timestamp mappings for down -> up and down -> next_down (for double-click)
    down_events: list[MouseDownEvent] = []
    down_to_up: dict[float, MouseUpEvent] = {}
    down_to_next_down: dict[float, MouseDownEvent] = {}

    # First pass: collect all down events and map to their up events
    prev_down: MouseDownEvent | None = None
    for event in events:
        if isinstance(event, MouseDownEvent):
            down_events.append(event)
            # Check if this could be second click of a double-click
            if prev_down is not None:
                dt = event.timestamp - prev_down.timestamp
                dx = abs(event.x - prev_down.x)
                dy = abs(event.y - prev_down.y)
                if (
                    dt <= double_click_interval
                    and dx <= double_click_distance
                    and dy <= double_click_distance
                    and event.button == prev_down.button
                ):
                    down_to_next_down[prev_down.timestamp] = event
            prev_down = event
        elif isinstance(event, MouseUpEvent):
            # Find the most recent unmatched down with same button
            for down in reversed(down_events):
                if down.button == event.button and down.timestamp not in down_to_up:
                    # Only map if distance is small enough (not a drag)
                    dist = calculate_distance(down.x, down.y, event.x, event.y)
                    if dist <= drag_distance_threshold:
                        down_to_up[down.timestamp] = event
                    break

    # Second pass: generate merged events
    result = []
    skip_timestamps: set[float] = set()

    for event in events:
        if event.timestamp in skip_timestamps:
            continue

        if isinstance(event, MouseDownEvent):
            down = event

            if down.timestamp in down_to_up:
                up = down_to_up[down.timestamp]

                # Check if this is the start of a double-click
                if down.timestamp in down_to_next_down:
                    next_down = down_to_next_down[down.timestamp]
                    if next_down.timestamp in down_to_up:
                        next_up = down_to_up[next_down.timestamp]

                        # Create double-click (pressure from first down event)
                        double_click = MouseDoubleClickEvent(
                            timestamp=down.timestamp,
                            x=down.x,
                            y=down.y,
                            button=down.button,
                            pressure=down.pressure,
                            modifier_flags=down.modifier_flags,
                            children=[down, up, next_down, next_up],
                        )
                        result.append(double_click)
                        skip_timestamps.add(up.timestamp)
                        skip_timestamps.add(next_down.timestamp)
                        skip_timestamps.add(next_up.timestamp)
                        continue

                # Create single click (pressure from down event)
                single_click = MouseClickEvent(
                    timestamp=down.timestamp,
                    x=down.x,
                    y=down.y,
                    button=down.button,
                    pressure=down.pressure,
                    modifier_flags=down.modifier_flags,
                    children=[down, up],
                )
                result.append(single_click)
                skip_timestamps.add(up.timestamp)
            else:
                # Unmatched down event
                result.append(event)

        elif isinstance(event, MouseUpEvent):
            # Already handled via down event mapping, or orphaned
            result.append(event)
        else:
            result.append(event)

    return result


def detect_drag_events(
    events: list[ActionEvent],
    min_distance: float = DRAG_DISTANCE_THRESHOLD,
) -> list[ActionEvent]:
    """Detect drag events from mouse down → move → up sequences.

    A drag is detected when:
    1. Mouse down occurs
    2. Mouse up occurs for the same button at a distance >= min_distance

    Keyboard, scroll, and gesture events that occur mid-drag are tolerated:
    they are emitted inline AND included as children of the drag event.
    Only truly unrelated events (WindowStateEvent, ScreenFrameEvent, merged
    clicks) flush the drag state.

    Args:
        events: List of events (should already have clicks merged).
        min_distance: Minimum drag distance in pixels.

    Returns:
        Events with drags detected as MouseDragEvent.
    """
    # Event types that are tolerated during a drag — emitted inline and
    # also captured as children of the drag.
    DRAG_SIBLING_TYPES = (
        KeyTypeEvent, KeyShortcutEvent, SpecialKeyEvent, KeyDownEvent, KeyUpEvent,
        MouseScrollEvent,
        MouseMagnifyEvent, MouseRotateEvent, MouseSmartMagnifyEvent,
    )

    result = []
    # {down: MouseDownEvent, children: [...sibling events during drag]}
    drag_state: dict[str, Any] | None = None

    def calculate_distance(x1: float, y1: float, x2: float, y2: float) -> float:
        return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

    def flush_drag_state() -> None:
        """Emit buffered drag state as separate events (drag not completed)."""
        nonlocal drag_state
        if drag_state is None:
            return
        result.append(drag_state["down"])
        result.extend(drag_state["children"])
        drag_state = None

    for event in events:
        if isinstance(event, MouseDownEvent):
            if drag_state is not None:
                if event.button != drag_state["down"].button:
                    # Second button during drag — emit inline, continue tracking original drag
                    drag_state["children"].append(event)
                    result.append(event)
                    continue
                else:
                    # Same button pressed again — flush old state, start new
                    flush_drag_state()
            drag_state = {"down": event, "children": []}

        elif isinstance(event, MouseMoveEvent) and drag_state is not None:
            drag_state["children"].append(event)

        elif isinstance(event, MouseUpEvent) and drag_state is not None:
            down_event: MouseDownEvent = drag_state["down"]

            if down_event.button == event.button:
                # Matching button — check if this is a drag
                distance = calculate_distance(
                    down_event.x, down_event.y, event.x, event.y
                )

                if distance >= min_distance:
                    # Create drag event
                    drag = MouseDragEvent(
                        timestamp=down_event.timestamp,
                        x=down_event.x,
                        y=down_event.y,
                        dx=event.x - down_event.x,
                        dy=event.y - down_event.y,
                        button=down_event.button,
                        children=[down_event] + drag_state["children"] + [event],
                    )
                    result.append(drag)
                else:
                    # Not a drag — output as separate events
                    result.append(down_event)
                    result.extend(drag_state["children"])
                    result.append(event)

                drag_state = None
            else:
                # MouseUp for a different button — emit inline, continue drag
                drag_state["children"].append(event)
                result.append(event)

        elif isinstance(event, DRAG_SIBLING_TYPES) and drag_state is not None:
            # Tolerate keyboard/scroll/gesture events during drag
            drag_state["children"].append(event)
            result.append(event)

        elif isinstance(event, (MouseClickEvent, MouseDoubleClickEvent)):
            # Already merged click — flush any incomplete drag
            flush_drag_state()
            result.append(event)

        else:
            # Truly unrelated events (WindowStateEvent, ScreenFrameEvent, etc.)
            flush_drag_state()
            result.append(event)

    # Handle any remaining drag state at end of event list
    flush_drag_state()

    return result


# =============================================================================
# Utility Functions
# =============================================================================


def get_action_events(events: list[Event]) -> list[ActionEvent]:
    """Filter to only action events (mouse, keyboard).

    Args:
        events: All events.

    Returns:
        Only action events.
    """
    action_types = (
        MouseMoveEvent,
        MouseDownEvent,
        MouseUpEvent,
        MouseScrollEvent,
        MouseMagnifyEvent,
        MouseRotateEvent,
        MouseSmartMagnifyEvent,
        KeyDownEvent,
        KeyUpEvent,
        MouseClickEvent,
        MouseDoubleClickEvent,
        MouseDragEvent,
        KeyTypeEvent,
        KeyShortcutEvent,
        SpecialKeyEvent,
    )
    return [e for e in events if isinstance(e, action_types)]


def get_screen_events(events: list[Event]) -> list[Event]:
    """Filter to only screen events.

    Args:
        events: All events.

    Returns:
        Only screen frame events.
    """
    from screencap.engine.events import ScreenFrameEvent

    return [e for e in events if isinstance(e, ScreenFrameEvent)]


def get_audio_events(events: list[Event]) -> list[Event]:
    """Filter to only audio events.

    Args:
        events: All events.

    Returns:
        Only audio chunk events.
    """
    from screencap.engine.events import AudioChunkEvent

    return [e for e in events if isinstance(e, AudioChunkEvent)]


# =============================================================================
# Window Switch Processing
# =============================================================================


def deduplicate_window_events(
    window_rows: list[dict],
) -> list[WindowSwitchEvent]:
    """Emit WindowSwitchEvent only when (app_bundle_id, window_id) changes.

    Takes sorted window event dicts from the DB, deduplicates by the
    (app_bundle_id, window_id) pair, and returns WindowSwitchEvent list.
    Title-only changes (common in browsers) are ignored.

    Privacy filtering is NOT done here — that belongs in the screencap
    layer (exporter.py / chunk_processor.py).

    Args:
        window_rows: List of dicts with window_event DB columns,
            sorted by timestamp.

    Returns:
        Deduplicated list of WindowSwitchEvent instances.
    """
    from screencap.engine.convert import dict_to_window_switch

    result: list[WindowSwitchEvent] = []
    last_key: tuple[str | None, str | None] = (None, None)

    for row in window_rows:
        key = (row.get("app_bundle_id"), str(row.get("window_id") or ""))
        if key != last_key:
            result.append(dict_to_window_switch(row))
            last_key = key
        elif result and row.get("browser_url") and not result[-1].domain:
            # AX URL extraction often arrives one event after the initial
            # window switch.  When the duplicate carries a browser_url that
            # the already-emitted event lacks, patch it in.
            converted = dict_to_window_switch(row)
            if converted.domain:
                result[-1] = result[-1].model_copy(
                    update={"domain": converted.domain},
                )

    return result


def interleave_window_events(
    action_events: list[ActionEvent],
    window_events: list[WindowSwitchEvent],
) -> list[BaseEvent]:
    """Merge window.switch events into the action event timeline by timestamp.

    Both lists must be sorted by timestamp. The result is a single
    time-ordered list containing both action events and window.switch events.

    Args:
        action_events: Processed action events (from process_events()).
        window_events: Deduplicated WindowSwitchEvent list.

    Returns:
        Combined, time-ordered list.
    """
    result: list[BaseEvent] = []
    ai, wi = 0, 0

    while ai < len(action_events) and wi < len(window_events):
        if window_events[wi].timestamp <= action_events[ai].timestamp:
            result.append(window_events[wi])
            wi += 1
        else:
            result.append(action_events[ai])
            ai += 1

    # Drain remaining
    while ai < len(action_events):
        result.append(action_events[ai])
        ai += 1
    while wi < len(window_events):
        result.append(window_events[wi])
        wi += 1

    return result
