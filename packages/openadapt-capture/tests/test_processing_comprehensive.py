"""Comprehensive tests for event processing pipeline.

These tests are modeled after OpenAdapt's test_events.py to ensure
thorough coverage of edge cases in event merging.
"""

import pytest

from openadapt_capture.events import (
    KeyDownEvent,
    KeyShortcutEvent,
    KeyTypeEvent,
    KeyUpEvent,
    MouseButton,
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
    WindowStateEvent,
)
from openadapt_capture.processing import (
    DOUBLE_CLICK_DISTANCE_PIXELS,
    DOUBLE_CLICK_INTERVAL_SECONDS,
    DRAG_DISTANCE_THRESHOLD,
    KEY_TYPE_MERGE_INTERVAL_SECONDS,
    detect_drag_events,
    merge_consecutive_keyboard_events,
    merge_consecutive_mouse_click_events,
    merge_consecutive_mouse_magnify_events,
    merge_consecutive_mouse_move_events,
    merge_consecutive_mouse_rotate_events,
    merge_consecutive_mouse_scroll_events,
    merge_sequential_key_type_events,
    process_events,
    remove_redundant_mouse_move_events,
)

# =============================================================================
# Test Fixtures and Helpers
# =============================================================================

class TimestampGenerator:
    """Helper to generate sequential timestamps for tests."""

    def __init__(self, start: float = 0.0, default_dt: float = 0.1):
        self.current = start
        self.default_dt = default_dt

    def next(self, dt: float = None) -> float:
        """Get next timestamp, optionally with custom delta."""
        if dt is None:
            dt = self.default_dt
        ts = self.current
        self.current += dt
        return ts

    def reset(self, start: float = 0.0) -> None:
        """Reset timestamp counter."""
        self.current = start


@pytest.fixture
def ts():
    """Fixture providing a timestamp generator."""
    return TimestampGenerator()


# =============================================================================
# Test: merge_consecutive_mouse_click_events
# =============================================================================

class TestMergeConsecutiveMouseClickEventsComprehensive:
    """Comprehensive tests for click merging based on OpenAdapt patterns."""

    def test_single_click_becomes_singleclick(self, ts):
        """A single click (down+up) should become a MouseClickEvent."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        assert len(result) == 1
        assert isinstance(result[0], MouseClickEvent)
        assert len(result[0].children) == 2

    def test_double_click_within_interval(self, ts):
        """Two quick clicks should merge into MouseDoubleClickEvent."""
        dt_short = DOUBLE_CLICK_INTERVAL_SECONDS / 10

        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        assert len(result) == 1
        assert isinstance(result[0], MouseDoubleClickEvent)
        assert len(result[0].children) == 4

    def test_clicks_too_far_apart_stay_separate(self, ts):
        """Two clicks far apart in time should stay as separate single clicks."""
        dt_long = DOUBLE_CLICK_INTERVAL_SECONDS * 3  # Well beyond threshold

        # First click
        events = [
            MouseDownEvent(timestamp=0.0, x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=0.05, x=100, y=100, button=MouseButton.LEFT),
            # Second click - starts AFTER the double-click interval
            MouseDownEvent(timestamp=dt_long, x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=dt_long + 0.05, x=100, y=100, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        assert len(result) == 2
        assert all(isinstance(e, MouseClickEvent) for e in result)
        assert not any(isinstance(e, MouseDoubleClickEvent) for e in result)

    def test_clicks_too_far_apart_spatially(self, ts):
        """Two quick clicks at different positions should stay separate."""
        dt_short = DOUBLE_CLICK_INTERVAL_SECONDS / 10
        distance = DOUBLE_CLICK_DISTANCE_PIXELS * 3  # Beyond threshold

        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=ts.next(dt_short), x=100 + distance, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100 + distance, y=100, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        assert len(result) == 2
        assert all(isinstance(e, MouseClickEvent) for e in result)

    def test_different_buttons_stay_separate(self, ts):
        """Clicks with different buttons should not merge."""
        dt_short = DOUBLE_CLICK_INTERVAL_SECONDS / 10

        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=100, y=100, button=MouseButton.RIGHT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        assert len(result) == 2

    def test_mixed_sequence_with_double_and_single_clicks(self, ts):
        """Complex sequence with both double and single clicks."""
        dt_short = DOUBLE_CLICK_INTERVAL_SECONDS / 10
        dt_long = DOUBLE_CLICK_INTERVAL_SECONDS * 2

        events = [
            # Right click (single)
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=ts.next(dt_long), x=100, y=100, button=MouseButton.RIGHT),
            # Left double-click
            MouseDownEvent(timestamp=ts.next(dt_short), x=200, y=200, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=200, y=200, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=ts.next(dt_short), x=200, y=200, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_short), x=200, y=200, button=MouseButton.LEFT),
            # Another right click (single)
            MouseDownEvent(timestamp=ts.next(dt_long), x=300, y=300, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=ts.next(dt_long), x=300, y=300, button=MouseButton.RIGHT),
            # Left single click
            MouseDownEvent(timestamp=ts.next(dt_long), x=400, y=400, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(dt_long), x=400, y=400, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)

        # Should have: right single, left double, right single, left single
        # Note: raw right clicks pass through since our impl focuses on left clicks
        assert any(isinstance(e, MouseDoubleClickEvent) for e in result)


# =============================================================================
# Test: merge_consecutive_mouse_move_events
# =============================================================================

class TestMergeConsecutiveMouseMoveEventsComprehensive:
    """Comprehensive tests for mouse move merging."""

    def test_consecutive_moves_merge_to_final_position(self, ts):
        """Multiple consecutive moves should merge, keeping final position."""
        events = [
            MouseMoveEvent(timestamp=ts.next(), x=0, y=0),
            MouseMoveEvent(timestamp=ts.next(), x=10, y=10),
            MouseMoveEvent(timestamp=ts.next(), x=20, y=20),
            MouseMoveEvent(timestamp=ts.next(), x=30, y=30),
        ]
        result = merge_consecutive_mouse_move_events(events)

        assert len(result) == 1
        assert result[0].x == 30
        assert result[0].y == 30

    def test_moves_interrupted_by_scroll_create_groups(self, ts):
        """Moves interrupted by scroll events should create separate groups."""
        events = [
            MouseScrollEvent(timestamp=ts.next(), x=0, y=0, dx=0, dy=1),
            MouseMoveEvent(timestamp=ts.next(), x=0, y=0),
            MouseMoveEvent(timestamp=ts.next(), x=10, y=10),
            MouseMoveEvent(timestamp=ts.next(), x=20, y=20),
            MouseScrollEvent(timestamp=ts.next(), x=20, y=20, dx=0, dy=1),
            MouseMoveEvent(timestamp=ts.next(), x=30, y=30),
            MouseMoveEvent(timestamp=ts.next(), x=40, y=40),
        ]
        result = merge_consecutive_mouse_move_events(events)

        # Should have: scroll, merged_move(20,20), scroll, merged_move(40,40)
        move_events = [e for e in result if isinstance(e, MouseMoveEvent)]
        assert len(move_events) == 2
        assert move_events[0].x == 20
        assert move_events[1].x == 40


# =============================================================================
# Test: merge_consecutive_mouse_scroll_events
# =============================================================================

class TestMergeConsecutiveMouseScrollEventsComprehensive:
    """Comprehensive tests for scroll merging."""

    def test_scroll_deltas_accumulate(self, ts):
        """Scroll deltas should sum correctly."""
        events = [
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=2, dy=0),
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=1, dy=0),
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=-1, dy=0),
        ]
        result = merge_consecutive_mouse_scroll_events(events)

        assert len(result) == 1
        assert result[0].dx == 2  # 2 + 1 - 1 = 2
        assert result[0].dy == 0

    def test_scrolls_interrupted_by_move_create_groups(self, ts):
        """Scrolls interrupted by moves should create separate groups."""
        events = [
            MouseMoveEvent(timestamp=ts.next(), x=0, y=0),
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=2, dy=0),
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=1, dy=0),
            MouseScrollEvent(timestamp=ts.next(), x=100, y=100, dx=-1, dy=0),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseScrollEvent(timestamp=ts.next(), x=200, y=200, dx=0, dy=1),
            MouseScrollEvent(timestamp=ts.next(), x=200, y=200, dx=1, dy=0),
        ]
        result = merge_consecutive_mouse_scroll_events(events)

        scroll_events = [e for e in result if isinstance(e, MouseScrollEvent)]
        assert len(scroll_events) == 2
        assert scroll_events[0].dx == 2
        assert scroll_events[1].dx == 1
        assert scroll_events[1].dy == 1


# =============================================================================
# Test: merge_consecutive_keyboard_events
# =============================================================================

class TestMergeConsecutiveKeyboardEventsComprehensive:
    """Comprehensive tests for keyboard event merging."""

    def test_key_sequence_merges_to_type_event(self, ts):
        """Sequence of key press/release should merge to KeyTypeEvent."""
        events = [
            KeyDownEvent(timestamp=ts.next(), key_char="a"),
            KeyUpEvent(timestamp=ts.next(), key_char="a"),
            KeyDownEvent(timestamp=ts.next(), key_char="b"),
            KeyUpEvent(timestamp=ts.next(), key_char="b"),
            KeyDownEvent(timestamp=ts.next(), key_char="c"),
            KeyUpEvent(timestamp=ts.next(), key_char="c"),
        ]
        result = merge_consecutive_keyboard_events(events)

        # Each press/release creates a KeyTypeEvent
        assert all(isinstance(e, KeyTypeEvent) for e in result)
        texts = [e.text for e in result]
        assert "a" in texts
        assert "b" in texts
        assert "c" in texts

    def test_keyboard_interrupted_by_mouse_creates_groups(self, ts):
        """Keyboard events interrupted by mouse should create separate groups."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            KeyDownEvent(timestamp=ts.next(), key_char="a"),
            KeyUpEvent(timestamp=ts.next(), key_char="a"),
            KeyDownEvent(timestamp=ts.next(), key_char="b"),
            KeyUpEvent(timestamp=ts.next(), key_char="b"),
            MouseUpEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            KeyDownEvent(timestamp=ts.next(), key_char="c"),
            KeyUpEvent(timestamp=ts.next(), key_char="c"),
        ]
        result = merge_consecutive_keyboard_events(events)

        # Should have click events interspersed with type events
        mouse_events = [e for e in result if isinstance(e, (MouseDownEvent, MouseUpEvent))]
        type_events = [e for e in result if isinstance(e, KeyTypeEvent)]
        assert len(mouse_events) == 2
        assert len(type_events) >= 2

    def test_modifier_keys_handled(self, ts):
        """Modifier keys (shift, ctrl) should be handled."""
        events = [
            KeyDownEvent(timestamp=ts.next(), key_name="shift"),
            KeyDownEvent(timestamp=ts.next(), key_char="a"),
            KeyUpEvent(timestamp=ts.next(), key_char="a"),
            KeyUpEvent(timestamp=ts.next(), key_name="shift"),
        ]
        result = merge_consecutive_keyboard_events(events)

        # All should be merged since keys are still pressed
        assert len(result) >= 1


# =============================================================================
# Test: detect_drag_events
# =============================================================================

class TestDetectDragEventsComprehensive:
    """Comprehensive tests for drag detection."""

    def test_drag_with_multiple_moves(self, ts):
        """Drag with multiple intermediate moves should be detected."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=150),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseMoveEvent(timestamp=ts.next(), x=250, y=250),
            MouseMoveEvent(timestamp=ts.next(), x=300, y=300),
            MouseUpEvent(timestamp=ts.next(), x=300, y=300, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        assert len(result) == 1
        assert isinstance(result[0], MouseDragEvent)
        assert result[0].x == 100  # start position
        assert result[0].dx == 200  # displacement (300 - 100)
        # Children should include all moves
        assert len(result[0].children) == 6

    def test_drag_with_keyboard_event_is_tolerated(self, ts):
        """Keyboard events during drag should be tolerated (drag still detected)."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=150),
            KeyDownEvent(timestamp=ts.next(), key_char="a"),  # Tolerated sibling
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        # KeyDownEvent should be in children
        drag = drags[0]
        key_children = [c for c in drag.children if isinstance(c, KeyDownEvent)]
        assert len(key_children) == 1

    def test_right_button_drag(self, ts):
        """Drag with right button should work."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.RIGHT),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.RIGHT),
        ]
        result = detect_drag_events(events)

        assert len(result) == 1
        assert isinstance(result[0], MouseDragEvent)
        assert result[0].button == MouseButton.RIGHT


# =============================================================================
# Test: remove_redundant_mouse_move_events
# =============================================================================

class TestRemoveRedundantMouseMoveEventsComprehensive:
    """Comprehensive tests for redundant move removal."""

    def test_removes_duplicates_in_long_chain(self, ts):
        """Should remove duplicate positions even in long chains."""
        events = []
        for _ in range(3):
            events.extend([
                MouseMoveEvent(timestamp=ts.next(), x=1, y=1),
                MouseDownEvent(timestamp=ts.next(), x=1, y=1, button=MouseButton.LEFT),
                MouseMoveEvent(timestamp=ts.next(), x=1, y=1),  # Redundant
                MouseUpEvent(timestamp=ts.next(), x=1, y=1, button=MouseButton.LEFT),
                MouseMoveEvent(timestamp=ts.next(), x=2, y=2),
                MouseDownEvent(timestamp=ts.next(), x=2, y=2, button=MouseButton.LEFT),
                MouseMoveEvent(timestamp=ts.next(), x=3, y=3),
                MouseUpEvent(timestamp=ts.next(), x=3, y=3, button=MouseButton.LEFT),
                MouseMoveEvent(timestamp=ts.next(), x=3, y=3),  # Redundant
            ])

        result = remove_redundant_mouse_move_events(events)

        # Count moves - should have fewer due to redundant removal
        original_moves = len([e for e in events if isinstance(e, MouseMoveEvent)])
        result_moves = len([e for e in result if isinstance(e, MouseMoveEvent)])
        assert result_moves < original_moves


# =============================================================================
# Test: Full Pipeline
# =============================================================================

class TestFullPipelineComprehensive:
    """Test complete processing pipeline with complex scenarios."""

    def test_realistic_workflow(self, ts):
        """Test a realistic user workflow: click, type, drag, scroll."""
        events = [
            # Click on text field
            MouseMoveEvent(timestamp=ts.next(), x=100, y=100),
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            # Type some text
            KeyDownEvent(timestamp=ts.next(), key_char="h"),
            KeyUpEvent(timestamp=ts.next(), key_char="h"),
            KeyDownEvent(timestamp=ts.next(), key_char="i"),
            KeyUpEvent(timestamp=ts.next(), key_char="i"),
            # Drag to select
            MouseDownEvent(timestamp=ts.next(), x=100, y=200, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=200),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.LEFT),
            # Scroll down
            MouseScrollEvent(timestamp=ts.next(), x=200, y=200, dx=0, dy=-3),
            MouseScrollEvent(timestamp=ts.next(), x=200, y=200, dx=0, dy=-2),
        ]
        result = process_events(events)

        # Should have processed into meaningful actions
        clicks = [e for e in result if isinstance(e, MouseClickEvent)]
        types = [e for e in result if isinstance(e, KeyTypeEvent)]
        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        scrolls = [e for e in result if isinstance(e, MouseScrollEvent)]

        assert len(clicks) >= 1, "Should have at least one click"
        assert len(types) >= 1, "Should have typed text"
        assert len(drags) >= 1, "Should have a drag"
        assert len(scrolls) >= 1, "Should have scrolls"

    def test_empty_events(self):
        """Empty event list should return empty."""
        result = process_events([])
        assert result == []

    def test_single_event(self, ts):
        """Single event should pass through."""
        events = [MouseMoveEvent(timestamp=ts.next(), x=100, y=100)]
        result = process_events(events)
        assert len(result) == 1


# =============================================================================
# Test: Robust Drag Detection (Phase 2)
# =============================================================================

class TestRobustDragDetection:
    """Tests for Phase 2 drag detection improvements."""

    def test_drag_with_modifier_keys(self, ts):
        """Shift held during drag → drag detected with KeyTypeEvent children."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=150),
            KeyTypeEvent(timestamp=ts.next(), text="", children=[
                KeyDownEvent(timestamp=ts.current - 0.05, key_name="shift"),
                KeyUpEvent(timestamp=ts.current, key_name="shift"),
            ]),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        key_children = [c for c in drags[0].children if isinstance(c, KeyTypeEvent)]
        assert len(key_children) == 1

    def test_drag_with_scroll_events(self, ts):
        """Scroll during drag → drag detected with MouseScrollEvent children."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=150),
            MouseScrollEvent(timestamp=ts.next(), x=150, y=150, dx=0, dy=3),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        scroll_children = [c for c in drags[0].children if isinstance(c, MouseScrollEvent)]
        assert len(scroll_children) == 1

    def test_drag_fast_no_moves(self, ts):
        """MouseDown + MouseUp, distance > threshold, no moves → drag detected."""
        dist = DRAG_DISTANCE_THRESHOLD + 10  # well above threshold
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=ts.next(), x=100 + dist, y=100, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        assert drags[0].dx == dist

    def test_drag_second_button(self, ts):
        """RMB pressed during LMB drag → original drag preserved."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=150),
            MouseDownEvent(timestamp=ts.next(), x=150, y=150, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=ts.next(), x=150, y=150, button=MouseButton.RIGHT),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        assert drags[0].button == MouseButton.LEFT

    def test_drag_with_gesture_events(self, ts):
        """Magnify event during drag → drag detected."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=150),
            MouseMagnifyEvent(timestamp=ts.next(), x=150, y=150, magnification=0.1),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        mag_children = [c for c in drags[0].children if isinstance(c, MouseMagnifyEvent)]
        assert len(mag_children) == 1

    def test_drag_up_wrong_button(self, ts):
        """MouseUp for different button than MouseDown → drag NOT ended."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=150),
            MouseUpEvent(timestamp=ts.next(), x=150, y=150, button=MouseButton.RIGHT),  # wrong button
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.LEFT),  # correct button
        ]
        result = detect_drag_events(events)

        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        # Drag should end at (200,200), not (150,150)
        assert drags[0].dx == 100

    def test_drag_state_flush_on_window_event(self, ts):
        """WindowStateEvent mid-drag → drag properly flushed (preserved behavior)."""
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=150, y=150),
            WindowStateEvent(
                timestamp=ts.next(), title="Test", left=0, top=0,
                width=800, height=600, window_id=1,
            ),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseUpEvent(timestamp=ts.next(), x=200, y=200, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        # WindowStateEvent should flush the drag state
        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drags) == 0


# =============================================================================
# Test: Gesture Event Types (Phase 1 & 4)
# =============================================================================

class TestGestureEventTypes:
    """Tests for MouseMagnifyEvent and MouseRotateEvent."""

    def test_magnify_event_creation(self):
        """MouseMagnifyEvent can be instantiated with correct fields."""
        ev = MouseMagnifyEvent(timestamp=1.0, x=500, y=300, magnification=0.05)
        assert ev.type == "mouse.magnify"
        assert ev.magnification == 0.05
        assert ev.x == 500
        assert ev.y == 300

    def test_rotate_event_creation(self):
        """MouseRotateEvent can be instantiated with correct fields."""
        ev = MouseRotateEvent(timestamp=1.0, x=500, y=300, rotation=45.0)
        assert ev.type == "mouse.rotate"
        assert ev.rotation == 45.0

    def test_magnify_event_merge(self, ts):
        """Consecutive magnify events get merged with summed delta."""
        events = [
            MouseMagnifyEvent(timestamp=ts.next(), x=100, y=100, magnification=0.02),
            MouseMagnifyEvent(timestamp=ts.next(), x=100, y=100, magnification=0.03),
            MouseMagnifyEvent(timestamp=ts.next(), x=100, y=100, magnification=0.05),
        ]
        result = merge_consecutive_mouse_magnify_events(events)

        assert len(result) == 1
        assert isinstance(result[0], MouseMagnifyEvent)
        assert abs(result[0].magnification - 0.10) < 1e-9

    def test_rotate_event_merge(self, ts):
        """Consecutive rotate events get merged with summed delta."""
        events = [
            MouseRotateEvent(timestamp=ts.next(), x=100, y=100, rotation=10.0),
            MouseRotateEvent(timestamp=ts.next(), x=100, y=100, rotation=15.0),
            MouseRotateEvent(timestamp=ts.next(), x=100, y=100, rotation=-5.0),
        ]
        result = merge_consecutive_mouse_rotate_events(events)

        assert len(result) == 1
        assert isinstance(result[0], MouseRotateEvent)
        assert abs(result[0].rotation - 20.0) < 1e-9

    def test_magnify_interrupted_creates_groups(self, ts):
        """Magnify events interrupted by other event create separate groups."""
        events = [
            MouseMagnifyEvent(timestamp=ts.next(), x=100, y=100, magnification=0.02),
            MouseMagnifyEvent(timestamp=ts.next(), x=100, y=100, magnification=0.03),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
            MouseMagnifyEvent(timestamp=ts.next(), x=200, y=200, magnification=0.01),
        ]
        result = merge_consecutive_mouse_magnify_events(events)

        mag_events = [e for e in result if isinstance(e, MouseMagnifyEvent)]
        assert len(mag_events) == 2
        assert abs(mag_events[0].magnification - 0.05) < 1e-9
        assert abs(mag_events[1].magnification - 0.01) < 1e-9

    def test_gesture_events_pass_through_pipeline(self, ts):
        """Magnify/rotate events survive all pipeline steps."""
        events = [
            MouseMoveEvent(timestamp=ts.next(), x=100, y=100),
            MouseMagnifyEvent(timestamp=ts.next(), x=100, y=100, magnification=0.05),
            MouseMagnifyEvent(timestamp=ts.next(), x=100, y=100, magnification=0.03),
            MouseRotateEvent(timestamp=ts.next(), x=100, y=100, rotation=30.0),
            MouseMoveEvent(timestamp=ts.next(), x=200, y=200),
        ]
        result = process_events(events)

        mag_events = [e for e in result if isinstance(e, MouseMagnifyEvent)]
        rot_events = [e for e in result if isinstance(e, MouseRotateEvent)]
        assert len(mag_events) >= 1
        assert len(rot_events) == 1

    def test_get_action_events_includes_gestures(self, ts):
        """Magnify/rotate included in get_action_events output."""
        from openadapt_capture.processing import get_action_events

        events = [
            MouseMagnifyEvent(timestamp=ts.next(), x=100, y=100, magnification=0.05),
            MouseRotateEvent(timestamp=ts.next(), x=100, y=100, rotation=30.0),
        ]
        result = get_action_events(events)

        assert len(result) == 2
        assert isinstance(result[0], MouseMagnifyEvent)
        assert isinstance(result[1], MouseRotateEvent)


# =============================================================================
# Test: SmartMagnify Event Types
# =============================================================================

class TestSmartMagnifyEventTypes:
    """Tests for MouseSmartMagnifyEvent (two-finger double-tap zoom toggle)."""

    def test_smart_magnify_event_creation(self):
        """MouseSmartMagnifyEvent can be instantiated with correct fields."""
        ev = MouseSmartMagnifyEvent(timestamp=1.0, x=500, y=300)
        assert ev.type == "mouse.smart_magnify"
        assert ev.x == 500
        assert ev.y == 300

    def test_smart_magnify_serialization_roundtrip(self):
        """MouseSmartMagnifyEvent survives model_dump / model_validate."""
        ev = MouseSmartMagnifyEvent(timestamp=1.0, x=500, y=300)
        data = ev.model_dump()
        restored = MouseSmartMagnifyEvent.model_validate(data)
        assert restored.type == "mouse.smart_magnify"
        assert restored.x == 500
        assert restored.y == 300
        assert restored.timestamp == 1.0

    def test_smart_magnify_passes_through_pipeline(self, ts):
        """SmartMagnify events survive all pipeline steps."""
        events = [
            MouseMoveEvent(timestamp=ts.next(), x=100, y=100),
            MouseSmartMagnifyEvent(timestamp=ts.next(), x=200, y=200),
            MouseMoveEvent(timestamp=ts.next(), x=300, y=300),
        ]
        result = process_events(events)

        smart_events = [e for e in result if isinstance(e, MouseSmartMagnifyEvent)]
        assert len(smart_events) == 1
        assert smart_events[0].x == 200
        assert smart_events[0].y == 200

    def test_smart_magnify_not_merged(self, ts):
        """Two consecutive SmartMagnify events remain two separate events."""
        events = [
            MouseSmartMagnifyEvent(timestamp=ts.next(), x=100, y=100),
            MouseSmartMagnifyEvent(timestamp=ts.next(), x=100, y=100),
        ]
        result = process_events(events)

        smart_events = [e for e in result if isinstance(e, MouseSmartMagnifyEvent)]
        assert len(smart_events) == 2

    def test_smart_magnify_in_drag_siblings(self, ts):
        """SmartMagnify is tolerated during drag detection (included as sibling)."""
        dist = DRAG_DISTANCE_THRESHOLD + 20
        events = [
            MouseDownEvent(timestamp=ts.next(), x=100, y=100, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=100 + dist, y=100),
            MouseSmartMagnifyEvent(timestamp=ts.next(), x=100 + dist, y=100),
            MouseUpEvent(timestamp=ts.next(), x=100 + dist, y=100, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)

        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        # SmartMagnify should be in the drag's children
        smart_children = [c for c in drags[0].children if isinstance(c, MouseSmartMagnifyEvent)]
        assert len(smart_children) == 1

    def test_smart_magnify_in_get_action_events(self, ts):
        """SmartMagnify included in get_action_events output."""
        from openadapt_capture.processing import get_action_events

        events = [
            MouseSmartMagnifyEvent(timestamp=ts.next(), x=100, y=100),
            MouseMagnifyEvent(timestamp=ts.next(), x=100, y=100, magnification=0.05),
        ]
        result = get_action_events(events)

        assert len(result) == 2
        assert isinstance(result[0], MouseSmartMagnifyEvent)
        assert isinstance(result[1], MouseMagnifyEvent)

    def test_smart_magnify_db_roundtrip(self, tmp_path):
        """SmartMagnify events persist through CaptureStorage write/read."""
        from openadapt_capture.storage import Capture, CaptureStorage

        db_path = tmp_path / "test.db"
        storage = CaptureStorage(db_path)
        capture = Capture(
            id="test", started_at=0.0, platform="darwin",
            screen_width=1920, screen_height=1080,
        )
        storage.init_capture(capture)

        ev = MouseSmartMagnifyEvent(timestamp=1.0, x=500, y=300)
        storage.write_event(ev)

        events = storage.get_events()
        assert len(events) == 1
        assert isinstance(events[0], MouseSmartMagnifyEvent)
        assert events[0].x == 500
        assert events[0].y == 300
        storage.close()

    def test_convert_action_event_smart_magnify(self):
        """Legacy DB conversion handles smart_magnify events."""
        from unittest.mock import MagicMock
        from openadapt_capture.capture import _convert_action_event

        db_event = MagicMock()
        db_event.name = "smart_magnify"
        db_event.timestamp = 1.0
        db_event.mouse_x = 500
        db_event.mouse_y = 300

        result = _convert_action_event(db_event)
        assert isinstance(result, MouseSmartMagnifyEvent)
        assert result.x == 500.0
        assert result.y == 300.0

    def test_event_type_map_includes_all_gestures(self):
        """EVENT_TYPE_MAP includes magnify, rotate, and smart_magnify."""
        from openadapt_capture.events import EventType
        from openadapt_capture.storage import EVENT_TYPE_MAP

        assert EventType.MOUSE_MAGNIFY.value in EVENT_TYPE_MAP
        assert EventType.MOUSE_ROTATE.value in EVENT_TYPE_MAP
        assert EventType.MOUSE_SMART_MAGNIFY.value in EVENT_TYPE_MAP


# =============================================================================
# Test: Integration (Blender-style workflow)
# =============================================================================

class TestBlenderWorkflowSimulation:
    """Simulates a Blender-style workflow to verify all events are correctly classified."""

    def test_blender_workflow(self, ts):
        """Simulate: MMB-drag (orbit), Shift+MMB-drag (pan), pinch-to-zoom,
        LMB-drag+Shift (constrained move), RMB cancel → all correctly classified."""
        dist = DRAG_DISTANCE_THRESHOLD + 20

        events = [
            # 1. MMB orbit drag
            MouseDownEvent(timestamp=ts.next(), x=400, y=400, button=MouseButton.MIDDLE),
            MouseMoveEvent(timestamp=ts.next(), x=400 + dist, y=400 + dist),
            MouseUpEvent(timestamp=ts.next(), x=400 + dist, y=400 + dist, button=MouseButton.MIDDLE),
            # 2. Shift+MMB pan drag
            MouseDownEvent(timestamp=ts.next(), x=400, y=400, button=MouseButton.MIDDLE),
            KeyTypeEvent(timestamp=ts.next(), text="", children=[
                KeyDownEvent(timestamp=ts.current - 0.05, key_name="shift"),
                KeyUpEvent(timestamp=ts.current, key_name="shift"),
            ]),
            MouseMoveEvent(timestamp=ts.next(), x=400, y=400 + dist),
            MouseUpEvent(timestamp=ts.next(), x=400, y=400 + dist, button=MouseButton.MIDDLE),
            # 3. Pinch-to-zoom
            MouseMagnifyEvent(timestamp=ts.next(), x=500, y=500, magnification=0.02),
            MouseMagnifyEvent(timestamp=ts.next(), x=500, y=500, magnification=0.03),
            MouseMagnifyEvent(timestamp=ts.next(), x=500, y=500, magnification=0.05),
            # 4. LMB-drag + Shift (constrained transform)
            MouseDownEvent(timestamp=ts.next(), x=300, y=300, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=ts.next(), x=300 + dist, y=300),
            KeyTypeEvent(timestamp=ts.next(), text="", children=[
                KeyDownEvent(timestamp=ts.current - 0.05, key_name="shift"),
                KeyUpEvent(timestamp=ts.current, key_name="shift"),
            ]),
            MouseMoveEvent(timestamp=ts.next(), x=300 + dist * 2, y=300),
            MouseUpEvent(timestamp=ts.next(), x=300 + dist * 2, y=300, button=MouseButton.LEFT),
            # 5. RMB cancel (click)
            MouseDownEvent(timestamp=ts.next(), x=300, y=300, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=ts.next(), x=300, y=300, button=MouseButton.RIGHT),
        ]

        result = process_events(events)

        # Count event types
        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        magnifies = [e for e in result if isinstance(e, MouseMagnifyEvent)]
        clicks = [e for e in result if isinstance(e, (MouseClickEvent, MouseDoubleClickEvent))]

        # Should have: orbit drag, pan drag, constrained drag = 3 drags
        assert len(drags) == 3, f"Expected 3 drags, got {len(drags)}"

        # Magnify events should be merged (3 raw → 1 merged)
        assert len(magnifies) == 1, f"Expected 1 merged magnify, got {len(magnifies)}"
        assert abs(magnifies[0].magnification - 0.10) < 1e-9

        # RMB cancel should be a click
        assert len(clicks) >= 1, f"Expected at least 1 click, got {len(clicks)}"

        # Constrained drag should have KeyTypeEvent in children
        constrained_drag = [d for d in drags if d.button == MouseButton.LEFT]
        assert len(constrained_drag) == 1
        key_children = [c for c in constrained_drag[0].children if isinstance(c, KeyTypeEvent)]
        assert len(key_children) == 1


# =============================================================================
# Helpers for merge_sequential_key_type_events tests
# =============================================================================


def _make_key_type(char: str, timestamp: float) -> KeyTypeEvent:
    """Create a single-character KeyTypeEvent with realistic children."""
    return KeyTypeEvent(
        timestamp=timestamp,
        text=char,
        children=[
            KeyDownEvent(timestamp=timestamp, key_char=char),
            KeyUpEvent(timestamp=timestamp + 0.05, key_char=char),
        ],
    )


# =============================================================================
# Test: merge_sequential_key_type_events
# =============================================================================


class TestMergeSequentialKeyTypeEvents:
    """Tests for merge_sequential_key_type_events."""

    def test_simple_merge(self, ts):
        """Sequential single-char KeyTypeEvents within threshold merge."""
        events = [
            _make_key_type("h", ts.next()),
            _make_key_type("e", ts.next()),
            _make_key_type("l", ts.next()),
            _make_key_type("l", ts.next()),
            _make_key_type("o", ts.next()),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 1
        assert result[0].text == "hello"
        assert len(result[0].children) == 10  # 5 keys * (down + up)

    def test_gap_exceeds_threshold(self, ts):
        """Gap >= threshold flushes buffer."""
        events = [
            _make_key_type("h", 0.0),
            _make_key_type("i", 0.6),  # 600ms gap, above 500ms threshold
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 2
        assert result[0].text == "h"
        assert result[1].text == "i"

    def test_gap_exactly_at_threshold(self, ts):
        """Gap exactly at threshold flushes (strict less-than comparison)."""
        events = [
            _make_key_type("h", 0.0),
            _make_key_type("i", KEY_TYPE_MERGE_INTERVAL_SECONDS),  # exactly 500ms
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 2
        assert result[0].text == "h"
        assert result[1].text == "i"

    def test_space_boundary(self, ts):
        """Space flushes buffer and is emitted standalone."""
        events = [
            _make_key_type("h", ts.next()),
            _make_key_type("i", ts.next()),
            _make_key_type(" ", ts.next()),
            _make_key_type("b", ts.next()),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 3
        assert result[0].text == "hi"
        assert result[1].text == " "
        assert result[2].text == "b"

    def test_shortcut_boundary(self, ts):
        """KeyShortcutEvent flushes buffer."""
        t0 = ts.next()
        t1 = ts.next()
        t2 = ts.next()
        t3 = ts.next()
        t4 = ts.next()
        events = [
            _make_key_type("h", t0),
            _make_key_type("e", t1),
            _make_key_type("l", t2),
            KeyShortcutEvent(
                timestamp=t3,
                keys=["ctrl", "z"],
                children=[
                    KeyDownEvent(timestamp=t3, key_name="ctrl"),
                    KeyDownEvent(timestamp=t3 + 0.01, key_char="z"),
                    KeyUpEvent(timestamp=t3 + 0.05, key_char="z"),
                    KeyUpEvent(timestamp=t3 + 0.06, key_name="ctrl"),
                ],
            ),
            _make_key_type("l", t4),
            _make_key_type("o", ts.next()),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 3
        assert result[0].text == "hel"
        assert isinstance(result[1], KeyShortcutEvent)
        assert result[2].text == "lo"

    def test_mouse_event_boundary(self, ts):
        """Non-keyboard events flush buffer."""
        events = [
            _make_key_type("a", ts.next()),
            _make_key_type("b", ts.next()),
            MouseMoveEvent(timestamp=ts.next(), x=100.0, y=100.0),
            _make_key_type("c", ts.next()),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 3
        assert result[0].text == "ab"
        assert isinstance(result[1], MouseMoveEvent)
        assert result[2].text == "c"

    def test_backspace_boundary(self, ts):
        """Backspace (empty text, key_name=backspace) flushes buffer."""
        t0 = ts.next()
        t1 = ts.next()
        t2 = ts.next()
        backspace_ts = ts.next()
        t3 = ts.next()
        t4 = ts.next()
        events = [
            _make_key_type("h", t0),
            _make_key_type("e", t1),
            _make_key_type("l", t2),
            KeyTypeEvent(
                timestamp=backspace_ts,
                text="",
                children=[
                    KeyDownEvent(timestamp=backspace_ts, key_name="backspace"),
                    KeyUpEvent(timestamp=backspace_ts + 0.05, key_name="backspace"),
                ],
            ),
            _make_key_type("l", t3),
            _make_key_type("o", t4),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 3
        assert result[0].text == "hel"
        assert result[1].text == ""  # backspace event
        assert result[2].text == "lo"

    def test_enter_as_newline(self, ts):
        """Enter delivered as text='\\n' is a whitespace boundary."""
        t0 = ts.next()
        t1 = ts.next()
        enter_ts = ts.next()
        t2 = ts.next()
        events = [
            _make_key_type("h", t0),
            _make_key_type("e", t1),
            KeyTypeEvent(
                timestamp=enter_ts,
                text="\n",
                children=[
                    KeyDownEvent(timestamp=enter_ts, key_name="enter"),
                    KeyUpEvent(timestamp=enter_ts + 0.05, key_name="enter"),
                ],
            ),
            _make_key_type("w", t2),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 3
        assert result[0].text == "he"
        assert result[1].text == "\n"
        assert result[2].text == "w"

    def test_enter_as_keyname(self, ts):
        """Enter delivered as text='' with key_name='enter' is a nonprintable boundary."""
        t0 = ts.next()
        t1 = ts.next()
        enter_ts = ts.next()
        t2 = ts.next()
        events = [
            _make_key_type("h", t0),
            _make_key_type("e", t1),
            KeyTypeEvent(
                timestamp=enter_ts,
                text="",
                children=[
                    KeyDownEvent(timestamp=enter_ts, key_name="enter"),
                    KeyUpEvent(timestamp=enter_ts + 0.05, key_name="enter"),
                ],
            ),
            _make_key_type("w", t2),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 3
        assert result[0].text == "he"
        assert result[1].text == ""
        assert result[2].text == "w"

    def test_tab_as_char(self, ts):
        """Tab delivered as text='\\t' is a whitespace boundary."""
        t0 = ts.next()
        tab_ts = ts.next()
        t1 = ts.next()
        events = [
            _make_key_type("a", t0),
            KeyTypeEvent(
                timestamp=tab_ts,
                text="\t",
                children=[
                    KeyDownEvent(timestamp=tab_ts, key_name="tab"),
                    KeyUpEvent(timestamp=tab_ts + 0.05, key_name="tab"),
                ],
            ),
            _make_key_type("b", t1),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 3
        assert result[0].text == "a"
        assert result[1].text == "\t"
        assert result[2].text == "b"

    def test_tab_as_keyname(self, ts):
        """Tab delivered as text='' with key_name='tab' is a nonprintable boundary."""
        t0 = ts.next()
        tab_ts = ts.next()
        t1 = ts.next()
        events = [
            _make_key_type("a", t0),
            KeyTypeEvent(
                timestamp=tab_ts,
                text="",
                children=[
                    KeyDownEvent(timestamp=tab_ts, key_name="tab"),
                    KeyUpEvent(timestamp=tab_ts + 0.05, key_name="tab"),
                ],
            ),
            _make_key_type("b", t1),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 3
        assert result[0].text == "a"
        assert result[1].text == ""
        assert result[2].text == "b"

    def test_bare_modifier_boundary(self, ts):
        """Bare Shift tap (text='', key_name='shift_l') flushes buffer."""
        t0 = ts.next()
        t1 = ts.next()
        shift_ts = ts.next()
        t2 = ts.next()
        t3 = ts.next()
        events = [
            _make_key_type("h", t0),
            _make_key_type("e", t1),
            KeyTypeEvent(
                timestamp=shift_ts,
                text="",
                children=[
                    KeyDownEvent(timestamp=shift_ts, key_name="shift_l"),
                    KeyUpEvent(timestamp=shift_ts + 0.05, key_name="shift_l"),
                ],
            ),
            _make_key_type("l", t2),
            _make_key_type("o", t3),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 3
        assert result[0].text == "he"
        assert result[1].text == ""  # bare modifier
        assert result[2].text == "lo"

    def test_punctuation_merges_with_word(self, ts):
        """Punctuation is NOT a boundary — merges into the word."""
        events = [
            _make_key_type("h", ts.next()),
            _make_key_type("e", ts.next()),
            _make_key_type(",", ts.next()),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 1
        assert result[0].text == "he,"

    def test_end_of_stream_flush(self, ts):
        """Buffer flushed at end of event list."""
        events = [
            _make_key_type("a", ts.next()),
            _make_key_type("b", ts.next()),
            _make_key_type("c", ts.next()),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 1
        assert result[0].text == "abc"

    def test_empty_input(self):
        """Empty list returns empty list."""
        assert merge_sequential_key_type_events([]) == []

    def test_single_event(self, ts):
        """Single event passes through unchanged."""
        event = _make_key_type("x", ts.next())
        result = merge_sequential_key_type_events([event])
        assert len(result) == 1
        assert result[0].text == "x"
        assert result[0] is event  # same object, not a copy

    def test_multi_char_input(self, ts):
        """Multi-character KeyTypeEvent included in merge."""
        t0 = ts.next()
        t1 = ts.next()
        events = [
            KeyTypeEvent(
                timestamp=t0,
                text="th",
                children=[
                    KeyDownEvent(timestamp=t0, key_char="t"),
                    KeyDownEvent(timestamp=t0 + 0.02, key_char="h"),
                    KeyUpEvent(timestamp=t0 + 0.04, key_char="t"),
                    KeyUpEvent(timestamp=t0 + 0.05, key_char="h"),
                ],
            ),
            _make_key_type("e", t1),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 1
        assert result[0].text == "the"
        assert len(result[0].children) == 6  # 4 from "th" + 2 from "e"

    def test_interval_zero(self, ts):
        """interval=0 disables merging (every gap >= 0)."""
        events = [
            _make_key_type("a", ts.next()),
            _make_key_type("b", ts.next()),
            _make_key_type("c", ts.next()),
        ]
        result = merge_sequential_key_type_events(events, interval=0)
        assert len(result) == 3
        assert result[0].text == "a"
        assert result[1].text == "b"
        assert result[2].text == "c"

    def test_merged_event_uses_first_timestamp(self, ts):
        """Merged event uses the first event's timestamp."""
        first_ts = ts.next()
        events = [
            _make_key_type("a", first_ts),
            _make_key_type("b", ts.next()),
            _make_key_type("c", ts.next()),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 1
        assert result[0].timestamp == first_ts

    def test_hello_world_scenario(self, ts):
        """Typing 'hello world' produces 3 events: 'hello', ' ', 'world'."""
        events = [
            _make_key_type("h", ts.next()),
            _make_key_type("e", ts.next()),
            _make_key_type("l", ts.next()),
            _make_key_type("l", ts.next()),
            _make_key_type("o", ts.next()),
            _make_key_type(" ", ts.next()),
            _make_key_type("w", ts.next()),
            _make_key_type("o", ts.next()),
            _make_key_type("r", ts.next()),
            _make_key_type("l", ts.next()),
            _make_key_type("d", ts.next()),
        ]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 3
        assert result[0].text == "hello"
        assert result[1].text == " "
        assert result[2].text == "world"

    def test_pipeline_integration(self, ts):
        """merge_sequential_key_type_events runs correctly in full pipeline."""
        # Simulate raw key down/up events that go through full pipeline
        events = [
            KeyDownEvent(timestamp=ts.next(), key_char="h"),
            KeyUpEvent(timestamp=ts.next(), key_char="h"),
            KeyDownEvent(timestamp=ts.next(), key_char="i"),
            KeyUpEvent(timestamp=ts.next(), key_char="i"),
            KeyDownEvent(timestamp=ts.next(), key_char=" "),
            KeyUpEvent(timestamp=ts.next(), key_char=" "),
            KeyDownEvent(timestamp=ts.next(), key_char="y"),
            KeyUpEvent(timestamp=ts.next(), key_char="y"),
            KeyDownEvent(timestamp=ts.next(), key_char="o"),
            KeyUpEvent(timestamp=ts.next(), key_char="o"),
        ]
        result = process_events(events)
        key_types = [e for e in result if isinstance(e, KeyTypeEvent)]
        # Should produce: "hi", " ", "yo"
        assert len(key_types) == 3
        assert key_types[0].text == "hi"
        assert key_types[1].text == " "
        assert key_types[2].text == "yo"
