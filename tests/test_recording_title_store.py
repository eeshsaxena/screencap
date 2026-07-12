"""Round-trip tests for the mutable, local-only ``recording.title`` store (U1).

Covers the write helper (``recording_db.write_user_title``) and the read helper
(``catalog._read_user_title``), including the two load-bearing edges: renaming a
recording whose DB predates the ``title`` column, and a title surviving a
reprocess that re-runs ``_migrate_schema`` over the same DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from screencap.catalog import _read_user_title
from screencap.recording_db import write_user_title


def _make_current_schema_db(db_path: Path) -> None:
    """A recording.db at the current model schema (the ``title`` column exists).

    Uses the real engine ``create_db`` + ``insert_recording`` so the single
    authoritative ``recording`` row is present, mirroring how a live capture
    lands its DB.
    """
    from screencap.engine.db import create_db, crud

    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(
        session,
        {
            "timestamp": 1000.0,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        },
    )
    session.close()
    engine.dispose()


def _make_precolumn_db(db_path: Path) -> None:
    """A recording.db whose ``recording`` table LACKS the ``title`` column.

    Models a pre-existing recording captured before the editable-title feature:
    ``open_recording_db`` never migrates, so only ``write_user_title``'s
    ``_migrate_schema`` call can add the column. One authoritative row.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, "
            "task_description TEXT)"
        )
        conn.execute("INSERT INTO recording (id, timestamp) VALUES (1, 1000.0)")
        conn.commit()
    finally:
        conn.close()


def test_write_then_read_round_trips_a_title(tmp_path):
    """(a) A written title reads back verbatim (stripped)."""
    db = tmp_path / "recording.db"
    _make_current_schema_db(db)

    write_user_title(db, "Stripe Webhook Debugging")
    assert _read_user_title(db) == "Stripe Webhook Debugging"

    # Surrounding whitespace is normalized away on write.
    write_user_title(db, "  Trimmed Title  ")
    assert _read_user_title(db) == "Trimmed Title"


def test_whitespace_or_empty_write_clears_the_column(tmp_path):
    """(b) An empty / whitespace-only write reverts the title to NULL."""
    db = tmp_path / "recording.db"
    _make_current_schema_db(db)

    write_user_title(db, "Some Title")
    assert _read_user_title(db) == "Some Title"

    # Whitespace-only clears.
    write_user_title(db, "   ")
    assert _read_user_title(db) is None

    # Empty string clears.
    write_user_title(db, "Another Title")
    assert _read_user_title(db) == "Another Title"
    write_user_title(db, "")
    assert _read_user_title(db) is None

    # Explicit None clears.
    write_user_title(db, "Yet Another")
    assert _read_user_title(db) == "Yet Another"
    write_user_title(db, None)
    assert _read_user_title(db) is None


def test_read_on_db_without_title_column_returns_none(tmp_path):
    """(c) Reading a DB whose schema predates ``title`` yields None, not an error."""
    db = tmp_path / "recording.db"
    _make_precolumn_db(db)
    assert _read_user_title(db) is None


def test_write_on_precolumn_db_migrates_then_persists(tmp_path):
    """(d) The pre-existing-recording rename case.

    Writing to a DB whose ``recording`` table lacks the ``title`` column SUCCEEDS
    because ``write_user_title`` runs ``_migrate_schema`` first (adding the
    column); the value then reads back.
    """
    db = tmp_path / "recording.db"
    _make_precolumn_db(db)
    assert _read_user_title(db) is None  # no column yet

    write_user_title(db, "Renamed After The Fact")
    assert _read_user_title(db) == "Renamed After The Fact"

    # The column now genuinely exists on the single authoritative row.
    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(recording)")}
        assert "title" in cols
        rows = conn.execute("SELECT title FROM recording").fetchall()
        assert rows == [("Renamed After The Fact",)]
    finally:
        conn.close()


def test_title_survives_reprocess_re_migration(tmp_path):
    """(e) R1 reprocessing clause: a set title is not dropped by a re-migrate.

    A reprocess re-opens the same DB through ``get_session_for_path``, which
    re-runs ``_migrate_schema``. The UPDATE-based title lives in a column value,
    so a no-op ALTER re-migration must leave it intact.
    """
    db = tmp_path / "recording.db"
    _make_current_schema_db(db)

    write_user_title(db, "Persisted Across Reprocess")
    assert _read_user_title(db) == "Persisted Across Reprocess"

    # Simulate a reprocess re-opening (and re-migrating) the same DB.
    from screencap.engine.db import get_session_for_path

    session = get_session_for_path(str(db))
    session.close()

    assert _read_user_title(db) == "Persisted Across Reprocess"
