"""Tests for keyboard shortcut detection."""

from sc_engine.events import (
    KeyDownEvent,
    KeyShortcutEvent,
    KeyTypeEvent,
    KeyUpEvent,
    MouseButton,
    MouseDownEvent,
    MouseDragEvent,
    MouseMoveEvent,
    MouseScrollEvent,
    MouseUpEvent,
)
from sc_engine.processing import (
    detect_key_shortcuts,
    merge_consecutive_keyboard_events,
    process_events,
)


def _make_key_type(timestamp, children):
    """Helper: create a KeyTypeEvent from children, extracting text from KeyDownEvent chars."""
    chars = []
    for c in children:
        if isinstance(c, KeyDownEvent) and c.key_char:
            chars.append(c.key_char)
    return KeyTypeEvent(
        timestamp=timestamp,
        text="".join(chars),
        children=children,
    )


class TestDetectKeyShortcuts:
    """Tests for detect_key_shortcuts()."""

    def test_ctrl_z(self):
        """Ctrl+Z → KeyShortcutEvent(keys=["ctrl", "z"], text="Ctrl+z")."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="ctrl"),
            KeyDownEvent(timestamp=1.05, key_char="z"),
            KeyUpEvent(timestamp=1.1, key_char="z"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyShortcutEvent)
        assert result[0].keys == ["ctrl", "z"]
        assert result[0].text == "Ctrl+z"

    def test_ctrl_shift_z(self):
        """Ctrl+Shift+Z → KeyShortcutEvent(keys=["ctrl", "shift", "z"])."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="ctrl"),
            KeyDownEvent(timestamp=1.02, key_name="shift"),
            KeyDownEvent(timestamp=1.05, key_char="z"),
            KeyUpEvent(timestamp=1.1, key_char="z"),
            KeyUpEvent(timestamp=1.12, key_name="shift"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyShortcutEvent)
        assert result[0].keys == ["ctrl", "shift", "z"]
        assert result[0].text == "Ctrl+Shift+z"

    def test_shift_a_stays_as_key_type(self):
        """Shift+A → stays as KeyTypeEvent (typing, not shortcut)."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="shift"),
            KeyDownEvent(timestamp=1.05, key_char="A"),
            KeyUpEvent(timestamp=1.1, key_char="A"),
            KeyUpEvent(timestamp=1.15, key_name="shift"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyTypeEvent)

    def test_shift_tab_is_shortcut(self):
        """Shift+Tab → KeyShortcutEvent (non-printable key)."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="shift"),
            KeyDownEvent(timestamp=1.05, key_name="tab"),
            KeyUpEvent(timestamp=1.1, key_name="tab"),
            KeyUpEvent(timestamp=1.15, key_name="shift"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyShortcutEvent)
        assert result[0].keys == ["shift", "tab"]
        assert result[0].text == "Shift+tab"

    def test_standalone_ctrl_stays_as_key_type(self):
        """Standalone Ctrl press/release → stays as KeyTypeEvent."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="ctrl"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyTypeEvent)

    def test_ctrl_shift_no_regular_key_stays_as_key_type(self):
        """Ctrl+Shift (no regular key) → stays as KeyTypeEvent."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="ctrl"),
            KeyDownEvent(timestamp=1.02, key_name="shift"),
            KeyUpEvent(timestamp=1.1, key_name="shift"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyTypeEvent)

    def test_cmd_c(self):
        """Cmd+C → KeyShortcutEvent."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="cmd"),
            KeyDownEvent(timestamp=1.05, key_char="c"),
            KeyUpEvent(timestamp=1.1, key_char="c"),
            KeyUpEvent(timestamp=1.15, key_name="cmd"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyShortcutEvent)
        assert result[0].keys == ["cmd", "c"]
        assert result[0].text == "Cmd+c"

    def test_alt_f4(self):
        """Alt+F4 → KeyShortcutEvent."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="alt"),
            KeyDownEvent(timestamp=1.05, key_name="f4"),
            KeyUpEvent(timestamp=1.1, key_name="f4"),
            KeyUpEvent(timestamp=1.15, key_name="alt"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyShortcutEvent)
        assert result[0].keys == ["alt", "f4"]
        assert result[0].text == "Alt+f4"

    def test_modifier_variants_normalized(self):
        """Left/right modifier variants are normalized."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="ctrl_l"),
            KeyDownEvent(timestamp=1.05, key_char="z"),
            KeyUpEvent(timestamp=1.1, key_char="z"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl_l"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyShortcutEvent)
        assert result[0].keys == ["ctrl", "z"]

    def test_canonical_key_ordering(self):
        """Keys are always in canonical order regardless of press order."""
        # Press shift before ctrl
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="shift"),
            KeyDownEvent(timestamp=1.02, key_name="ctrl"),
            KeyDownEvent(timestamp=1.05, key_char="z"),
            KeyUpEvent(timestamp=1.1, key_char="z"),
            KeyUpEvent(timestamp=1.12, key_name="ctrl"),
            KeyUpEvent(timestamp=1.15, key_name="shift"),
        ])
        result = detect_key_shortcuts([event])
        assert result[0].keys == ["ctrl", "shift", "z"]

    def test_preserves_timestamp(self):
        """KeyShortcutEvent preserves original timestamp."""
        event = _make_key_type(42.5, [
            KeyDownEvent(timestamp=42.5, key_name="ctrl"),
            KeyDownEvent(timestamp=42.55, key_char="z"),
            KeyUpEvent(timestamp=42.6, key_char="z"),
            KeyUpEvent(timestamp=42.65, key_name="ctrl"),
        ])
        result = detect_key_shortcuts([event])
        assert result[0].timestamp == 42.5

    def test_preserves_children(self):
        """KeyShortcutEvent preserves all children from original KeyTypeEvent."""
        children = [
            KeyDownEvent(timestamp=1.0, key_name="ctrl"),
            KeyDownEvent(timestamp=1.05, key_char="z"),
            KeyUpEvent(timestamp=1.1, key_char="z"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
        ]
        event = _make_key_type(1.0, children)
        result = detect_key_shortcuts([event])
        assert len(result[0].children) == 4

    def test_non_keyboard_events_pass_through(self):
        """Non-KeyTypeEvent events pass through unchanged."""
        move = MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0)
        result = detect_key_shortcuts([move])
        assert len(result) == 1
        assert result[0] is move

    def test_mixed_events(self):
        """Mix of shortcuts and regular typing."""
        shortcut = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="ctrl"),
            KeyDownEvent(timestamp=1.05, key_char="z"),
            KeyUpEvent(timestamp=1.1, key_char="z"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
        ])
        regular = _make_key_type(2.0, [
            KeyDownEvent(timestamp=2.0, key_char="a"),
            KeyUpEvent(timestamp=2.1, key_char="a"),
        ])
        result = detect_key_shortcuts([shortcut, regular])
        assert len(result) == 2
        assert isinstance(result[0], KeyShortcutEvent)
        assert isinstance(result[1], KeyTypeEvent)

    def test_shift_number_stays_as_key_type(self):
        """Shift+1 (producing '!') → stays as KeyTypeEvent."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="shift"),
            KeyDownEvent(timestamp=1.05, key_char="!"),
            KeyUpEvent(timestamp=1.1, key_char="!"),
            KeyUpEvent(timestamp=1.15, key_name="shift"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyTypeEvent)

    def test_ctrl_shift_a_is_shortcut(self):
        """Ctrl+Shift+A → shortcut (Ctrl overrides the shift+printable rule)."""
        event = _make_key_type(1.0, [
            KeyDownEvent(timestamp=1.0, key_name="ctrl"),
            KeyDownEvent(timestamp=1.02, key_name="shift"),
            KeyDownEvent(timestamp=1.05, key_char="a"),
            KeyUpEvent(timestamp=1.1, key_char="a"),
            KeyUpEvent(timestamp=1.12, key_name="shift"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
        ])
        result = detect_key_shortcuts([event])
        assert len(result) == 1
        assert isinstance(result[0], KeyShortcutEvent)
        assert result[0].keys == ["ctrl", "shift", "a"]


class TestKeyShortcutModel:
    """Tests for KeyShortcutEvent Pydantic model."""

    def test_computed_text_field(self):
        """text is computed from keys."""
        event = KeyShortcutEvent(
            timestamp=1.0,
            keys=["ctrl", "shift", "z"],
        )
        assert event.text == "Ctrl+Shift+z"

    def test_serialization_includes_text(self):
        """model_dump_json includes computed text field."""
        event = KeyShortcutEvent(
            timestamp=1.0,
            keys=["ctrl", "z"],
        )
        import json
        data = json.loads(event.model_dump_json())
        assert data["text"] == "Ctrl+z"
        assert data["keys"] == ["ctrl", "z"]
        assert data["type"] == "key.shortcut"

    def test_event_type(self):
        """Event type is KEY_SHORTCUT."""
        event = KeyShortcutEvent(
            timestamp=1.0,
            keys=["ctrl", "z"],
        )
        assert event.type == "key.shortcut"


class TestKeyShortcutInPipeline:
    """Tests for shortcut detection within the full processing pipeline."""

    def test_ctrl_z_through_full_pipeline(self):
        """Ctrl+Z through process_events() becomes KeyShortcutEvent."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="ctrl"),
            KeyDownEvent(timestamp=1.05, key_char="z"),
            KeyUpEvent(timestamp=1.1, key_char="z"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
        ]
        result = process_events(events)
        assert len(result) == 1
        assert isinstance(result[0], KeyShortcutEvent)
        assert result[0].keys == ["ctrl", "z"]

    def test_regular_typing_unaffected_by_pipeline(self):
        """Regular typing still produces KeyTypeEvent after pipeline."""
        events = [
            KeyDownEvent(timestamp=1.0, key_char="a"),
            KeyUpEvent(timestamp=1.1, key_char="a"),
        ]
        result = process_events(events)
        assert len(result) == 1
        assert isinstance(result[0], KeyTypeEvent)
        assert result[0].text == "a"

    def test_shortcut_during_drag(self):
        """Shortcut mid-drag is tolerated and included in drag children."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
            MouseMoveEvent(timestamp=1.1, x=150.0, y=150.0),
            # Ctrl+Z shortcut mid-drag
            KeyDownEvent(timestamp=1.2, key_name="ctrl"),
            KeyDownEvent(timestamp=1.25, key_char="z"),
            KeyUpEvent(timestamp=1.3, key_char="z"),
            KeyUpEvent(timestamp=1.35, key_name="ctrl"),
            MouseMoveEvent(timestamp=1.4, x=200.0, y=200.0),
            MouseUpEvent(timestamp=1.5, x=200.0, y=200.0, button=MouseButton.LEFT),
        ]
        result = process_events(events)
        # Should have: KeyShortcutEvent (inline) + MouseDragEvent
        drag_events = [e for e in result if isinstance(e, MouseDragEvent)]
        assert len(drag_events) == 1
        drag = drag_events[0]
        # Drag children should include the KeyShortcutEvent
        shortcut_children = [
            c for c in drag.children if isinstance(c, KeyShortcutEvent)
        ]
        assert len(shortcut_children) == 1
        assert shortcut_children[0].keys == ["ctrl", "z"]

    def test_repeated_shortcuts_separate_events(self):
        """Each Ctrl+Z press/release cycle is a separate KeyShortcutEvent."""
        events = [
            # First Ctrl+Z
            KeyDownEvent(timestamp=1.0, key_name="ctrl"),
            KeyDownEvent(timestamp=1.05, key_char="z"),
            KeyUpEvent(timestamp=1.1, key_char="z"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
            # Second Ctrl+Z
            KeyDownEvent(timestamp=2.0, key_name="ctrl"),
            KeyDownEvent(timestamp=2.05, key_char="z"),
            KeyUpEvent(timestamp=2.1, key_char="z"),
            KeyUpEvent(timestamp=2.15, key_name="ctrl"),
        ]
        result = process_events(events)
        shortcuts = [e for e in result if isinstance(e, KeyShortcutEvent)]
        assert len(shortcuts) == 2
        assert all(s.keys == ["ctrl", "z"] for s in shortcuts)

    def test_mouse_drag_children_type_validates(self):
        """KeyShortcutEvent can be a child of MouseDragEvent (Pydantic validates)."""
        shortcut = KeyShortcutEvent(
            timestamp=1.2,
            keys=["ctrl", "z"],
            children=[
                KeyDownEvent(timestamp=1.2, key_name="ctrl"),
                KeyDownEvent(timestamp=1.25, key_char="z"),
                KeyUpEvent(timestamp=1.3, key_char="z"),
                KeyUpEvent(timestamp=1.35, key_name="ctrl"),
            ],
        )
        # Should not raise Pydantic validation error
        drag = MouseDragEvent(
            timestamp=1.0,
            x=100.0,
            y=100.0,
            dx=100.0,
            dy=100.0,
            button=MouseButton.LEFT,
            children=[
                MouseDownEvent(timestamp=1.0, x=100.0, y=100.0, button=MouseButton.LEFT),
                shortcut,
                MouseUpEvent(timestamp=1.5, x=200.0, y=200.0, button=MouseButton.LEFT),
            ],
        )
        assert any(isinstance(c, KeyShortcutEvent) for c in drag.children)


class TestShortcutsWithInterleavedMouseEvents:
    """Pipeline integration tests: shortcuts detected despite interleaved mouse events."""

    def test_cmd_c_through_full_pipeline(self):
        """Cmd+C with MouseMove between modifier and letter → KeyShortcutEvent."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="cmd"),
            MouseMoveEvent(timestamp=1.02, x=200, y=200),
            KeyDownEvent(timestamp=1.05, key_char="c"),
            KeyUpEvent(timestamp=1.10, key_char="c"),
            KeyUpEvent(timestamp=1.12, key_name="cmd"),
        ]
        result = process_events(events)

        shortcuts = [e for e in result if isinstance(e, KeyShortcutEvent)]
        mouse_events = [e for e in result if isinstance(e, MouseMoveEvent)]

        assert len(shortcuts) == 1, f"Expected 1 KeyShortcutEvent, got {len(shortcuts)}"
        assert shortcuts[0].keys == ["cmd", "c"]
        assert len(mouse_events) == 1, "MouseMoveEvent should be preserved"

    def test_cmd_shift_z_through_full_pipeline(self):
        """Cmd+Shift+Z with mouse moves between each key → KeyShortcutEvent."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="cmd"),
            MouseMoveEvent(timestamp=1.01, x=100, y=100),
            KeyDownEvent(timestamp=1.03, key_name="shift"),
            MouseMoveEvent(timestamp=1.04, x=150, y=150),
            KeyDownEvent(timestamp=1.06, key_char="z"),
            KeyUpEvent(timestamp=1.10, key_char="z"),
            KeyUpEvent(timestamp=1.12, key_name="shift"),
            KeyUpEvent(timestamp=1.14, key_name="cmd"),
        ]
        result = process_events(events)

        shortcuts = [e for e in result if isinstance(e, KeyShortcutEvent)]
        mouse_events = [e for e in result if isinstance(e, MouseMoveEvent)]

        assert len(shortcuts) == 1, f"Expected 1 KeyShortcutEvent, got {len(shortcuts)}"
        assert set(shortcuts[0].keys) == {"shift", "cmd", "z"}
        assert len(mouse_events) >= 1, "MouseMoveEvents should be preserved"

    def test_plain_typing_with_mouse_between_chars(self):
        """a-dn, a-up, MouseMove, b-dn, b-up → two separate KeyTypeEvents (no keys held at mouse)."""
        events = [
            KeyDownEvent(timestamp=1.0, key_char="a"),
            KeyUpEvent(timestamp=1.1, key_char="a"),
            MouseMoveEvent(timestamp=1.2, x=200, y=200),
            KeyDownEvent(timestamp=1.3, key_char="b"),
            KeyUpEvent(timestamp=1.4, key_char="b"),
        ]
        result = process_events(events)

        type_events = [e for e in result if isinstance(e, KeyTypeEvent)]
        mouse_events = [e for e in result if isinstance(e, MouseMoveEvent)]

        assert len(type_events) == 2, f"Expected 2 KeyTypeEvents, got {len(type_events)}"
        assert type_events[0].text == "a"
        assert type_events[1].text == "b"
        assert len(mouse_events) == 1

    def test_cmd_c_with_scroll_interleaved(self):
        """Cmd+C with MouseScrollEvent between modifier and letter → KeyShortcutEvent."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="cmd"),
            MouseScrollEvent(timestamp=1.02, x=200, y=200, dx=0, dy=1),
            KeyDownEvent(timestamp=1.05, key_char="c"),
            KeyUpEvent(timestamp=1.10, key_char="c"),
            KeyUpEvent(timestamp=1.12, key_name="cmd"),
        ]
        result = process_events(events)

        shortcuts = [e for e in result if isinstance(e, KeyShortcutEvent)]
        assert len(shortcuts) == 1
        assert shortcuts[0].keys == ["cmd", "c"]

    def test_cmd_click_modifier_after_mouse(self):
        """Cmd held, mouse click, Cmd released → MouseDown/Up emitted inline, KeyTypeEvent after."""
        events = [
            KeyDownEvent(timestamp=1.0, key_name="cmd"),
            MouseDownEvent(timestamp=1.02, x=100, y=100, button=MouseButton.LEFT),
            MouseUpEvent(timestamp=1.05, x=100, y=100, button=MouseButton.LEFT),
            KeyUpEvent(timestamp=1.10, key_name="cmd"),
        ]
        result = process_events(events)

        # The modifier KeyTypeEvent should appear (cmd held, no letter → not a shortcut)
        type_events = [e for e in result if isinstance(e, KeyTypeEvent)]
        assert len(type_events) == 1
        assert type_events[0].text == ""  # modifier-only, no chars
