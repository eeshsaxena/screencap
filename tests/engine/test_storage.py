"""Tests for the event-type registry.

The legacy ``capture.db`` storage layer was deleted along with the
``CaptureStorage`` / ``Capture`` / ``create_capture`` / ``load_capture``
surface; only ``EVENT_TYPE_MAP`` remains. The tests below lock down its
completeness so any future ``BaseEvent`` subclass added without a
corresponding map entry fails the suite instead of silently dropping.
"""

from __future__ import annotations

import pytest

from screencap.engine.events import (
    EVENT_TYPE_MAP,
    BaseEvent,
    EventType,
    KeyShortcutEvent,
    WindowSwitchEvent,
)


def _concrete_event_classes():
    """Yield (subclass, event_type_value) for every concrete BaseEvent subclass."""
    from typing import Literal, get_args, get_origin

    for cls in BaseEvent.__subclasses__():
        type_field = cls.model_fields.get("type")
        if type_field is None:
            continue
        annotation = type_field.annotation
        if get_origin(annotation) is not Literal:
            continue
        args = get_args(annotation)
        if len(args) != 1:
            continue
        member = args[0]
        value = member.value if hasattr(member, "value") else member
        yield cls, value


class TestEventTypeMap:
    """EVENT_TYPE_MAP must cover every concrete BaseEvent subclass.

    Locks completeness so future drift (a new event type added without a
    corresponding map entry, or a map entry pointing at the wrong class)
    fails loudly instead of silently dropping events on read.
    """

    @pytest.mark.parametrize(
        "event_class,event_type_value",
        list(_concrete_event_classes()),
        ids=lambda value: value.__name__ if isinstance(value, type) else str(value),
    )
    def test_subclass_has_map_entry(self, event_class, event_type_value):
        assert event_type_value in EVENT_TYPE_MAP, (
            f"{event_class.__name__} (type={event_type_value!r}) is missing from "
            f"EVENT_TYPE_MAP — events of this type would silently drop on read."
        )
        assert EVENT_TYPE_MAP[event_type_value] is event_class, (
            f"EVENT_TYPE_MAP[{event_type_value!r}] is "
            f"{EVENT_TYPE_MAP[event_type_value].__name__}, expected {event_class.__name__}"
        )

    def test_key_shortcut_entry(self):
        assert EVENT_TYPE_MAP[EventType.KEY_SHORTCUT.value] is KeyShortcutEvent

    def test_window_switch_entry(self):
        assert EVENT_TYPE_MAP[EventType.WINDOW_SWITCH.value] is WindowSwitchEvent
