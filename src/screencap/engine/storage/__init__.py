"""Event-type registry for screencap engine events.

The legacy ``capture.db`` storage backend has been removed. This package now
exposes only ``EVENT_TYPE_MAP``, the canonical mapping from event ``type``
strings to their Pydantic event classes. ``recording.db`` (the live engine
schema) is opened directly via raw ``sqlite3`` in the screencap layer or via
SQLAlchemy in ``screencap.engine.db``.
"""

from __future__ import annotations

from screencap.engine.storage.sqlite import EVENT_TYPE_MAP

__all__ = ["EVENT_TYPE_MAP"]
