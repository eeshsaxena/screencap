"""Tests for event processing pipeline."""


from openadapt_capture.events import (
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
)
from openadapt_capture.processing import (
    detect_drag_events,
    merge_consecutive_keyboard_events,
    merge_consecutive_mouse_click_events,
    merge_consecutive_mouse_move_events,
    merge_consecutive_mouse_scroll_events,
    process_events,
    remove_invalid_keyboard_events,
    remove_redundant_mouse_move_events,
)


class TestRemoveInvalidKeyboardEvents:
    """Tests for remove_invalid_keyboard_events."""

    def test_removes_empty_key_events(self):
        """Test that events with no key info are removed."""
        events = [
            KeyDownEvent(timestamp=1.0),  # No key info
            KeyDownEvent(timestamp=2.0, key_char="a"),  # Valid
            KeyUpEvent(timestamp=3.0),  # No key info
        ]
        result = remove_invalid_keyboard_events(events)
        assert len(result) == 1
        assert result[0].key_char == "a"

    def test_keeps_valid_key_events(self):
        """Test that valid key events are kept."""
        events = [
            KeyDownEvent(timestamp=1.0, key_char="a"),
            KeyDownEvent(timestamp=2.0, key_name="shift"),
            KeyDownEvent(timestamp=3.0, key_vk="65"),
        ]
        result = remove_invalid_keyboard_events(events)
        assert len(result) == 3


class TestRemoveRedundantMouseMoveEvents:
    """Tests for remove_redundant_mouse_move_events."""

    def test_removes_duplicate_positions(self):
        """Test that consecutive moves to same position are removed."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
            MouseMoveEvent(timestamp=2.0, x=100.0, y=100.0),  # Duplicate
            MouseMoveEvent(timestamp=3.0, x=200.0, y=200.0),
        ]
        result = remove_redundant_mouse_move_events(events)
        assert len(result) == 2
        assert result[0].x == 100.0
        assert result[1].x == 200.0

    def test_keeps_different_positions(self):
        """Test that moves to different positions are kept."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
            MouseMoveEvent(timestamp=2.0, x=100.0, y=101.0),
            MouseMoveEvent(timestamp=3.0, x=101.0, y=101.0),
        ]
        result = remove_redundant_mouse_move_events(events)
        assert len(result) == 3


class TestMergeConsecutiveKeyboardEvents:
    """Tests for merge_consecutive_keyboard_events."""

    def test_merges_typed_text(self):
        """Test merging key events into typed text.

        Note: Each press/release cycle creates a separate KeyTypeEvent.
        This matches OpenAdapt's behavior of grouping by pressed state.
        """
        events = [
            KeyDownEvent(timestamp=1.0, key_char="h"),
            KeyUpEvent(timestamp=1.1, key_char="h"),
            KeyDownEvent(timestamp=1.2, key_char="i"),
            KeyUpEvent(timestamp=1.3, key_char="i"),
        ]
        result = merge_consecutive_keyboard_events(events)
        # Each key press/release pair becomes a separate KeyTypeEvent
        assert len(result) == 2
        assert all(isinstance(r, KeyTypeEvent) for r in result)
        assert result[0].text == "h"
        assert result[1].text == "i"

    def test_preserves_non_keyboard_events(self):
        """Test that non-keyboard events break the merge."""
        events = [
            KeyDownEvent(timestamp=1.0, key_char="a"),
            KeyUpEvent(timestamp=1.1, key_char="a"),
            MouseMoveEvent(timestamp=1.5, x=100.0, y=100.0),
            KeyDownEvent(timestamp=2.0, key_char="b"),
            KeyUpEvent(timestamp=2.1, key_char="b"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 3  # KeyTypeEvent("a"), MouseMove, KeyTypeEvent("b")


class TestMediaKeyProcessing:
    """Tests for media key handling in the processing pipeline."""

    def test_media_key_not_merged_into_key_type(self):
        """Test that media key press/release pair is NOT merged into KeyTypeEvent."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_play_pause"),
            KeyUpEvent(timestamp=1.1, key_name="media_play_pause"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 2
        assert isinstance(result[0], KeyDownEvent)
        assert isinstance(result[1], KeyUpEvent)
        assert result[0].key_name == "media_play_pause"

    def test_regular_keys_still_merge_with_media_interspersed(self):
        """Test: type 'abc' -> press Play -> type 'def' produces correct sequence."""
        events = [
            # Type "a"
            KeyDownEvent(timestamp=1.0, key_char="a"),
            KeyUpEvent(timestamp=1.1, key_char="a"),
            # Type "b"
            KeyDownEvent(timestamp=1.2, key_char="b"),
            KeyUpEvent(timestamp=1.3, key_char="b"),
            # Type "c"
            KeyDownEvent(timestamp=1.4, key_char="c"),
            KeyUpEvent(timestamp=1.5, key_char="c"),
            # Media key
            KeyDownEvent(timestamp=2.0, key_name="media_play_pause"),
            KeyUpEvent(timestamp=2.1, key_name="media_play_pause"),
            # Type "d"
            KeyDownEvent(timestamp=3.0, key_char="d"),
            KeyUpEvent(timestamp=3.1, key_char="d"),
            # Type "e"
            KeyDownEvent(timestamp=3.2, key_char="e"),
            KeyUpEvent(timestamp=3.3, key_char="e"),
            # Type "f"
            KeyDownEvent(timestamp=3.4, key_char="f"),
            KeyUpEvent(timestamp=3.5, key_char="f"),
        ]
        result = merge_consecutive_keyboard_events(events)

        # Expect: KeyTypeEvent("a"), KeyTypeEvent("b"), KeyTypeEvent("c"),
        #         KeyDown(media_play_pause), KeyUp(media_play_pause),
        #         KeyTypeEvent("d"), KeyTypeEvent("e"), KeyTypeEvent("f")
        assert len(result) == 8

        # First three are KeyTypeEvents for a, b, c
        assert isinstance(result[0], KeyTypeEvent)
        assert result[0].text == "a"
        assert isinstance(result[1], KeyTypeEvent)
        assert result[1].text == "b"
        assert isinstance(result[2], KeyTypeEvent)
        assert result[2].text == "c"

        # Media key events pass through as raw
        assert isinstance(result[3], KeyDownEvent)
        assert result[3].key_name == "media_play_pause"
        assert isinstance(result[4], KeyUpEvent)
        assert result[4].key_name == "media_play_pause"

        # Last three are KeyTypeEvents for d, e, f
        assert isinstance(result[5], KeyTypeEvent)
        assert result[5].text == "d"
        assert isinstance(result[6], KeyTypeEvent)
        assert result[6].text == "e"
        assert isinstance(result[7], KeyTypeEvent)
        assert result[7].text == "f"

    def test_brightness_key_not_merged(self):
        """Test that brightness key events are not merged."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_brightness_up", key_vk="21"),
            KeyUpEvent(timestamp=1.1, key_name="media_brightness_up", key_vk="21"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 2
        assert isinstance(result[0], KeyDownEvent)
        assert isinstance(result[1], KeyUpEvent)

    def test_volume_key_not_merged(self):
        """Test that volume key events are not merged."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_volume_up"),
            KeyUpEvent(timestamp=1.1, key_name="media_volume_up"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 2
        assert isinstance(result[0], KeyDownEvent)
        assert isinstance(result[1], KeyUpEvent)

    def test_remove_invalid_keeps_vk_only_events(self):
        """Test that remove_invalid_keyboard_events passes events with only key_vk set."""
        events = [
            KeyDownEvent(timestamp=1.0, key_vk="21"),  # brightness key before mapping
            KeyDownEvent(timestamp=2.0, key_char="a"),
            KeyDownEvent(timestamp=3.0),  # truly empty — should be removed
        ]
        result = remove_invalid_keyboard_events(events)
        assert len(result) == 2
        assert result[0].key_vk == "21"
        assert result[1].key_char == "a"

    def test_media_key_in_full_pipeline(self):
        """Test media keys through the full process_events pipeline."""
        events = [
            KeyDownEvent(timestamp=1.0, key_char="a"),
            KeyUpEvent(timestamp=1.1, key_char="a"),
            KeyDownEvent(timestamp=2.0, key_name="media_play_pause"),
            KeyUpEvent(timestamp=2.1, key_name="media_play_pause"),
            KeyDownEvent(timestamp=3.0, key_char="b"),
            KeyUpEvent(timestamp=3.1, key_char="b"),
        ]
        result = process_events(events)

        # Should have: KeyTypeEvent("a"), KeyDown(media), KeyUp(media), KeyTypeEvent("b")
        assert len(result) == 4
        assert isinstance(result[0], KeyTypeEvent)
        assert result[0].text == "a"
        assert isinstance(result[1], KeyDownEvent)
        assert result[1].key_name == "media_play_pause"
        assert isinstance(result[2], KeyUpEvent)
        assert result[2].key_name == "media_play_pause"
        assert isinstance(result[3], KeyTypeEvent)
        assert result[3].text == "b"


class TestMergeConsecutiveMouseMoveEvents:
    """Tests for merge_consecutive_mouse_move_events."""

    def test_merges_consecutive_moves(self):
        """Test merging consecutive mouse moves."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
            MouseMoveEvent(timestamp=1.1, x=110.0, y=110.0),
            MouseMoveEvent(timestamp=1.2, x=120.0, y=120.0),
        ]
        result = merge_consecutive_mouse_move_events(events)
        assert len(result) == 1
        # Should have final position
        assert result[0].x == 120.0
        assert result[0].y == 120.0

    def test_single_move_not_merged(self):
        """Test that a single move is not modified."""
        events = [MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0)]
        result = merge_consecutive_mouse_move_events(events)
        assert len(result) == 1
        assert result[0].x == 100.0

    def test_single_move_has_path(self):
        """Single (unmerged) move should have path=[(x, y)]."""
        events = [MouseMoveEvent(timestamp=1.0, x=50.0, y=75.0)]
        result = merge_consecutive_mouse_move_events(events)
        assert len(result) == 1
        assert result[0].path == [(50.0, 75.0)]

    def test_merged_moves_preserve_all_waypoints(self):
        """Merged moves should have path with all intermediate positions."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=0.0, y=0.0),
            MouseMoveEvent(timestamp=1.1, x=10.0, y=5.0),
            MouseMoveEvent(timestamp=1.2, x=20.0, y=10.0),
            MouseMoveEvent(timestamp=1.3, x=30.0, y=15.0),
        ]
        result = merge_consecutive_mouse_move_events(events)
        assert len(result) == 1
        assert result[0].x == 30.0
        assert result[0].y == 15.0
        assert result[0].path == [
            (0.0, 0.0),
            (10.0, 5.0),
            (20.0, 10.0),
            (30.0, 15.0),
        ]

    def test_interrupted_moves_each_have_path(self):
        """Moves interrupted by other events each get their own path."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=0.0, y=0.0),
            MouseMoveEvent(timestamp=1.1, x=10.0, y=10.0),
            MouseScrollEvent(timestamp=1.2, x=10.0, y=10.0, dx=0.0, dy=1.0),
            MouseMoveEvent(timestamp=1.3, x=20.0, y=20.0),
        ]
        result = merge_consecutive_mouse_move_events(events)
        moves = [e for e in result if isinstance(e, MouseMoveEvent)]
        assert len(moves) == 2
        assert moves[0].path == [(0.0, 0.0), (10.0, 10.0)]
        assert moves[1].path == [(20.0, 20.0)]

    def test_path_serializes_as_nested_arrays(self):
        """path should serialize as [[x1,y1],[x2,y2],...] in JSON."""
        event = MouseMoveEvent(
            timestamp=1.0, x=20.0, y=10.0,
            path=[(0.0, 0.0), (10.0, 5.0), (20.0, 10.0)],
        )
        data = event.model_dump()
        assert data["path"] == [(0.0, 0.0), (10.0, 5.0), (20.0, 10.0)]
        # JSON round-trip
        import json
        json_str = event.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["path"] == [[0.0, 0.0], [10.0, 5.0], [20.0, 10.0]]

    def test_default_path_is_empty_list(self):
        """MouseMoveEvent created without path should default to empty list."""
        event = MouseMoveEvent(timestamp=1.0, x=5.0, y=5.0)
        assert event.path == []


class TestMergeConsecutiveMouseScrollEvents:
    """Tests for merge_consecutive_mouse_scroll_events."""

    def test_merges_scroll_deltas(self):
        """Test that scroll deltas are summed."""
        events = [
            MouseScrollEvent(timestamp=1.0, x=100.0, y=100.0, dx=0.0, dy=-1.0),
            MouseScrollEvent(timestamp=1.1, x=100.0, y=100.0, dx=0.0, dy=-2.0),
            MouseScrollEvent(timestamp=1.2, x=100.0, y=100.0, dx=0.0, dy=-1.0),
        ]
        result = merge_consecutive_mouse_scroll_events(events)
        assert len(result) == 1
        assert result[0].dy == -4.0  # Sum of all dy values


class TestMergeConsecutiveMouseClickEvents:
    """Tests for merge_consecutive_mouse_click_events."""

    def test_creates_single_click(self):
        """Test single click detection."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.1, x=100.0, y=100.0, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseClickEvent)
        assert result[0].button == MouseButton.LEFT

    def test_creates_double_click(self):
        """Test double click detection."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.05, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=1.1, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.15, x=100.0, y=100.0, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseDoubleClickEvent)

    def test_separate_clicks_too_far_apart(self):
        """Test that clicks too far apart in time stay separate."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.05, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=3.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=3.05, x=100.0, y=100.0, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 2
        assert all(isinstance(r, MouseClickEvent) for r in result)


class TestDetectDragEvents:
    """Tests for detect_drag_events."""

    def test_detects_drag(self):
        """Test drag detection from down + moves + up."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=150.0, y=150.0),
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            MouseUpEvent(timestamp=1.3, x=200.0, y=200.0, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseDragEvent)
        assert result[0].x == 100.0  # start position
        assert result[0].dx == 100.0  # displacement (200 - 100)

    def test_no_drag_for_small_movement(self):
        """Test that small movements don't create drags."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=101.0, y=101.0),
            MouseUpEvent(timestamp=1.2, x=102.0, y=102.0, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)
        # Should not create drag due to small distance
        assert not any(isinstance(e, MouseDragEvent) for e in result)


class TestProcessEvents:
    """Tests for full processing pipeline."""

    def test_full_pipeline(self):
        """Test complete processing pipeline."""
        events = [
            # Mouse movement
            MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
            MouseMoveEvent(timestamp=1.1, x=100.0, y=100.0),  # Redundant
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            # Click
            MouseDownEvent(timestamp=2.0, x=200.0, y=200.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=2.1, x=200.0, y=200.0, button=MouseButton.LEFT),
            # Type
            KeyDownEvent(timestamp=3.0, key_char="a"),
            KeyUpEvent(timestamp=3.1, key_char="a"),
            KeyDownEvent(timestamp=3.2, key_char="b"),
            KeyUpEvent(timestamp=3.3, key_char="b"),
        ]
        result = process_events(events)

        # Should have: merged move, single click, key types
        types = [type(e).__name__ for e in result]
        assert "MouseMoveEvent" in types
        assert "MouseClickEvent" in types
        assert "KeyTypeEvent" in types

        # Check that keyboard events were merged into KeyTypeEvents
        # The sequential merge step combines "a" and "b" (200ms apart < 500ms threshold)
        key_types = [e for e in result if isinstance(e, KeyTypeEvent)]
        assert len(key_types) == 1
        assert key_types[0].text == "ab"
