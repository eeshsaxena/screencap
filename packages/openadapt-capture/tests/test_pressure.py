"""Tests for pressure sensitivity capture feature.

Tests cover:
- Pydantic event model round-trip with pressure
- DB column existence and round-trip (via SQLAlchemy models)
- Processing pipeline: redundancy, merge, click, drag semantics
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import sqlalchemy as sa

from openadapt_capture.events import (
    MouseButton,
    MouseClickEvent,
    MouseDoubleClickEvent,
    MouseDownEvent,
    MouseDragEvent,
    MouseMoveEvent,
    MouseUpEvent,
)
from openadapt_capture.processing import (
    detect_drag_events,
    merge_consecutive_mouse_click_events,
    merge_consecutive_mouse_move_events,
    process_events,
    remove_redundant_mouse_move_events,
)


# =============================================================================
# Event Model Tests
# =============================================================================


class TestPressureEventModel:
    """Tests for pressure field on Pydantic event models."""

    def test_move_event_with_pressure(self):
        """MouseMoveEvent with pressure round-trips through serialization."""
        event = MouseMoveEvent(timestamp=1.0, x=100, y=200, pressure=0.5)
        assert event.pressure == 0.5

        data = event.model_dump()
        assert data["pressure"] == 0.5

        restored = MouseMoveEvent(**data)
        assert restored.pressure == 0.5

    def test_move_event_pressure_none_default(self):
        """MouseMoveEvent defaults pressure to None."""
        event = MouseMoveEvent(timestamp=1.0, x=100, y=200)
        assert event.pressure is None

    def test_down_event_with_pressure(self):
        """MouseDownEvent with pressure serializes correctly."""
        event = MouseDownEvent(
            timestamp=1.0, x=100, y=200, button="left", pressure=0.7
        )
        assert event.pressure == 0.7

        data = event.model_dump()
        restored = MouseDownEvent(**data)
        assert restored.pressure == 0.7

    def test_up_event_with_pressure(self):
        """MouseUpEvent with pressure serializes correctly."""
        event = MouseUpEvent(
            timestamp=1.0, x=100, y=200, button="left", pressure=0.0
        )
        assert event.pressure == 0.0

    def test_click_event_with_pressure(self):
        """MouseClickEvent carries pressure from down event."""
        click = MouseClickEvent(
            timestamp=1.0, x=100, y=200, button="left", pressure=0.6
        )
        assert click.pressure == 0.6

    def test_double_click_event_with_pressure(self):
        """MouseDoubleClickEvent carries pressure from first down event."""
        dbl = MouseDoubleClickEvent(
            timestamp=1.0, x=100, y=200, button="left", pressure=0.8
        )
        assert dbl.pressure == 0.8

    def test_drag_event_pressure_none(self):
        """MouseDragEvent always has pressure=None at parent level."""
        drag = MouseDragEvent(
            timestamp=1.0, x=100, y=200, dx=50, dy=50, button="left"
        )
        assert drag.pressure is None

    def test_drag_children_preserve_pressure(self):
        """Drag children retain their individual pressure values."""
        down = MouseDownEvent(
            timestamp=1.0, x=100, y=200, button="left", pressure=0.3
        )
        move1 = MouseMoveEvent(timestamp=1.1, x=110, y=210, pressure=0.5)
        move2 = MouseMoveEvent(timestamp=1.2, x=120, y=220, pressure=0.8)
        up = MouseUpEvent(
            timestamp=1.3, x=130, y=230, button="left", pressure=0.0
        )
        drag = MouseDragEvent(
            timestamp=1.0,
            x=100,
            y=200,
            dx=30,
            dy=30,
            button="left",
            children=[down, move1, move2, up],
        )
        assert drag.pressure is None
        assert drag.children[0].pressure == 0.3
        assert drag.children[1].pressure == 0.5
        assert drag.children[2].pressure == 0.8
        assert drag.children[3].pressure == 0.0


# =============================================================================
# DB Round-Trip Tests
# =============================================================================


class TestPressureDB:
    """Tests for mouse_pressure column in ActionEvent DB model."""

    @pytest.fixture
    def db_session(self):
        """Create a temporary in-memory DB with the ActionEvent schema."""
        from openadapt_capture.db import Base
        from openadapt_capture.db.models import ActionEvent, Recording

        engine = sa.create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sa.orm.sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()

    def test_mouse_pressure_column_exists(self, db_session):
        """New recordings have mouse_pressure column in action_event table."""
        from openadapt_capture.db.models import ActionEvent

        columns = {c.name for c in ActionEvent.__table__.columns}
        assert "mouse_pressure" in columns

    def test_insert_with_pressure(self, db_session):
        """Insert event with mouse_pressure, read back, verify value."""
        from openadapt_capture.db.models import ActionEvent, Recording

        rec = Recording(
            id=1, timestamp=1000.0, monitor_width=1920, monitor_height=1080
        )
        db_session.add(rec)
        db_session.commit()

        ae = ActionEvent(
            name="move",
            timestamp=1001.0,
            recording_id=1,
            recording_timestamp=1000.0,
            mouse_x=100,
            mouse_y=200,
            mouse_pressure=0.5,
        )
        db_session.add(ae)
        db_session.commit()

        result = db_session.query(ActionEvent).first()
        assert result.mouse_pressure == pytest.approx(0.5)

    def test_insert_without_pressure(self, db_session):
        """Insert event without mouse_pressure, read back, verify None."""
        from openadapt_capture.db.models import ActionEvent, Recording

        rec = Recording(
            id=1, timestamp=1000.0, monitor_width=1920, monitor_height=1080
        )
        db_session.add(rec)
        db_session.commit()

        ae = ActionEvent(
            name="move",
            timestamp=1001.0,
            recording_id=1,
            recording_timestamp=1000.0,
            mouse_x=100,
            mouse_y=200,
        )
        db_session.add(ae)
        db_session.commit()

        result = db_session.query(ActionEvent).first()
        assert result.mouse_pressure is None

    def test_crud_insert_accepts_mouse_pressure(self, db_session):
        """crud._insert() accepts mouse_pressure key without assertion failure."""
        from openadapt_capture.db.crud import _insert
        from openadapt_capture.db.models import ActionEvent

        event_data = {
            "name": "move",
            "timestamp": 1001.0,
            "recording_id": 1,
            "recording_timestamp": 1000.0,
            "mouse_x": 100,
            "mouse_y": 200,
            "mouse_pressure": 0.5,
        }
        # Should not raise AssertionError
        _insert(db_session, event_data, ActionEvent)


# =============================================================================
# Processing Pipeline Tests
# =============================================================================


class TestPressureRedundancy:
    """Tests for pressure in remove_redundant_mouse_move_events."""

    def test_same_position_different_pressure_not_redundant(self):
        """Two moves at same (x,y) but different pressure are NOT redundant."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100, y=200, pressure=0.2),
            MouseMoveEvent(timestamp=1.1, x=100, y=200, pressure=0.5),
            MouseMoveEvent(timestamp=1.2, x=100, y=200, pressure=0.8),
        ]
        result = remove_redundant_mouse_move_events(events)
        assert len(result) == 3

    def test_same_position_same_pressure_redundant(self):
        """Two moves at same (x,y) with same pressure ARE redundant."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100, y=200, pressure=0.5),
            MouseMoveEvent(timestamp=1.1, x=100, y=200, pressure=0.5),
        ]
        result = remove_redundant_mouse_move_events(events)
        assert len(result) == 1

    def test_same_position_no_pressure_redundant(self):
        """Two moves at same (x,y) with no pressure ARE redundant (original behavior)."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100, y=200),
            MouseMoveEvent(timestamp=1.1, x=100, y=200),
        ]
        result = remove_redundant_mouse_move_events(events)
        assert len(result) == 1


class TestPressureMergeMoves:
    """Tests for pressure in merge_consecutive_mouse_move_events."""

    def test_merged_move_gets_last_pressure(self):
        """Merged move event preserves last pressure value."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100, y=200, pressure=0.2),
            MouseMoveEvent(timestamp=1.1, x=110, y=210, pressure=0.5),
            MouseMoveEvent(timestamp=1.2, x=120, y=220, pressure=0.8),
        ]
        result = merge_consecutive_mouse_move_events(events)
        assert len(result) == 1
        assert result[0].pressure == 0.8
        assert result[0].x == 120
        assert result[0].y == 220

    def test_merged_move_none_pressure(self):
        """Merged move with no pressure stays None."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100, y=200),
            MouseMoveEvent(timestamp=1.1, x=110, y=210),
        ]
        result = merge_consecutive_mouse_move_events(events)
        assert len(result) == 1
        assert result[0].pressure is None

    def test_single_move_preserves_pressure(self):
        """Single move is not merged and preserves its pressure."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100, y=200, pressure=0.5),
        ]
        result = merge_consecutive_mouse_move_events(events)
        assert len(result) == 1
        assert result[0].pressure == 0.5


class TestPressureClickMerge:
    """Tests for pressure in click event merging."""

    def test_click_gets_down_pressure(self):
        """Click event gets down event's pressure."""
        events = [
            MouseDownEvent(
                timestamp=1.0, x=100, y=200, button="left", pressure=0.6
            ),
            MouseUpEvent(
                timestamp=1.1, x=100, y=200, button="left", pressure=0.0
            ),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseClickEvent)
        assert result[0].pressure == 0.6

    def test_click_no_pressure(self):
        """Click from regular mouse has pressure=None."""
        events = [
            MouseDownEvent(timestamp=1.0, x=100, y=200, button="left"),
            MouseUpEvent(timestamp=1.1, x=100, y=200, button="left"),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseClickEvent)
        assert result[0].pressure is None

    def test_double_click_gets_first_down_pressure(self):
        """Double-click event gets first down event's pressure."""
        events = [
            MouseDownEvent(
                timestamp=1.0, x=100, y=200, button="left", pressure=0.7
            ),
            MouseUpEvent(
                timestamp=1.05, x=100, y=200, button="left", pressure=0.0
            ),
            MouseDownEvent(
                timestamp=1.1, x=100, y=200, button="left", pressure=0.3
            ),
            MouseUpEvent(
                timestamp=1.15, x=100, y=200, button="left", pressure=0.0
            ),
        ]
        result = merge_consecutive_mouse_click_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseDoubleClickEvent)
        assert result[0].pressure == 0.7


class TestPressureDragMerge:
    """Tests for pressure in drag event detection."""

    def test_drag_parent_pressure_none(self):
        """Drag parent has pressure=None, children have individual pressures."""
        events = [
            MouseDownEvent(
                timestamp=1.0, x=100, y=200, button="left", pressure=0.4
            ),
            MouseMoveEvent(timestamp=1.1, x=150, y=250, pressure=0.6),
            MouseMoveEvent(timestamp=1.2, x=200, y=300, pressure=0.8),
            MouseUpEvent(
                timestamp=1.3, x=200, y=300, button="left", pressure=0.0
            ),
        ]
        result = detect_drag_events(events)
        assert len(result) == 1
        assert isinstance(result[0], MouseDragEvent)
        assert result[0].pressure is None
        # Children preserve their pressures
        children = result[0].children
        assert children[0].pressure == 0.4  # down
        assert children[1].pressure == 0.6  # move 1
        assert children[2].pressure == 0.8  # move 2
        assert children[3].pressure == 0.0  # up


class TestPressureRegularMouse:
    """Tests for regular mouse (no pressure) passthrough."""

    def test_regular_mouse_through_pipeline(self):
        """Events without pressure pass through pipeline unchanged."""
        events = [
            MouseMoveEvent(timestamp=1.0, x=100, y=200),
            MouseMoveEvent(timestamp=1.1, x=110, y=210),
            MouseDownEvent(timestamp=1.2, x=110, y=210, button="left"),
            MouseUpEvent(timestamp=1.3, x=110, y=210, button="left"),
        ]
        result = process_events(events)
        # Should produce: merged move + click
        for event in result:
            if hasattr(event, "pressure"):
                assert event.pressure is None
