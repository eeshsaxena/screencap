"""U9 — the day-level ``tasks`` band on ``timeline.day`` / ``day_segments``.

Additive: each ``DaySegmentRecording`` now carries its recording's named task
segments (``tasks``) so the Day-timeline gets every band for the day in one
round-trip — no per-recording ``tasks.list`` call. These tests pin:

* a recording WITH task rows returns them nested under ``tasks``, ordered by
  ``task_index``, in the shared 6-field ``TaskSegment`` wire shape (``source`` /
  ``edited`` never exposed);
* a recording with NO tasks (or a legacy / missing / unreadable ``recording.db``)
  returns ``tasks: []`` — a fail-open read surface, never an error;
* the pre-U9 ``DaySegmentRecording`` fields are unchanged (additivity);
* the ``timeline.day`` verb API version const was bumped;
* (U2, SCR-277 provenance) the additive ``end_status`` / ``purged`` fields and
  the top-level ``store_mounted`` survive the daemon boundary — the
  ``DaySegmentRecording`` round-trip is ``extra="ignore"``, so a key missing
  from the model is SILENTLY dropped from the wire.

Privacy-marked (mirrors ``test_day_segments``): CI runs only ``pytest -m
privacy``, and this exercises the same local-only day read surface. Vision-free
(raw sqlite + the pipeline ledger, no OCR), so it runs on both CI privacy lanes.
"""

from __future__ import annotations

import calendar
import contextlib
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from screencap import day_segments
from screencap.daemon import schema
from screencap.daemon.app import build_app

pytestmark = pytest.mark.privacy

# A fixed UTC day (tz_offset_seconds=0) so day bounds are exact.
_DAY = "2026-07-03"
_DAY_START = float(calendar.timegm((2026, 7, 3, 0, 0, 0, 0, 0, 0)))


def _make_recording_db(rec_dir: Path, *, started: float, end: float) -> None:
    """Minimal recording dir catalog will list: a ``recording`` row (so the
    pipeline ledger resolves a ``recording_id``) + one window/action event for
    the span. Mirrors ``test_day_segments._make_recording_db`` (trimmed)."""
    rec_dir.mkdir(parents=True, exist_ok=True)
    db = rec_dir / "recording.db"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, ?, 2.0)", (started,))
        conn.execute(
            """CREATE TABLE window_event (
                id INTEGER PRIMARY KEY, recording_id INTEGER, timestamp REAL,
                app_bundle_id TEXT, window_id TEXT, title TEXT, state TEXT,
                app_name TEXT, browser_url TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO window_event (id, recording_id, timestamp, app_bundle_id, "
            "window_id, title, state, app_name, browser_url) "
            "VALUES (1, 1, ?, 'com.example.unknownbenign', 'w1', 'Notes', NULL, NULL, NULL)",
            (started,),
        )
        conn.execute(
            """CREATE TABLE action_event (
                id INTEGER PRIMARY KEY, recording_id INTEGER, name TEXT,
                timestamp REAL, key_char TEXT, element_state TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO action_event (id, recording_id, name, timestamp) VALUES (1, 1, 'click', ?)",
            (end,),
        )
        conn.commit()


def _seed_task_segments(rec_dir: Path, rows: list[dict]) -> None:
    """Write task rows into the recording's ``pipeline_task_segments`` ledger.

    Each ``rows`` item carries an explicit ``task_index`` so a test can insert
    out of order and assert the read is sorted. ``source`` / ``edited`` default
    to the agent-unedited state (verifying they never surface on the wire).
    """
    from screencap.pipeline_state import (
        PipelineLedger,
        TaskSegmentRow,
        ensure_pipeline_state_schema,
    )

    db_path = rec_dir / "recording.db"
    ensure_pipeline_state_schema(db_path)
    PipelineLedger(db_path).replace_task_segments(
        [
            TaskSegmentRow(
                task_index=r["task_index"],
                start_ts=float(r["start_ts"]),
                end_ts=float(r["end_ts"]),
                name=r["name"],
                category=r.get("category"),
                confidence=r.get("confidence"),
            )
            for r in rows
        ]
    )


def _find(result: dict, name: str) -> dict | None:
    return next((r for r in result["recordings"] if r["name"] == name), None)


_TASK_FIELDS = {"task_index", "start_ts", "end_ts", "name", "category", "confidence"}
# The typed ``TaskSegment`` model additionally carries the day-diary fields (U1),
# which ``model_dump`` always emits with empty/None defaults even when the raw
# wire dict omitted them (a non-diary row). Old clients ignore the extras.
_TASK_MODEL_FIELDS = _TASK_FIELDS | {"bullets", "block_id", "thread_id", "is_open"}


# --- tasks populated + ordered ---------------------------------------------


def test_recording_with_tasks_returns_them_nested_ordered(tmp_path):
    """A day-recording with task rows returns them under ``tasks``, ordered by
    ``task_index`` (inserted out of order), in the 6-field wire shape only."""
    rec = tmp_path / "ambient-20260703"
    _make_recording_db(rec, started=_DAY_START + 3600, end=_DAY_START + 7200)
    _seed_task_segments(
        rec,
        [
            # Deliberately inserted index 1 BEFORE index 0 — the read must sort.
            {"task_index": 1, "start_ts": _DAY_START + 5000, "end_ts": _DAY_START + 7200,
             "name": "Email triage"},
            {"task_index": 0, "start_ts": _DAY_START + 3600, "end_ts": _DAY_START + 5000,
             "name": "Payroll run in Gusto", "category": "finance", "confidence": "high"},
        ],
    )

    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "ambient-20260703")
    assert entry is not None
    tasks = entry["tasks"]
    assert [t["task_index"] for t in tasks] == [0, 1], "ordered by task_index"

    first = tasks[0]
    assert set(first) == _TASK_FIELDS, "only the 6-field TaskSegment shape (no source/edited)"
    assert first["name"] == "Payroll run in Gusto"
    assert first["category"] == "finance"
    assert first["confidence"] == "high"
    assert first["start_ts"] == _DAY_START + 3600
    # A heuristic-style task carries no category/confidence — null, not absent.
    assert tasks[1]["name"] == "Email triage"
    assert tasks[1]["category"] is None
    assert tasks[1]["confidence"] is None


def test_tasks_flow_through_the_typed_response_model(tmp_path):
    """The day surface validates through ``DaySegmentRecording`` (the verb's
    parity check) with ``tasks`` populated — i.e. the field is a real part of the
    wire model, coerced + dumped, not just a stray dict key."""
    rec = tmp_path / "ambient-20260703"
    _make_recording_db(rec, started=_DAY_START + 100, end=_DAY_START + 900)
    _seed_task_segments(
        rec,
        [{"task_index": 0, "start_ts": _DAY_START + 100, "end_ts": _DAY_START + 900,
          "name": "Design review"}],
    )
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "ambient-20260703")
    dumped = schema.DaySegmentRecording(**entry).model_dump()
    assert dumped["tasks"][0]["name"] == "Design review"
    assert set(dumped["tasks"][0]) == _TASK_MODEL_FIELDS


# --- empty / legacy cases (fail-open) --------------------------------------


def test_recording_without_tasks_returns_empty_tasks_list(tmp_path):
    """A recording whose segmentation produced no tasks returns ``tasks: []`` —
    empty, never absent, never an error."""
    rec = tmp_path / "ambient-20260703"
    _make_recording_db(rec, started=_DAY_START + 3600, end=_DAY_START + 4200)
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "ambient-20260703")
    assert entry is not None
    assert entry["tasks"] == []


def test_read_task_segments_fail_open_on_missing_or_unreadable_db(tmp_path):
    """The task read is fail-open: a missing ``recording.db`` (legacy dir) and a
    DB with no ``recording`` row both yield ``[]`` rather than raising."""
    # No recording.db at all.
    assert day_segments._read_task_segments(tmp_path / "nope") == []

    # A recording.db that exists but has no ``recording`` row (ledger can't
    # resolve a recording_id) → [] via the caught LedgerError, never a 500.
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    db = legacy / "recording.db"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL)")
        conn.commit()
    assert day_segments._read_task_segments(legacy) == []


# --- additivity + version --------------------------------------------------


def test_existing_day_segment_fields_unchanged_additive(tmp_path):
    """The pre-existing fields are all still present and unchanged; ``tasks``
    (U9), ``end_status`` / ``purged`` (SCR-277 provenance), and ``deleted`` (U8
    "removed by you") are the only new keys (additivity regression guard — closed
    set)."""
    rec = tmp_path / "ambient-20260703"
    _make_recording_db(rec, started=_DAY_START + 3600, end=_DAY_START + 4200)
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "ambient-20260703")
    assert set(entry) == {
        "name", "recording_id", "state", "start_ms", "end_ms",
        "blocked_proven", "unverifiable", "tasks", "end_status", "purged",
        "deleted",
    }
    # The original honesty split + span keys keep their meaning/types.
    assert entry["name"] == "ambient-20260703"
    assert entry["start_ms"] == int(round((_DAY_START + 3600) * 1000))
    assert entry["end_ms"] == int(round((_DAY_START + 4200) * 1000))
    assert entry["blocked_proven"] == []
    assert entry["unverifiable"] == []


def test_timeline_day_api_version_bumped():
    """The additive ``tasks`` field (v2) and the SCR-277 provenance fields
    (``end_status`` / ``purged`` / ``store_mounted``, v3) each bumped the
    ``timeline.day`` verb const."""
    assert schema._TIMELINE_DAY_API_VERSION == 3


# --- U2: provenance fields survive the daemon boundary ----------------------


@pytest.mark.asyncio
async def test_provenance_fields_survive_the_verb_wire(monkeypatch):
    """A day_segments payload carrying ``end_status`` + purged-with-identity +
    ``store_mounted`` reaches the HTTP response intact — guards the
    ``extra="ignore"`` silent drop at the ``DaySegmentRecording`` round-trip and
    the explicit envelope threading of ``store_mounted``."""
    canned = {
        "date": _DAY,
        "store_mounted": False,  # non-default, proves it's threaded not defaulted
        "coverage_complete": False,  # non-default, proves it's threaded too
        "recordings": [
            {"name": "r", "recording_id": None, "state": "ready",
             "end_status": "interrupted",
             "start_ms": 1, "end_ms": 2,
             "blocked_proven": [], "unverifiable": [],
             "purged": [{"start_ms": 5, "end_ms": 9,
                         "bundle_id": "com.example.app", "app_name": "Example"}],
             "tasks": []},
        ],
    }
    monkeypatch.setattr(day_segments, "day_segments", lambda *a, **k: canned)
    app = build_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v0/timeline.day", json={"date": _DAY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["store_mounted"] is False
    assert body["coverage_complete"] is False
    rec = body["recordings"][0]
    assert rec["end_status"] == "interrupted"
    p = rec["purged"][0]
    assert (p["start_ms"], p["end_ms"]) == (5, 9)
    assert p["bundle_id"] == "com.example.app"
    assert p["app_name"] == "Example"
    # An identity key day_segments omitted dumps as null — never a guessed value.
    assert p["root_domain"] is None


@pytest.mark.asyncio
async def test_verb_serves_recordings_without_the_new_keys(monkeypatch):
    """Backward shape: a payload WITHOUT the new keys (pre-U1 producer) still
    validates and serves — the fields are additive with tolerant defaults, and
    an absent ``store_mounted`` / ``coverage_complete`` defaults to True on the
    envelope."""
    canned = {"date": _DAY, "recordings": [
        {"name": "r", "recording_id": None, "state": "ready",
         "start_ms": 1, "end_ms": 2, "blocked_proven": [], "unverifiable": []},
    ]}
    monkeypatch.setattr(day_segments, "day_segments", lambda *a, **k: canned)
    app = build_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v0/timeline.day", json={"date": _DAY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["store_mounted"] is True
    assert body["coverage_complete"] is True
    rec = body["recordings"][0]
    assert rec["end_status"] is None
    assert rec["purged"] == []


@pytest.mark.asyncio
async def test_verb_threads_store_state_into_day_segments(monkeypatch):
    """The handler resolves the daemon's real store state (KTD-14 posture) and
    passes ``store_mounted`` into ``day_segments`` — a locked store must reach
    the day surface as ``store_mounted: False``, never a confident empty day."""
    from screencap.daemon.store_lifecycle import StoreState

    seen: dict = {}

    def fake(date, tz_offset_seconds=0, recordings_dir=None, store_mounted=True):
        seen["store_mounted"] = store_mounted
        return {"date": date, "store_mounted": store_mounted, "recordings": []}

    monkeypatch.setattr(day_segments, "day_segments", fake)
    app = build_app()
    app.state.store_state = StoreState.LOCKED
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v0/timeline.day", json={"date": _DAY})
    assert resp.status_code == 200, resp.text
    assert seen["store_mounted"] is False
    assert resp.json()["store_mounted"] is False
