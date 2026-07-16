"""U2 (honest status): per-recording segmentation outcome storage + wiring.

Covers the ledger set/get/upsert + missing-table tolerance, and the
``_run_local_segmentation`` branch → reason wiring (produced / mechanical /
nothing / couldn't-run / in-progress) plus monotonic no-downgrade.
"""

from __future__ import annotations

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
           segment_exc=False, is_live=False) -> str | None:
    rec = tmp_path / "rec"
    rec.mkdir(exist_ok=True)

    if segment_exc:
        def _raise(_d):
            raise RuntimeError("boom")
        monkeypatch.setattr(ts, "_segment_local_tasks", _raise)
    else:
        monkeypatch.setattr(ts, "_segment_local_tasks", lambda d: ("summary", {"p": 1}))

    monkeypatch.setattr(consent_mod.ConsentPolicy, "from_config", lambda *a, **k: None)
    monkeypatch.setattr(degrade_mod, "resolve_day_split", lambda *a, **k: decision)
    monkeypatch.setattr(ts, "_summary_cloud_fallback", lambda s: cloud)
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
