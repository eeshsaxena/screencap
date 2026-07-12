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

PRAGMA statements (e.g., ``wal_checkpoint``, ``optimize``) bypass
``query_only`` and execute regardless. ``read_only=True`` does not protect
against PRAGMA-driven mutations.

We deliberately avoid the ``mode=ro`` URI form because it fails when the
``.db-shm``/``.db-wal`` sidecars are missing (the post-upload state — see
``upload.py`` exclusions). ``query_only=ON`` works regardless of sidecar state.

Documented exception
--------------------
``src/screencap/enforcement/scrub_worker.py`` keeps its own ``sqlite3.connect``
call. It has different concurrency requirements (live-writer coexistence and
``busy_timeout=10000``) that the standard helper does not serve. See the
inline comment in that file for the list of evolving columns it reads.

Limitations
-----------
- ``open_recording_db`` uses ``Path.is_file()`` for its existence guard,
  which follows symlinks. The helper assumes the caller controls the
  path; do not pass untrusted user input.
- ``has_table`` and ``has_column`` are point-in-time queries against the
  current schema. If the engine writer's ``_migrate_schema`` runs an
  ``ALTER TABLE`` between a consumer's ``has_column`` check and its
  subsequent ``SELECT``, the consumer can take the wrong branch. In
  practice ``_migrate_schema`` only fires at recorder startup, so the
  window is narrow. Consumers that need stronger guarantees should
  re-check inside a ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from sqlite3 import Connection, Cursor, OperationalError, Row

__all__ = [
    "open_recording_db",
    "has_table",
    "has_column",
    "write_user_title",
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
    busy_timeout_ms: int = 5000,
) -> Iterator[sqlite3.Connection]:
    """Open ``recording.db`` with the canonical PRAGMAs and yield the connection.

    Raises ``FileNotFoundError`` if the path does not point at an existing
    file (prevents ``sqlite3.connect`` from silently creating an empty DB).

    Sets ``PRAGMA busy_timeout`` (``busy_timeout_ms``, default 5000). Lower it
    for a read that must fail fast on a locked DB rather than block for the full
    default — the caller then classifies the resulting ``OperationalError``
    (e.g. ``catalog._read_recording_meta``'s locked-vs-corrupt split, SCR-166).
    When ``read_only=True`` (the default), also sets ``PRAGMA query_only=ON``.
    When ``row_factory`` is provided (typically ``Row`` for column-by-name
    access), assigns it before yielding.

    Closes the connection on exit, even if the body raises.
    """
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(f"recording database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
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
    accept bound parameters. ``table`` must be a Python identifier; this is
    enforced to close the SQL injection vector at zero runtime cost.
    """
    if not table.isidentifier():
        raise ValueError(f"invalid table name: {table!r}")
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == col for row in cur.fetchall())


def write_user_title(path: Path | str, title: str | None) -> None:
    """Persist the mutable, local-only ``recording.title`` (an editable rename).

    Read back by :func:`screencap.catalog._read_user_title`. Local-only by rule
    (R8): it lives in ``recording.db``, which is never uploaded.

    ``title`` is stored stripped; an empty / whitespace-only ``title`` (or
    ``None``) clears the column back to ``NULL`` — i.e. reverts to the default
    humanized directory name the catalog falls back to.

    A pre-existing recording's DB can predate the ``title`` column (``recording``
    was captured before this feature; ``open_recording_db`` never migrates). So
    we run :func:`screencap.engine.db._migrate_schema` FIRST — it ALTER-ADDs the
    missing column (and tolerates a concurrent adder via its internal
    duplicate-column guard) — so the subsequent ``UPDATE`` always targets a real
    column. This is the pre-existing-recording rename path.

    One-row-per-``recording.db`` assumption: there is exactly one authoritative
    ``recording`` row per DB, so the ``UPDATE`` deliberately has NO
    ``WHERE id=?`` — it sets the single row. (Never bind the STRING
    ``.recording_id`` to the INTEGER ``id`` column: that matches zero rows
    silently. There is no such bind here.) Copy rows created via
    ``original_recording_id`` are an out-of-scope edge tracked in the plan's Open
    Questions.

    Lock-tolerant in the same spirit as the read side: it opens through
    ``open_recording_db`` (which sets ``busy_timeout`` so a transient writer lock
    is waited out) and guards ``has_table`` so a DB with no ``recording`` table is
    a no-op rather than a crash.
    """
    # Deferred: importing the engine db pulls SQLAlchemy; keep this module light.
    from screencap.engine.db import _migrate_schema

    # Ensure the column exists on a DB that predates it (handles the
    # duplicate-column race internally). No-op on an already-current schema.
    _migrate_schema(str(path))

    normalized = title.strip() if title and title.strip() else None
    with open_recording_db(path, read_only=False) as conn:
        if not has_table(conn, "recording"):
            return
        conn.execute("UPDATE recording SET title=?", (normalized,))
        conn.commit()
