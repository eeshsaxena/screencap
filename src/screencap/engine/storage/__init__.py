"""Storage implementations for capture data persistence.

This module provides storage backends for persisting captured GUI events.
The primary implementation uses SQLite for reliable, portable storage.

Usage:
    from screencap.engine.storage import SQLiteStorage

    # Create storage
    storage = SQLiteStorage("./capture/capture.db")

    # Write events
    storage.write_event(event)

    # Query events
    events = storage.get_events(start_time=0.0, end_time=100.0)

    storage.close()
"""

from __future__ import annotations

from pathlib import Path

# Import SQLite-specific implementation
from screencap.engine.storage.sqlite import SQLiteStorage

# Re-export from the original storage module for backward compatibility
from screencap.engine.storage_impl import (
    EVENT_TYPE_MAP,
    Capture,
    CaptureStorage,
    Stream,
    create_capture,
    load_capture,
)


def get_storage(db_path: str | Path) -> CaptureStorage:
    """Get a storage instance for the given database path.

    This is a convenience function that creates a CaptureStorage instance.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        CaptureStorage instance.
    """
    return CaptureStorage(db_path)


__all__ = [
    # Storage classes
    "CaptureStorage",
    "SQLiteStorage",
    # Data models
    "Capture",
    "Stream",
    # Mappings
    "EVENT_TYPE_MAP",
    # Convenience functions
    "create_capture",
    "load_capture",
    "get_storage",
]
