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
import json
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


def test_recording_ending_exactly_at_midnight_belongs_only_to_previous_day(tmp_path):
    """A recording ending exactly at local midnight is entirely within the previous
    day — it must NOT appear as a zero-width span at the start of the next day
    (half-open `end > win_start`, not `>=`)."""
    rec = tmp_path / "ends-at-midnight"
    _make_recording_db(
        rec, started=_DAY_START - 600, end=_DAY_START,
        windows=[{"ts": _DAY_START - 600, "bundle": "com.example.unknownbenign"}],
    )
    assert _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "ends-at-midnight") is None
    prev = _find(day_segments.day_segments("2026-07-02", 0, recordings_dir=tmp_path), "ends-at-midnight")
    assert prev is not None
    assert prev["end_ms"] == int(_DAY_START * 1000)


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
    assert result == {"date": _DAY, "recordings": [], "store_mounted": True}


# --- end-status classifier (U1) --------------------------------------------
#
# Honesty rule: ambiguous evidence must land in `unknown`, never a confident
# cause. Absence of clean-stop artifacts alone (no start marker) is NEVER
# `interrupted`.


def _write_ready(rec_dir: Path, **overrides) -> None:
    """Write the `.recording_ready` sentinel session.py emits at clean stop."""
    payload = {
        "elapsed": 600.0,
        "completed_at": _DAY_START + 4200,
        "disk_full": False,
        "force_stopped": False,
        "terminated_reason": None,
    }
    payload.update(overrides)
    (rec_dir / ".recording_ready").write_text(json.dumps(payload))


def _write_start_metrics(rec_dir: Path, end=None) -> None:
    """Write the start-phase `system_metrics.json` (metrics.py writes `end: None`
    at recording START; the end phase fills it)."""
    (rec_dir / "system_metrics.json").write_text(
        json.dumps({"schema_version": 1, "static": {}, "start": {"ts": 1}, "end": end})
    )


def test_clean_stop_is_clean(tmp_path):
    rec = tmp_path / "clean-rec"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _write_start_metrics(rec, end={"ts": 2})
    _write_ready(rec)
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "clean-rec")
    assert entry is not None
    assert entry["end_status"] == "clean"


def test_start_marker_null_end_without_ready_is_interrupted(tmp_path):
    """system_metrics.json exists with `end: null` and no `.recording_ready` —
    the recorder started and never reached its end phase."""
    rec = tmp_path / "died-rec"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _write_start_metrics(rec, end=None)
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "died-rec")
    assert entry is not None
    assert entry["end_status"] == "interrupted"


def test_terminated_reason_in_ready_is_interrupted(tmp_path):
    rec = tmp_path / "diskfull-rec"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _write_start_metrics(rec, end={"ts": 2})
    _write_ready(rec, disk_full=True, terminated_reason="disk_full")
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "diskfull-rec")
    assert entry is not None
    assert entry["end_status"] == "interrupted"


def test_disk_full_alone_in_ready_is_interrupted(tmp_path):
    """session.py sets `disk_full` on its OWN detection path, independent of the
    engine's `terminated_reason` — a disk-full death must never classify clean."""
    rec = tmp_path / "diskfull-only"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _write_start_metrics(rec, end={"ts": 2})
    # disk_full=True with terminated_reason null and force_stopped absent.
    (rec / ".recording_ready").write_text(json.dumps({
        "elapsed": 600.0,
        "completed_at": _DAY_START + 4200,
        "disk_full": True,
        "terminated_reason": None,
    }))
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "diskfull-only")
    assert entry is not None
    assert entry["end_status"] == "interrupted"


def _write_stop_meta(rec_dir: Path, **payload) -> None:
    """Write the engine's `.recording_stop_meta.json` sidecar (no ready sentinel)."""
    (rec_dir / ".recording_stop_meta.json").write_text(json.dumps(payload))


def test_stop_meta_terminated_reason_without_ready_is_interrupted(tmp_path):
    """Stop-meta-only path: `.recording_stop_meta.json` records an abnormal
    termination and no `.recording_ready` exists — still `interrupted` (and for
    the right reason: the start marker is complete, so it isn't the null-end path)."""
    rec = tmp_path / "stopmeta-sigterm"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _write_start_metrics(rec, end={"ts": 2})
    _write_stop_meta(rec, terminated_reason="sigterm")
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "stopmeta-sigterm")
    assert entry is not None
    assert entry["end_status"] == "interrupted"


def test_stop_meta_force_stopped_without_ready_is_interrupted(tmp_path):
    rec = tmp_path / "stopmeta-forced"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _write_start_metrics(rec, end={"ts": 2})
    _write_stop_meta(rec, terminated_reason=None, force_stopped=True)
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "stopmeta-forced")
    assert entry is not None
    assert entry["end_status"] == "interrupted"


def test_live_recording_is_live_never_interrupted(tmp_path, monkeypatch):
    """A live recording has the start-phase marker with `end: null` and no ready
    sentinel — exactly the interrupted signature — but `state == "recording"`
    must win and classify it `live`."""
    rec = tmp_path / "live-rec"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _write_start_metrics(rec, end=None)
    monkeypatch.setattr("screencap.catalog._active_recording_name", lambda: "live-rec")
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "live-rec")
    assert entry is not None
    assert entry["state"] == "recording"
    assert entry["end_status"] == "live"


def test_no_start_marker_is_unknown(tmp_path):
    """No start-phase marker at all: absence of clean-stop artifacts alone must
    NEVER classify interrupted — fail-closed to `unknown`."""
    rec = tmp_path / "bare-rec"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "bare-rec")
    assert entry is not None
    assert entry["end_status"] == "unknown"


def test_corrupt_metrics_is_unknown(tmp_path):
    rec = tmp_path / "corrupt-metrics"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    (rec / "system_metrics.json").write_bytes(b"\x00not json{{{")
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "corrupt-metrics")
    assert entry is not None
    assert entry["end_status"] == "unknown"


def test_corrupt_stop_meta_is_unknown(tmp_path):
    rec = tmp_path / "corrupt-stopmeta"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _write_start_metrics(rec, end={"ts": 2})
    (rec / ".recording_stop_meta.json").write_text("{truncated")
    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "corrupt-stopmeta")
    assert entry is not None
    assert entry["end_status"] == "unknown"


def test_previous_day_death_does_not_reach_this_day(tmp_path):
    """Day-bounded: a recording that died wholly within the previous day never
    appears in (and so never marks) this day."""
    rec = tmp_path / "died-yesterday"
    _make_recording_db(
        rec, started=_DAY_START - 7200, end=_DAY_START - 3600,
        windows=[{"ts": _DAY_START - 7200, "bundle": "com.example.unknownbenign"}],
    )
    _write_start_metrics(rec, end=None)  # interrupted signature
    assert _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "died-yesterday") is None
    prev = _find(day_segments.day_segments("2026-07-02", 0, recordings_dir=tmp_path), "died-yesterday")
    assert prev is not None
    assert prev["end_status"] == "interrupted"


def test_midnight_clamp_is_not_an_interruption_boundary(tmp_path):
    """An overnight recording clamped at 00:00 keeps its artifact-derived status
    in BOTH days — the clamp itself never manufactures an interruption."""
    rec = tmp_path / "overnight-clean"
    started = _DAY_START - 1800
    end = _DAY_START + 1800
    _make_recording_db(
        rec, started=started, end=end,
        windows=[{"ts": started, "bundle": "com.example.unknownbenign"}],
    )
    _write_start_metrics(rec, end={"ts": 2})
    _write_ready(rec)
    today = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "overnight-clean")
    assert today is not None
    assert today["start_ms"] == int(_DAY_START * 1000)  # clamped at midnight...
    assert today["end_status"] == "clean"               # ...but not "interrupted"
    prev = _find(day_segments.day_segments("2026-07-02", 0, recordings_dir=tmp_path), "overnight-clean")
    assert prev is not None
    assert prev["end_status"] == "clean"


# --- store gate (U1) --------------------------------------------------------


def test_mounted_store_flag_true_by_default(tmp_path):
    result = day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path)
    assert result["store_mounted"] is True


def test_missing_recordings_dir_signals_store_not_mounted(tmp_path):
    result = day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path / "absent")
    assert result["store_mounted"] is False
    assert result["recordings"] == []


def test_locked_store_signalled_and_yields_no_recordings(tmp_path):
    """When the caller (the daemon handler, which owns store state) says the
    store is not mounted, the result must say so and report NO recordings —
    gaps resolve to "can't verify", never a confident cause."""
    rec = tmp_path / "leftover"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4200,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    result = day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path, store_mounted=False)
    assert result["store_mounted"] is False
    assert result["recordings"] == []


# --- purge split (U1 / SCR-277) ---------------------------------------------
#
# RETROACTIVE_PURGE spans are ground truth from the purge, not proven
# capture-time masking — they must land in `purged`, never `blocked_proven`.


def _add_purged_interval(rec_dir: Path, start: float, end: float | None,
                         disabled_at: float | str | None) -> None:
    with contextlib.closing(sqlite3.connect(str(rec_dir / "recording.db"))) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS purged_interval ("
            "  id INTEGER PRIMARY KEY, start_ts REAL NOT NULL,"
            "  end_ts REAL, disabled_at REAL)"
        )
        conn.execute(
            "INSERT INTO purged_interval (start_ts, end_ts, disabled_at) VALUES (?, ?, ?)",
            (start, end, disabled_at),
        )
        conn.commit()


def _write_disable_log(rec_dir: Path, entries: list[dict]) -> None:
    lines = [json.dumps({"_meta": True, "format_version": 1, "screencap_version": "0"})]
    lines += [json.dumps(e) for e in entries]
    (rec_dir / ".menubar_disable_log.jsonl").write_text("\n".join(lines) + "\n")


def _disable_entry(ts_unix: float, bundle_id="com.spotify.client",
                   app_name="Spotify", root_domain=None) -> dict:
    return {
        "ts_unix": ts_unix,
        "target_kind": "app",
        "target": {"bundle_id": bundle_id, "app_name": app_name,
                   "root_domain": root_domain},
        "source": "menubar",
        "scrub_status": "ok",
    }


def test_purge_lands_in_purged_with_identity_never_blocked_proven(tmp_path):
    rec = tmp_path / "purged-rec"
    disabled_at = _DAY_START + 5000.0
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4800,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _add_purged_interval(rec, _DAY_START + 3700, _DAY_START + 3900, disabled_at)
    _write_disable_log(rec, [_disable_entry(disabled_at)])

    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "purged-rec")
    assert entry is not None
    assert entry["blocked_proven"] == [], "a purge span must NOT read as proven masking"
    assert len(entry["purged"]) == 1
    p = entry["purged"][0]
    assert p["start_ms"] == int((_DAY_START + 3700) * 1000)
    assert p["end_ms"] == int((_DAY_START + 3900) * 1000)
    assert p["bundle_id"] == "com.spotify.client"
    assert p["app_name"] == "Spotify"
    assert "root_domain" not in p  # None target fields are omitted


def test_purge_with_null_disabled_at_is_identity_free(tmp_path):
    rec = tmp_path / "purged-null"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4800,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _add_purged_interval(rec, _DAY_START + 3700, _DAY_START + 3900, None)
    _write_disable_log(rec, [_disable_entry(_DAY_START + 5000.0)])

    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "purged-null")
    assert entry is not None
    assert len(entry["purged"]) == 1
    p = entry["purged"][0]
    assert "bundle_id" not in p and "app_name" not in p and "root_domain" not in p


def test_two_log_candidates_degrade_to_identity_free(tmp_path):
    """Two disable-log lines share `ts_unix`: the join is ambiguous — never
    guess an identity."""
    rec = tmp_path / "purged-ambig"
    disabled_at = _DAY_START + 5000.0
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4800,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _add_purged_interval(rec, _DAY_START + 3700, _DAY_START + 3900, disabled_at)
    _write_disable_log(rec, [
        _disable_entry(disabled_at),
        _disable_entry(disabled_at, bundle_id="com.other.app", app_name="Other"),
    ])

    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "purged-ambig")
    assert entry is not None
    assert len(entry["purged"]) == 1
    p = entry["purged"][0]
    assert "bundle_id" not in p and "app_name" not in p


def test_unreadable_disable_log_yields_identity_free_purges(tmp_path):
    """A wholly unreadable log (invalid bytes) degrades to identity-free purge
    spans — never an exception, never a dropped span."""
    rec = tmp_path / "purged-badlog"
    disabled_at = _DAY_START + 5000.0
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4800,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _add_purged_interval(rec, _DAY_START + 3700, _DAY_START + 3900, disabled_at)
    (rec / ".menubar_disable_log.jsonl").write_bytes(b"\xff\xfe\x00garbage")

    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "purged-badlog")
    assert entry is not None
    assert len(entry["purged"]) == 1
    p = entry["purged"][0]
    assert "bundle_id" not in p and "app_name" not in p


def test_text_disabled_at_yields_identity_free_purge_never_500(tmp_path):
    """SQLite REAL affinity can store TEXT in `disabled_at`; a corrupt value must
    degrade the span to identity-free — never raise out of the read surface."""
    rec = tmp_path / "purged-text-ts"
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4800,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _add_purged_interval(rec, _DAY_START + 3700, _DAY_START + 3900, "corrupt")
    _write_disable_log(rec, [_disable_entry(_DAY_START + 5000.0)])

    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "purged-text-ts")
    assert entry is not None
    assert len(entry["purged"]) == 1
    p = entry["purged"][0]
    assert "bundle_id" not in p and "app_name" not in p and "root_domain" not in p


def test_non_string_identity_values_are_dropped(tmp_path):
    """A valid log line whose target carries a non-string value keeps only the
    string fields — a non-str identity value must never reach the wire payload."""
    rec = tmp_path / "purged-nonstr"
    disabled_at = _DAY_START + 5000.0
    _make_recording_db(
        rec, started=_DAY_START + 3600, end=_DAY_START + 4800,
        windows=[{"ts": _DAY_START + 3600, "bundle": "com.example.unknownbenign"}],
    )
    _add_purged_interval(rec, _DAY_START + 3700, _DAY_START + 3900, disabled_at)
    _write_disable_log(rec, [_disable_entry(disabled_at, bundle_id="com.x", app_name=42)])

    entry = _find(day_segments.day_segments(_DAY, 0, recordings_dir=tmp_path), "purged-nonstr")
    assert entry is not None
    assert len(entry["purged"]) == 1
    p = entry["purged"][0]
    assert p["bundle_id"] == "com.x"
    assert "app_name" not in p


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
