"""Tests for SQLite storage."""

from __future__ import annotations

import pytest

from screencap.engine.events import (
    BaseEvent,
    EventType,
    KeyDownEvent,
    KeyShortcutEvent,
    MouseButton,
    MouseClickEvent,
    MouseDownEvent,
    MouseMoveEvent,
    MouseUpEvent,
    WindowSwitchEvent,
)
from screencap.engine.storage import (
    EVENT_TYPE_MAP,
    Capture,
    CaptureStorage,
    create_capture,
    load_capture,
)


class TestCaptureStorage:
    """Tests for CaptureStorage class."""

    def test_init_storage(self, tmp_path):
        """Test storage initialization."""
        db_path = tmp_path / "capture.db"
        storage = CaptureStorage(db_path)
        assert storage.db_path == db_path
        storage.close()

    def test_context_manager(self, tmp_path):
        """Test storage as context manager."""
        with CaptureStorage(tmp_path / "capture.db") as storage:
            assert storage.is_open is False  # Connection created lazily
            capture = Capture(
                id="test",
                started_at=1234567890.0,
                platform="darwin",
                screen_width=1920,
                screen_height=1080,
            )
            storage.init_capture(capture)
            assert storage.is_open is True

    def test_init_and_get_capture(self, tmp_path):
        """Test initializing and retrieving capture metadata."""
        with CaptureStorage(tmp_path / "capture.db") as storage:
            capture = Capture(
                id="abc123",
                started_at=1234567890.0,
                platform="darwin",
                screen_width=1920,
                screen_height=1080,
                task_description="Test task",
            )
            storage.init_capture(capture)

            retrieved = storage.get_capture()
            assert retrieved is not None
            assert retrieved.id == "abc123"
            assert retrieved.platform == "darwin"
            assert retrieved.task_description == "Test task"

    def test_update_capture(self, tmp_path):
        """Test updating capture metadata."""
        with CaptureStorage(tmp_path / "capture.db") as storage:
            capture = Capture(
                id="abc123",
                started_at=1234567890.0,
                platform="darwin",
                screen_width=1920,
                screen_height=1080,
            )
            storage.init_capture(capture)

            capture.ended_at = 1234567900.0
            capture.task_description = "Updated task"
            storage.update_capture(capture)

            retrieved = storage.get_capture()
            assert retrieved.ended_at == 1234567900.0
            assert retrieved.task_description == "Updated task"

    def test_write_and_get_event(self, tmp_path):
        """Test writing and retrieving a single event."""
        with CaptureStorage(tmp_path / "capture.db") as storage:
            event = MouseMoveEvent(
                timestamp=1234567890.0,
                x=100.0,
                y=200.0,
            )
            event_id = storage.write_event(event)
            assert event_id == 1

            events = storage.get_events()
            assert len(events) == 1
            assert events[0].x == 100.0
            assert events[0].y == 200.0

    def test_write_multiple_events(self, tmp_path):
        """Test writing multiple events."""
        with CaptureStorage(tmp_path / "capture.db") as storage:
            events = [
                MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
                MouseMoveEvent(timestamp=2.0, x=200.0, y=200.0),
                MouseDownEvent(timestamp=3.0, x=200.0, y=200.0, button=MouseButton.LEFT),
                MouseUpEvent(timestamp=3.1, x=200.0, y=200.0, button=MouseButton.LEFT),
            ]
            storage.write_events(events)

            retrieved = storage.get_events()
            assert len(retrieved) == 4

    def test_get_events_by_time_range(self, tmp_path):
        """Test filtering events by timestamp."""
        with CaptureStorage(tmp_path / "capture.db") as storage:
            events = [
                MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
                MouseMoveEvent(timestamp=5.0, x=200.0, y=200.0),
                MouseMoveEvent(timestamp=10.0, x=300.0, y=300.0),
            ]
            storage.write_events(events)

            # Get events in range
            filtered = storage.get_events(start_time=2.0, end_time=8.0)
            assert len(filtered) == 1
            assert filtered[0].x == 200.0

    def test_get_events_by_type(self, tmp_path):
        """Test filtering events by type."""
        with CaptureStorage(tmp_path / "capture.db") as storage:
            events = [
                MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
                MouseDownEvent(timestamp=2.0, x=100.0, y=100.0, button=MouseButton.LEFT),
                KeyDownEvent(timestamp=3.0, key_char="a"),
            ]
            storage.write_events(events)

            # Get only mouse down events
            mouse_downs = storage.get_events(event_types=[EventType.MOUSE_DOWN])
            assert len(mouse_downs) == 1

            # Get only keyboard events
            key_events = storage.get_events(event_types=[EventType.KEY_DOWN])
            assert len(key_events) == 1

    def test_get_event_count(self, tmp_path):
        """Test getting event counts."""
        with CaptureStorage(tmp_path / "capture.db") as storage:
            events = [
                MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0),
                MouseMoveEvent(timestamp=2.0, x=200.0, y=200.0),
                KeyDownEvent(timestamp=3.0, key_char="a"),
            ]
            storage.write_events(events)

            assert storage.get_event_count() == 3
            assert storage.get_event_count(EventType.MOUSE_MOVE) == 2
            assert storage.get_event_count(EventType.KEY_DOWN) == 1

    def test_iter_events(self, tmp_path):
        """Test iterating over events."""
        with CaptureStorage(tmp_path / "capture.db") as storage:
            events = [
                MouseMoveEvent(timestamp=float(i), x=float(i * 10), y=float(i * 10))
                for i in range(100)
            ]
            storage.write_events(events)

            count = 0
            for event in storage.iter_events(batch_size=10):
                count += 1
            assert count == 100


class TestCreateAndLoadCapture:
    """Tests for create_capture and load_capture functions."""

    def test_create_capture(self, tmp_path):
        """Test creating a new capture."""
        capture, storage = create_capture(
            capture_dir=tmp_path,
            platform="darwin",
            screen_width=1920,
            screen_height=1080,
            task_description="Test capture",
        )

        assert capture.platform == "darwin"
        assert capture.screen_width == 1920
        assert capture.task_description == "Test capture"
        assert len(capture.id) == 8

        storage.close()

    def test_load_capture(self, tmp_path):
        """Test loading an existing capture."""
        # Create capture
        capture, storage = create_capture(
            capture_dir=tmp_path,
            platform="linux",
            screen_width=2560,
            screen_height=1440,
        )
        capture_id = capture.id
        storage.write_event(MouseMoveEvent(timestamp=1.0, x=100.0, y=100.0))
        storage.close()

        # Load capture
        loaded_capture, loaded_storage = load_capture(tmp_path)

        assert loaded_capture is not None
        assert loaded_capture.id == capture_id
        assert loaded_capture.platform == "linux"

        events = loaded_storage.get_events()
        assert len(events) == 1

        loaded_storage.close()

    def test_load_nonexistent_capture(self, tmp_path):
        """Test loading a capture that doesn't exist."""
        with pytest.raises(FileNotFoundError):
            load_capture(tmp_path / "nonexistent")


class TestCaptureModel:
    """Tests for Capture Pydantic model."""

    def test_capture_defaults(self):
        """Test Capture model default values."""
        capture = Capture(
            id="test",
            started_at=1234567890.0,
            platform="darwin",
            screen_width=1920,
            screen_height=1080,
        )
        assert capture.ended_at is None
        assert capture.task_description is None
        assert capture.double_click_interval_seconds == 0.5
        assert capture.double_click_distance_pixels == 5.0
        assert capture.metadata == {}

    def test_capture_with_metadata(self):
        """Test Capture model with metadata."""
        capture = Capture(
            id="test",
            started_at=1234567890.0,
            platform="win32",
            screen_width=1920,
            screen_height=1080,
            metadata={"user": "test_user", "version": "1.0"},
        )
        assert capture.metadata["user"] == "test_user"


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


class TestNewEventRoundTrip:
    """Round-trip persistence for the two event types added to EVENT_TYPE_MAP."""

    def test_key_shortcut_round_trip(self, tmp_path):
        with CaptureStorage(tmp_path / "capture.db") as storage:
            event = KeyShortcutEvent(timestamp=1.0, keys=["ctrl", "z"])
            storage.write_event(event)

            events = storage.get_events()

        assert len(events) == 1
        loaded = events[0]
        assert isinstance(loaded, KeyShortcutEvent)
        assert loaded.keys == ["ctrl", "z"]
        assert loaded.text == "Ctrl+z"

    def test_window_switch_round_trip(self, tmp_path):
        with CaptureStorage(tmp_path / "capture.db") as storage:
            event = WindowSwitchEvent(
                timestamp=1.0,
                app_name="Finder",
                app_bundle_id="com.apple.finder",
                window_title="Documents",
                window_id="123",
                x=0,
                y=0,
                width=800,
                height=600,
            )
            storage.write_event(event)

            events = storage.get_events()

        assert len(events) == 1
        loaded = events[0]
        assert isinstance(loaded, WindowSwitchEvent)
        assert loaded.app_name == "Finder"
        assert loaded.app_bundle_id == "com.apple.finder"
        assert loaded.window_title == "Documents"
        assert loaded.window_id == "123"


class TestChildrenRoundTrip:
    """Parent-child persistence: write_event recurses into children, get_events
    skips them by default and surfaces them with include_children=True."""

    def test_mouse_click_with_children_round_trip(self, tmp_path):
        down = MouseDownEvent(timestamp=1.0, x=10.0, y=20.0, button=MouseButton.LEFT)
        up = MouseUpEvent(timestamp=1.1, x=10.0, y=20.0, button=MouseButton.LEFT)
        click = MouseClickEvent(
            timestamp=1.05,
            x=10.0,
            y=20.0,
            button=MouseButton.LEFT,
            children=[down, up],
        )

        with CaptureStorage(tmp_path / "capture.db") as storage:
            parent_id = storage.write_event(click)

            # Default read excludes child rows
            top_level = storage.get_events()
            assert len(top_level) == 1
            assert isinstance(top_level[0], MouseClickEvent)

            # include_children=True returns parent + children, preserving order
            all_events = storage.get_events(include_children=True)
            assert len(all_events) == 3
            assert isinstance(all_events[0], MouseDownEvent)
            assert isinstance(all_events[1], MouseClickEvent)
            assert isinstance(all_events[2], MouseUpEvent)

            # parent_id chain is persisted: children point at the click row
            cursor = storage.conn.cursor()
            cursor.execute(
                "SELECT id, type, parent_id FROM events ORDER BY id"
            )
            rows = cursor.fetchall()

        assert rows[0]["id"] == parent_id
        assert rows[0]["parent_id"] is None
        assert rows[1]["parent_id"] == parent_id
        assert rows[1]["type"] == EventType.MOUSE_DOWN.value
        assert rows[2]["parent_id"] == parent_id
        assert rows[2]["type"] == EventType.MOUSE_UP.value
