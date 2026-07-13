"""Shared SQLite ledger connection-open helper (leaf, stdlib-only).

Both :class:`screencap.migration.MigrationLedger` and
:class:`screencap.backfill.ledger.BackfillLedger` open their short-lived
``sqlite3`` connections with the exact same ``content_index.py``-style discipline
(mkdir + ``0o700`` parent, symlink guard on parent + db path, connect, WAL +
``busy_timeout`` pragmas, and a post-WAL ``0o600`` chmod of the ``-wal``/``-shm``
sidecars). That sequence used to be copy-pasted line-for-line in both ledgers,
differing only in an error-message label; this module is the single home so the
lock/perm discipline can never drift between them.

Deliberately a **leaf**: it imports only the standard library (``os`` /
``sqlite3`` / ``pathlib``) — no ``screencap.daemon`` and nothing from the
privacy/enforcement/redaction packages — so both non-daemon ledgers (and the
SCR-33 DAG) can depend on it freely.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Cross-process coordination window: a concurrent writer (a second daemon, a
# resumed run) waits this long on a busy lock before erroring (mirrors
# ``PipelineLedger``). Shared so both ledgers stay identical.
BUSY_TIMEOUT_MS = 10000


def open_ledger_connection(
    db_path: Path,
    *,
    label: str,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a hardened ``sqlite3`` connection for an on-disk local-only ledger.

    Mirrors ``content_index._open``: creates the parent (``0o700``), refuses a
    symlinked parent or db path (sqlite cannot open with ``O_NOFOLLOW``, so we
    reject BEFORE connecting), connects, applies ``busy_timeout`` + WAL, and
    locks the create-to-chmod window (``0o600`` db + ``-wal``/``-shm`` sidecars)
    before any second reader can open them.

    Args:
        db_path: the ledger DB path (its parent is created if missing).
        label: a short human label (e.g. ``"backfill-ledger"``) used only in the
            symlink-guard error messages so a failure names the right ledger.
        busy_timeout_ms: SQLite ``busy_timeout`` in ms.

    Returns:
        An open connection with ``row_factory = sqlite3.Row``.

    Raises:
        OSError: if the parent resolves through a symlink or the db path is a
            symlink (the tamper vector).
    """
    path = db_path
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass

    # Symlink guard: sqlite cannot open with O_NOFOLLOW, so refuse a symlinked
    # DB path or parent before connect (mirrors content_index._open).
    if os.path.realpath(str(parent)) != os.path.abspath(str(parent)):
        raise OSError(f"{label} parent dir resolves through a symlink: {parent}")
    if path.is_symlink():
        raise OSError(f"{label} db path is a symlink: {path}")

    existed = path.exists()
    conn = sqlite3.connect(str(path))
    # Close the create-to-chmod window before any second reader can open it.
    if not existed:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    conn.execute("PRAGMA journal_mode=WAL")
    # WAL sidecars are created by SQLite's C internals at umask mode — lock them
    # down immediately, before a concurrent reader could open them.
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            try:
                os.chmod(side, 0o600)
            except OSError:
                pass
    conn.row_factory = sqlite3.Row
    return conn
