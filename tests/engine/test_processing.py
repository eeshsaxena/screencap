"""Tests for event processing pipeline."""

from __future__ import annotations

import pytest

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
    MouseMagnifyEvent,
    MouseMoveEvent,
    MouseRotateEvent,
    MouseScrollEvent,
    MouseSmartMagnifyEvent,
    MouseUpEvent,
    SpecialKeyEvent,
    WindowStateEvent,
)
from screencap.engine.processing import (
    DOUBLE_CLICK_DISTANCE_PIXELS,
    DOUBLE_CLICK_INTERVAL_SECONDS,
    DRAG_DISTANCE_THRESHOLD,
    KEY_TYPE_MERGE_INTERVAL_SECONDS,
    detect_drag_events,
    detect_key_shortcuts,
    get_action_events,
    merge_consecutive_keyboard_events,
    merge_consecutive_mouse_click_events,
    merge_consecutive_mouse_magnify_events,
    merge_consecutive_mouse_move_events,
    merge_consecutive_mouse_scroll_events,
    merge_sequential_key_type_events,
    process_events,
    remove_invalid_keyboard_events,
    remove_redundant_mouse_move_events,
)


class TestRemoveInvalidKeyboardEvents:
    def test_removes_events_with_no_key_info_keeps_vk_only(self):
        events = [
            KeyDownEvent(timestamp=1.0),  # no key info — drop
            KeyDownEvent(timestamp=2.0, key_char="a"),
            KeyUpEvent(timestamp=3.0),  # no key info — drop
            KeyDownEvent(timestamp=4.0, key_vk="21"),  # vk-only is valid (e.g. brightness pre-mapping)
            KeyDownEvent(timestamp=5.0, key_name="shift"),
        ]
        result = remove_invalid_keyboard_events(events)
        assert [(e.key_char, e.key_vk, e.key_name) for e in result] == [
            ("a", None, None),
            (None, "21", None),
            (None, None, "shift"),
        ]


class TestRemoveRedundantMouseMoveEvents:
    def test_removes_duplicate_positions_only(self):
        events = [
            MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
            MouseMoveEvent(timestamp=2.0, x=100.0, y=100.0),  # duplicate — drop
            MouseMoveEvent(timestamp=3.0, x=200.0, y=200.0),
        ]
        result = remove_redundant_mouse_move_events(events)
        assert [(e.x, e.y) for e in result] == [(100.0, 100.0), (200.0, 200.0)]


class TestMergeConsecutiveKeyboardEvents:
    """Keyboard merging: KeyDown/KeyUp pairs → KeyTypeEvent / SpecialKeyEvent."""

    def test_press_release_pairs_become_keytype_events(self):
        events = [
            KeyDownEvent(timestamp=1.0, key_char="h"),
            KeyUpEvent(timestamp=1.1, key_char="h"),
            KeyDownEvent(timestamp=1.2, key_char="i"),
            KeyUpEvent(timestamp=1.3, key_char="i"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert [(type(r).__name__, r.text) for r in result] == [
            ("KeyTypeEvent", "h"),
            ("KeyTypeEvent", "i"),
        ]

    def test_mouse_event_flushes_buffer_when_no_keys_held(self):
        events = [
            KeyDownEvent(timestamp=1.0, key_char="a"),
            KeyUpEvent(timestamp=1.1, key_char="a"),
            MouseMoveEvent(timestamp=1.5, x=100.0, y=100.0),
            KeyDownEvent(timestamp=2.0, key_char="b"),
            KeyUpEvent(timestamp=2.1, key_char="b"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert [type(r).__name__ for r in result] == [
            "KeyTypeEvent",
            "MouseMoveEvent",
            "KeyTypeEvent",
        ]

    @pytest.mark.parametrize("key_name,expected_text", [
        ("media_play_pause", "Play/Pause"),
        ("media_volume_up", "Volume/Up"),
        ("media_brightness_up", "Brightness/Up"),
        ("brightness_up", "Brightness Up"),
        ("media_next", "Next"),
        ("f5", "F5"),
        ("f12", "F12"),
        ("insert", "INSERT"),
    ])
    def test_special_key_pair_wraps_with_correct_text(self, key_name, expected_text):
        events = [
            KeyDownEvent(timestamp=1.0, key_name=key_name),
            KeyUpEvent(timestamp=1.1, key_name=key_name),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert len(result) == 1
        assert isinstance(result[0], SpecialKeyEvent)
        assert result[0].key_name == key_name
        assert result[0].text == expected_text

    def test_common_editing_keys_are_not_wrapped_as_special(self):
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
            assert not any(isinstance(ev, SpecialKeyEvent) for ev in result), (
                f"{key_name} should flow through KeyTypeEvent, not become SpecialKeyEvent"
            )

    def test_special_key_between_typed_chars_preserves_segments(self):
        ts = 1.0
        events = []
        for char in "abc":
            events += [KeyDownEvent(timestamp=ts, key_char=char),
                       KeyUpEvent(timestamp=ts + 0.05, key_char=char)]
            ts += 0.1
        events += [KeyDownEvent(timestamp=ts, key_name="media_play_pause"),
                   KeyUpEvent(timestamp=ts + 0.05, key_name="media_play_pause")]
        ts += 0.1
        for char in "def":
            events += [KeyDownEvent(timestamp=ts, key_char=char),
                       KeyUpEvent(timestamp=ts + 0.05, key_char=char)]
            ts += 0.1

        result = merge_consecutive_keyboard_events(events)
        assert [type(r).__name__ for r in result] == [
            "KeyTypeEvent", "KeyTypeEvent", "KeyTypeEvent",
            "SpecialKeyEvent",
            "KeyTypeEvent", "KeyTypeEvent", "KeyTypeEvent",
        ]
        typed_chars = [r.text for r in result if isinstance(r, KeyTypeEvent)]
        assert typed_chars == ["a", "b", "c", "d", "e", "f"]
        assert isinstance(result[3], SpecialKeyEvent)
        assert result[3].key_name == "media_play_pause"

    def test_orphan_special_key_down_passes_through_as_raw(self):
        events = [KeyDownEvent(timestamp=1.0, key_name="media_play_pause")]
        result = merge_consecutive_keyboard_events(events)
        assert result == events

    def test_orphan_special_key_up_passes_through_as_raw(self):
        events = [KeyUpEvent(timestamp=1.0, key_name="media_play_pause")]
        result = merge_consecutive_keyboard_events(events)
        assert result == events

    def test_special_key_with_intervening_mouse_emits_as_raw(self):
        """down(media), mouse_move, up(media) → all three pass through; no SpecialKeyEvent
        because the media key down/up are not adjacent."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_play_pause"),
            MouseMoveEvent(timestamp=1.05, x=100.0, y=100.0),
            KeyUpEvent(timestamp=1.1, key_name="media_play_pause"),
        ]
        result = merge_consecutive_keyboard_events(events)
        assert [type(r).__name__ for r in result] == [
            "KeyDownEvent", "MouseMoveEvent", "KeyUpEvent",
        ]

    def test_auto_repeat_special_key_emits_intermediates_as_raw(self):
        """Multi-down, single-up: only the final pair wraps; earlier downs are raw."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="media_volume_up"),
            KeyDownEvent(timestamp=1.1, key_name="media_volume_up"),
            KeyDownEvent(timestamp=1.2, key_name="media_volume_up"),
            KeyUpEvent(timestamp=1.3, key_name="media_volume_up"),
        ]
        result = merge_consecutive_keyboard_events(events)
        special = [e for e in result if isinstance(e, SpecialKeyEvent)]
        raw_downs = [e for e in result if isinstance(e, KeyDownEvent)]
        assert len(special) == 1
        assert len(raw_downs) == 2

    def test_modifier_held_keeps_buffer_open_across_mouse_events(self):
        """Cmd held while mouse moves: mouse emits inline, but the keyboard buffer
        survives until cmd-up so detect_key_shortcuts can fire (Cmd+C scenario)."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="cmd"),
            MouseMoveEvent(timestamp=1.05, x=200.0, y=200.0),
            KeyDownEvent(timestamp=1.1, key_char="c"),
            KeyUpEvent(timestamp=1.15, key_char="c"),
            KeyUpEvent(timestamp=1.2, key_name="cmd"),
        ]
        merged = merge_consecutive_keyboard_events(events)
        assert sum(1 for e in merged if isinstance(e, MouseMoveEvent)) == 1
        type_events = [e for e in merged if isinstance(e, KeyTypeEvent)]
        assert len(type_events) == 1
        assert len(type_events[0].children) == 4  # cmd_dn, c_dn, c_up, cmd_up

        shortcuts = detect_key_shortcuts(merged)
        assert any(isinstance(e, KeyShortcutEvent) for e in shortcuts)

    def test_multi_modifier_shortcut_with_interleaved_mouse(self):
        """Cmd, mouse, Shift, mouse, Z, Shift-up, Cmd-up → 1 KeyTypeEvent + 2 mouse moves."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="cmd"),
            MouseMoveEvent(timestamp=1.1, x=100.0, y=100.0),
            KeyDownEvent(timestamp=1.2, key_name="shift"),
            MouseMoveEvent(timestamp=1.3, x=200.0, y=200.0),
            KeyDownEvent(timestamp=1.4, key_char="z"),
            KeyUpEvent(timestamp=1.5, key_char="z"),
            KeyUpEvent(timestamp=1.6, key_name="shift"),
            KeyUpEvent(timestamp=1.7, key_name="cmd"),
        ]
        result = merge_consecutive_keyboard_events(events)
        type_events = [e for e in result if isinstance(e, KeyTypeEvent)]
        mouse_events = [e for e in result if isinstance(e, MouseMoveEvent)]
        assert len(type_events) == 1
        assert len(mouse_events) == 2
        assert len(type_events[0].children) == 6


class TestMergeConsecutiveMouseMoveEvents:
    def test_merges_to_final_position_with_full_path(self):
        events = [
            MouseMoveEvent(timestamp=1.0, x=0.0, y=0.0),
            MouseMoveEvent(timestamp=1.1, x=10.0, y=5.0),
            MouseMoveEvent(timestamp=1.2, x=20.0, y=10.0),
            MouseMoveEvent(timestamp=1.3, x=30.0, y=15.0),
        ]
        result = merge_consecutive_mouse_move_events(events)
        assert len(result) == 1
        assert (result[0].x, result[0].y) == (30.0, 15.0)
        assert result[0].path == [(0.0, 0.0), (10.0, 5.0), (20.0, 10.0), (30.0, 15.0)]

    def test_single_move_keeps_path_as_self(self):
        events = [MouseMoveEvent(timestamp=1.0, x=50.0, y=75.0)]
        result = merge_consecutive_mouse_move_events(events)
        assert result[0].path == [(50.0, 75.0)]

    def test_scroll_interrupts_creates_separate_move_groups(self):
        events = [
            MouseMoveEvent(timestamp=1.0, x=0.0, y=0.0),
            MouseMoveEvent(timestamp=1.1, x=10.0, y=10.0),
            MouseScrollEvent(timestamp=1.2, x=10.0, y=10.0, dx=0.0, dy=1.0),
            MouseMoveEvent(timestamp=1.3, x=20.0, y=20.0),
        ]
        result = merge_consecutive_mouse_move_events(events)
        moves = [e for e in result if isinstance(e, MouseMoveEvent)]
        assert [m.path for m in moves] == [[(0.0, 0.0), (10.0, 10.0)], [(20.0, 20.0)]]

    def test_merged_run_carries_last_timestamp(self):
        """P1 privacy fix: the scrub layer needs the END of a merged span to
        detect intersection with a blocked interval. Without last_timestamp,
        a run of moves crossing into a MASK_WINDOW interval would slip past
        the point-lookup find_blocked_interval(timestamp) check.
        Single (unmerged) moves leave last_timestamp=None — the scrub layer
        treats None as a degenerate point at `timestamp`."""
        merged = merge_consecutive_mouse_move_events([
            MouseMoveEvent(timestamp=1.0, x=0.0, y=0.0),
            MouseMoveEvent(timestamp=1.1, x=10.0, y=5.0),
            MouseMoveEvent(timestamp=1.2, x=20.0, y=10.0),
        ])[0]
        assert merged.timestamp == 1.0
        assert merged.last_timestamp == 1.2

        single = merge_consecutive_mouse_move_events(
            [MouseMoveEvent(timestamp=1.0, x=50.0, y=75.0)]
        )[0]
        assert single.last_timestamp is None


class TestMergeConsecutiveMouseScrollEvents:
    def test_consecutive_scrolls_sum_deltas(self):
        events = [
            MouseScrollEvent(timestamp=1.0, x=100.0, y=100.0, dx=0.0, dy=-1.0),
            MouseScrollEvent(timestamp=1.1, x=100.0, y=100.0, dx=0.0, dy=-2.0),
            MouseScrollEvent(timestamp=1.2, x=100.0, y=100.0, dx=0.0, dy=-1.0),
        ]
        result = merge_consecutive_mouse_scroll_events(events)
        assert len(result) == 1
        assert result[0].dy == -4.0

    def test_move_interrupts_creates_separate_scroll_groups(self):
        events = [
            MouseScrollEvent(timestamp=1.0, x=100.0, y=100.0, dx=2.0, dy=0.0),
            MouseScrollEvent(timestamp=1.1, x=100.0, y=100.0, dx=1.0, dy=0.0),
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            MouseScrollEvent(timestamp=1.3, x=200.0, y=200.0, dx=0.0, dy=1.0),
        ]
        result = merge_consecutive_mouse_scroll_events(events)
        scrolls = [e for e in result if isinstance(e, MouseScrollEvent)]
        assert [(s.dx, s.dy) for s in scrolls] == [(3.0, 0.0), (0.0, 1.0)]


class TestMergeConsecutiveMouseClickEvents:
    def test_down_up_pair_becomes_single_click(self):
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.1, x=100.0, y=100.0, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseClickEvent)
        assert result[0].button == MouseButton.LEFT

    def test_two_quick_close_clicks_become_double_click(self):
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.05, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=1.1, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.15, x=100.0, y=100.0, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseDoubleClickEvent)

    def test_clicks_far_apart_in_time_stay_separate(self):
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.05, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=3.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=3.05, x=100.0, y=100.0, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert all(isinstance(r, MouseClickEvent) for r in result)
        assert len(result) == 2

    def test_clicks_far_apart_spatially_stay_separate(self):
        dt = DOUBLE_CLICK_INTERVAL_SECONDS / 10
        distance = DOUBLE_CLICK_DISTANCE_PIXELS * 3
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.0 + dt, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=1.0 + 2 * dt, x=100.0 + distance, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.0 + 3 * dt, x=100.0 + distance, y=100.0, button=MouseButton.LEFT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert all(isinstance(r, MouseClickEvent) for r in result)
        assert len(result) == 2

    def test_different_buttons_dont_merge_into_double_click(self):
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.05, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseDownEvent(timestamp=1.1, x=100.0, y=100.0, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=1.15, x=100.0, y=100.0, button=MouseButton.RIGHT),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 2
        assert not any(isinstance(r, MouseDoubleClickEvent) for r in result)


class TestDetectDragEvents:
    def test_down_moves_up_creates_drag(self):
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=150.0, y=150.0),
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            MouseUpEvent(timestamp=1.3, x=200.0, y=200.0, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)
        assert len(result) == 1
        drag = result[0]
        assert isinstance(drag, MouseDragEvent)
        assert (drag.x, drag.y, drag.dx, drag.dy) == (100.0, 100.0, 100.0, 100.0)

    def test_short_movement_does_not_create_drag(self):
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=101.0, y=101.0),
            MouseUpEvent(timestamp=1.2, x=102.0, y=102.0, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)
        assert not any(isinstance(e, MouseDragEvent) for e in result)

    def test_fast_drag_with_no_intermediate_moves_still_detected(self):
        dist = DRAG_DISTANCE_THRESHOLD + 10
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.1, x=100.0 + dist, y=100.0, button=MouseButton.LEFT),
        ]
        drags = [e for e in detect_drag_events(events) if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        assert drags[0].dx == dist

    def test_keyboard_event_during_drag_is_tolerated(self):
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=150.0, y=150.0),
            KeyDownEvent(timestamp=1.15, key_char="a"),
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            MouseUpEvent(timestamp=1.3, x=200.0, y=200.0, button=MouseButton.LEFT),
        ]
        drags = [e for e in detect_drag_events(events) if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        assert any(isinstance(c, KeyDownEvent) for c in drags[0].children)

    def test_scroll_event_during_drag_is_tolerated(self):
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=150.0, y=150.0),
            MouseScrollEvent(timestamp=1.15, x=150.0, y=150.0, dx=0.0, dy=3.0),
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            MouseUpEvent(timestamp=1.3, x=200.0, y=200.0, button=MouseButton.LEFT),
        ]
        drags = [e for e in detect_drag_events(events) if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        assert any(isinstance(c, MouseScrollEvent) for c in drags[0].children)

    def test_second_button_press_during_drag_keeps_original_drag(self):
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=150.0, y=150.0),
            MouseDownEvent(timestamp=1.15, x=150.0, y=150.0, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=1.2, x=150.0, y=150.0, button=MouseButton.RIGHT),
            MouseMoveEvent(timestamp=1.25, x=200.0, y=200.0),
            MouseUpEvent(timestamp=1.3, x=200.0, y=200.0, button=MouseButton.LEFT),
        ]
        drags = [e for e in detect_drag_events(events) if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        assert drags[0].button == MouseButton.LEFT

    def test_wrong_button_up_does_not_end_drag(self):
        """RMB up mid-LMB-drag is a sibling, not an end. Drag ends on matching LMB up."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=150.0, y=150.0),
            MouseUpEvent(timestamp=1.15, x=150.0, y=150.0, button=MouseButton.RIGHT),
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            MouseUpEvent(timestamp=1.3, x=200.0, y=200.0, button=MouseButton.LEFT),
        ]
        drags = [e for e in detect_drag_events(events) if isinstance(e, MouseDragEvent)]
        assert len(drags) == 1
        assert drags[0].dx == 100.0

    def test_window_event_flushes_incomplete_drag(self):
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=150.0, y=150.0),
            WindowStateEvent(
                timestamp=1.15, title="Test", left=0, top=0,
                width=800, height=600, window_id=1,
            ),
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            MouseUpEvent(timestamp=1.3, x=200.0, y=200.0, button=MouseButton.LEFT),
        ]
        result = detect_drag_events(events)
        assert not any(isinstance(e, MouseDragEvent) for e in result)


class TestGestureEvents:
    def test_consecutive_magnify_events_sum_deltas(self):
        events = [
            MouseMagnifyEvent(timestamp=1.0, x=100, y=100, magnification=0.02),
            MouseMagnifyEvent(timestamp=1.1, x=100, y=100, magnification=0.03),
            MouseMagnifyEvent(timestamp=1.2, x=100, y=100, magnification=0.05),
        ]
        result = merge_consecutive_mouse_magnify_events(events)
        assert len(result) == 1
        assert abs(result[0].magnification - 0.10) < 1e-9

    def test_move_interrupts_creates_separate_magnify_groups(self):
        events = [
            MouseMagnifyEvent(timestamp=1.0, x=100, y=100, magnification=0.02),
            MouseMagnifyEvent(timestamp=1.1, x=100, y=100, magnification=0.03),
            MouseMoveEvent(timestamp=1.2, x=200, y=200),
            MouseMagnifyEvent(timestamp=1.3, x=200, y=200, magnification=0.01),
        ]
        result = merge_consecutive_mouse_magnify_events(events)
        mags = [e for e in result if isinstance(e, MouseMagnifyEvent)]
        assert [round(m.magnification, 2) for m in mags] == [0.05, 0.01]

    def test_smart_magnify_does_not_merge_with_neighbors(self):
        """SmartMagnify is an instantaneous toggle (two-finger double-tap zoom),
        not a continuous gesture; consecutive events stay distinct."""
        events = [
            MouseSmartMagnifyEvent(timestamp=1.0, x=100, y=100),
            MouseSmartMagnifyEvent(timestamp=1.1, x=100, y=100),
        ]
        result = process_events(events)
        assert sum(1 for e in result if isinstance(e, MouseSmartMagnifyEvent)) == 2

    def test_get_action_events_includes_all_gesture_types(self):
        events = [
            MouseMagnifyEvent(timestamp=1.0, x=100, y=100, magnification=0.05),
            MouseRotateEvent(timestamp=1.1, x=100, y=100, rotation=30.0),
            MouseSmartMagnifyEvent(timestamp=1.2, x=100, y=100),
        ]
        assert len(get_action_events(events)) == 3


class TestMergeSequentialKeyTypeEvents:
    """Word-level merge of KeyTypeEvents produced by merge_consecutive_keyboard_events."""

    @staticmethod
    def _kt(char: str, timestamp: float) -> KeyTypeEvent:
        return KeyTypeEvent(
            timestamp=timestamp,
            text=char,
            children=[
                KeyDownEvent(timestamp=timestamp, key_char=char),
                KeyUpEvent(timestamp=timestamp + 0.05, key_char=char),
            ],
        )

    def test_close_keytypes_merge_into_word(self):
        events = [self._kt(c, i * 0.1) for i, c in enumerate("hello")]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 1
        assert result[0].text == "hello"
        assert result[0].timestamp == 0.0
        assert len(result[0].children) == 10  # 5 keys × (down + up)

    @pytest.mark.parametrize("gap_seconds,expected", [
        (KEY_TYPE_MERGE_INTERVAL_SECONDS - 0.1, ["hi"]),
        (KEY_TYPE_MERGE_INTERVAL_SECONDS, ["h", "i"]),
        (KEY_TYPE_MERGE_INTERVAL_SECONDS + 0.1, ["h", "i"]),
    ])
    def test_gap_at_or_above_threshold_splits(self, gap_seconds, expected):
        events = [self._kt("h", 0.0), self._kt("i", gap_seconds)]
        assert [r.text for r in merge_sequential_key_type_events(events)] == expected

    def test_punctuation_merges_into_the_word(self):
        events = [self._kt(c, i * 0.1) for i, c in enumerate("he,")]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 1
        assert result[0].text == "he,"

    def test_whitespace_is_a_word_boundary(self):
        events = [self._kt(c, i * 0.1) for i, c in enumerate("hi b")]
        result = merge_sequential_key_type_events(events)
        assert [r.text for r in result] == ["hi", " ", "b"]

    def test_nonprintable_keytype_is_a_word_boundary(self):
        backspace = KeyTypeEvent(
            timestamp=0.2, text="",
            children=[
                KeyDownEvent(timestamp=0.2, key_name="backspace"),
                KeyUpEvent(timestamp=0.25, key_name="backspace"),
            ],
        )
        events = [self._kt("h", 0.0), self._kt("e", 0.1), backspace, self._kt("l", 0.3)]
        result = merge_sequential_key_type_events(events)
        assert [r.text for r in result] == ["he", "", "l"]

    def test_keyshortcut_is_a_word_boundary(self):
        shortcut = KeyShortcutEvent(
            timestamp=0.3,
            keys=["ctrl", "z"],
            children=[
                KeyDownEvent(timestamp=0.3, key_name="ctrl"),
                KeyDownEvent(timestamp=0.31, key_char="z"),
                KeyUpEvent(timestamp=0.35, key_char="z"),
                KeyUpEvent(timestamp=0.36, key_name="ctrl"),
            ],
        )
        events = [self._kt("a", 0.0), self._kt("b", 0.1), shortcut, self._kt("c", 0.4)]
        result = merge_sequential_key_type_events(events)
        assert [type(r).__name__ for r in result] == [
            "KeyTypeEvent", "KeyShortcutEvent", "KeyTypeEvent",
        ]
        assert (result[0].text, result[2].text) == ("ab", "c")

    def test_non_keyboard_event_is_a_word_boundary(self):
        events = [
            self._kt("a", 0.0),
            self._kt("b", 0.1),
            MouseMoveEvent(timestamp=0.2, x=100.0, y=100.0),
            self._kt("c", 0.3),
        ]
        result = merge_sequential_key_type_events(events)
        assert [type(r).__name__ for r in result] == [
            "KeyTypeEvent", "MouseMoveEvent", "KeyTypeEvent",
        ]
        assert (result[0].text, result[2].text) == ("ab", "c")

    def test_interval_zero_disables_merging(self):
        events = [self._kt(c, i * 0.1) for i, c in enumerate("abc")]
        result = merge_sequential_key_type_events(events, interval=0)
        assert [r.text for r in result] == ["a", "b", "c"]

    def test_multi_char_keytype_event_participates_in_merge(self):
        """A KeyTypeEvent already carrying multi-char text (auto-replace, IME)
        joins the surrounding word."""
        first = KeyTypeEvent(
            timestamp=0.0, text="th",
            children=[
                KeyDownEvent(timestamp=0.0, key_char="t"),
                KeyDownEvent(timestamp=0.02, key_char="h"),
                KeyUpEvent(timestamp=0.04, key_char="t"),
                KeyUpEvent(timestamp=0.05, key_char="h"),
            ],
        )
        events = [first, self._kt("e", 0.1)]
        result = merge_sequential_key_type_events(events)
        assert len(result) == 1
        assert result[0].text == "the"


class TestProcessEvents:
    def test_pipeline_merges_moves_clicks_and_typed_text(self):
        events = [
            MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
            MouseMoveEvent(timestamp=1.1, x=100.0, y=100.0),  # redundant
            MouseMoveEvent(timestamp=1.2, x=200.0, y=200.0),
            MouseDownEvent(timestamp=2.0, x=200.0, y=200.0, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=2.1, x=200.0, y=200.0, button=MouseButton.LEFT),
            KeyDownEvent(timestamp=3.0, key_char="a"),
            KeyUpEvent(timestamp=3.1, key_char="a"),
            KeyDownEvent(timestamp=3.2, key_char="b"),
            KeyUpEvent(timestamp=3.3, key_char="b"),
        ]
        result = process_events(events)
        types = [type(e).__name__ for e in result]
        assert "MouseMoveEvent" in types
        assert "MouseClickEvent" in types
        # "a" and "b" are 200ms apart < 500ms — merge into one word
        key_types = [e for e in result if isinstance(e, KeyTypeEvent)]
        assert [k.text for k in key_types] == ["ab"]

    def test_blender_style_workflow_classifies_events_correctly(self):
        """End-to-end on a realistic 3D-app session: orbit drag, pan drag with
        shift, pinch zoom, constrained LMB drag, RMB cancel."""
        dist = DRAG_DISTANCE_THRESHOLD + 20
        ts = 0.0

        def step(dt: float = 0.1) -> float:
            nonlocal ts
            ts += dt
            return ts

        shift_tap = KeyTypeEvent(
            timestamp=step(),
            text="",
            children=[
                KeyDownEvent(timestamp=ts - 0.05, key_name="shift"),
                KeyUpEvent(timestamp=ts, key_name="shift"),
            ],
        )

        events = [
            # MMB orbit
            MouseDownEvent(timestamp=step(), x=400, y=400, button=MouseButton.MIDDLE),
            MouseMoveEvent(timestamp=step(), x=400 + dist, y=400 + dist),
            MouseUpEvent(timestamp=step(), x=400 + dist, y=400 + dist, button=MouseButton.MIDDLE),
            # Shift+MMB pan
            MouseDownEvent(timestamp=step(), x=400, y=400, button=MouseButton.MIDDLE),
            shift_tap,
            MouseMoveEvent(timestamp=step(), x=400, y=400 + dist),
            MouseUpEvent(timestamp=step(), x=400, y=400 + dist, button=MouseButton.MIDDLE),
            # Pinch zoom
            MouseMagnifyEvent(timestamp=step(), x=500, y=500, magnification=0.02),
            MouseMagnifyEvent(timestamp=step(), x=500, y=500, magnification=0.03),
            MouseMagnifyEvent(timestamp=step(), x=500, y=500, magnification=0.05),
            # LMB drag
            MouseDownEvent(timestamp=step(), x=300, y=300, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=step(), x=300 + dist, y=300),
            MouseMoveEvent(timestamp=step(), x=300 + dist * 2, y=300),
            MouseUpEvent(timestamp=step(), x=300 + dist * 2, y=300, button=MouseButton.LEFT),
            # RMB click
            MouseDownEvent(timestamp=step(), x=300, y=300, button=MouseButton.RIGHT),
            MouseUpEvent(timestamp=step(), x=300, y=300, button=MouseButton.RIGHT),
        ]
        result = process_events(events)
        drags = [e for e in result if isinstance(e, MouseDragEvent)]
        magnifies = [e for e in result if isinstance(e, MouseMagnifyEvent)]
        clicks = [e for e in result if isinstance(e, (MouseClickEvent, MouseDoubleClickEvent))]

        assert len(drags) == 3
        assert len(magnifies) == 1
        assert abs(magnifies[0].magnification - 0.10) < 1e-9
        assert len(clicks) >= 1
