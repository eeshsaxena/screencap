"""ScreenCap Engine - GUI interaction capture.

Platform-agnostic event streams with time-aligned media.
"""

__version__ = "0.1.0"

# High-level APIs (primary interface)
from sc_engine.capture import Action, Capture, CaptureSession

# Frame comparison utilities
from sc_engine.comparison import (
    ComparisonReport,
    FrameComparison,
    compare_frames,
    compare_video_to_images,
    plot_comparison,
)
from sc_engine.config import RecordingConfig
from sc_engine.db.models import (
    ActionEvent as DBActionEvent,
)

# Database models (low-level)
from sc_engine.db.models import (
    Recording,
    Screenshot,
)
from sc_engine.db.models import (
    WindowEvent as DBWindowEvent,
)

# Event types
from sc_engine.events import (
    ActionEvent,
    AudioChunkEvent,
    AudioEvent,
    BaseEvent,
    Event,
    EventType,
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
    ScreenEvent,
    ScreenFrameEvent,
    SpecialKeyEvent,
    WindowEvent,
    WindowStateEvent,
)

# Event processing
from sc_engine.processing import (
    detect_drag_events,
    detect_key_shortcuts,
    get_action_events,
    get_audio_events,
    get_screen_events,
    merge_consecutive_keyboard_events,
    merge_consecutive_mouse_click_events,
    merge_consecutive_mouse_move_events,
    merge_consecutive_mouse_scroll_events,
    merge_sequential_key_type_events,
    process_events,
    remove_invalid_keyboard_events,
    remove_redundant_mouse_move_events,
)

# Recorder requires pynput which needs a display server (X11/Wayland/macOS/Windows).
# Make it optional so the package is importable in headless environments (CI, servers).
try:
    from sc_engine.recorder import Recorder
except ImportError:
    Recorder = None  # type: ignore[assignment,misc]

# Performance statistics
from sc_engine.stats import (
    CaptureStats,
    PerfStat,
    plot_capture_performance,
)

# Visualization
from sc_engine.visualize import create_demo, create_html

__all__ = [
    # Version
    "__version__",
    # High-level APIs
    "Recorder",
    "RecordingConfig",
    "Capture",
    "CaptureSession",
    "Action",
    # Event types
    "EventType",
    "MouseButton",
    "BaseEvent",
    "Event",
    "ActionEvent",
    "ScreenEvent",
    "AudioEvent",
    # Mouse events
    "MouseMoveEvent",
    "MouseDownEvent",
    "MouseUpEvent",
    "MouseScrollEvent",
    "MouseClickEvent",
    "MouseDoubleClickEvent",
    "MouseDragEvent",
    # Keyboard events
    "KeyDownEvent",
    "KeyUpEvent",
    "KeyTypeEvent",
    "KeyShortcutEvent",
    "SpecialKeyEvent",
    # Screen/audio events
    "ScreenFrameEvent",
    "AudioChunkEvent",
    # Window events
    "WindowEvent",
    "WindowStateEvent",
    # Database models (low-level)
    "Recording",
    "DBActionEvent",
    "Screenshot",
    "DBWindowEvent",
    # Processing
    "process_events",
    "remove_invalid_keyboard_events",
    "remove_redundant_mouse_move_events",
    "merge_consecutive_keyboard_events",
    "detect_key_shortcuts",
    "merge_consecutive_mouse_move_events",
    "merge_consecutive_mouse_scroll_events",
    "merge_sequential_key_type_events",
    "merge_consecutive_mouse_click_events",
    "detect_drag_events",
    "get_action_events",
    "get_screen_events",
    "get_audio_events",
    # Performance statistics
    "CaptureStats",
    "PerfStat",
    "plot_capture_performance",
    # Frame comparison
    "ComparisonReport",
    "FrameComparison",
    "compare_frames",
    "compare_video_to_images",
    "plot_comparison",
    # Visualization
    "create_demo",
    "create_html",
]
