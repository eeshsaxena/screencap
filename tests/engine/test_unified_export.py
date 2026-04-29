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
        from collections.abc import Iterator

        result = unified_export_events([], [])
        # Iterator: yields and is consumed. Not a list.
        assert isinstance(result, Iterator)
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
# Conversion-exception tolerance (regression: pre-refactor chunk processor's
# try/except around dict_to_action_event was lost, causing a single bad row
# to crash the entire chunk export).
# ---------------------------------------------------------------------------


class TestMalformedRowToleratesExceptions:
    """Rows that raise during ``dict_to_action_event`` are skipped, not propagated.

    Restored from the pre-refactor chunk processor's behavior. Without this,
    one bad row crashes the entire chunk export, and recovery's
    skip-on-error policy then loses the WHOLE chunk instead of just the bad
    row. ``TestEdgeCases.test_all_malformed_action_rows_yields_no_events``
    covers the None-return path; this class covers the raise path.
    """

    def test_row_raising_validation_error_is_skipped(self, caplog):
        """A click row with a non-numeric ``timestamp`` raises Pydantic
        ``ValidationError`` inside the converter. The unified pipeline must
        skip it (with a debug log) and continue processing the surrounding
        good rows, yielding the expected merged event(s).
        """
        # Two good clicks at the same position bracketing a bad row. The
        # good pair (down at t=1.0, up at t=2.0) merges into a singleclick
        # via process_events.
        good_down = _action_row(
            1.0, "click", mouse_x=100, mouse_y=200, mouse_pressed=True,
        )
        good_up = _action_row(
            2.0, "click", mouse_x=100, mouse_y=200, mouse_pressed=False,
        )
        # `timestamp="not-a-number"` triggers a Pydantic float_parsing
        # validation error inside MouseDownEvent construction. Confirmed
        # by direct invocation of dict_to_action_event with this input.
        bad_row = {
            "name": "click",
            "timestamp": "not-a-number",
            "mouse_button_name": "left",
            "mouse_pressed": True,
            "mouse_x": 100,
            "mouse_y": 200,
            "mouse_pressure": None,
            "modifier_flags": None,
            "mouse_dx": 0,
            "mouse_dy": 0,
            "key_char": None,
            "key_name": None,
            "key_vk": None,
            "canonical_key_char": None,
            "canonical_key_name": None,
            "canonical_key_vk": None,
            "scroll_phase": None,
            "momentum_phase": None,
            "is_continuous": None,
        }

        import logging

        with caplog.at_level(logging.DEBUG, logger="screencap.engine.export"):
            events = list(unified_export_events(
                [good_down, bad_row, good_up], [],
            ))

        # The good down/up pair merged into one MouseClickEvent.
        # The bad row was skipped, not propagated.
        assert len(events) == 1
        assert isinstance(events[0], MouseClickEvent)
        assert events[0].x == 100
        assert events[0].y == 200

        # A debug log was emitted for the skipped row, including its
        # malformed timestamp value for traceability.
        assert any(
            "Skipping malformed action event" in rec.message
            and "not-a-number" in rec.message
            for rec in caplog.records
        ), f"Expected skip-debug log; got: {[r.message for r in caplog.records]}"

    def test_multiple_raising_rows_all_skipped(self):
        """Several rows that raise are all skipped; surrounding good rows
        still produce the expected output.
        """
        good_move = _action_row(1.0, "move", mouse_x=10, mouse_y=20)
        # Use a sentinel object that fails Pydantic float coercion
        # (confirmed via direct dict_to_action_event invocation).
        sentinel = object()
        bad1 = {
            "name": "move",
            "timestamp": sentinel,
            "mouse_x": 0,
            "mouse_y": 0,
            "mouse_pressure": None,
            "modifier_flags": None,
        }
        bad2 = {
            "name": "smart_magnify",
            "timestamp": 1.5,
            "mouse_x": sentinel,  # raises TypeError inside float() coercion
            "mouse_y": 0,
        }

        events = list(unified_export_events([good_move, bad1, bad2], []))
        # The single good move survives; both bad rows are skipped.
        assert len(events) == 1
        assert isinstance(events[0], MouseMoveEvent)
        assert events[0].x == 10


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


# ---------------------------------------------------------------------------
# Network row support (V1: parameter passthrough; callers keep network_rows=None).
# ---------------------------------------------------------------------------


def _network_row(
    ts: float,
    *,
    kind: str = "request",
    flow_id: str = "flow-1",
    method: str | None = "GET",
    url: str | None = "https://example.com/api",
    host: str = "example.com",
    status: int | None = None,
    headers_json: str | None = None,
    body_size: int | None = 0,
    body_sha256: bytes | None = None,
    content_type: str | None = "text/plain",
    direction: str | None = None,
    frame_type: str | None = None,
    http_version: str | None = "HTTP/1.1",
    details_json: str | None = None,
    timestamp_ns: int | None = None,
) -> dict:
    """Return a row dict matching network_event DB column shape.

    Only the columns that ``dict_to_network_event`` reads are populated;
    the converter is robust to missing keys via ``row.get`` so we keep
    the helper minimal.
    """
    return {
        "kind": kind,
        "flow_id": flow_id,
        "method": method,
        "url": url,
        "host": host,
        "status": status,
        "headers_json": headers_json,
        "body_size": body_size,
        "body_sha256": body_sha256,
        "content_type": content_type,
        "direction": direction,
        "frame_type": frame_type,
        "http_version": http_version,
        "details_json": details_json,
        "timestamp": ts,
        "timestamp_ns": timestamp_ns if timestamp_ns is not None else int(ts * 1e9),
    }


class TestNetworkRowsParameter:
    """``network_rows`` extension preserves prior behaviour and interleaves correctly."""

    def test_network_rows_none_matches_pre_network_signature(self):
        """``network_rows=None`` (default) → behaves identically to action+window only."""
        action_rows = [
            _action_row(2.0, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(2.05, "click", mouse_x=10, mouse_y=20, mouse_pressed=False),
        ]
        window_rows = [_window_row(1.0)]
        baseline = list(unified_export_events(action_rows, window_rows))
        with_none = list(
            unified_export_events(action_rows, window_rows, network_rows=None),
        )
        assert [type(e) for e in baseline] == [type(e) for e in with_none]
        assert [e.timestamp for e in baseline] == [e.timestamp for e in with_none]

    def test_empty_network_rows_list_matches_baseline(self):
        """``network_rows=[]`` → identical to ``network_rows=None``."""
        action_rows = [
            _action_row(2.0, "move", mouse_x=10, mouse_y=20),
        ]
        baseline = list(unified_export_events(action_rows, []))
        empty = list(unified_export_events(action_rows, [], network_rows=[]))
        assert [type(e) for e in baseline] == [type(e) for e in empty]

    def test_network_rows_interleave_into_combined_stream(self):
        """Network rows produce a single time-ordered iterator alongside actions+windows."""
        action_rows = [
            _action_row(2.0, "click", mouse_x=10, mouse_y=20, mouse_pressed=True),
            _action_row(2.05, "click", mouse_x=10, mouse_y=20, mouse_pressed=False),
        ]
        window_rows = [_window_row(1.0)]
        network_rows = [
            _network_row(1.5, kind="request", host="api.example.com"),
            _network_row(3.0, kind="response", status=200, host="api.example.com"),
        ]
        result = list(unified_export_events(
            action_rows, window_rows, network_rows=network_rows,
        ))
        timestamps = [e.timestamp for e in result]
        assert timestamps == sorted(timestamps), (
            f"Expected non-decreasing timestamps, got {timestamps}"
        )
        types = [e.type for e in result]
        # window.switch at t=1.0, network.request at t=1.5,
        # mouse.singleclick at t=2.0, network.response at t=3.0.
        assert types == [
            "window.switch",
            "network.request",
            "mouse.singleclick",
            "network.response",
        ]

    def test_malformed_network_row_skipped_not_propagated(self, caplog):
        """A row with an invalid ``kind`` or missing required field is skipped, not raised."""
        import logging

        good_request = _network_row(1.0, kind="request", host="api.good.example")
        # ``ws_frame`` requires ``direction in {"sent","received"}``; passing
        # an unknown direction makes ``dict_to_network_event`` return None
        # rather than raising. Use a row that triggers a real exception path
        # by feeding a non-numeric ``status`` to a "response" row (forces
        # int() coercion failure inside the converter's try/except).
        bad_response = _network_row(
            2.0, kind="response", status="not-a-number", host="api.good.example",
        )
        good_response = _network_row(
            3.0, kind="response", status=200, host="api.good.example",
        )

        with caplog.at_level(logging.DEBUG):
            events = list(unified_export_events(
                [], [], network_rows=[good_request, bad_response, good_response],
            ))

        # The two good rows survive; the bad row was skipped without
        # losing the rest of the chunk.
        assert len(events) == 2
        types = [e.type for e in events]
        assert "network.request" in types
        assert "network.response" in types

    def test_unknown_network_kind_is_skipped_silently(self):
        """``dict_to_network_event`` returns ``None`` for unknown kinds → row dropped, no raise."""
        rows = [
            _network_row(1.0, kind="not-a-real-kind", host="x.example"),
            _network_row(2.0, kind="request", host="x.example"),
        ]
        result = list(unified_export_events([], [], network_rows=rows))
        assert len(result) == 1
        assert result[0].type == "network.request"

    def test_three_way_tie_break_window_then_action_then_network(self):
        """Tie-breaker at the same timestamp resolves window-first, action-second, network-third."""
        # All three event sources share timestamp 5.0 exactly.
        action_rows = [
            _action_row(5.0, "move", mouse_x=10, mouse_y=20),
        ]
        window_rows = [_window_row(5.0)]
        network_rows = [_network_row(5.0, kind="request", host="example.com")]

        result = list(unified_export_events(
            action_rows, window_rows, network_rows=network_rows,
        ))
        # Filter to events at t=5.0 in case anything else slips in.
        equal_ts_events = [e for e in result if e.timestamp == 5.0]
        types = [e.type for e in equal_ts_events]
        assert types == ["window.switch", "mouse.move", "network.request"], (
            f"Tie-break order broken: {types}"
        )

    def test_network_rows_only_no_actions_or_windows(self):
        """Network rows alone produce an iterator of only network events."""
        rows = [
            _network_row(1.0, kind="request"),
            _network_row(2.0, kind="response", status=200),
        ]
        result = list(unified_export_events([], [], network_rows=rows))
        assert [e.type for e in result] == ["network.request", "network.response"]

    def test_network_rows_returned_in_iterator_form(self):
        """Network branch preserves the ``Iterator`` return contract."""
        from collections.abc import Iterator as _Iterator

        rows = [_network_row(1.0, kind="request")]
        result = unified_export_events([], [], network_rows=rows)
        assert isinstance(result, _Iterator)
        assert not isinstance(result, list)


# ---------------------------------------------------------------------------
# V1.5: network_scrub_pipeline kwarg
# ---------------------------------------------------------------------------


class _RecordingFakePipeline:
    """Test double for NetworkScrubPipeline.

    Records the capture-side events it receives and returns a marker
    payload via ``decrypt_and_scrub`` so the test can assert the
    pipeline was invoked AND that its output reaches the iterator.
    """

    def __init__(self):
        self.calls: list = []

    def decrypt_and_scrub(self, capture_event):
        self.calls.append(capture_event)
        # Return an export-side event with a marker body_text. The
        # capture-side event types are mapped to export-side classes
        # by the real pipeline; this fake just builds the matching
        # one so the iterator output is well-typed.
        from screencap.engine.events import (
            NetworkRequestEvent,
            NetworkRequestExportEvent,
            NetworkResponseEvent,
            NetworkResponseExportEvent,
            NetworkWebSocketFrameEvent,
            NetworkWebSocketFrameExportEvent,
            NetworkWebSocketUpgradeEvent,
            NetworkWebSocketUpgradeExportEvent,
        )

        if isinstance(capture_event, NetworkRequestEvent):
            cls = NetworkRequestExportEvent
        elif isinstance(capture_event, NetworkResponseEvent):
            cls = NetworkResponseExportEvent
        elif isinstance(capture_event, NetworkWebSocketUpgradeEvent):
            cls = NetworkWebSocketUpgradeExportEvent
        elif isinstance(capture_event, NetworkWebSocketFrameEvent):
            cls = NetworkWebSocketFrameExportEvent
        else:
            raise ValueError(f"unexpected: {type(capture_event)}")

        data = capture_event.model_dump()
        for key in ("body_ciphertext", "body_nonce", "body_aad"):
            data.pop(key, None)
        data["body_text"] = "FAKE-SCRUBBED"
        return cls(**data)


class TestNetworkScrubPipelineKwarg:
    """``network_scrub_pipeline`` is consumed exactly when supplied."""

    def test_pipeline_applied_to_ciphertext_rows(self):
        """When a pipeline is provided, network rows are converted to
        export-side events via the pipeline."""
        from screencap.engine.events import NetworkRequestExportEvent

        pipeline = _RecordingFakePipeline()
        rows = [_network_row(1.0, kind="request", host="x.example")]
        result = list(unified_export_events(
            [], [],
            network_rows=rows,
            network_scrub_pipeline=pipeline,
        ))
        assert len(pipeline.calls) == 1, (
            "Pipeline.decrypt_and_scrub was not invoked for the row"
        )
        assert len(result) == 1
        # The event in the iterator is the export-side class, not the
        # capture-side class -- ciphertext can never reach JSONL by
        # construction.
        assert isinstance(result[0], NetworkRequestExportEvent)
        assert result[0].body_text == "FAKE-SCRUBBED"

    def test_pipeline_none_keeps_capture_side_behaviour(self):
        """Explicit ``network_scrub_pipeline=None`` → capture-side
        events as today (V1 behaviour preserved)."""
        from screencap.engine.events import NetworkRequestEvent

        rows = [_network_row(1.0, kind="request", host="x.example")]
        result = list(unified_export_events(
            [], [],
            network_rows=rows,
            network_scrub_pipeline=None,
        ))
        assert len(result) == 1
        assert isinstance(result[0], NetworkRequestEvent), (
            "Without a pipeline, the iterator must yield capture-side "
            "events identical to today's V1 behaviour."
        )

    def test_drop_burst_passes_through_pipeline_unchanged(self):
        """``NetworkDropBurstEvent`` has no ciphertext fields and
        skips the pipeline -- the pipeline must NOT be invoked for it
        and the drop-burst row must still surface in the iterator."""
        from screencap.engine.events import NetworkDropBurstEvent

        pipeline = _RecordingFakePipeline()
        rows = [
            _network_row(
                1.0,
                kind="drop_burst",
                details_json='{"dropped_count": 3, "hosts_affected": ["x.example"], "source": "addon"}',
                # drop_burst rows never carry method/url/host fields.
                method=None,
                url=None,
                host="",
            ),
        ]
        result = list(unified_export_events(
            [], [],
            network_rows=rows,
            network_scrub_pipeline=pipeline,
        ))
        assert pipeline.calls == [], (
            "drop_burst events must NOT be routed through the scrub "
            "pipeline (they have no body to decrypt)."
        )
        assert len(result) == 1
        assert isinstance(result[0], NetworkDropBurstEvent)

    def test_tunneled_passes_through_pipeline_unchanged(self):
        """V1.5 P2 #5: ``NetworkTunneledEvent`` has no ciphertext
        fields. The original whitelist only allowed drop_burst through
        the pipeline, so tunneled events were sent to
        ``decrypt_and_scrub``, raised ValueError, and got silently
        dropped — losing the API-not-observable signal exactly when
        the recording is encrypted (the case where it's most useful).
        """
        from screencap.engine.events import NetworkTunneledEvent

        pipeline = _RecordingFakePipeline()
        rows = [
            _network_row(
                1.0,
                kind="tunneled",
                host="pinned.example.com",
                method=None,
                url=None,
                details_json=(
                    '{"started_at": 100.0, "duration_seconds": 30.0}'
                ),
            ),
        ]
        result = list(unified_export_events(
            [], [],
            network_rows=rows,
            network_scrub_pipeline=pipeline,
        ))
        assert pipeline.calls == [], (
            "tunneled events must NOT be routed through the scrub "
            "pipeline (they have no body to decrypt)."
        )
        assert len(result) == 1
        assert isinstance(result[0], NetworkTunneledEvent)
        assert result[0].host == "pinned.example.com"
