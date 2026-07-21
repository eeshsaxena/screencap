"""U5 — the cross-day ``/v0/tasks.query`` daemon verb + ``tasks_query`` core.

``tasks.query`` answers "which named tasks exist in this date range" across
recordings: it iterates the recordings whose day-clamped coverage intersects the
requested ``[start_date, end_date]`` window and reads each recording's named task
segments via the SAME per-recording read path ``tasks.list`` uses
(``pipeline_task_segments`` ledger via ``read_task_segments_wire``). Each task is
grouped under its local calendar day per KTD-11's single day-mapping rule (the
inverse of ``day_segments.day_bounds`` — a task maps to the local day of its
``start_ts``). A per-recording honest-status rollup (the same
``produced_tasks`` / ``mechanical_only`` / ``nothing_to_name`` / ``couldnt_run`` /
``in_progress`` vocabulary ``tasks.list`` exposes) rides alongside so the UI can
render honest zero states (R21) instead of "you did nothing".

These tests pin:

* a range spanning multiple days returns day-grouped tasks (reverse-chronological
  days, tasks ordered within a day), each task carrying its ``recording`` +
  ``recording_id`` so the UI can seek/curate;
* an empty range (no recordings) returns empty ``days`` + empty ``recordings``;
  a range whose recordings produced NO tasks returns empty ``days`` but a
  non-empty honest-status rollup (the R21 zero-state signal);
* a sealed/absent vault store degrades to ``store_state`` on a 200 (never a 500);
* a malformed / inverted date range is a typed 400 (``invalid_request``);
* a midnight-spanning task lands under the local day of its start (KTD-11), and
  that mapping is consistent with ``day_segments.day_bounds``;
* the verb is read-only — NOT in ``_ACTIVITY_PATHS`` (a query never resets the
  idle-shutdown clock).

Privacy-marked + Vision-free (raw sqlite + the pipeline ledger, no OCR), mirroring
``test_timeline_day_tasks`` — CI runs only ``pytest -m privacy``.
"""

from __future__ import annotations

import calendar
import contextlib
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from screencap import day_segments, tasks_query
from screencap.daemon import schema
from screencap.daemon.app import build_app

pytestmark = pytest.mark.privacy

# Fixed UTC days (tz_offset_seconds=0) so day bounds are exact.
_DAY0 = "2026-07-03"
_DAY1 = "2026-07-04"
_DAY0_START = float(calendar.timegm((2026, 7, 3, 0, 0, 0, 0, 0, 0)))
_DAY_SECONDS = 86400


def _make_recording_db(rec_dir: Path, *, started: float, end: float) -> None:
    """Minimal recording dir catalog will list: a ``recording`` row (so the
    pipeline ledger resolves a ``recording_id``) plus one window/action event so
    the span resolves ``[started, end]``. Mirrors
    ``test_timeline_day_tasks._make_recording_db``."""
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
    """Write task rows into the recording's ``pipeline_task_segments`` ledger."""
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
                block_id=r.get("block_id"),
                thread_id=r.get("thread_id"),
            )
            for r in rows
        ]
    )


def _set_outcome(rec_dir: Path, reason: str, detail: str | None = None) -> None:
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    db_path = rec_dir / "recording.db"
    ensure_pipeline_state_schema(db_path)
    PipelineLedger(db_path).set_recording_outcome(reason, detail=detail)


def _rollup(result: dict) -> dict[str, dict]:
    return {r["name"]: r for r in result["recordings"]}


def _day(result: dict, date: str) -> dict | None:
    return next((d for d in result["days"] if d["date"] == date), None)


_TASK_FIELDS = {
    "recording", "recording_id", "task_index",
    "start_ts", "end_ts", "name", "category", "confidence",
}


# --- multi-day grouping -----------------------------------------------------


def test_range_spanning_days_returns_day_grouped_tasks(tmp_path):
    rec_a = tmp_path / "rec-a"
    _make_recording_db(rec_a, started=_DAY0_START + 3600, end=_DAY0_START + 7200)
    _seed_task_segments(
        rec_a,
        [
            {"task_index": 1, "start_ts": _DAY0_START + 5000, "end_ts": _DAY0_START + 7200,
             "name": "Email triage"},
            {"task_index": 0, "start_ts": _DAY0_START + 3600, "end_ts": _DAY0_START + 5000,
             "name": "Payroll run in Gusto", "category": "finance", "confidence": "high"},
        ],
    )
    _set_outcome(rec_a, "produced_tasks")

    rec_b = tmp_path / "rec-b"
    _make_recording_db(
        rec_b, started=_DAY0_START + _DAY_SECONDS + 3600, end=_DAY0_START + _DAY_SECONDS + 7200
    )
    _seed_task_segments(
        rec_b,
        [{"task_index": 0, "start_ts": _DAY0_START + _DAY_SECONDS + 3600,
          "end_ts": _DAY0_START + _DAY_SECONDS + 7200, "name": "Design review"}],
    )
    _set_outcome(rec_b, "mechanical_only")

    result = tasks_query.query_tasks(_DAY0, _DAY1, 0, recordings_dir=tmp_path)

    # Days are reverse-chronological (newest first).
    assert [d["date"] for d in result["days"]] == [_DAY1, _DAY0]

    day0 = _day(result, _DAY0)
    assert [t["name"] for t in day0["tasks"]] == ["Payroll run in Gusto", "Email triage"]
    first = day0["tasks"][0]
    assert set(first) == _TASK_FIELDS  # includes recording + recording_id
    assert first["recording"] == "rec-a"
    assert first["category"] == "finance"
    assert first["confidence"] == "high"

    day1 = _day(result, _DAY1)
    assert [t["name"] for t in day1["tasks"]] == ["Design review"]
    assert day1["tasks"][0]["recording"] == "rec-b"

    # Per-recording honest-status rollup carries the outcome vocabulary.
    rollup = _rollup(result)
    assert rollup["rec-a"]["reason"] == "produced_tasks"
    assert rollup["rec-b"]["reason"] == "mechanical_only"


# --- thread rollups (R7, U7): computed at read time, composite-keyed ---------


def test_thread_rollup_reported_on_members_and_absent_on_lone_block(tmp_path):
    """A thread of two blocks (same recording, same ``thread_id``) reports the
    rollup (total minutes, sitting 1-of-2 / 2-of-2) on BOTH rows; a lone block in
    the same day reports none (no rollup keys → serialized null downstream)."""
    rec = tmp_path / "rec-thread"
    _make_recording_db(rec, started=_DAY0_START + 3600, end=_DAY0_START + 9000)
    _seed_task_segments(
        rec,
        [
            {"task_index": 0, "start_ts": _DAY0_START + 3600, "end_ts": _DAY0_START + 5400,
             "name": "Kata drills", "block_id": "blk-a", "thread_id": "thr-kata"},
            {"task_index": 1, "start_ts": _DAY0_START + 7200, "end_ts": _DAY0_START + 9000,
             "name": "Kata drills", "block_id": "blk-b", "thread_id": "thr-kata"},
            # A lone, unrelated block on the same day — no thread.
            {"task_index": 2, "start_ts": _DAY0_START + 5400, "end_ts": _DAY0_START + 6000,
             "name": "Email", "block_id": "blk-c"},
        ],
    )

    result = tasks_query.query_tasks(_DAY0, _DAY0, 0, recordings_dir=tmp_path)
    by_block = {t["block_id"]: t for t in _day(result, _DAY0)["tasks"]}
    a, b, c = by_block["blk-a"], by_block["blk-b"], by_block["blk-c"]

    # Each thread member: total 60 min (30 + 30), count 2, chronological index.
    assert a["thread_total_minutes"] == 60.0 and a["thread_sitting_count"] == 2
    assert a["thread_sitting_index"] == 1
    assert b["thread_total_minutes"] == 60.0 and b["thread_sitting_count"] == 2
    assert b["thread_sitting_index"] == 2
    assert a["thread_id"] == b["thread_id"] == "thr-kata"

    # The lone block carries NO rollup (and no thread_id) — never "sitting 1 of 1".
    assert "thread_total_minutes" not in c
    assert "thread_sitting_count" not in c and "thread_sitting_index" not in c
    assert "thread_id" not in c


def test_same_thread_id_across_recordings_never_merges(tmp_path):
    """Cross-recording safety: the SAME ``thread_id`` string minted in two
    different recordings on the same day stays TWO threads — the rollup is keyed on
    the composite ``(recording, thread_id)``, so the tokens can never falsely
    merge (same-day cross-recording linking is deferred)."""
    shared = "thr-collision"  # the identical token string in both recordings
    rec_a = tmp_path / "rec-a"
    _make_recording_db(rec_a, started=_DAY0_START + 3600, end=_DAY0_START + 9000)
    _seed_task_segments(
        rec_a,
        [
            {"task_index": 0, "start_ts": _DAY0_START + 3600, "end_ts": _DAY0_START + 5400,
             "name": "A one", "block_id": "a1", "thread_id": shared},
            {"task_index": 1, "start_ts": _DAY0_START + 7200, "end_ts": _DAY0_START + 9000,
             "name": "A two", "block_id": "a2", "thread_id": shared},
        ],
    )
    rec_b = tmp_path / "rec-b"
    _make_recording_db(rec_b, started=_DAY0_START + 10000, end=_DAY0_START + 20000)
    _seed_task_segments(
        rec_b,
        [
            {"task_index": 0, "start_ts": _DAY0_START + 10000, "end_ts": _DAY0_START + 13600,
             "name": "B one", "block_id": "b1", "thread_id": shared},
            {"task_index": 1, "start_ts": _DAY0_START + 14000, "end_ts": _DAY0_START + 17600,
             "name": "B two", "block_id": "b2", "thread_id": shared},
        ],
    )

    result = tasks_query.query_tasks(_DAY0, _DAY0, 0, recordings_dir=tmp_path)
    by_block = {t["block_id"]: t for t in _day(result, _DAY0)["tasks"]}

    # rec-a's thread: count 2 (NOT 4 — the rec-b blocks did not join), total 60 min.
    assert by_block["a1"]["thread_sitting_count"] == 2
    assert by_block["a1"]["thread_total_minutes"] == 60.0
    # rec-b's thread: its OWN count 2, total 120 min (2 x 60).
    assert by_block["b1"]["thread_sitting_count"] == 2
    assert by_block["b1"]["thread_total_minutes"] == 120.0
    # Sitting index restarts per (recording, thread) — both threads own a "1 of 2".
    assert by_block["a1"]["thread_sitting_index"] == 1
    assert by_block["b1"]["thread_sitting_index"] == 1


@pytest.mark.asyncio
async def test_verb_carries_thread_rollup_through_typed_model(tmp_path, monkeypatch):
    """The rollup survives the typed ``TasksQueryResponse`` parity check — i.e. the
    fields are a real part of the wire model, not a stray dict key dropped by
    ``extra='ignore'``."""
    rec = tmp_path / "rec-a"
    _make_recording_db(rec, started=_DAY0_START + 3600, end=_DAY0_START + 9000)
    _seed_task_segments(
        rec,
        [
            {"task_index": 0, "start_ts": _DAY0_START + 3600, "end_ts": _DAY0_START + 5400,
             "name": "Kata", "block_id": "blk-a", "thread_id": "thr-k"},
            {"task_index": 1, "start_ts": _DAY0_START + 7200, "end_ts": _DAY0_START + 9000,
             "name": "Kata", "block_id": "blk-b", "thread_id": "thr-k"},
        ],
    )
    _set_outcome(rec, "produced_tasks")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    app = build_app()
    resp = await _asgi_post(
        app, "/v0/tasks.query", {"start_date": _DAY0, "end_date": _DAY0}
    )
    assert resp.status_code == 200, resp.text
    parsed = schema.TasksQueryResponse(**resp.json()).model_dump()
    by_block = {t["block_id"]: t for t in parsed["days"][0]["tasks"]}
    assert by_block["blk-a"]["thread_total_minutes"] == 60.0
    assert by_block["blk-a"]["thread_sitting_count"] == 2
    assert by_block["blk-a"]["thread_sitting_index"] == 1
    assert by_block["blk-a"]["thread_id"] == "thr-k"
    assert by_block["blk-b"]["thread_sitting_index"] == 2
    # Bullets/block_id/is_open mirror TaskSegment too (additive parity, U7).
    assert by_block["blk-a"]["is_open"] is False


# --- empty range + honest statuses ------------------------------------------


def test_empty_range_no_recordings_returns_empty_lists(tmp_path):
    _make_recording_db(
        tmp_path / "rec-a", started=_DAY0_START + 3600, end=_DAY0_START + 7200
    )
    # Query a window that no recording intersects.
    result = tasks_query.query_tasks("2020-01-01", "2020-01-02", 0, recordings_dir=tmp_path)
    assert result["days"] == []
    assert result["recordings"] == []


def test_recordings_without_tasks_return_empty_days_but_honest_rollup(tmp_path):
    """The R21 zero-state signal: a recording is on file for the range but named
    no tasks — empty ``days`` yet a rollup carrying WHY (``nothing_to_name``)."""
    rec = tmp_path / "rec-nada"
    _make_recording_db(rec, started=_DAY0_START + 3600, end=_DAY0_START + 4200)
    _set_outcome(rec, "nothing_to_name")

    result = tasks_query.query_tasks(_DAY0, _DAY0, 0, recordings_dir=tmp_path)
    assert result["days"] == []
    rollup = _rollup(result)
    assert set(rollup) == {"rec-nada"}
    assert rollup["rec-nada"]["reason"] == "nothing_to_name"


def test_legacy_recording_without_outcome_reports_null_reason(tmp_path):
    """A pre-U2 recording with no recorded outcome yields ``reason: None`` (the
    app derives the neutral "unknown"/"not set up" state), never a crash."""
    rec = tmp_path / "rec-legacy"
    _make_recording_db(rec, started=_DAY0_START + 100, end=_DAY0_START + 900)
    result = tasks_query.query_tasks(_DAY0, _DAY0, 0, recordings_dir=tmp_path)
    rollup = _rollup(result)
    assert rollup["rec-legacy"]["reason"] is None
    assert rollup["rec-legacy"]["detail"] is None


# --- KTD-11 midnight-spanning task ------------------------------------------


def test_midnight_spanning_task_lands_on_day_of_start(tmp_path):
    """KTD-11: a task starting 23:50 on day0 and ending 00:10 on day1 maps to
    day0 (the local calendar day of its ``start_ts``)."""
    rec = tmp_path / "rec-midnight"
    _make_recording_db(
        rec,
        started=_DAY0_START + _DAY_SECONDS - 1800,   # 23:30 day0
        end=_DAY0_START + _DAY_SECONDS + 1800,        # 00:30 day1
    )
    task_start = _DAY0_START + _DAY_SECONDS - 600      # 23:50 day0
    _seed_task_segments(
        rec,
        [{"task_index": 0, "start_ts": task_start,
          "end_ts": _DAY0_START + _DAY_SECONDS + 600,  # 00:10 day1
          "name": "Nightly deploy"}],
    )

    result = tasks_query.query_tasks(_DAY0, _DAY1, 0, recordings_dir=tmp_path)
    day0 = _day(result, _DAY0)
    day1 = _day(result, _DAY1)
    assert day0 is not None and [t["name"] for t in day0["tasks"]] == ["Nightly deploy"]
    assert day1 is None  # the task did NOT duplicate onto day1

    # The mapping is consistent with the single-sourced day rule (KTD-11): the
    # chosen day's bounds bracket the task's start_ts.
    lo, hi = day_segments.day_bounds(_DAY0, 0)
    assert lo <= task_start < hi


# --- invalid input rejected -------------------------------------------------


def test_malformed_date_raises_invalid_request(tmp_path):
    with pytest.raises(day_segments.InvalidDayRequest):
        tasks_query.query_tasks("not-a-date", _DAY1, 0, recordings_dir=tmp_path)


def test_inverted_range_raises_invalid_request(tmp_path):
    with pytest.raises(day_segments.InvalidDayRequest):
        tasks_query.query_tasks(_DAY1, _DAY0, 0, recordings_dir=tmp_path)


# --- verb wire: shape + store_state + validation ----------------------------


async def _asgi_post(app, path: str, body: dict):
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(path, json=body)


@pytest.mark.asyncio
async def test_verb_returns_grouped_tasks_and_validates(tmp_path, monkeypatch):
    rec = tmp_path / "rec-a"
    _make_recording_db(rec, started=_DAY0_START + 3600, end=_DAY0_START + 7200)
    _seed_task_segments(
        rec,
        [{"task_index": 0, "start_ts": _DAY0_START + 3600, "end_ts": _DAY0_START + 5000,
          "name": "Payroll run"}],
    )
    _set_outcome(rec, "produced_tasks")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))

    app = build_app()
    resp = await _asgi_post(
        app, "/v0/tasks.query", {"start_date": _DAY0, "end_date": _DAY0}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["store_state"] == "mounted"
    # Validates through the typed response model (verb parity check).
    parsed = schema.TasksQueryResponse(**body).model_dump()
    assert parsed["days"][0]["date"] == _DAY0
    assert parsed["days"][0]["tasks"][0]["name"] == "Payroll run"
    assert parsed["days"][0]["tasks"][0]["recording"] == "rec-a"
    assert parsed["recordings"][0]["reason"] == "produced_tasks"


@pytest.mark.asyncio
async def test_verb_sealed_store_degrades_to_store_state(tmp_path, monkeypatch):
    """A locked vault store is a healthy serving state (KTD-14): empty payload +
    ``store_state``, never a 500."""
    from screencap.daemon.store_lifecycle import StoreState

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    app = build_app()
    app.state.store_state = StoreState.LOCKED
    resp = await _asgi_post(
        app, "/v0/tasks.query", {"start_date": _DAY0, "end_date": _DAY1}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["store_state"] == "locked"
    assert body["days"] == []
    assert body["recordings"] == []


@pytest.mark.asyncio
async def test_verb_absent_store_degrades_to_store_state(tmp_path, monkeypatch):
    from screencap.daemon.store_lifecycle import StoreState

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    app = build_app()
    app.state.store_state = StoreState.ABSENT
    resp = await _asgi_post(
        app, "/v0/tasks.query", {"start_date": _DAY0, "end_date": _DAY1}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["store_state"] == "absent"
    assert body["days"] == []


@pytest.mark.asyncio
async def test_verb_malformed_date_is_typed_400(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    app = build_app()
    resp = await _asgi_post(
        app, "/v0/tasks.query", {"start_date": "2026-13-99", "end_date": _DAY1}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_verb_inverted_range_is_typed_400(tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    app = build_app()
    resp = await _asgi_post(
        app, "/v0/tasks.query", {"start_date": _DAY1, "end_date": _DAY0}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_verb_does_not_bump_idle_activity(tmp_path, monkeypatch):
    """tasks.query is read-only — NOT in ``_ACTIVITY_PATHS``, so a query must not
    reset the idle-shutdown clock."""
    from screencap.daemon import _idle_shutdown
    from screencap.daemon.app import build_app as _build

    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path))
    app = _build()
    _idle_shutdown.attach(app, idle_seconds=600.0)
    sentinel = 12345.0
    app.state.idle_last_activity = sentinel
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v0/tasks.query", json={"start_date": _DAY0, "end_date": _DAY1}
            )
    assert resp.status_code == 200
    assert app.state.idle_last_activity == sentinel
