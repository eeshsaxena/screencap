"""U2 (honest status): per-recording segmentation outcome storage + wiring.

Covers the ledger set/get/upsert + missing-table tolerance, and the
``_run_local_segmentation`` branch → reason wiring (produced / mechanical /
nothing / couldn't-run / in-progress) plus monotonic no-downgrade.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import screencap.pipeline_state as ps
import screencap.segmentation.consent as consent_mod
import screencap.segmentation.degrade as degrade_mod
import screencap.terminal_stage as ts
from screencap.segmentation.degrade import DegradeAction


def _make_recording_db(db_path: Path) -> None:
    from screencap.engine.db import create_db, crud

    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(
        session,
        {
            "timestamp": 1000.0,
            "monitor_width": 1920,
            "monitor_height": 1080,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5,
            "platform": "darwin",
            "task_description": "outcome-fixture",
        },
    )
    session.close()
    engine.dispose()


@pytest.fixture
def ledger(tmp_path: Path) -> ps.PipelineLedger:
    p = tmp_path / "recording.db"
    _make_recording_db(p)
    ps.ensure_pipeline_state_schema(p)
    return ps.PipelineLedger(p)


# --- storage ---------------------------------------------------------------

def test_outcome_absent_is_none(ledger):
    assert ledger.get_recording_outcome() is None


def test_set_then_get(ledger):
    ledger.set_recording_outcome("produced_tasks")
    assert ledger.get_recording_outcome() == "produced_tasks"


def test_upsert_overwrites(ledger):
    ledger.set_recording_outcome("in_progress")
    ledger.set_recording_outcome("produced_tasks")
    assert ledger.get_recording_outcome() == "produced_tasks"


def test_get_tolerates_missing_table(tmp_path):
    # A pre-U2 recording.db without the outcome table → None, never an error.
    p = tmp_path / "recording.db"
    _make_recording_db(p)  # NOTE: no ensure_pipeline_state_schema → table absent
    assert ps.PipelineLedger(p).get_recording_outcome() is None


# --- wiring ----------------------------------------------------------------

class _Dec:
    def __init__(self, action, tasks=None):
        self.action = action
        self.tasks = tasks


def _drive(ledger, monkeypatch, tmp_path, *, decision, cloud=None, heuristic=None,
           segment_exc=False, is_live=False, provider_result=None,
           reason=None) -> str | None:
    rec = tmp_path / "rec"
    rec.mkdir(exist_ok=True)

    if provider_result is None:
        provider_result = {"p": 1}
    if segment_exc:
        def _raise(_d, **_k):
            raise RuntimeError("boom")
        monkeypatch.setattr(ts, "_segment_local_tasks", _raise)
    else:
        monkeypatch.setattr(
            ts, "_segment_local_tasks",
            lambda d, **k: ("summary", provider_result, reason),
        )

    monkeypatch.setattr(consent_mod.ConsentPolicy, "from_config", lambda *a, **k: None)
    monkeypatch.setattr(degrade_mod, "resolve_day_split", lambda *a, **k: decision)
    monkeypatch.setattr(ts, "_summary_cloud_fallback", lambda s, **k: cloud)
    monkeypatch.setattr(ts, "_heuristic_local_tasks", lambda d: heuristic)

    ts._run_local_segmentation(
        rec, ledger, ts.TerminalResult(destination="local"), is_live=is_live
    )
    return ledger.get_recording_outcome()


def _tasks(name):
    return {"tasks": [{"start_ts": 0.0, "end_ts": 1.0, "name": name}]}


def _mechanical(name="task_1"):
    return {"tasks": [{"start_ts": 0.0, "end_ts": 1.0, "name": name}],
            "summary": {"source": "idle_gap_heuristic"}, "tags": []}


def test_wiring_produced(ledger, monkeypatch, tmp_path):
    out = _drive(ledger, monkeypatch, tmp_path,
                 decision=_Dec(DegradeAction.USE_PROVIDER, tasks=_tasks("A")))
    assert out == "produced_tasks"


def test_wiring_cloud_fallback_is_produced_not_mechanical(ledger, monkeypatch, tmp_path):
    # HEURISTIC branch, but the consented cloud fallback named the day → produced.
    out = _drive(ledger, monkeypatch, tmp_path,
                 decision=_Dec(DegradeAction.HEURISTIC), cloud=_tasks("Cloudy"))
    assert out == "produced_tasks"


def test_wiring_mechanical(ledger, monkeypatch, tmp_path):
    out = _drive(ledger, monkeypatch, tmp_path,
                 decision=_Dec(DegradeAction.HEURISTIC), cloud=None, heuristic=_mechanical())
    assert out == "mechanical_only"


def test_wiring_nothing_finalize_vs_live(ledger, monkeypatch, tmp_path):
    assert _drive(ledger, monkeypatch, tmp_path,
                  decision=_Dec(DegradeAction.NONE), is_live=False) == "nothing_to_name"


def test_wiring_nothing_live_is_in_progress(ledger, monkeypatch, tmp_path):
    assert _drive(ledger, monkeypatch, tmp_path,
                  decision=_Dec(DegradeAction.NONE), is_live=True) == "in_progress"


def test_wiring_failed_finalize_is_couldnt_run(ledger, monkeypatch, tmp_path):
    assert _drive(ledger, monkeypatch, tmp_path,
                  decision=None, segment_exc=True, is_live=False) == "couldnt_run"


def test_wiring_monotonic_no_downgrade(ledger, monkeypatch, tmp_path):
    # First a produced pass, then a live mechanical tick → stays produced_tasks.
    _drive(ledger, monkeypatch, tmp_path,
           decision=_Dec(DegradeAction.USE_PROVIDER, tasks=_tasks("A")))
    out = _drive(ledger, monkeypatch, tmp_path,
                 decision=_Dec(DegradeAction.HEURISTIC), heuristic=_mechanical(), is_live=True)
    assert out == "produced_tasks"


def test_wiring_monotonic_does_not_overwrite_ai_rows(ledger, monkeypatch, tmp_path):
    # Row-level honesty invariant: a gated mechanical tick must NOT overwrite the
    # persisted AI task rows — reason stays produced AND the rows stay AI-named
    # (so the card never shows "AI-named" above a mechanical list).
    # The ledger's recording.db lives at tmp_path/recording.db (the `ledger` fixture),
    # so read the persisted rows from tmp_path — not the recording_dir (tmp_path/rec),
    # which only holds tasks.json.
    _drive(ledger, monkeypatch, tmp_path,
           decision=_Dec(DegradeAction.USE_PROVIDER, tasks=_tasks("AI-Task")))
    rows_before = ps.read_task_segments_wire(tmp_path)
    _drive(ledger, monkeypatch, tmp_path,
           decision=_Dec(DegradeAction.HEURISTIC), heuristic=_mechanical("task_1"), is_live=True)
    rows_after = ps.read_task_segments_wire(tmp_path)
    assert [r["name"] for r in rows_after] == ["AI-Task"]
    assert rows_before == rows_after
    assert ledger.get_recording_outcome() == "produced_tasks"


def test_wiring_failed_via_resolve_raises(ledger, monkeypatch, tmp_path):
    # An exception in resolve_day_split (not just _segment_local_tasks) → couldnt_run.
    rec = tmp_path / "rec"
    rec.mkdir(exist_ok=True)
    monkeypatch.setattr(
        ts, "_segment_local_tasks", lambda d, **k: ("summary", {"p": 1}, None),
    )
    monkeypatch.setattr(consent_mod.ConsentPolicy, "from_config", lambda *a, **k: None)

    def _raise(*a, **k):
        raise RuntimeError("ladder boom")

    monkeypatch.setattr(degrade_mod, "resolve_day_split", _raise)
    ts._run_local_segmentation(rec, ledger, ts.TerminalResult(destination="local"), is_live=False)
    assert ledger.get_recording_outcome() == "couldnt_run"


def test_wiring_both_fallbacks_empty_is_couldnt_run(ledger, monkeypatch, tmp_path):
    # HEURISTIC branch, cloud fallback declines AND heuristic produces nothing → couldnt_run.
    assert _drive(ledger, monkeypatch, tmp_path,
                  decision=_Dec(DegradeAction.HEURISTIC), cloud=None, heuristic=None,
                  is_live=False) == "couldnt_run"


def test_get_recording_outcome_failsafe_on_db_error(ledger, monkeypatch):
    # A connect/read error resolves to None (fail-safe), never raises — the authority
    # the monotonic gate consults must not escape the strictly-fail-open path.
    def _boom():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ledger, "_connect", _boom)
    assert ledger.get_recording_outcome() is None


# --- SCR-275 U6 — the optional reason DETAIL column -------------------------


@pytest.mark.privacy
def test_detail_roundtrip_and_default_none(ledger):
    ledger.set_recording_outcome("mechanical_only", detail="context-window")
    assert ledger.get_recording_outcome() == "mechanical_only"
    assert ledger.get_recording_outcome_detail() == "context-window"
    # A detail-less write NULLs the detail (it describes the CURRENT outcome).
    ledger.set_recording_outcome("produced_tasks")
    assert ledger.get_recording_outcome_detail() is None


@pytest.mark.privacy
def test_detail_column_migrated_onto_pre_u6_table(tmp_path):
    """Guarded-ALTER migration: an existing recording.db whose outcome table
    predates the ``detail`` column gains it via ensure_pipeline_state_schema,
    keeps its old row (detail NULL), and accepts detail-bearing writes."""
    p = tmp_path / "recording.db"
    _make_recording_db(p)
    # Create the PRE-U6 table shape by hand (reason but no detail) + a row.
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE pipeline_recording_outcome ("
        " recording_id INTEGER PRIMARY KEY, reason TEXT NOT NULL, updated_at REAL)"
    )
    conn.execute(
        "INSERT INTO pipeline_recording_outcome (recording_id, reason, updated_at) "
        "VALUES (1, 'in_progress', 0.0)"
    )
    conn.commit()
    conn.close()

    ps.ensure_pipeline_state_schema(p)
    ledger = ps.PipelineLedger(p)
    assert ledger.get_recording_outcome() == "in_progress"
    assert ledger.get_recording_outcome_detail() is None  # absent detail → NULL
    ledger.set_recording_outcome("produced_tasks_partial", detail="guardrail")
    assert ledger.get_recording_outcome() == "produced_tasks_partial"
    assert ledger.get_recording_outcome_detail() == "guardrail"


@pytest.mark.privacy
def test_detail_write_migrates_defensively_without_ensure(tmp_path):
    """set_recording_outcome itself tolerates a pre-detail table (the defensive
    in-transaction migration), so a detail-bearing write never crashes on an
    old recording.db that skipped ensure_pipeline_state_schema."""
    p = tmp_path / "recording.db"
    _make_recording_db(p)
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE pipeline_recording_outcome ("
        " recording_id INTEGER PRIMARY KEY, reason TEXT NOT NULL, updated_at REAL)"
    )
    conn.commit()
    conn.close()

    ledger = ps.PipelineLedger(p)
    ledger.set_recording_outcome("mechanical_only", detail="context-window")
    assert ledger.get_recording_outcome_detail() == "context-window"


@pytest.mark.privacy
def test_detail_read_failsafe_on_missing_table(tmp_path):
    p = tmp_path / "recording.db"
    _make_recording_db(p)  # no ensure → table absent
    assert ps.PipelineLedger(p).get_recording_outcome_detail() is None


# --- SCR-275 U6 — wiring: detail + the stopped special-case -----------------


@pytest.mark.privacy
def test_wiring_mechanical_records_unavailable_reason_as_detail(
    ledger, monkeypatch, tmp_path,
):
    out = _drive(ledger, monkeypatch, tmp_path,
                 decision=_Dec(DegradeAction.HEURISTIC), cloud=None,
                 heuristic=_mechanical(), reason="context-window")
    assert out == "mechanical_only"
    assert ledger.get_recording_outcome_detail() == "context-window"


@pytest.mark.privacy
def test_wiring_both_fallbacks_empty_couldnt_run_keeps_detail(
    ledger, monkeypatch, tmp_path,
):
    out = _drive(ledger, monkeypatch, tmp_path,
                 decision=_Dec(DegradeAction.HEURISTIC), cloud=None, heuristic=None,
                 is_live=False, reason="model-unavailable-deviceNotEligible")
    assert out == "couldnt_run"
    assert ledger.get_recording_outcome_detail() == (
        "model-unavailable-deviceNotEligible"
    )


@pytest.mark.privacy
def test_wiring_produced_forces_detail_null(ledger, monkeypatch, tmp_path):
    # A fully-produced outcome never carries a failure detail, whatever the
    # provider attribute holds.
    out = _drive(ledger, monkeypatch, tmp_path,
                 decision=_Dec(DegradeAction.USE_PROVIDER, tasks=_tasks("A")),
                 reason="decoding-failure")
    assert out == "produced_tasks"
    assert ledger.get_recording_outcome_detail() is None


@pytest.mark.privacy
def test_wiring_stopped_sentinel_short_circuits(ledger, monkeypatch, tmp_path):
    """A PROVIDER_UNAVAILABLE + reason='stopped' pass is a quiesce interrupt:
    neither fallback runs (trip-wired), nothing persists, and the outcome maps
    through the provisional live branch — in_progress even at finalize."""
    from screencap.segmentation.provider import PROVIDER_UNAVAILABLE

    # decision=None: if the stopped special-case failed to short-circuit, the
    # ladder would consult resolve_day_split → None.action raises → couldnt_run,
    # making the in_progress assertion below a real discriminator.
    out = _drive(ledger, monkeypatch, tmp_path,
                 decision=None,
                 provider_result=PROVIDER_UNAVAILABLE, reason="stopped",
                 is_live=False)
    assert out == "in_progress"
    assert ledger.get_recording_outcome_detail() is None
    assert not (tmp_path / "rec" / "tasks.json").exists()
