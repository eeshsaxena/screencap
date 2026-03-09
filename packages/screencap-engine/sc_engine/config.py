"""Configuration management using pydantic-settings.

Loads settings from environment variables and .env file.

"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields

from pydantic_settings import BaseSettings

STOP_STRS = [
    "sc.stop",
]
SPECIAL_CHAR_STOP_SEQUENCES = [["ctrl", "ctrl", "ctrl"]]


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env file.

    Priority order for configuration values:
    1. Environment variables
    2. .env file
    3. Default values

    
    """

    # API keys
    openai_api_key: str | None = None

    # Record and replay defaults
    RECORD_WINDOW_DATA: bool = True
    RECORD_READ_ACTIVE_ELEMENT_STATE: bool = True
    RECORD_VIDEO: bool = True
    RECORD_AUDIO: bool = False
    RECORD_BROWSER_EVENTS: bool = False
    # if false, only write video events corresponding to screenshots
    RECORD_FULL_VIDEO: bool = False
    RECORD_IMAGES: bool = False
    # useful for debugging but expensive computationally
    LOG_MEMORY: bool = False
    VIDEO_ENCODING: str = "libx264"
    VIDEO_PIXEL_FORMAT: str = "yuv444p"
    VIDEO_GOP_SIZE: int = 48  # keyframe interval (frames); bounds max crash loss to 1 GOP
    # sequences that when typed, will stop the recording of ActionEvents
    STOP_SEQUENCES: list[list[str]] = [
        list(stop_str) for stop_str in STOP_STRS
    ] + SPECIAL_CHAR_STOP_SEQUENCES

    # Maximum screenshots per second (0 = unlimited / legacy behavior)
    SCREEN_CAPTURE_FPS: float = 20.0

    # Auto-cut video into chunks at this interval (seconds). 0 = legacy single-file.
    VIDEO_CHUNK_DURATION: float = 3600.0

    # Skip post_process_events in chunked mode (set automatically)
    SKIP_POST_PROCESS: bool = False

    # Accessibility query tuning — controls how aggressively the recorder
    # queries the target app's accessibility tree during recording.
    # Minimum seconds between accessibility queries (0 = every event)
    AX_QUERY_INTERVAL: float = 0.5
    # Max depth for accessibility tree traversal (lower = less IPC to target app)
    AX_MAX_DEPTH: int = 3
    # Max wall-clock seconds for a single dump_state traversal
    AX_DUMP_TIMEOUT: float = 0.5
    # Per-element IPC timeout in seconds (caps hangs on unresponsive apps)
    AX_ELEMENT_TIMEOUT: float = 1.0
    # Event-aware AX query routing — different event types get different depths
    AX_MOVE_QUERY_INTERVAL: float = 1.0   # Longer interval for moves
    AX_CLICK_MAX_DEPTH: int = 4           # Deeper for clicks
    AX_MOVE_MAX_DEPTH: int = 1            # Shallow for moves
    AX_SCROLL_MAX_DEPTH: int = 2          # Medium for scrolls

    # Screenshot deduplication
    SCREENSHOT_DEDUP: bool = False
    SCREENSHOT_MIN_INTERVAL: float = 1.0       # seconds between saves
    SCREENSHOT_HASH_THRESHOLD: int = 8         # Hamming distance (0=identical, 64=opposite)

    # Variable-rate capture (action-aware)
    SCREENSHOT_ACTION_AWARE: bool = True
    SCREENSHOT_CLICK_INTERVAL: float = 0.0       # Always save (0 = no floor)
    SCREENSHOT_DRAG_INTERVAL: float = 0.1        # ~10 fps during drag
    SCREENSHOT_SCROLL_INTERVAL: float = 0.1      # ~10 fps during scroll/zoom
    SCREENSHOT_TYPE_INTERVAL: float = 1.0        # ~1 fps during typing
    SCREENSHOT_IDLE_INTERVAL: float = 2.0        # ~0.5 fps idle mouse move
    SCREENSHOT_SCROLL_SETTLE: float = 0.4        # Settle frame after scroll silence

    # Performance plotting
    PLOT_PERFORMANCE: bool = True

    # Browser Events Record (extension) configurations
    BROWSER_WEBSOCKET_SERVER_IP: str = "localhost"
    BROWSER_WEBSOCKET_PORT: int = 8765
    BROWSER_WEBSOCKET_MAX_SIZE: int = 2**22  # 4MB

    # Database
    DB_ECHO: bool = False

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",  # ignore extra env vars
    }


config = Settings()
# Keep backward-compatible alias
settings = config


# ---------------------------------------------------------------------------
# RecordingConfig: user-facing overrides for Recorder constructor
# ---------------------------------------------------------------------------

# Mapping from RecordingConfig field names to Settings attribute names
_FIELD_TO_CONFIG_ATTR = {
    "capture_video": "RECORD_VIDEO",
    "capture_audio": "RECORD_AUDIO",
    "capture_images": "RECORD_IMAGES",
    "capture_window_data": "RECORD_WINDOW_DATA",
    "capture_browser_events": "RECORD_BROWSER_EVENTS",
    "capture_full_video": "RECORD_FULL_VIDEO",
    "video_encoding": "VIDEO_ENCODING",
    "video_pixel_format": "VIDEO_PIXEL_FORMAT",
    "stop_sequences": "STOP_SEQUENCES",
    "log_memory": "LOG_MEMORY",
    "plot_performance": "PLOT_PERFORMANCE",
    "screen_capture_fps": "SCREEN_CAPTURE_FPS",
    "video_chunk_duration": "VIDEO_CHUNK_DURATION",
    "skip_post_process": "SKIP_POST_PROCESS",
    "ax_query_interval": "AX_QUERY_INTERVAL",
    "ax_max_depth": "AX_MAX_DEPTH",
    "ax_dump_timeout": "AX_DUMP_TIMEOUT",
    "ax_element_timeout": "AX_ELEMENT_TIMEOUT",
    "ax_move_query_interval": "AX_MOVE_QUERY_INTERVAL",
    "ax_click_max_depth": "AX_CLICK_MAX_DEPTH",
    "ax_move_max_depth": "AX_MOVE_MAX_DEPTH",
    "ax_scroll_max_depth": "AX_SCROLL_MAX_DEPTH",
    "screenshot_dedup": "SCREENSHOT_DEDUP",
    "screenshot_min_interval": "SCREENSHOT_MIN_INTERVAL",
    "screenshot_hash_threshold": "SCREENSHOT_HASH_THRESHOLD",
    "screenshot_action_aware": "SCREENSHOT_ACTION_AWARE",
    "screenshot_click_interval": "SCREENSHOT_CLICK_INTERVAL",
    "screenshot_drag_interval": "SCREENSHOT_DRAG_INTERVAL",
    "screenshot_scroll_interval": "SCREENSHOT_SCROLL_INTERVAL",
    "screenshot_type_interval": "SCREENSHOT_TYPE_INTERVAL",
    "screenshot_idle_interval": "SCREENSHOT_IDLE_INTERVAL",
    "screenshot_scroll_settle": "SCREENSHOT_SCROLL_SETTLE",
}


@dataclass
class RecordingConfig:
    """User-facing recording options. ``None`` means 'use default from config'."""

    capture_video: bool | None = None
    capture_audio: bool | None = None
    capture_images: bool | None = None
    capture_window_data: bool | None = None
    capture_browser_events: bool | None = None
    capture_full_video: bool | None = None
    video_encoding: str | None = None
    video_pixel_format: str | None = None
    stop_sequences: list[list[str]] | None = None
    log_memory: bool | None = None
    plot_performance: bool | None = None
    screen_capture_fps: float | None = None
    video_chunk_duration: float | None = None
    skip_post_process: bool | None = None
    ax_query_interval: float | None = None
    ax_max_depth: int | None = None
    ax_dump_timeout: float | None = None
    ax_element_timeout: float | None = None
    ax_move_query_interval: float | None = None
    ax_click_max_depth: int | None = None
    ax_move_max_depth: int | None = None
    ax_scroll_max_depth: int | None = None
    screenshot_dedup: bool | None = None
    screenshot_min_interval: float | None = None
    screenshot_hash_threshold: int | None = None
    screenshot_action_aware: bool | None = None
    screenshot_click_interval: float | None = None
    screenshot_drag_interval: float | None = None
    screenshot_scroll_interval: float | None = None
    screenshot_type_interval: float | None = None
    screenshot_idle_interval: float | None = None
    screenshot_scroll_settle: float | None = None


@contextmanager
def config_override(recording_config: RecordingConfig):
    """Temporarily override config settings from a RecordingConfig.

    Saves original values, applies non-None overrides, yields, then restores.
    """
    originals: dict[str, object] = {}
    for field in fields(recording_config):
        value = getattr(recording_config, field.name)
        if value is not None:
            config_attr = _FIELD_TO_CONFIG_ATTR[field.name]
            originals[config_attr] = getattr(config, config_attr)
            object.__setattr__(config, config_attr, value)
    try:
        yield
    finally:
        for config_attr, original_value in originals.items():
            object.__setattr__(config, config_attr, original_value)


def build_config_overrides(recording_config: RecordingConfig) -> dict[str, object]:
    """Build a dict of config overrides to pass to child processes.

    Returns a mapping of Settings attribute names to their overridden values,
    derived from non-None fields in the RecordingConfig.
    """
    overrides: dict[str, object] = {}
    for field in fields(recording_config):
        value = getattr(recording_config, field.name)
        if value is not None:
            config_attr = _FIELD_TO_CONFIG_ATTR[field.name]
            overrides[config_attr] = value
    return overrides


def apply_config_overrides(overrides: dict[str, object] | None) -> None:
    """Apply config overrides in a child process.

    Called at the start of spawned child processes to restore config
    values that were set via config_override() in the parent process.
    Spawn mode on macOS re-imports modules, losing in-memory changes.
    """
    if not overrides:
        return
    for attr, value in overrides.items():
        object.__setattr__(config, attr, value)
