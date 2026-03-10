"""High-level capture loading and iteration API.

Provides time-aligned access to captured events with associated screenshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from sc_engine.convert import dict_to_action_event
from sc_engine.events import (
    ActionEvent as PydanticActionEvent,
    BaseEvent,
    WindowSwitchEvent,
)
from sc_engine.events import (
    KeyDownEvent,
    KeyShortcutEvent,
    KeyTypeEvent,
    KeyUpEvent,
    MouseMoveEvent,
    SpecialKeyEvent,
)
from sc_engine.processing import (
    deduplicate_window_events,
    interleave_window_events,
    process_events,
)

if TYPE_CHECKING:
    from PIL import Image


def _convert_action_event(db_event) -> PydanticActionEvent | None:
    """Convert a SQLAlchemy ActionEvent to a Pydantic event.

    Thin wrapper around :func:`dict_to_action_event` that extracts
    a dict from the ORM object.

    Args:
        db_event: SQLAlchemy ActionEvent instance.

    Returns:
        Pydantic event or None if unrecognized.
    """
    # Build a dict from the ORM object's columns.  Use getattr with
    # defaults so missing columns (older DB schemas) don't crash.
    row = {
        "timestamp": db_event.timestamp,
        "name": db_event.name,
        "mouse_x": getattr(db_event, "mouse_x", None),
        "mouse_y": getattr(db_event, "mouse_y", None),
        "mouse_dx": getattr(db_event, "mouse_dx", None),
        "mouse_dy": getattr(db_event, "mouse_dy", None),
        "mouse_button_name": getattr(db_event, "mouse_button_name", None),
        "mouse_pressed": getattr(db_event, "mouse_pressed", None),
        "mouse_pressure": getattr(db_event, "mouse_pressure", None),
        "modifier_flags": getattr(db_event, "modifier_flags", None),
        "scroll_phase": getattr(db_event, "scroll_phase", None),
        "momentum_phase": getattr(db_event, "momentum_phase", None),
        "is_continuous": getattr(db_event, "is_continuous", None),
        "key_name": getattr(db_event, "key_name", None),
        "key_char": getattr(db_event, "key_char", None),
        "key_vk": getattr(db_event, "key_vk", None),
        "canonical_key_name": getattr(db_event, "canonical_key_name", None),
        "canonical_key_char": getattr(db_event, "canonical_key_char", None),
        "canonical_key_vk": getattr(db_event, "canonical_key_vk", None),
    }
    return dict_to_action_event(row)


@dataclass
class Action:
    """A processed action event with associated screenshot.

    Represents a user action (click, type, drag, etc.) along with
    the screen state at the time of the action.
    """

    event: PydanticActionEvent
    _capture: "CaptureSession"
    element_state: dict | None = None
    window_title: str | None = None
    window_data: dict | None = None

    @property
    def timestamp(self) -> float:
        """Unix timestamp of the action."""
        return self.event.timestamp

    @property
    def type(self) -> str:
        """Action type (e.g., 'mouse.singleclick', 'key.type')."""
        return self.event.type if isinstance(self.event.type, str) else self.event.type.value

    @property
    def x(self) -> float | None:
        """X coordinate for mouse actions (start position for drags)."""
        if hasattr(self.event, "x"):
            return self.event.x
        return None

    @property
    def y(self) -> float | None:
        """Y coordinate for mouse actions (start position for drags)."""
        if hasattr(self.event, "y"):
            return self.event.y
        return None

    @property
    def text(self) -> str | None:
        """Typed text for keyboard actions."""
        if isinstance(self.event, KeyShortcutEvent):
            return self.event.text
        if isinstance(self.event, SpecialKeyEvent):
            return self.event.text
        if isinstance(self.event, KeyTypeEvent):
            return self.event.text
        return None

    @property
    def keys(self) -> list[str] | None:
        """Key names for keyboard actions (useful when text is empty).

        Returns list of key names like ['ctrl', 'space'] or ['enter'].
        For KeyShortcutEvent, returns the canonical keys list directly.
        For SpecialKeyEvent, returns [key_name].
        """
        if isinstance(self.event, KeyShortcutEvent):
            return self.event.keys
        if isinstance(self.event, SpecialKeyEvent):
            return [self.event.key_name]
        if isinstance(self.event, KeyTypeEvent):
            key_names = []
            seen = set()
            for child in self.event.children:
                if isinstance(child, KeyDownEvent):
                    # Get key identifier
                    key_id = child.key_name or child.key_char or child.key_vk
                    if key_id and key_id not in seen:
                        seen.add(key_id)
                        key_names.append(key_id)
            return key_names if key_names else None
        return None

    @property
    def dx(self) -> float | None:
        """Horizontal displacement for scroll/drag actions."""
        if hasattr(self.event, "dx"):
            return self.event.dx
        return None

    @property
    def dy(self) -> float | None:
        """Vertical displacement for scroll/drag actions."""
        if hasattr(self.event, "dy"):
            return self.event.dy
        return None

    @property
    def button(self) -> str | None:
        """Mouse button for click/drag actions."""
        if hasattr(self.event, "button"):
            btn = self.event.button
            return btn.value if hasattr(btn, "value") else str(btn)
        return None

    @property
    def screenshot(self) -> "Image" | None:
        """Get the screenshot at the time of this action.

        Returns:
            PIL Image of the screen at action time, or None if not available.
        """
        return self._capture.get_frame_at(self.timestamp)


class CaptureSession:
    """A loaded capture session for analysis and replay.

    Provides access to time-aligned events and screenshots.
    Reads from the SQLAlchemy-based per-capture database (recording.db).

    Usage:
        capture = CaptureSession.load("./my_capture")

        for action in capture.actions():
            print(f"{action.type} at {action.timestamp}")
            img = action.screenshot
    """

    def __init__(
        self,
        capture_dir: str | Path,
        session,
        recording,
    ) -> None:
        """Initialize capture session.

        Use CaptureSession.load() instead of calling this directly.
        """
        self.capture_dir = Path(capture_dir)
        self._session = session
        self._recording = recording

    @classmethod
    def load(cls, capture_dir: str | Path) -> "CaptureSession":
        """Load a capture from disk.

        Args:
            capture_dir: Path to capture directory.

        Returns:
            CaptureSession instance.

        Raises:
            FileNotFoundError: If capture doesn't exist.
        """
        capture_dir = Path(capture_dir)
        db_path = capture_dir / "recording.db"

        if not db_path.exists():
            raise FileNotFoundError(f"Capture not found: {capture_dir}")

        from sc_engine.db import get_session_for_path
        from sc_engine.db.models import Recording

        session = get_session_for_path(str(db_path))
        try:
            recording = session.query(Recording).first()
        except Exception:
            session.close()
            raise

        if recording is None:
            session.close()
            raise FileNotFoundError(f"Invalid capture (no recording found): {capture_dir}")

        return cls(capture_dir, session, recording)

    @property
    def id(self) -> str:
        """Capture ID."""
        return str(self._recording.id)

    @property
    def started_at(self) -> float:
        """Start timestamp."""
        return self._recording.timestamp

    @property
    def ended_at(self) -> float | None:
        """End timestamp (from last action event)."""
        if self._recording.action_events:
            return self._recording.action_events[-1].timestamp
        return None

    @property
    def duration(self) -> float | None:
        """Duration in seconds."""
        ended = self.ended_at
        if ended is not None:
            return ended - self._recording.timestamp
        return None

    @property
    def platform(self) -> str:
        """Platform (darwin, win32, linux)."""
        return self._recording.platform or ""

    @property
    def screen_size(self) -> tuple[int, int]:
        """Screen dimensions (width, height) in physical pixels."""
        return (
            self._recording.monitor_width or 0,
            self._recording.monitor_height or 0,
        )

    @property
    def task_description(self) -> str | None:
        """Task description."""
        return self._recording.task_description

    @property
    def video_path(self) -> Path | None:
        """Path to video file if exists."""
        # Current format: recording-{timestamp}.mp4
        for p in self.capture_dir.glob("recording-*.mp4"):
            return p
        # Legacy format: oa_recording-{timestamp}.mp4
        for p in self.capture_dir.glob("oa_recording-*.mp4"):
            return p
        # Fallback: video.mp4
        video_path = self.capture_dir / "video.mp4"
        return video_path if video_path.exists() else None

    @property
    def video_paths(self) -> list[Path]:
        """All video file paths, sorted (supports chunked recordings)."""
        chunks = sorted(self.capture_dir.glob("chunk_*.mp4"))
        if chunks:
            return chunks
        single = self.video_path
        return [single] if single else []

    @property
    def audio_path(self) -> Path | None:
        """Path to audio file if exists."""
        audio_path = self.capture_dir / "audio.flac"
        return audio_path if audio_path.exists() else None

    @property
    def audio_paths(self) -> list[Path]:
        """All audio file paths, sorted (supports chunked recordings)."""
        chunks = sorted(self.capture_dir.glob("audio_*.flac"))
        if chunks:
            return chunks
        single = self.audio_path
        return [single] if single else []

    @property
    def pixel_ratio(self) -> float:
        """Display pixel ratio (physical/logical), e.g. 2.0 for Retina.

        Defaults to 1.0 if not stored in the recording.
        """
        # Check if the Recording model has a pixel_ratio column
        ratio = getattr(self._recording, "pixel_ratio", None)
        if ratio is not None:
            return float(ratio)
        # Check the config JSON for pixel_ratio
        config = getattr(self._recording, "config", None)
        if isinstance(config, dict) and "pixel_ratio" in config:
            return float(config["pixel_ratio"])
        return 1.0

    @property
    def display_map(self) -> list[dict] | None:
        """Per-display bounds and pixel ratios, or None if not stored."""
        config = getattr(self._recording, "config", None)
        if isinstance(config, dict):
            return config.get("displays")
        return None

    @property
    def audio_start_time(self) -> float | None:
        """Start timestamp of the audio recording, or None if unavailable."""
        # Check the AudioInfo relationship for the timestamp
        audio_infos = getattr(self._recording, "audio_info", None)
        if audio_infos:
            first = audio_infos[0] if isinstance(audio_infos, list) else audio_infos
            ts = getattr(first, "timestamp", None)
            if ts is not None:
                return float(ts)
        return None

    def raw_events(self) -> list[PydanticActionEvent]:
        """Get all raw action events (unprocessed).

        Converts SQLAlchemy ActionEvent models to Pydantic events.

        Returns:
            List of raw mouse and keyboard events.
        """
        events = []
        for db_event in self._recording.action_events:
            if getattr(db_event, "disabled", False):
                continue
            pydantic_event = _convert_action_event(db_event)
            if pydantic_event is not None:
                events.append(pydantic_event)
        return events

    def export_events(self, include_moves: bool = False) -> list[BaseEvent]:
        """Produce a combined, time-ordered list of processed action events
        and deduplicated window.switch events for JSONL export.

        Unlike :meth:`actions`, this returns raw Pydantic event objects
        (no Action wrapper, no screenshots) and includes WindowSwitchEvent
        entries interleaved by timestamp.

        Privacy filtering is NOT applied here — callers (exporter,
        chunk processor) apply their own privacy policy.

        Args:
            include_moves: Whether to include mouse.move events.

        Returns:
            Combined list of action + window.switch events, sorted by timestamp.
        """
        # 1. Process action events through the merge pipeline
        raw = self.raw_events()
        processed = process_events(
            raw,
            double_click_interval=self._recording.double_click_interval_seconds or 0.5,
            double_click_distance=self._recording.double_click_distance_pixels or 5,
        )

        # Filter moves if not requested
        if not include_moves:
            processed = [e for e in processed if not isinstance(e, MouseMoveEvent)]

        # 2. Build deduplicated window.switch events from DB
        window_rows = []
        for we in getattr(self._recording, "window_events", []):
            window_rows.append({
                "timestamp": we.timestamp,
                "app_bundle_id": getattr(we, "app_bundle_id", None),
                "title": getattr(we, "title", None),
                "window_id": str(getattr(we, "window_id", "") or ""),
                "left": getattr(we, "left", 0),
                "top": getattr(we, "top", 0),
                "width": getattr(we, "width", 0),
                "height": getattr(we, "height", 0),
                "browser_url": getattr(we, "browser_url", None),
            })
        # window_events ORM relationship is already ordered by timestamp
        window_switches = deduplicate_window_events(window_rows)

        # 3. Interleave by timestamp
        return interleave_window_events(processed, window_switches)

    def actions(self, include_moves: bool = False) -> Iterator[Action]:
        """Iterate over processed actions.

        Yields time-ordered actions (clicks, drags, typed text) with
        associated screenshots and accessibility data.

        Args:
            include_moves: Whether to include mouse move events.

        Yields:
            Action objects with event data and screenshot access.
        """
        import json as _json

        # Build timestamp → DB event lookup for a11y data
        db_event_by_ts: dict[float, object] = {}
        for db_event in self._recording.action_events:
            if not getattr(db_event, "disabled", False):
                db_event_by_ts[db_event.timestamp] = db_event

        # Build window_event lookup by timestamp (FK is often NULL, but
        # window_event_timestamp on action_event IS populated).
        window_events_by_ts: dict[float, object] = {}
        for we in getattr(self._recording, "window_events", []):
            window_events_by_ts[we.timestamp] = we

        # Get and process raw events
        raw_events = self.raw_events()
        processed = process_events(
            raw_events,
            double_click_interval=self._recording.double_click_interval_seconds or 0.5,
            double_click_distance=self._recording.double_click_distance_pixels or 5,
        )

        # Track last-known a11y state so keyboard events (which lack
        # element_state) can inherit from the most recent mouse event.
        last_element_state = None
        last_window_title = None
        last_window_data = None

        # Filter out moves if not requested
        for event in processed:
            if not include_moves and isinstance(event, MouseMoveEvent):
                continue

            # Attach a11y data from the matching DB event
            element_state = None
            window_title = None
            window_data = None
            db_event = db_event_by_ts.get(event.timestamp)
            if db_event is not None:
                # Element state (element under cursor)
                es = getattr(db_event, "element_state", None)
                if es and es != {} and es != "null":
                    element_state = _json.loads(es) if isinstance(es, str) else es
                    if element_state == {} or element_state is None:
                        element_state = None

                # Window event — try FK relationship first, fall back to
                # timestamp-based lookup (recorder sets window_event_timestamp
                # but often leaves window_event_id NULL).
                we = getattr(db_event, "window_event", None)
                if we is None:
                    we_ts = getattr(db_event, "window_event_timestamp", None)
                    if we_ts is not None:
                        we = window_events_by_ts.get(we_ts)

                if we is not None:
                    window_title = getattr(we, "title", None)
                    state = getattr(we, "state", None)
                    if state:
                        window_data = _json.loads(state) if isinstance(state, str) else state

            # Update last-known state from mouse events
            if element_state is not None:
                last_element_state = element_state
            if window_title is not None:
                last_window_title = window_title
            if window_data is not None:
                last_window_data = window_data

            # For events without their own a11y data (e.g. keyboard events),
            # inherit the most recent state so the viewer always has context.
            if element_state is None:
                element_state = last_element_state
            if window_title is None:
                window_title = last_window_title
            if window_data is None:
                window_data = last_window_data

            yield Action(
                event=event,
                _capture=self,
                element_state=element_state,
                window_title=window_title,
                window_data=window_data,
            )

    def get_frame_at(self, timestamp: float, tolerance: float = 0.5) -> "Image" | None:
        """Get the screen frame closest to a timestamp.

        Tries video first, then falls back to screenshot files on disk.

        Args:
            timestamp: Unix timestamp.
            tolerance: Maximum time difference in seconds.

        Returns:
            PIL Image or None if not available.
        """
        video_path = self.video_path
        if video_path is not None:
            try:
                from sc_engine.video import extract_frame

                # Convert to video-relative timestamp
                video_start = self._recording.video_start_time or self._recording.timestamp
                video_timestamp = timestamp - video_start

                if video_timestamp < 0:
                    video_timestamp = 0

                frame = extract_frame(video_path, video_timestamp, tolerance=tolerance)
                if frame is not None:
                    return frame
            except Exception:
                pass

        # Fall back to screenshot files
        return self._get_screenshot_frame(timestamp, tolerance)

    def _get_screenshot_frame(self, timestamp: float, tolerance: float) -> "Image" | None:
        """Find nearest screenshot file by timestamp.

        Lazy-loads and caches the sorted list of (timestamp, path) pairs.
        """
        if not hasattr(self, "_screenshot_index"):
            self._screenshot_index = self._build_screenshot_index()

        if not self._screenshot_index:
            return None

        import bisect

        timestamps = [t for t, _ in self._screenshot_index]
        idx = bisect.bisect_left(timestamps, timestamp)

        best_idx = None
        best_diff = float("inf")
        for candidate in (idx - 1, idx):
            if 0 <= candidate < len(timestamps):
                diff = abs(timestamps[candidate] - timestamp)
                if diff < best_diff:
                    best_diff = diff
                    best_idx = candidate

        if best_idx is None or best_diff > tolerance:
            return None

        _, path = self._screenshot_index[best_idx]
        try:
            from PIL import Image as PILImage

            return PILImage.open(path)
        except Exception:
            return None

    def _build_screenshot_index(self) -> list[tuple[float, Path]]:
        """Build sorted list of (timestamp, file_path) from DB image_path values."""
        from sc_engine.db.models import Screenshot

        index = []
        try:
            screenshots = (
                self._session.query(Screenshot.timestamp, Screenshot.image_path)
                .filter(Screenshot.image_path.isnot(None))
                .order_by(Screenshot.timestamp)
                .all()
            )
            for ts, rel_path in screenshots:
                if ts is not None and rel_path:
                    full_path = self.capture_dir / rel_path
                    if full_path.exists():
                        index.append((ts, full_path))
        except Exception:
            pass
        return index

    def close(self) -> None:
        """Close the capture and release resources."""
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> "CaptureSession":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()


# Alias for simpler import
Capture = CaptureSession
