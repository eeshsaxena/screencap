"""U3: tasks.list exposes the per-recording segmentation outcome reason."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import screencap.config as config
import screencap.pipeline_state as ps
from screencap.daemon import app as daemon_app

# Hermetic (tmp recording dir, no Vision) — run on CI's privacy lane (SCR-275 U6).
pytestmark = pytest.mark.privacy


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
            "task_description": "u3-fixture",
        },
    )
    session.close()
    engine.dispose()


@pytest.fixture
def rec_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "rec-2026"
    d.mkdir()
    _make_recording_db(d / "recording.db")
    ps.ensure_pipeline_state_schema(d / "recording.db")
    monkeypatch.setattr(config, "resolve_recording_dir", lambda name: d)
    return d


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def test_reason_none_when_unrecorded(rec_dir):
    assert daemon_app._run_recording_outcome("rec-2026") == (None, None)


def test_reason_reflects_stored_outcome(rec_dir):
    ps.PipelineLedger(rec_dir / "recording.db").set_recording_outcome("mechanical_only")
    assert daemon_app._run_recording_outcome("rec-2026") == ("mechanical_only", None)


def test_reason_and_detail_reflect_stored_outcome(rec_dir):
    # SCR-275 U6: the distinct degradation reason rides as an optional detail.
    ps.PipelineLedger(rec_dir / "recording.db").set_recording_outcome(
        "produced_tasks_partial", detail="context-window",
    )
    assert daemon_app._run_recording_outcome("rec-2026") == (
        "produced_tasks_partial", "context-window",
    )


def test_reason_none_when_db_absent(tmp_path, monkeypatch):
    d = tmp_path / "empty-rec"
    d.mkdir()
    monkeypatch.setattr(config, "resolve_recording_dir", lambda name: d)
    assert daemon_app._run_recording_outcome("empty-rec") == (None, None)


def test_tasks_list_envelope_carries_reason(rec_dir):
    ps.PipelineLedger(rec_dir / "recording.db").set_recording_outcome("couldnt_run")
    resp = asyncio.run(daemon_app.tasks_list(_Req({"recording": "rec-2026"})))
    body = json.loads(bytes(resp.body))
    assert body["ok"] is True
    assert body["recording"] == "rec-2026"
    assert body["tasks"] == []
    assert body["reason"] == "couldnt_run"
    assert body["detail"] is None  # additive field; absent detail → null.


def test_tasks_list_envelope_carries_partial_reason_and_detail(rec_dir):
    ps.PipelineLedger(rec_dir / "recording.db").set_recording_outcome(
        "produced_tasks_partial", detail="context-window",
    )
    resp = asyncio.run(daemon_app.tasks_list(_Req({"recording": "rec-2026"})))
    body = json.loads(bytes(resp.body))
    assert body["ok"] is True
    assert body["reason"] == "produced_tasks_partial"
    assert body["detail"] == "context-window"


def test_tasks_list_envelope_reason_none_for_unrecorded(rec_dir):
    resp = asyncio.run(daemon_app.tasks_list(_Req({"recording": "rec-2026"})))
    body = json.loads(bytes(resp.body))
    assert body["reason"] is None
    assert body["detail"] is None
