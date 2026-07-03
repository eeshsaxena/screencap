"""U3 — the day-scoped Day-timeline read surface (``day_segments`` + the
``/v0/timeline.day`` verb).

Privacy-load-bearing: it draws the proven/unverifiable line the UI hatches
"blocked" only on the PROVEN side (R7). CI runs only ``pytest -m privacy`` (no
general lane), so the module is privacy-marked so the split actually runs as a CI
guard. Vision-free (real classifier + raw sqlite, no OCR), so it runs on both CI
privacy lanes.
"""

from __future__ import annotations

import calendar
import contextlib
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from screencap import day_segments
from screencap.daemon.app import build_app

pytestmark = pytest.mark.privacy

# A fixed UTC day (tz_offset_seconds=0) so day bounds are exact.
_DAY = "2026-07-03"
_DAY_START = float(calendar.timegm((2026, 7, 3, 0, 0, 0, 0, 0, 0)))
_DAY_END = _DAY_START + 86400


def _make_recording_db(
    rec_dir: Path,
    *,
    started: float,
    end: float,
    windows: list[dict],
    screenshots: list[float] | None = None,
) -> None:
    """Build a minimal recording dir (recording + window_event + action_event),
    plus optional flat ``screenshots/*.jpg`` files (for the coverage-gap pass).

    ``windows`` items: ``{ts, bundle, title}``. An ``action_event`` at ``end``
    gives the recording its duration (so ``_read_recording_meta`` computes the
    span).
    """
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
        for i, w in enumerate(windows, start=1):
            conn.execute(
                "INSERT INTO window_event (id, recording_id, timestamp, app_bundle_id, "
                "window_id, title, state, app_name, browser_url) "
                "VALUES (?, 1, ?, ?, ?, ?, NULL, ?, ?)",
                (i, w["ts"], w.get("bundle"), f"w{i}", w.get("title"),
                 w.get("app_name"), w.get("url")),
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

    if screenshots:
        shots = rec_dir / "screenshots"
        shots.mkdir(exist_ok=True)
        for ts in screenshots:
            (shots / f"{ts}.jpg").write_bytes(b"\xff\xd8\xff")  # tiny jpeg header


def _find(result: dict, name: str) -> dict | None:
    return next((r for r in result["recordings"] if r["name"] == name), None)


# --- day_bounds ------------------------------------------------------------


def test_day_bounds_utc():
    start, end = day_segments.day_bounds(_DAY, 0)
    assert start == _DAY_START
    assert end == _DAY_END


def test_day_bounds_applies_east_of_utc_offset():
    # UTC+1 (3600s east): local midnight is one hour BEFORE UTC midnight.
    start, _end = day_segments.day_bounds(_DAY, 3600)
    assert start == _DAY_START - 3600


def test_malformed_date_raises_invalid_day_request():
    with pytest.raises(day_segments.InvalidDayRequest):
        day_segments.day_bounds("not-a-date", 0)
    with pytest.raises(day_segments.InvalidDayRequest):
        day_segments.day_segments("2026-13-99", 0, recordings_dir=Path("/nonexistent"))


# --- proven vs unverifiable split -----------------------------------------


def test_masked_app_returns_blocked_proven_clipped_to_day(tmp_path):
    """A 1Password window (EXCLUDE) yields a `blocked_proven` interval, and every
    proven interval is clipped inside the day window."""
    rec = tmp_path / "vault-work"
    _make_recording_db(
        rec,
        started=_DAY_START + 3600,
        end=_DAY_START + 4200,
        windows=[
            {"ts": _DAY_START + 3600, "bundle": "com.1password.1password", "title": "Vault"},
            {"ts": _DAY_START + 4000, "bundle": "com.example.unknownbenign", "title": "Notes"},
        ],
    )
    result = day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path)
    entry = _find(result, "vault-work")
    assert entry is not None
    assert entry["blocked_proven"], "the 1Password span must be proven-blocked"
    assert entry["unverifiable"] == []
    day_start_ms, day_end_ms = int(_DAY_START * 1000), int(_DAY_END * 1000)
    for iv in entry["blocked_proven"]:
        assert day_start_ms <= iv["start_ms"] < iv["end_ms"] <= day_end_ms


def test_coverage_gap_is_unverifiable_never_proven(tmp_path):
    """A screenshot before the earliest surviving window (a deleted-row coverage
    gap) is reported `unverifiable`, never `blocked_proven`."""
    rec = tmp_path / "gap-rec"
    # Earliest surviving window at +300; a screenshot at +100 precedes it.
    _make_recording_db(
        rec,
        started=_DAY_START + 3600,
        end=_DAY_START + 4200,
        windows=[
            {"ts": _DAY_START + 3900, "bundle": "com.example.unknownbenign", "title": "Notes"},
        ],
        screenshots=[_DAY_START + 3700],
    )
    result = day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path)
    entry = _find(result, "gap-rec")
    assert entry is not None
    assert entry["unverifiable"], "the uncovered gap must be reported unverifiable"
    assert entry["blocked_proven"] == [], "a coverage gap is never proven-blocked"


# --- spans / clamping ------------------------------------------------------


def test_midnight_spanning_recording_appears_in_both_days_clamped(tmp_path):
    """A recording that crosses midnight appears in BOTH days, clamped to each."""
    rec = tmp_path / "overnight"
    started = _DAY_START - 1800   # 23:30 on the previous day
    end = _DAY_START + 1800       # 00:30 on _DAY
    _make_recording_db(
        rec, started=started, end=end,
        windows=[{"ts": started, "bundle": "com.example.unknownbenign", "title": "Notes"}],
    )

    today = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "overnight")
    assert today is not None
    assert today["start_ms"] == int(_DAY_START * 1000)          # clamped to midnight
    assert today["end_ms"] == int(end * 1000)

    prev = _find(day_segments.day_segments("2026-07-02", 0, recordings_dir=tmp_path), "overnight")
    assert prev is not None
    assert prev["start_ms"] == int(started * 1000)
    assert prev["end_ms"] == int(_DAY_START * 1000)             # clamped to midnight


def test_recording_outside_the_day_is_excluded(tmp_path):
    rec = tmp_path / "other-day"
    _make_recording_db(
        rec, started=_DAY_START - 100_000, end=_DAY_START - 90_000,
        windows=[{"ts": _DAY_START - 100_000, "bundle": "com.example.unknownbenign"}],
    )
    result = day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path)
    assert _find(result, "other-day") is None


def test_empty_day_returns_no_recordings(tmp_path):
    result = day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path)
    assert result == {"date": _DAY, "recordings": []}


# --- daemon verb -----------------------------------------------------------


@pytest.mark.asyncio
async def test_verb_malformed_date_returns_400():
    app = build_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/v0/timeline.day", json={"date": "not-a-date"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_verb_returns_day_surface(monkeypatch):
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
            resp = await client.post("/v0/timeline.day", json={"date": _DAY, "tz_offset_seconds": 0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["date"] == _DAY
    assert body["recordings"][0]["name"] == "r"


def test_verb_not_in_activity_paths():
    """A read verb must not reset the idle-shutdown clock (KTD / SCR idle rule)."""
    from screencap.daemon._idle_shutdown import _ACTIVITY_PATHS

    assert "/v0/timeline.day" not in _ACTIVITY_PATHS
