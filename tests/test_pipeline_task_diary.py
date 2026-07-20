"""U1 (day-diary) — diary columns, field-scoped protection, and the wire shape.

The task table grows the diary data model: stable ``block_id`` identity,
``thread_id`` same-day thread membership, a live-trailing ``is_open`` flag, and a
field-scoped ``edited_fields`` bitmask that records WHICH fields a user curated.

The load-bearing privacy rule here is the protection SPLIT (KTD-3, enabling
AE5): ``task_row_is_protected`` keeps its row-level meaning for the agent
re-carve (a user-touched row is never bulk-deleted), while the NEW
``task_field_is_protected`` answers per-field so a user-RENAMED block keeps its
name but its agent-written bullets stay purge-eligible and regeneratable. Get
that wrong and renaming a block silently exempts its bullets from AE5's privacy
promise — so these are marked ``@pytest.mark.privacy`` (the only lane CI runs).

Raw sqlite + ``PipelineLedger`` only — no Vision / OCR surface.
"""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

pytestmark = pytest.mark.privacy

# The pre-diary DDL (as pipeline_task_segments existed BEFORE U1's diary
# columns, i.e. the U5 source/edited shape) — used to simulate an existing
# user's recording.db whose table lacks block_id/thread_id/is_open/edited_fields.
_PRE_DIARY_TASK_SEGMENTS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_task_segments (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL,
    task_index INTEGER NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    name TEXT NOT NULL,
    category TEXT,
    confidence TEXT,
    metadata TEXT,
    source TEXT NOT NULL DEFAULT 'agent',
    edited INTEGER NOT NULL DEFAULT 0,
    updated_at REAL,
    UNIQUE (recording_id, task_index)
)
"""


def _make_recording_db(tmp_path: Path, *, name: str = "rec", ensure: bool = True) -> Path:
    """Create a recording.db with one recording row (recording_id resolvable)."""
    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import ensure_pipeline_state_schema

    rec_dir = tmp_path / name
    rec_dir.mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(session, {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    session.close()
    engine.dispose()

    if ensure:
        ensure_pipeline_state_schema(db_path)
    return db_path


def _agent_row(idx: int, name: str, **kw):
    from screencap.pipeline_state import TaskSegmentRow

    return TaskSegmentRow(
        task_index=idx,
        start_ts=kw.pop("start", float(idx)),
        end_ts=kw.pop("end", float(idx) + 1.0),
        name=name,
        source="agent",
        edited=False,
        **kw,
    )


# ---------------------------------------------------------------------------
# Protection split — the load-bearing predicate (test FIRST).
# ---------------------------------------------------------------------------


def test_agent_row_protects_no_field():
    """A plain agent row (nothing curated) protects no field — all purge-eligible."""
    from screencap.pipeline_state import (
        TASK_FIELD_BULLETS,
        TASK_FIELD_CATEGORY,
        TASK_FIELD_NAME,
        TASK_FIELD_TIME,
        TaskSegmentRow,
        task_field_is_protected,
    )

    row = TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=1.0, name="agent block")
    for field in (TASK_FIELD_NAME, TASK_FIELD_CATEGORY, TASK_FIELD_TIME, TASK_FIELD_BULLETS):
        assert task_field_is_protected(row, field) is False


def test_name_edited_block_protects_name_but_not_bullets():
    """AE5 core: a user-renamed agent block protects its NAME; bullets stay purge-eligible."""
    from screencap.pipeline_state import (
        EDITED_FIELD_NAME,
        TASK_FIELD_BULLETS,
        TASK_FIELD_CATEGORY,
        TASK_FIELD_NAME,
        TASK_FIELD_TIME,
        TaskSegmentRow,
        task_field_is_protected,
    )

    # An agent row the user renamed: edited=1 (row-level) + the NAME bit set.
    row = TaskSegmentRow(
        task_index=0, start_ts=0.0, end_ts=1.0, name="my rename",
        source="agent", edited=True, edited_fields=EDITED_FIELD_NAME,
    )
    assert task_field_is_protected(row, TASK_FIELD_NAME) is True
    # Everything the user did NOT touch — including bullets — stays eligible.
    assert task_field_is_protected(row, TASK_FIELD_BULLETS) is False
    assert task_field_is_protected(row, TASK_FIELD_CATEGORY) is False
    assert task_field_is_protected(row, TASK_FIELD_TIME) is False


def test_bullets_edited_block_protects_bullets():
    """A user-edited bullet set is protected; the untouched name is not."""
    from screencap.pipeline_state import (
        EDITED_FIELD_BULLETS,
        TASK_FIELD_BULLETS,
        TASK_FIELD_NAME,
        TaskSegmentRow,
        task_field_is_protected,
    )

    row = TaskSegmentRow(
        task_index=0, start_ts=0.0, end_ts=1.0, name="agent name",
        source="agent", edited=True, edited_fields=EDITED_FIELD_BULLETS,
    )
    assert task_field_is_protected(row, TASK_FIELD_BULLETS) is True
    assert task_field_is_protected(row, TASK_FIELD_NAME) is False


def test_user_row_protects_every_field():
    """A user-CREATED block is entirely user content — every field protected (U5)."""
    from screencap.pipeline_state import (
        TASK_FIELD_BULLETS,
        TASK_FIELD_CATEGORY,
        TASK_FIELD_NAME,
        TASK_FIELD_TIME,
        TaskSegmentRow,
        task_field_is_protected,
    )

    row = TaskSegmentRow(
        task_index=0, start_ts=0.0, end_ts=1.0, name="my block", source="user",
    )
    for field in (TASK_FIELD_NAME, TASK_FIELD_CATEGORY, TASK_FIELD_TIME, TASK_FIELD_BULLETS):
        assert task_field_is_protected(row, field) is True


def test_legacy_edited_row_conservatively_protects_all_fields():
    """A pre-bitmask edited agent row (edited=1, edited_fields=0) protects everything.

    We can't tell WHICH field a legacy row's user edited, so the migration-safe
    reading is the pre-split whole-row protection the user already saw — never
    silently drop protection on migrated data.
    """
    from screencap.pipeline_state import (
        TASK_FIELD_BULLETS,
        TASK_FIELD_NAME,
        TaskSegmentRow,
        task_field_is_protected,
    )

    row = TaskSegmentRow(
        task_index=0, start_ts=0.0, end_ts=1.0, name="renamed long ago",
        source="agent", edited=True, edited_fields=0,
    )
    assert task_field_is_protected(row, TASK_FIELD_NAME) is True
    assert task_field_is_protected(row, TASK_FIELD_BULLETS) is True


def test_row_level_protection_predicate_unchanged():
    """``task_row_is_protected`` keeps its row-level meaning (user OR edited)."""
    from screencap.pipeline_state import TaskSegmentRow, task_row_is_protected

    agent = TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=1.0, name="a")
    user = TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=1.0, name="u", source="user")
    edited = TaskSegmentRow(
        task_index=0, start_ts=0.0, end_ts=1.0, name="e", source="agent", edited=True,
    )
    assert task_row_is_protected(agent) is False
    assert task_row_is_protected(user) is True
    assert task_row_is_protected(edited) is True


def test_unknown_protection_field_raises():
    """A typo'd field name is a programming error, not a silent False."""
    from screencap.pipeline_state import TaskSegmentRow, task_field_is_protected

    row = TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=1.0, name="a")
    with pytest.raises(KeyError):
        task_field_is_protected(row, "nonsense")


# ---------------------------------------------------------------------------
# Half-edited row — the DB round-trip: rename-only re-homes to HIGH, records
# ONLY the name bit, and never collides with the LOW-range agent re-insert.
# ---------------------------------------------------------------------------


def test_half_edited_row_rehomes_high_and_records_name_bit(tmp_path):
    from screencap.pipeline_state import (
        TASK_FIELD_BULLETS,
        TASK_FIELD_NAME,
        USER_TASK_INDEX_BASE,
        PipelineLedger,
        task_field_is_protected,
    )

    db_path = _make_recording_db(tmp_path)
    ledger = PipelineLedger(db_path)

    ledger.replace_task_segments([
        _agent_row(0, "agent A"),
        _agent_row(1, "agent B"),
        _agent_row(2, "agent C"),
    ])

    # User renames ONLY the name of agent row 1 → re-homes into the HIGH range.
    edited_idx = ledger.update_task_segment(1, name="renamed B", mark_edited=True)
    assert edited_idx >= USER_TASK_INDEX_BASE

    rows = {r.task_index: r for r in ledger.read_task_segments()}
    half = rows[edited_idx]
    assert half.source == "agent"
    assert half.edited is True
    # The bitmask recorded ONLY the name edit → bullets remain agent-owned.
    assert task_field_is_protected(half, TASK_FIELD_NAME) is True
    assert task_field_is_protected(half, TASK_FIELD_BULLETS) is False

    # A fresh agent re-insert of the LOW range 0..N must NOT collide with the
    # re-homed half-edited row.
    ledger.replace_task_segments([_agent_row(i, f"agent {i}'") for i in range(3)])
    rows2 = {r.task_index: r for r in ledger.read_task_segments()}
    assert edited_idx in rows2  # survived the re-carve untouched
    assert rows2[edited_idx].name == "renamed B"
    agent_low = sorted(r.task_index for r in rows2.values() if r.source == "agent" and not r.edited)
    assert agent_low == [0, 1, 2]


def test_time_edit_records_time_bit_only(tmp_path):
    """Re-bounding an agent row protects its time span, not its name."""
    from screencap.pipeline_state import (
        TASK_FIELD_NAME,
        TASK_FIELD_TIME,
        PipelineLedger,
        task_field_is_protected,
    )

    db_path = _make_recording_db(tmp_path)
    ledger = PipelineLedger(db_path)
    ledger.replace_task_segments([_agent_row(0, "agent A", start=0.0, end=10.0)])

    new_idx = ledger.update_task_segment(0, start_ts=2.0, end_ts=8.0, mark_edited=True)
    row = next(r for r in ledger.read_task_segments() if r.task_index == new_idx)
    assert task_field_is_protected(row, TASK_FIELD_TIME) is True
    assert task_field_is_protected(row, TASK_FIELD_NAME) is False


def test_replace_still_never_deletes_user_or_edited_rows(tmp_path):
    """The re-carve protection (row-level) is unchanged by the field split."""
    from screencap.pipeline_state import PipelineLedger, TaskSegmentRow

    db_path = _make_recording_db(tmp_path)
    ledger = PipelineLedger(db_path)
    ledger.replace_task_segments([_agent_row(0, "a0"), _agent_row(1, "a1")])
    user_idx = ledger.insert_task_segment(
        TaskSegmentRow(task_index=0, start_ts=0.0, end_ts=1.0, name="mine")
    )
    edited_idx = ledger.update_task_segment(0, name="kept", mark_edited=True)

    ledger.replace_task_segments([_agent_row(0, "a0'")])

    survivors = {r.task_index for r in ledger.read_task_segments()}
    assert user_idx in survivors
    assert edited_idx in survivors


# ---------------------------------------------------------------------------
# Migration — the diary columns land on a PRE-diary existing db.
# ---------------------------------------------------------------------------


def test_migration_adds_diary_columns_to_pre_diary_db(tmp_path):
    from screencap.pipeline_state import ensure_pipeline_state_schema

    db_path = _make_recording_db(tmp_path, ensure=False)

    conn = sqlite3.connect(str(db_path))
    rec_id = conn.execute("SELECT id FROM recording LIMIT 1").fetchone()[0]
    conn.execute(_PRE_DIARY_TASK_SEGMENTS_DDL)
    conn.execute(
        "INSERT INTO pipeline_task_segments "
        "(recording_id, task_index, start_ts, end_ts, name, source, edited) "
        "VALUES (?, ?, ?, ?, ?, 'agent', 0)",
        (rec_id, 0, 1.0, 2.0, "pre-diary task"),
    )
    conn.commit()
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_task_segments)")}
    conn.close()
    assert "block_id" not in cols_before
    assert "edited_fields" not in cols_before

    ensure_pipeline_state_schema(db_path)

    conn = sqlite3.connect(str(db_path))
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_task_segments)")}
    row = conn.execute(
        "SELECT block_id, thread_id, is_open, edited_fields "
        "FROM pipeline_task_segments WHERE task_index=0"
    ).fetchone()
    conn.close()

    assert {"block_id", "thread_id", "is_open", "edited_fields"} <= cols_after
    # The pre-diary row reads back with the neutral defaults.
    assert row == (None, None, 0, 0)


def test_migration_diary_columns_idempotent(tmp_path):
    """Re-running ensure over an already-migrated DB never raises (duplicate-col tolerated)."""
    from screencap.pipeline_state import ensure_pipeline_state_schema

    db_path = _make_recording_db(tmp_path)
    ensure_pipeline_state_schema(db_path)
    ensure_pipeline_state_schema(db_path)

    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_task_segments)")}
    conn.close()
    assert {"block_id", "thread_id", "is_open", "edited_fields"} <= cols


def test_concurrent_duplicate_column_add_is_tolerated(tmp_path):
    """A second opener racing the ALTER hits 'duplicate column name' — must be swallowed."""
    from screencap.pipeline_state import _migrate_task_segments_columns

    db_path = _make_recording_db(tmp_path, ensure=False)
    conn = sqlite3.connect(str(db_path))
    conn.execute(_PRE_DIARY_TASK_SEGMENTS_DDL)
    conn.commit()

    # First migration adds the columns.
    _migrate_task_segments_columns(conn)
    # Manually re-adding one column raises the exact race error the migrator must
    # tolerate; then a second migrator pass over the now-migrated table is a no-op.
    with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
        conn.execute("ALTER TABLE pipeline_task_segments ADD COLUMN block_id TEXT")
    _migrate_task_segments_columns(conn)  # must not raise
    conn.close()


def test_ensure_on_read_only_db_does_not_raise(tmp_path):
    """A read-only recording.db (chmod 444) tolerates a schema ensure (no write path)."""
    from screencap.pipeline_state import ensure_pipeline_state_schema

    db_path = _make_recording_db(tmp_path)  # fully migrated, writable
    db_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        ensure_pipeline_state_schema(db_path)  # must not raise on a read-only file
    finally:
        db_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# Wire projection — new fields present serialize; absent serialize without.
# ---------------------------------------------------------------------------


def test_wire_projects_new_fields_when_present(tmp_path):
    from screencap.pipeline_state import (
        PipelineLedger,
        TaskSegmentRow,
        read_task_segments_wire,
    )

    db_path = _make_recording_db(tmp_path)
    rec_dir = db_path.parent
    ledger = PipelineLedger(db_path)

    ledger.replace_task_segments([
        TaskSegmentRow(
            task_index=0, start_ts=0.0, end_ts=60.0, name="Design systems work",
            category="design", confidence="high",
            metadata=json.dumps({
                "description": "worked on tokens",
                "bullets": ["token naming", "dark-mode audit"],
            }),
            block_id="blk-1", thread_id="thr-1", is_open=True,
        ),
    ])

    wire = read_task_segments_wire(rec_dir)
    assert len(wire) == 1
    row = wire[0]
    assert row["block_id"] == "blk-1"
    assert row["thread_id"] == "thr-1"
    assert row["is_open"] is True
    assert row["bullets"] == ["token naming", "dark-mode audit"]
    # The original six fields are still present and unchanged.
    assert row["task_index"] == 0
    assert row["name"] == "Design systems work"
    assert row["category"] == "design"
    assert row["confidence"] == "high"


def test_wire_omits_new_fields_when_absent(tmp_path):
    """A plain agent row (no diary data) projects the original 6-field shape only."""
    from screencap.pipeline_state import PipelineLedger, read_task_segments_wire

    db_path = _make_recording_db(tmp_path)
    rec_dir = db_path.parent
    ledger = PipelineLedger(db_path)
    ledger.replace_task_segments([_agent_row(0, "plain block")])

    wire = read_task_segments_wire(rec_dir)
    assert len(wire) == 1
    row = wire[0]
    assert set(row) == {
        "task_index", "start_ts", "end_ts", "name", "category", "confidence",
    }
    # None of the diary keys leak in when the row carries no diary data.
    for k in ("block_id", "thread_id", "is_open", "bullets"):
        assert k not in row


def test_wire_bullets_tolerates_unparseable_metadata(tmp_path):
    """Non-JSON / bullet-free metadata never raises and never fabricates bullets."""
    from screencap.pipeline_state import (
        PipelineLedger,
        TaskSegmentRow,
        read_task_segments_wire,
    )

    db_path = _make_recording_db(tmp_path)
    rec_dir = db_path.parent
    ledger = PipelineLedger(db_path)
    ledger.replace_task_segments([
        TaskSegmentRow(
            task_index=0, start_ts=0.0, end_ts=1.0, name="block",
            metadata="not json at all", block_id="b1",
        ),
    ])
    wire = read_task_segments_wire(rec_dir)
    row = wire[0]
    assert row["block_id"] == "b1"
    assert "bullets" not in row  # unparseable → no bullets, not a crash


def test_pydantic_tasksegment_carries_diary_fields():
    """The daemon TaskSegment model round-trips the new fields (forward-compat defaults)."""
    from screencap.daemon import schema

    # Present: the wire dict populates the model.
    seg = schema.TaskSegment(
        task_index=0, start_ts=0.0, end_ts=1.0, name="b",
        block_id="b1", thread_id="t1", is_open=True, bullets=["x", "y"],
    )
    dumped = seg.model_dump()
    assert dumped["block_id"] == "b1"
    assert dumped["thread_id"] == "t1"
    assert dumped["is_open"] is True
    assert dumped["bullets"] == ["x", "y"]

    # Absent: an older-shape wire dict (no diary keys) still validates with
    # empty/None defaults, so an old daemon's payload decodes.
    old = schema.TaskSegment(task_index=1, start_ts=0.0, end_ts=1.0, name="old")
    d2 = old.model_dump()
    assert d2["block_id"] is None
    assert d2["thread_id"] is None
    assert d2["is_open"] is False
    assert d2["bullets"] == []
