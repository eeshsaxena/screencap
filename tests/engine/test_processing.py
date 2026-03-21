"""Tests for event processing pipeline."""


from screencap.engine.events import (
    KeyDownEvent,
    KeyShortcutEvent,
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
    SpecialKeyEvent,
)
from screencap.engine.processing import (
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
        This matches legacy behavior of grouping by pressed state.
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


class TestSpecialKeyProcessing:
    """Tests for special key (media, function, etc.) handling in the processing pipeline."""

    def test_standalone_media_key_wrapped(self):
        """Test that media key press/release pair is wrapped into SpecialKeyEvent."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_play_pause"),
            KeyUpEvent(timestamp=1.1, key_name="media_play_pause"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 1
        assert isinstance(result[0], SpecialKeyEvent)
        assert result[0].key_name == "media_play_pause"
        assert result[0].text == "Play/Pause"
        assert len(result[0].children) == 2

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
        #         SpecialKeyEvent(media_play_pause),
        #         KeyTypeEvent("d"), KeyTypeEvent("e"), KeyTypeEvent("f")
        assert len(result) == 7

        assert isinstance(result[0], KeyTypeEvent) and result[0].text == "a"
        assert isinstance(result[1], KeyTypeEvent) and result[1].text == "b"
        assert isinstance(result[2], KeyTypeEvent) and result[2].text == "c"

        assert isinstance(result[3], SpecialKeyEvent)
        assert result[3].key_name == "media_play_pause"

        assert isinstance(result[4], KeyTypeEvent) and result[4].text == "d"
        assert isinstance(result[5], KeyTypeEvent) and result[5].text == "e"
        assert isinstance(result[6], KeyTypeEvent) and result[6].text == "f"

    def test_brightness_key_wrapped(self):
        """Test that brightness key events are wrapped into SpecialKeyEvent."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_brightness_up", key_vk="21"),
            KeyUpEvent(timestamp=1.1, key_name="media_brightness_up", key_vk="21"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 1
        assert isinstance(result[0], SpecialKeyEvent)
        assert result[0].key_name == "media_brightness_up"

    def test_volume_key_wrapped(self):
        """Test that volume key events are wrapped into SpecialKeyEvent."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_volume_up"),
            KeyUpEvent(timestamp=1.1, key_name="media_volume_up"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 1
        assert isinstance(result[0], SpecialKeyEvent)
        assert result[0].key_name == "media_volume_up"
        assert result[0].text == "Volume/Up"

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

        # Should have: KeyTypeEvent("a"), SpecialKeyEvent(media_play_pause), KeyTypeEvent("b")
        assert len(result) == 3
        assert isinstance(result[0], KeyTypeEvent)
        assert result[0].text == "a"
        assert isinstance(result[1], SpecialKeyEvent)
        assert result[1].key_name == "media_play_pause"
        assert isinstance(result[2], KeyTypeEvent)
        assert result[2].text == "b"

    def test_standalone_function_key_wrapped(self):
        """Test that standalone F5 press produces SpecialKeyEvent."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="f5"),
            KeyUpEvent(timestamp=1.1, key_name="f5"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 1
        assert isinstance(result[0], SpecialKeyEvent)
        assert result[0].key_name == "f5"
        assert result[0].text == "F5"

    def test_ctrl_f5_still_produces_shortcut(self):
        """Test that Ctrl+F5 still produces KeyShortcutEvent, no regression."""
        from screencap.engine.processing import detect_key_shortcuts
        events = [
            KeyDownEvent(timestamp=1.0, key_name="ctrl"),
            KeyDownEvent(timestamp=1.05, key_name="f5"),
            KeyUpEvent(timestamp=1.1, key_name="f5"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
        ]
        merged = merge_consecutive_keyboard_events(events)
        result = detect_key_shortcuts(merged)
        assert len(result) == 1
        assert isinstance(result[0], KeyShortcutEvent)
        assert result[0].keys == ["ctrl", "f5"]

    def test_media_key_between_text(self):
        """Test: type 'a' -> press media_next -> type 'b'."""
        events = [
            KeyDownEvent(timestamp=1.0, key_char="a"),
            KeyUpEvent(timestamp=1.1, key_char="a"),
            KeyDownEvent(timestamp=2.0, key_name="media_next"),
            KeyUpEvent(timestamp=2.1, key_name="media_next"),
            KeyDownEvent(timestamp=3.0, key_char="b"),
            KeyUpEvent(timestamp=3.1, key_char="b"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 3
        assert isinstance(result[0], KeyTypeEvent) and result[0].text == "a"
        assert isinstance(result[1], SpecialKeyEvent) and result[1].key_name == "media_next"
        assert isinstance(result[2], KeyTypeEvent) and result[2].text == "b"

    def test_auto_repeat_volume(self):
        """Test auto-repeat: multiple downs then one up. Final down+up should wrap."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_volume_up"),
            KeyDownEvent(timestamp=1.1, key_name="media_volume_up"),
            KeyDownEvent(timestamp=1.2, key_name="media_volume_up"),
            KeyUpEvent(timestamp=1.3, key_name="media_volume_up"),
        ]
        result = merge_consecutive_keyboard_events(events)
        # Each down except the last emits as raw (pending is replaced).
        # The last down+up wraps into SpecialKeyEvent.
        special = [e for e in result if isinstance(e, SpecialKeyEvent)]
        raw_downs = [e for e in result if isinstance(e, KeyDownEvent)]
        assert len(special) == 1
        assert special[0].key_name == "media_volume_up"
        assert len(raw_downs) == 2  # first two downs emitted as raw

    def test_orphan_down_at_end(self):
        """Test that an unpaired down at recording end passes through as raw."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_play_pause"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 1
        assert isinstance(result[0], KeyDownEvent)

    def test_orphan_up_at_start(self):
        """Test that an unpaired up at recording start passes through as raw."""
        events = [
            KeyUpEvent(timestamp=1.0, key_name="media_play_pause"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 1
        assert isinstance(result[0], KeyUpEvent)

    def test_media_key_during_modifier_hold(self):
        """Cmd held + volume up should not break keyboard buffer."""
        from screencap.engine.processing import detect_key_shortcuts
        events = [
            KeyDownEvent(timestamp=1.0, key_name="cmd"),
            KeyDownEvent(timestamp=1.1, key_name="media_volume_up"),
            KeyUpEvent(timestamp=1.15, key_name="media_volume_up"),
            KeyDownEvent(timestamp=1.2, key_name="c", key_char="c"),
            KeyUpEvent(timestamp=1.25, key_name="c", key_char="c"),
            KeyUpEvent(timestamp=1.3, key_name="cmd"),
        ]
        merged = merge_consecutive_keyboard_events(events)
        # Buffer stays open because cmd is held throughout.
        # After flushing: one KeyTypeEvent with all children (including media key)
        assert len(merged) == 1
        assert isinstance(merged[0], KeyTypeEvent)
        # detect_key_shortcuts should convert to KeyShortcutEvent
        result = detect_key_shortcuts(merged)
        assert len(result) == 1
        assert isinstance(result[0], KeyShortcutEvent)
        # cmd is a modifier; the first non-modifier key (media_volume_up) becomes the regular key
        assert "cmd" in result[0].keys

    def test_non_adjacent_pair_emits_raw(self):
        """down(media_play), mouse_move, up(media_play) → raw events (not paired)."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_play_pause"),
            MouseMoveEvent(timestamp=1.05, x=100.0, y=100.0),
            KeyUpEvent(timestamp=1.1, key_name="media_play_pause"),
        ]
        result = merge_consecutive_keyboard_events(events)
        # The mouse move breaks adjacency, so down emits raw, mouse passes through, up emits raw
        assert len(result) == 3
        assert isinstance(result[0], KeyDownEvent)
        assert isinstance(result[1], MouseMoveEvent)
        assert isinstance(result[2], KeyUpEvent)

    def test_serialization_roundtrip(self):
        """Test that SpecialKeyEvent serializes and deserializes correctly."""
        import json
        from screencap.engine.storage import EVENT_TYPE_MAP

        event = SpecialKeyEvent(
            timestamp=1.0,
            key_name="media_play_pause",
            children=[
                KeyDownEvent(timestamp=1.0, key_name="media_play_pause"),
                KeyUpEvent(timestamp=1.1, key_name="media_play_pause"),
            ],
        )

        # model_dump_json
        json_str = event.model_dump_json()
        data = json.loads(json_str)
        assert data["type"] == "key.special"
        assert data["key_name"] == "media_play_pause"
        assert data["text"] == "Play/Pause"

        # Deserialize via EVENT_TYPE_MAP
        event_class = EVENT_TYPE_MAP[data["type"]]
        restored = event_class(**{k: v for k, v in data.items() if k != "text"})
        assert isinstance(restored, SpecialKeyEvent)
        assert restored.key_name == "media_play_pause"
        assert restored.text == "Play/Pause"

    def test_special_key_text_formatting(self):
        """Test text computed field for various key types."""
        assert SpecialKeyEvent(timestamp=0, key_name="media_play_pause").text == "Play/Pause"
        assert SpecialKeyEvent(timestamp=0, key_name="media_volume_up").text == "Volume/Up"
        assert SpecialKeyEvent(timestamp=0, key_name="brightness_up").text == "Brightness Up"
        assert SpecialKeyEvent(timestamp=0, key_name="f5").text == "F5"
        assert SpecialKeyEvent(timestamp=0, key_name="f12").text == "F12"
        assert SpecialKeyEvent(timestamp=0, key_name="insert").text == "INSERT"

    def test_common_editing_keys_not_wrapped(self):
        """Space, backspace, esc, enter, arrows etc. should NOT become SpecialKeyEvent.

        On macOS these keys have key_name set and key_char=None, but they are
        common editing/navigation keys that belong in the KeyTypeEvent flow.
        """
        editing_keys = [
            "space", "backspace", "enter", "return", "tab", "escape",
            "delete", "up", "down", "left", "right",
            "home", "end", "page_up", "page_down", "caps_lock",
        ]
        for key_name in editing_keys:
            events = [
                KeyDownEvent(timestamp=1.0, key_name=key_name),
                KeyUpEvent(timestamp=1.1, key_name=key_name),
            ]
            result = merge_consecutive_keyboard_events(events)
            for ev in result:
                assert not isinstance(ev, SpecialKeyEvent), (
                    f"{key_name} should NOT be wrapped as SpecialKeyEvent"
                )


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
