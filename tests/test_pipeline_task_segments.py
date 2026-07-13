"""Tests for U5 — source/edited coexistence in the ``pipeline_task_segments`` store.

The store must let agent-generated and user-authored task segments coexist so
re-segmentation never clobbers user work (R8, KTD3):

* ``replace_task_segments`` (the agent re-segmentation sink) is SCOPED — it
  deletes only unedited agent rows (``source='agent' AND edited=0``) and never
  touches ``source='user'`` rows or user-edited (``edited=1``) agent rows.
* Agent rows live in a contiguous LOW ``task_index`` range; user rows draw from a
  disjoint HIGH range (``USER_TASK_INDEX_BASE`` +) so a fresh agent re-insert of
  ``0..N`` can never collide with a user row on ``UNIQUE(recording_id, task_index)``.
* An edited agent row is RE-HOMED into the high range so the same collision guard
  covers it.
* The ``source``/``edited`` columns are added to EXISTING (pre-columns) DBs via a
  guarded ``ALTER TABLE`` inside ``ensure_pipeline_state_schema`` — a raw-DDL
  table that ``CREATE TABLE IF NOT EXISTS`` alone can NOT migrate.

These are data-model tests (no capture / privacy surface), so none are marked
``@pytest.mark.privacy``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# The pre-columns DDL (as pipeline_task_segments existed BEFORE U5) — used to
# simulate an existing user's recording.db whose table lacks source/edited.
_OLD_TASK_SEGMENTS_DDL = """
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
    updated_at REAL,
    UNIQUE (recording_id, task_index)
)
"""


def _make_recording_db(tmp_path: Path, *, name: str = "rec", ensure: bool = True) -> Path:
    """Create a recording.db with one recording row (recording_id resolvable).

    With ``ensure=True`` the current ``pipeline_task_segments`` schema is created;
    with ``ensure=False`` the table is left absent so a test can create the
    PRE-columns table itself (the migration case).
    """
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


def _agent_row(idx: int, name: str, *, start: float | None = None, end: float | None = None):
    """A fresh agent row at ``idx`` (source='agent', edited=False)."""
    from screencap.pipeline_state import TaskSegmentRow

    return TaskSegmentRow(
        task_index=idx,
        start_ts=start if start is not None else float(idx),
        end_ts=end if end is not None else float(idx) + 1.0,
        name=name,
        source="agent",
        edited=False,
    )


def _seg(name: str, *, start: float = 0.0, end: float = 1.0):
    """An input row for ``insert_task_segment`` (task_index/source are allocated/forced)."""
    from screencap.pipeline_state import TaskSegmentRow

    return TaskSegmentRow(task_index=0, start_ts=start, end_ts=end, name=name)


# ---------------------------------------------------------------------------
# Migration on a PRE-columns EXISTING db (the common existing-user case).
# ---------------------------------------------------------------------------


def test_migration_adds_source_edited_to_pre_columns_db(tmp_path):
    """An EXISTING table without source/edited gains them + old rows default correctly."""
    from screencap.pipeline_state import ensure_pipeline_state_schema

    db_path = _make_recording_db(tmp_path, ensure=False)

    # Simulate an existing user's DB: the OLD table + a pre-existing agent row.
    conn = sqlite3.connect(str(db_path))
    rec_id = conn.execute("SELECT id FROM recording LIMIT 1").fetchone()[0]
    conn.execute(_OLD_TASK_SEGMENTS_DDL)
    conn.execute(
        "INSERT INTO pipeline_task_segments "
        "(recording_id, task_index, start_ts, end_ts, name) VALUES (?, ?, ?, ?, ?)",
        (rec_id, 0, 1.0, 2.0, "pre-existing task"),
    )
    conn.commit()
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_task_segments)")}
    conn.close()
    assert "source" not in cols_before
    assert "edited" not in cols_before

    # Migrate the existing DB.
    ensure_pipeline_state_schema(db_path)

    conn = sqlite3.connect(str(db_path))
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_task_segments)")}
    row = conn.execute(
        "SELECT source, edited FROM pipeline_task_segments WHERE task_index=0"
    ).fetchone()
    conn.close()

    assert "source" in cols_after
    assert "edited" in cols_after
    # The pre-existing row reads back as an unedited agent row.
    assert row[0] == "agent"
    assert row[1] == 0


def test_migration_is_idempotent(tmp_path):
    """Re-running ensure over an already-migrated DB is a no-op (no duplicate-col error)."""
    from screencap.pipeline_state import ensure_pipeline_state_schema

    db_path = _make_recording_db(tmp_path)  # already has current schema
    ensure_pipeline_state_schema(db_path)  # second call must not raise
    ensure_pipeline_state_schema(db_path)  # third call, still fine

    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_task_segments)")}
    conn.close()
    assert {"source", "edited"} <= cols


# ---------------------------------------------------------------------------
# Scoped replace — never clobbers user / edited rows.
# ---------------------------------------------------------------------------


def test_scoped_replace_preserves_user_and_edited_rows(tmp_path):
    from screencap.pipeline_state import USER_TASK_INDEX_BASE, PipelineLedger

    db_path = _make_recording_db(tmp_path)
    ledger = PipelineLedger(db_path)

    # 1. An initial agent set at low indices 0,1,2.
    ledger.replace_task_segments([
        _agent_row(0, "agent A"),
        _agent_row(1, "agent B"),
        _agent_row(2, "agent C"),
    ])

    # 2. A user-authored task (high range) and a user-EDITED agent row. Editing
    #    the agent row at LOW index 1 RE-HOMES it into the high range, so the
    #    fresh agent re-insert of index 1 below can't collide with it.
    user_idx = ledger.insert_task_segment(_seg("my task"))
    edited_idx = ledger.update_task_segment(1, name="renamed B", mark_edited=True)
    assert edited_idx >= USER_TASK_INDEX_BASE  # re-homed out of the agent's range

    # 3. A fresh agent re-segmentation over 2 new spans.
    ledger.replace_task_segments([
        _agent_row(0, "agent A'"),
        _agent_row(1, "agent B'"),
    ])

    rows = {r.task_index: r for r in ledger.read_task_segments()}

    # The user row survived untouched.
    assert user_idx in rows
    assert rows[user_idx].source == "user"
    assert rows[user_idx].name == "my task"

    # The edited agent row survived with its user-supplied name.
    assert edited_idx in rows
    assert rows[edited_idx].source == "agent"
    assert rows[edited_idx].edited is True
    assert rows[edited_idx].name == "renamed B"

    # The unedited agent rows were refreshed to the new set (low, contiguous).
    agent_unedited = sorted(
        (r for r in ledger.read_task_segments() if r.source == "agent" and not r.edited),
        key=lambda r: r.task_index,
    )
    assert [r.name for r in agent_unedited] == ["agent A'", "agent B'"]
    assert [r.task_index for r in agent_unedited] == [0, 1]


def test_empty_agent_result_clears_only_unedited_agent_rows(tmp_path):
    from screencap.pipeline_state import PipelineLedger

    db_path = _make_recording_db(tmp_path)
    ledger = PipelineLedger(db_path)

    ledger.replace_task_segments([_agent_row(0, "agent A"), _agent_row(1, "agent B")])
    user_idx = ledger.insert_task_segment(_seg("kept task"))
    edited_idx = ledger.update_task_segment(0, name="kept agent", mark_edited=True)

    # An empty agent result (provider produced nothing this pass).
    ledger.replace_task_segments([])

    rows = {r.task_index: r for r in ledger.read_task_segments()}
    # Only the two protected rows remain — the unedited agent row is gone.
    assert set(rows) == {user_idx, edited_idx}
    assert rows[user_idx].source == "user"
    assert rows[edited_idx].edited is True


# ---------------------------------------------------------------------------
# Disjoint index ranges — the KEY regression: no UNIQUE collision.
# ---------------------------------------------------------------------------


def test_fresh_agent_reinsert_never_collides_with_user_row(tmp_path):
    """A fresh agent re-insert of 0..N must never raise IntegrityError vs a user row."""
    from screencap.pipeline_state import USER_TASK_INDEX_BASE, PipelineLedger

    db_path = _make_recording_db(tmp_path)
    ledger = PipelineLedger(db_path)

    ledger.replace_task_segments([_agent_row(i, f"agent {i}") for i in range(3)])

    # A user task lands in the disjoint HIGH range.
    user_idx = ledger.insert_task_segment(_seg("user task"))
    assert user_idx >= USER_TASK_INDEX_BASE

    # A fresh agent pass re-inserts low indices 0,1,2 — must NOT collide.
    ledger.replace_task_segments([_agent_row(i, f"agent {i}'") for i in range(3)])

    rows = ledger.read_task_segments()
    agent = [r for r in rows if r.source == "agent"]
    user = [r for r in rows if r.source == "user"]
    assert [r.task_index for r in agent] == [0, 1, 2]
    assert len(user) == 1
    assert user[0].task_index == user_idx


def test_multiple_user_inserts_get_distinct_high_indices(tmp_path):
    from screencap.pipeline_state import USER_TASK_INDEX_BASE, PipelineLedger

    db_path = _make_recording_db(tmp_path)
    ledger = PipelineLedger(db_path)

    i1 = ledger.insert_task_segment(_seg("u1"))
    i2 = ledger.insert_task_segment(_seg("u2"))
    i3 = ledger.insert_task_segment(_seg("u3"))

    assert i1 >= USER_TASK_INDEX_BASE
    assert len({i1, i2, i3}) == 3  # distinct
    assert i2 > i1 and i3 > i2  # monotonically allocated above the base


# ---------------------------------------------------------------------------
# Row-level insert / update / delete round-trip.
# ---------------------------------------------------------------------------


def test_row_level_insert_update_delete_round_trip(tmp_path):
    from screencap.pipeline_state import PipelineLedger

    db_path = _make_recording_db(tmp_path)
    ledger = PipelineLedger(db_path)

    # insert
    idx = ledger.insert_task_segment(_seg("task one", start=5.0, end=9.0))
    seg = next(r for r in ledger.read_task_segments() if r.task_index == idx)
    assert seg.source == "user"
    assert seg.name == "task one"
    assert seg.start_ts == 5.0 and seg.end_ts == 9.0

    # update (rename + bounds), no edited flag needed for a user row
    ret = ledger.update_task_segment(idx, name="task one!", start_ts=6.0, end_ts=10.0)
    assert ret == idx  # user rows stay in place (already high)
    seg = next(r for r in ledger.read_task_segments() if r.task_index == idx)
    assert seg.name == "task one!"
    assert seg.start_ts == 6.0 and seg.end_ts == 10.0

    # delete
    assert ledger.delete_task_segment(idx) is True
    assert all(r.task_index != idx for r in ledger.read_task_segments())
    # deleting a non-existent row returns False (no raise)
    assert ledger.delete_task_segment(idx) is False


def test_update_nonexistent_returns_none(tmp_path):
    from screencap.pipeline_state import PipelineLedger

    db_path = _make_recording_db(tmp_path)
    ledger = PipelineLedger(db_path)
    assert ledger.update_task_segment(999, name="nope") is None


# ---------------------------------------------------------------------------
# read_task_segments ordering across mixed sources.
# ---------------------------------------------------------------------------


def test_read_orders_by_task_index_across_mixed_sources(tmp_path):
    from screencap.pipeline_state import PipelineLedger

    db_path = _make_recording_db(tmp_path)
    ledger = PipelineLedger(db_path)

    ledger.replace_task_segments([_agent_row(0, "a0"), _agent_row(1, "a1")])
    u1 = ledger.insert_task_segment(_seg("user1"))
    u2 = ledger.insert_task_segment(_seg("user2"))

    rows = ledger.read_task_segments()
    indices = [r.task_index for r in rows]
    # Ascending order, agent (low) before user (high).
    assert indices == sorted(indices)
    assert indices[:2] == [0, 1]
    assert indices[2:] == [u1, u2]
    assert [r.name for r in rows] == ["a0", "a1", "user1", "user2"]


# ---------------------------------------------------------------------------
# terminal_stage._persist_local_tasks — agent pass preserves user rows in BOTH
# the ledger AND tasks.json.
# ---------------------------------------------------------------------------


def test_persist_local_tasks_preserves_user_rows(tmp_path):
    import json

    from screencap.pipeline_state import PipelineLedger
    from screencap.terminal_stage import _persist_local_tasks

    db_path = _make_recording_db(tmp_path)
    rec_dir = db_path.parent
    ledger = PipelineLedger(db_path)

    # A pre-existing user row in the ledger + a user entry in tasks.json.
    user_idx = ledger.insert_task_segment(_seg("my manual task", start=100.0, end=200.0))
    (rec_dir / "tasks.json").write_text(json.dumps({
        "tasks": [{"name": "my manual task", "start_ts": 100.0, "end_ts": 200.0,
                   "source": "user", "edited": False}],
    }))

    # An agent re-segmentation pass with two fresh tasks.
    agent_tasks = {"tasks": [
        {"name": "agent one", "start_ts": 0.0, "end_ts": 10.0, "category": "dev"},
        {"name": "agent two", "start_ts": 10.0, "end_ts": 20.0},
    ]}
    n = _persist_local_tasks(rec_dir, ledger, agent_tasks)
    assert n == 2  # reports the agent task count for this pass

    # Ledger: 2 fresh agent rows (low) + the preserved user row (high).
    rows = ledger.read_task_segments()
    agent = [r for r in rows if r.source == "agent"]
    user = [r for r in rows if r.source == "user"]
    assert [r.name for r in agent] == ["agent one", "agent two"]
    assert [r.task_index for r in agent] == [0, 1]
    assert len(user) == 1
    assert user[0].task_index == user_idx
    assert user[0].name == "my manual task"

    # tasks.json: agent entries (tagged source='agent') + the preserved user entry.
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    names = [t["name"] for t in persisted["tasks"]]
    assert names == ["agent one", "agent two", "my manual task"]
    assert persisted["tasks"][0]["source"] == "agent"
    assert persisted["tasks"][-1]["source"] == "user"
