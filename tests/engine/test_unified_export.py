"""Tests for screencap.engine.export — the unified export callable.

These tests are pure: no DB, no file I/O, no privacy. The function
under test (``unified_export_events``) is the single source of truth
for the row-to-JSONL transform. Construct dict rows directly, exercise
the full transform, assert on the resulting Pydantic event sequence.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from screencap.engine.events import (
    BaseEvent,
    EventType,
    KeyTypeEvent,
    MouseClickEvent,
    MouseDoubleClickEvent,
    MouseDragEvent,
    MouseMoveEvent,
    WindowSwitchEvent,
)
from screencap.engine.export import unified_export_events


# ---------------------------------------------------------------------------
# Helpers — construct DB-shaped row dicts.
# ---------------------------------------------------------------------------


def _action_row(
    ts: float,
    name: str,
    *,
    mouse_x: float = 0,
    mouse_y: float = 0,
    mouse_button_name: str = "left",
    mouse_pressed: bool | None = None,
    key_char: str | None = None,
    key_name: str | None = None,
    mouse_dx: float = 0,
    mouse_dy: float = 0,
) -> dict:
    """Return a row dict matching action_event DB column shape."""
    return {
        "timestamp": ts,
        "name": name,
        "mouse_x": mouse_x,
        "mouse_y": mouse_y,
        "mouse_button_name": mouse_button_name,
        "mouse_pressed": mouse_pressed,
        "mouse_pressure": None,
        "modifier_flags": None,
        "mouse_dx": mouse_dx,
        "mouse_dy": mouse_dy,
        "key_char": key_char,
        "key_name": key_name,
        "key_vk": None,
        "canonical_key_char": key_char,
        "canonical_key_name": key_name,
        "canonical_key_vk": None,
        "scroll_phase": None,
        "momentum_phase": None,
        "is_continuous": None,
    }


def _window_row(
    ts: float,
    *,
    bundle_id: str = "com.apple.finder",
    window_id: str = "1",
    title: str = "Finder",
    browser_url: str | None = None,
) -> dict:
    """Return a row dict matching window_event DB column shape."""
    return {
        "timestamp": ts,
        "app_bundle_id": bundle_id,
        "title": title,
        "window_id": window_id,
        "left": 0,
        "top": 0,
        "width": 800,
        "height": 600,
        "browser_url": browser_url,
    }


# ---------------------------------------------------------------------------
# Return-type contract (R18: Iterator[BaseEvent]).
# ---------------------------------------------------------------------------


class TestReturnType:
    """The function returns an Iterator, not a list."""

    def test_returns_iterator(self):
        result = unified_export_events([], [])
        # Iterator: yields and is consumed. Not a list.
        assert iter(result) is result or hasattr(result, "__next__")
        assert not isinstance(result, list)

    def test_iterator_is_exhausted_after_consumption(self):
        """A second pass returns no events — iterators are not re-iterable."""
        rows = [_window_row(1.0)]
        result = unified_export_events([], rows)
        first_pass = list(result)
        second_pass = list(result)
        assert len(first_pass) == 1
        assert second_pass == []


# ---------------------------------------------------------------------------
# Happy paths — process_events is invoked.
# ---------------------------------------------------------------------------


class TestProcessEventsInvoked:
    """The unified callable runs ``process_events`` on action rows."""

    def test_click_pair_becomes_singleclick(self):
        """A mouse-down + mouse-up pair at same position → mouse.singleclick."""
        action_rows = [
            _action_row(1.0, "click", mouse_x=100, mouse_y=200, mouse_pressed=True),
            _action_row(1.05, "click", mouse_x=100, mouse_y=200, mouse_pressed=False),
        ]
        result = list(unified_export_events(action_rows, []))
        assert len(result) == 1
        assert isinstance(result[0], MouseClickEvent)
        assert result[0].x == 100
        assert result[0].y == 200

    def test_double_click_pair_becomes_doubleclick(self):
        """Two click pairs within default interval → mouse.doubleclick."""
        action_rows = [
            _action_row(1.0, "click", mouse_x=100, mouse_y=200, mouse_pressed=True),
            _action_row(1.02, "click", mouse_x=100, mouse_y=200, mouse_pressed=False),
            _action_row(1.10, "click", mouse_x=100, mouse_y=200, mouse_pressed=True),
            _action_row(1.12, "click", mouse_x=100, mouse_y=200, mouse_pressed=False),
        ]
        result = list(unified_export_events(action_rows, []))
        assert len(result) == 1
        assert isinstance(result[0], MouseDoubleClickEvent)


class TestMouseMoveRetained:
    """R7: the unified callable does NOT filter mouse.move events."""

    def test_standalone_mouse_move_passes_through(self):
        """A mouse.move with no surrounding click is retained (R7)."""
        action_rows = [
            _action_row(1.0, "move", mouse_x=10, mouse_y=20),
        ]
        result = list(unified_export_events(action_rows, []))
        assert len(result) == 1
        assert isinstance(result[0], MouseMoveEvent)
        assert result[0].x == 10

    def test_mouse_moves_appear_in_mixed_input(self):
        """Mouse.move events survive alongside actions (R7)."""
        action_rows = [
            _action_row(1.0, "move", mouse_x=10, mouse_y=20),
            _action_row(2.0, "click", mouse_x=50, mouse_y=60, mouse_pressed=True),
            _action_row(2.05, "click", mouse_x=50, mouse_y=60, mouse_pressed=False),
            _action_row(3.0, "move", mouse_x=70, mouse_y=80),
        ]
        result = list(unified_export_events(action_rows, []))
        types = [type(e) for e in result]
        assert MouseMoveEvent in types
        # exactly two MouseMoveEvents (one before, one after the click)
        assert sum(1 for e in result if isinstance(e, MouseMoveEvent)) == 2


# ---------------------------------------------------------------------------
# Window dedup behavior.
# ---------------------------------------------------------------------------


class TestWindowDedup:
    """``deduplicate_window_events`` is invoked on window_rows."""

    def test_two_window_rows_same_key_emit_one_switch(self):
        """Two rows sharing (app_bundle_id, window_id) collapse to one switch."""
        window_rows = [
            _window_row(1.0, bundle_id="com.apple.finder", window_id="1", title="Docs"),
            _window_row(2.0, bundle_id="com.apple.finder", window_id="1", title="Downloads"),
        ]
        result = list(unified_export_events([], window_rows))
        switches = [e for e in result if isinstance(e, WindowSwitchEvent)]
        assert len(switches) == 1
        # First-occurrence title preserved.
        assert switches[0].window_title == "Docs"

    def test_different_window_ids_emit_separate_switches(self):
        window_rows = [
            _window_row(1.0, bundle_id="com.apple.finder", window_id="1"),
            _window_row(2.0, bundle_id="com.apple.finder", window_id="2"),
        ]
        result = list(unified_export_events([], window_rows))
        switches = [e for e in result if isinstance(e, WindowSwitchEvent)]
        assert len(switches) == 2


# ---------------------------------------------------------------------------
# Interleaving by timestamp.
# ---------------------------------------------------------------------------


class TestInterleave:
    """``interleave_window_events`` produces a time-sorted output."""

    def test_window_and_action_events_interleaved_by_timestamp(self):
        action_rows = [
            _action_row(2.0, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(2.05, "click", mouse_x=10, mouse_y=20, mouse_pressed=False),
            _action_row(5.0, "move", mouse_x=50, mouse_y=60),
        ]
        window_rows = [
            _window_row(1.0, bundle_id="com.apple.finder", window_id="1"),
            _window_row(4.0, bundle_id="com.google.Chrome", window_id="2"),
        ]
        result = list(unified_export_events(action_rows, window_rows))
        # Timestamps must be non-decreasing.
        timestamps = [e.timestamp for e in result]
        assert timestamps == sorted(timestamps)
        # First event is the window switch at t=1.0.
        assert isinstance(result[0], WindowSwitchEvent)
        assert result[0].timestamp == 1.0


# ---------------------------------------------------------------------------
# initial_window_row prepending (R3, R15).
# ---------------------------------------------------------------------------


class TestInitialWindowRow:
    """R3 + R15: ``initial_window_row`` is unconditional; prepended when given."""

    def test_initial_window_row_appears_first(self):
        """A pre-rewritten initial row appears as the first event."""
        # Caller has already rewritten the timestamp to start_ts - 0.001 (R3).
        initial = _window_row(0.999, bundle_id="com.apple.finder", window_id="0", title="Old")
        action_rows = [
            _action_row(2.0, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(2.05, "click", mouse_x=10, mouse_y=20, mouse_pressed=False),
        ]
        window_rows = [
            _window_row(3.0, bundle_id="com.google.Chrome", window_id="2"),
        ]
        result = list(unified_export_events(
            action_rows, window_rows, initial_window_row=initial,
        ))
        assert isinstance(result[0], WindowSwitchEvent)
        assert result[0].timestamp == 0.999
        assert result[0].window_id == "0"

    def test_initial_window_row_is_deduplicated_against_first_chunk_window(self):
        """If chunk's first window row matches initial row's key, dedup collapses."""
        initial = _window_row(0.999, bundle_id="com.apple.finder", window_id="1")
        window_rows = [
            _window_row(1.5, bundle_id="com.apple.finder", window_id="1", title="NewTitle"),
        ]
        result = list(unified_export_events(
            [], window_rows, initial_window_row=initial,
        ))
        # Same (bundle, window_id) → one switch (first occurrence wins).
        switches = [e for e in result if isinstance(e, WindowSwitchEvent)]
        assert len(switches) == 1
        assert switches[0].timestamp == 0.999

    def test_initial_window_row_none_omits_synthetic_event(self):
        """``initial_window_row=None`` → output starts with first time-ordered event."""
        action_rows = [
            _action_row(1.0, "move", mouse_x=10, mouse_y=20),
        ]
        result = list(unified_export_events(action_rows, [], initial_window_row=None))
        assert len(result) == 1
        assert isinstance(result[0], MouseMoveEvent)


# ---------------------------------------------------------------------------
# Click threshold overrides (R4).
# ---------------------------------------------------------------------------


class TestClickThresholdOverrides:
    """R4: ``double_click_interval`` and ``double_click_distance`` propagate."""

    def test_tight_double_click_interval_prevents_doubleclick(self):
        """Pair separated by 0.4s with override interval=0.3s → two singleclicks."""
        # Two click pairs 0.4s apart
        action_rows = [
            _action_row(1.0, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(1.02, "click", mouse_x=10, mouse_y=20, mouse_pressed=False),
            _action_row(1.40, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(1.42, "click", mouse_x=10, mouse_y=20, mouse_pressed=False),
        ]
        # With default 0.5s interval, this would be a doubleclick.
        result_default = list(unified_export_events(action_rows, []))
        assert len(result_default) == 1
        assert isinstance(result_default[0], MouseDoubleClickEvent)

        # With tight 0.3s override, the second pair is too late → two singleclicks.
        result_tight = list(unified_export_events(
            action_rows, [], double_click_interval=0.3,
        ))
        assert len(result_tight) == 2
        assert all(isinstance(e, MouseClickEvent) for e in result_tight)

    def test_distance_override_prevents_doubleclick(self):
        """Two click pairs 4px apart with distance override=2px → two singleclicks."""
        action_rows = [
            _action_row(1.0, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(1.02, "click", mouse_x=10, mouse_y=20, mouse_pressed=False),
            _action_row(1.10, "click", mouse_x=14, mouse_y=20, mouse_pressed=True),
            _action_row(1.12, "click", mouse_x=14, mouse_y=20, mouse_pressed=False),
        ]
        # Default 5px → doubleclick.
        result_default = list(unified_export_events(action_rows, []))
        assert len(result_default) == 1
        assert isinstance(result_default[0], MouseDoubleClickEvent)

        # Tight 2px override → two singleclicks.
        result_tight = list(unified_export_events(
            action_rows, [], double_click_distance=2.0,
        ))
        assert len(result_tight) == 2
        assert all(isinstance(e, MouseClickEvent) for e in result_tight)

    def test_key_type_merge_interval_override(self):
        """Tight key_type_merge_interval keeps adjacent KeyTypeEvents distinct."""
        # Two key presses 0.4s apart.
        action_rows = [
            _action_row(1.0, "press", key_char="a"),
            _action_row(1.05, "release", key_char="a"),
            _action_row(1.40, "press", key_char="b"),
            _action_row(1.45, "release", key_char="b"),
        ]
        # Default 0.5s → adjacent KeyTypeEvents merge into one.
        result_default = list(unified_export_events(action_rows, []))
        key_types_default = [e for e in result_default if isinstance(e, KeyTypeEvent)]
        assert len(key_types_default) == 1
        assert key_types_default[0].text == "ab"

        # Tight 0.2s override → second key beyond merge window → two events.
        result_tight = list(unified_export_events(
            action_rows, [], key_type_merge_interval=0.2,
        ))
        key_types_tight = [e for e in result_tight if isinstance(e, KeyTypeEvent)]
        assert len(key_types_tight) == 2


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Empty inputs, malformed rows, default behaviors."""

    def test_empty_inputs_yields_empty_iterator(self):
        result = list(unified_export_events([], []))
        assert result == []

    def test_only_action_rows_no_windows(self):
        action_rows = [
            _action_row(1.0, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(1.05, "click", mouse_x=10, mouse_y=20, mouse_pressed=False),
        ]
        result = list(unified_export_events(action_rows, []))
        assert len(result) == 1
        assert isinstance(result[0], MouseClickEvent)

    def test_only_window_rows_no_actions(self):
        window_rows = [_window_row(1.0)]
        result = list(unified_export_events([], window_rows))
        assert len(result) == 1
        assert isinstance(result[0], WindowSwitchEvent)

    def test_all_malformed_action_rows_yields_no_events(self):
        """When ``dict_to_action_event`` returns None for every row, no errors."""
        # name=None and unrecognized name both return None from converter.
        action_rows = [
            {"timestamp": 1.0, "name": None},
            {"timestamp": 2.0, "name": "unknown_event_type"},
            # click row with mouse_pressed=None → converter returns None.
            _action_row(3.0, "click", mouse_pressed=None),
        ]
        result = list(unified_export_events(action_rows, []))
        assert result == []

    def test_window_filter_default_none_passes_all_through(self):
        """``window_filter=None`` (default) → all dedup'd window events emitted."""
        window_rows = [
            _window_row(1.0, bundle_id="com.apple.finder", window_id="1"),
            _window_row(2.0, bundle_id="com.google.Chrome", window_id="2"),
        ]
        result = list(unified_export_events([], window_rows))
        switches = [e for e in result if isinstance(e, WindowSwitchEvent)]
        assert len(switches) == 2


# ---------------------------------------------------------------------------
# window_filter (R5).
# ---------------------------------------------------------------------------


class TestWindowFilter:
    """R5: optional ``window_filter`` callable; ``None`` returns drop the event."""

    def test_window_filter_drops_none_returns(self):
        """Filter returning None → event suppressed."""
        window_rows = [
            _window_row(1.0, bundle_id="com.apple.finder", window_id="1"),
            _window_row(2.0, bundle_id="com.google.Chrome", window_id="2"),
        ]

        def drop_chrome(ws: WindowSwitchEvent) -> WindowSwitchEvent | None:
            if ws.app_bundle_id == "com.google.Chrome":
                return None
            return ws

        result = list(unified_export_events(
            [], window_rows, window_filter=drop_chrome,
        ))
        switches = [e for e in result if isinstance(e, WindowSwitchEvent)]
        assert len(switches) == 1
        assert switches[0].app_bundle_id == "com.apple.finder"

    def test_window_filter_drops_all(self):
        """Filter returning None for everything → zero switches in output."""
        action_rows = [
            _action_row(1.0, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(1.05, "click", mouse_x=10, mouse_y=20, mouse_pressed=False),
        ]
        window_rows = [
            _window_row(0.5, bundle_id="com.apple.finder", window_id="1"),
            _window_row(2.0, bundle_id="com.google.Chrome", window_id="2"),
        ]

        def drop_all(ws):
            return None

        result = list(unified_export_events(
            action_rows, window_rows, window_filter=drop_all,
        ))
        # No window switches; only the click survives.
        switches = [e for e in result if isinstance(e, WindowSwitchEvent)]
        assert switches == []
        non_switches = [e for e in result if not isinstance(e, WindowSwitchEvent)]
        assert len(non_switches) == 1
        assert isinstance(non_switches[0], MouseClickEvent)

    def test_window_filter_can_modify_event(self):
        """Filter can return a modified WindowSwitchEvent (e.g., masked title)."""
        window_rows = [
            _window_row(1.0, bundle_id="com.apple.finder", window_id="1", title="Secret"),
        ]

        def mask_title(ws):
            return ws.model_copy(update={"window_title": ws.app_name, "domain": None})

        result = list(unified_export_events(
            [], window_rows, window_filter=mask_title,
        ))
        switches = [e for e in result if isinstance(e, WindowSwitchEvent)]
        assert len(switches) == 1
        # Title replaced with app_name (derived from bundle_id).
        assert switches[0].window_title == switches[0].app_name


# ---------------------------------------------------------------------------
# Exception propagation.
# ---------------------------------------------------------------------------


class TestExceptionPropagation:
    """Exceptions from process_events / interleave propagate, not swallowed."""

    def test_window_filter_exception_propagates(self):
        """Exception raised inside window_filter propagates to the consumer."""
        window_rows = [_window_row(1.0)]

        def boom(ws):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            list(unified_export_events([], window_rows, window_filter=boom))


# ---------------------------------------------------------------------------
# Integration — mixed actions, windows, and a drag with move children.
# ---------------------------------------------------------------------------


class TestIntegration:
    """Recording-style fixture verifying the full pipeline end-to-end."""

    def test_mixed_actions_windows_and_drag_against_hand_computed_output(self):
        """A representative input → time-sorted output containing the expected types.

        Sequence:
          - t=1.0: window switch (Finder)
          - t=2.0..2.20: drag (down @ (10,20), moves, up @ (60,20)) — 50px → drag
          - t=3.0: window switch (Chrome)
          - t=4.0: standalone mouse.move (R7 — must survive)
          - t=5.0..5.05: click pair → singleclick
        """
        action_rows = [
            # Drag: down at (10,20), several moves, up at (60,20). Distance 50px.
            _action_row(2.00, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(2.05, "move", mouse_x=20, mouse_y=20),
            _action_row(2.10, "move", mouse_x=40, mouse_y=20),
            _action_row(2.15, "move", mouse_x=60, mouse_y=20),
            _action_row(2.20, "click", mouse_x=60, mouse_y=20, mouse_pressed=False),
            # Standalone mouse.move (after drag, before click)
            _action_row(4.00, "move", mouse_x=100, mouse_y=200),
            # Click pair → singleclick
            _action_row(5.00, "click", mouse_x=300, mouse_y=400, mouse_pressed=True),
            _action_row(5.05, "click", mouse_x=300, mouse_y=400, mouse_pressed=False),
        ]
        window_rows = [
            _window_row(1.0, bundle_id="com.apple.finder", window_id="1", title="Finder"),
            _window_row(3.0, bundle_id="com.google.Chrome", window_id="2", title="Chrome"),
        ]
        result = list(unified_export_events(action_rows, window_rows))

        # Time-sorted invariant.
        timestamps = [e.timestamp for e in result]
        assert timestamps == sorted(timestamps)

        # Type breakdown:
        type_counts: dict[type, int] = {}
        for e in result:
            type_counts[type(e)] = type_counts.get(type(e), 0) + 1

        # Two window switches.
        assert type_counts.get(WindowSwitchEvent, 0) == 2
        # One drag.
        assert type_counts.get(MouseDragEvent, 0) == 1
        # One singleclick.
        assert type_counts.get(MouseClickEvent, 0) == 1
        # The standalone mouse.move at t=4.0 survives (R7).
        # (The drag's children moves are nested inside the drag, not top-level.)
        move_events = [e for e in result if isinstance(e, MouseMoveEvent)]
        assert len(move_events) == 1
        assert move_events[0].timestamp == 4.0

        # Total top-level events: 2 windows + 1 drag + 1 move + 1 click = 5.
        assert len(result) == 5


# ---------------------------------------------------------------------------
# Type contract — the iterator yields BaseEvent instances.
# ---------------------------------------------------------------------------


class TestTypeContract:
    """Every yielded event subclasses ``BaseEvent`` and has a ``type`` field."""

    def test_all_yielded_items_are_base_events(self):
        action_rows = [
            _action_row(2.0, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(2.05, "click", mouse_x=10, mouse_y=20, mouse_pressed=False),
        ]
        window_rows = [_window_row(1.0)]
        for evt in unified_export_events(action_rows, window_rows):
            assert isinstance(evt, BaseEvent)
            # use_enum_values stores .type as the string value.
            assert evt.type in {t.value for t in EventType}
