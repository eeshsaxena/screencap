"""Open helpers and schema-presence checks for ``recording.db``.

This module is the single source of truth for opening a ``recording.db``
SQLite connection in the screencap layer (everything under ``src/screencap/``
that is *not* the ``engine/`` sub-package). Callers should import
``open_recording_db``, ``has_table``, ``has_column``, and ``Row`` from here
instead of importing ``sqlite3`` directly.

Why functions, not a class
--------------------------
The module exposes three plain functions plus a re-export of ``sqlite3.Row``.
There is no adapter object, no caching, and no row-level abstraction. Each
consumer continues to write its own ``SELECT`` statements against the yielded
connection — the helper only standardises connection acquisition (PRAGMAs,
existence check) and column/table-presence introspection.

Read-only is advisory
---------------------
``read_only=True`` (the default) sets ``PRAGMA query_only=ON``. This is a
*soft* mode — code that calls ``conn.execute("PRAGMA query_only=OFF")`` can
flip it back. The flag is intended to catch accidental writes, not to defend
against malicious internal code. Writers must opt in explicitly with
``read_only=False``.

We deliberately avoid the ``mode=ro`` URI form because it fails when the
``.db-shm``/``.db-wal`` sidecars are missing (the post-upload state — see
``upload.py`` exclusions). ``query_only=ON`` works regardless of sidecar state.

Documented exception
--------------------
``src/screencap/privacy/scrub_worker.py`` keeps its own ``sqlite3.connect``
call. It has different concurrency requirements (live-writer coexistence and
``busy_timeout=10000``) that the standard helper does not serve. See the
inline comment in that file for the list of evolving columns it reads.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection, Cursor, OperationalError, Row
from typing import Iterator

__all__ = [
    "open_recording_db",
    "has_table",
    "has_column",
    "Connection",
    "Cursor",
    "OperationalError",
    "Row",
]


@contextmanager
def open_recording_db(
    path: Path | str,
    *,
    read_only: bool = True,
    row_factory: object | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open ``recording.db`` with the canonical PRAGMAs and yield the connection.

    Raises ``FileNotFoundError`` if the path does not point at an existing
    file (prevents ``sqlite3.connect`` from silently creating an empty DB).

    Sets ``PRAGMA busy_timeout=5000``. When ``read_only=True`` (the default),
    also sets ``PRAGMA query_only=ON``. When ``row_factory`` is provided
    (typically ``Row`` for column-by-name access), assigns it before yielding.

    Closes the connection on exit, even if the body raises.
    """
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(f"recording.db not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        if row_factory is not None:
            conn.row_factory = row_factory  # type: ignore[assignment]
        yield conn
    finally:
        conn.close()


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    """Return whether the given table exists in ``conn``."""
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    """Return whether ``table`` has a column named ``col``.

    Returns ``False`` (without raising) when ``table`` does not exist —
    ``PRAGMA table_info`` on a missing table yields an empty cursor.

    The table name is interpolated into the PRAGMA because PRAGMA does not
    accept bound parameters. Callers must pass trusted, internal table names.
    """
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == col for row in cur.fetchall())
