"""Tests for U7 — the graceful-degradation ladder in run_terminal_stage (R5).

Scope (the U7 test scenarios), all over a LOCAL recording so nothing can upload:

* On-device UNAVAILABLE (``PROVIDER_UNAVAILABLE``) + no cloud → day-split falls
  back to the local idle-gap heuristic, producing task boundaries persisted to
  the LOCAL store (tasks.json + the pipeline_task_segments ledger). (AE4)
* On-device UNAVAILABLE + cloud configured + summary/recall consented → STILL
  the heuristic; the cloud provider path is NEVER invoked (R7 over R5 / KTD6).
* A real provider tasks dict → used unchanged; the heuristic is NOT run.
* ``None`` (provider ran, produced nothing) → fail-open, no tasks; the heuristic
  is NOT run (the None-vs-unavailable distinction).
* No degradation path forces upload for a LOCAL recording (trip-wired seam).

The resolver ``segmentation.degrade`` is also unit-tested directly for the
never-cloud invariant, independent of the terminal wiring.

Fixtures mirror ``tests/test_terminal_stage_segmentation.py`` (a LOCAL recording
dir with recording.db + ledger schema + on-disk chunk artifacts) but ALSO seed
real ``action_event`` rows with a deliberate idle gap so the idle-gap heuristic
has something to split.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from screencap.segmentation.consent import ConsentPolicy, TaskKind
from screencap.segmentation.degrade import (
    DegradeAction,
    resolve,
    resolve_day_split,
)
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Two clusters of action events split by a > rest_threshold (120s default) gap,
# so _segment_tasks yields exactly two heuristic tasks over the whole recording.
_CLUSTER_A = (10.0, 12.0, 15.0)
_CLUSTER_B = (400.0, 402.0, 405.0)  # 385s after the last A event → a new task.


def _make_local_recording(
    tmp_path: Path, *, destination: str = "local", n_chunks: int = 1,
    name: str = "rec",
) -> Path:
    """Create a LOCAL recording dir with recording.db + ledger + chunk artifacts.

    Seeds real ``action_event`` rows with two idle-separated clusters so the
    idle-gap heuristic (``task_manifest._segment_tasks``) yields two tasks. Also
    writes the v2 manifests/events the LocalActivitySource reads so the provider
    path builds a summary just like the U4 tests.
    """
    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import (
        PipelineLedger,
        ensure_pipeline_state_schema,
    )

    rec_dir = tmp_path / name
    rec_dir.mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"

    engine, Session = create_db(str(db_path))
    session = Session()
    recording = crud.insert_recording(session, {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })
    base = 1000.0
    chunk_dur = 3600.0
    event_offsets = (10.0, 20.0, 30.0)
    for i in range(n_chunks):
        cs = base + i * chunk_dur
        # Two idle-separated clusters of real click events for the heuristic.
        for off in (*_CLUSTER_A, *_CLUSTER_B):
            crud.insert_action_event(session, recording, cs + off, {
                "name": "click", "mouse_x": 10.0, "mouse_y": 20.0,
                "mouse_button_name": "left", "mouse_pressed": True,
            })
        crud.insert_window_event(session, recording, cs + 1, {
            "app_bundle_id": "com.example.unknownbenign",
            "window_id": f"w{i}",
            "title": f"file_{i}.py",
            "app_name": "Editor",
        })
        for off in event_offsets:
            crud.insert_screenshot(session, recording, cs + off, {
                "image_path": f"screenshots/{cs + off}.jpg",
            })
    session.close()
    engine.dispose()

    for i in range(n_chunks):
        cs = base + i * chunk_dur
        ce = cs + chunk_dur
        (rec_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 1024)
        (rec_dir / f"chunk_{i:04d}_manifest.json").write_text(json.dumps({
            "format_version": 2, "chunk_index": i,
            "chunk_start": cs, "chunk_end": ce,
            "stats": {"total_events": 3, "total_window_switches": 1},
            "blocked_intervals": [],
        }))
        events = [
            {"_meta": True, "format_version": 2},
            {"type": "window.switch", "timestamp": cs + event_offsets[0],
             "app_bundle_id": "com.microsoft.VSCode",
             "window_title": f"file_{i}.py — screencap"},
            {"type": "key.type", "timestamp": cs + event_offsets[1],
             "text": "def foo():"},
            {"type": "mouse.singleclick", "timestamp": cs + event_offsets[2]},
        ]
        (rec_dir / f"events_{i:04d}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n"
        )

    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    for i in range(n_chunks):
        ledger.seed_chunk(i)
        ledger.mark_staged(i)
    ledger.freeze_chunks_expected(n_chunks)

    (rec_dir / ".recording_intent").write_text(json.dumps({
        "version": 2,
        "destination": destination,
        "retention_policy": "keep_forever",
        "retention_params": {},
        "show_on_website": False,
    }))
    (rec_dir / ".recording_id").write_text(name)
    return rec_dir


def _canned_tasks() -> dict:
    """A validated-shape provider tasks dict (distinguishable from heuristic)."""
    return {
        "tasks": [
            {"start_ts": 1000.0, "end_ts": 2800.0,
             "name": "Implement auth module", "derived_name": "implement-auth-module",
             "description": "Wrote auth.py", "category": "development",
             "apps_used": ["VS Code"], "confidence": "high"},
        ],
        "summary": {"overview": "Built auth.", "primary_focus": "development",
                    "time_breakdown": {}, "key_accomplishments": []},
        "tags": ["python"],
    }


class _FakeProvider:
    """A canned LLMProvider — records calls, returns a fixed SegmentResult."""

    def __init__(self, result) -> None:  # noqa: ANN001 — dict | None | sentinel
        self._result = result
        self.calls: list[dict] = []

    def segment(self, activity_summary: dict):  # noqa: ANN201
        self.calls.append(activity_summary)
        return self._result


@pytest.fixture(autouse=True)
def _isolate_run_dir(tmp_path, monkeypatch):
    """Point the terminal-stage lock dir at a per-test tmp dir."""
    import screencap.terminal_stage as ts

    monkeypatch.setattr(ts, "_RUN_DIR", tmp_path / "run")


def _install_provider(monkeypatch, provider) -> None:
    """Wire a fake provider into the U8 day-split routing seam."""
    import screencap.segmentation.routing as routing

    monkeypatch.setattr(routing, "build_day_split_provider", lambda: provider)


def _install_consent(monkeypatch, policy: ConsentPolicy) -> None:
    """Force ``ConsentPolicy.from_config`` to return a specific policy."""
    import screencap.segmentation.consent as consent

    monkeypatch.setattr(
        consent.ConsentPolicy, "from_config", classmethod(lambda cls: policy),
    )


def _run_terminal(rec_dir: Path):
    from screencap import terminal_stage as ts

    return ts.run_terminal_stage(rec_dir)


# ---------------------------------------------------------------------------
# Unit: the resolver's never-cloud invariant (independent of terminal wiring).
# ---------------------------------------------------------------------------


def test_resolve_day_split_never_cloud_even_when_configured_and_consented():
    """DAY_SPLIT + unavailable + cloud configured + consented → HEURISTIC (R7>R5)."""
    policy = ConsentPolicy(
        cloud_provider="gemini",
        summary_cloud_consent=True,
        recall_cloud_consent=True,
    )
    d = resolve_day_split(PROVIDER_UNAVAILABLE, policy)
    assert d.action is DegradeAction.HEURISTIC
    assert d.tasks is None


def test_resolve_distinguishes_none_from_unavailable():
    """None → NONE (fail-open); PROVIDER_UNAVAILABLE → HEURISTIC. No cloud."""
    policy = ConsentPolicy(cloud_provider="gemini", summary_cloud_consent=True)
    assert resolve_day_split(None, policy).action is DegradeAction.NONE
    assert (
        resolve_day_split(PROVIDER_UNAVAILABLE, policy).action
        is DegradeAction.HEURISTIC
    )


def test_resolve_real_dict_uses_provider():
    tasks = {"tasks": [{"name": "x"}]}
    d = resolve_day_split(tasks, ConsentPolicy())
    assert d.action is DegradeAction.USE_PROVIDER
    assert d.tasks is tasks


def test_resolve_summary_unavailable_consented_reaches_cloud():
    """A consented SUMMARY (not day-split) CAN degrade to cloud — the capability
    exists for the future on-demand flow; terminal only runs day-split."""
    policy = ConsentPolicy(cloud_provider="gemini", summary_cloud_consent=True)
    d = resolve(TaskKind.SUMMARY, PROVIDER_UNAVAILABLE, policy)
    assert d.action is DegradeAction.CLOUD


# ---------------------------------------------------------------------------
# Terminal wiring: on-device unavailable → idle-gap heuristic (AE4).
# ---------------------------------------------------------------------------


def test_unavailable_no_cloud_falls_back_to_heuristic(tmp_path, monkeypatch):
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    fake = _FakeProvider(PROVIDER_UNAVAILABLE)
    _install_provider(monkeypatch, fake)
    _install_consent(monkeypatch, ConsentPolicy())  # no cloud configured.

    result = _run_terminal(rec_dir)

    # The provider was consulted and reported unavailable → heuristic ran.
    assert len(fake.calls) == 1

    # tasks.json + ledger carry the two idle-gap boundaries, mechanically named.
    tasks_path = rec_dir / "tasks.json"
    assert tasks_path.is_file()
    persisted = json.loads(tasks_path.read_text())
    assert len(persisted["tasks"]) == 2
    assert [t["name"] for t in persisted["tasks"]] == ["task_1", "task_2"]
    assert persisted["summary"]["source"] == "idle_gap_heuristic"
    # Boundaries reflect the two seeded clusters (base=1000 + offsets).
    assert persisted["tasks"][0]["start_ts"] == 1000.0 + _CLUSTER_A[0]
    assert persisted["tasks"][1]["start_ts"] == 1000.0 + _CLUSTER_B[0]

    ledger = PipelineLedger(rec_dir / "recording.db")
    segs = ledger.read_task_segments()
    assert [s.name for s in segs] == ["task_1", "task_2"]
    assert [s.task_index for s in segs] == [0, 1]

    assert result.tasks_persisted == 2
    assert result.routed is True
    assert result.destination == "local"


def test_unavailable_with_cloud_still_heuristic_never_cloud(tmp_path, monkeypatch):
    """R7 over R5: cloud configured + consented, yet day-split stays heuristic and
    no cloud/provider-cloud path is invoked."""
    import screencap.segmentation.provider as prov

    rec_dir = _make_local_recording(tmp_path)
    fake = _FakeProvider(PROVIDER_UNAVAILABLE)
    _install_provider(monkeypatch, fake)
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="gemini",
        summary_cloud_consent=True,
        recall_cloud_consent=True,
    ))

    # Trip-wire: get_provider is only allowed to hand back our single fake (the
    # on-device backend under test). Any attempt to resolve a *second*, cloud
    # backend for day-split is a violation of KTD6.
    original_get_provider = prov.get_provider

    def _guarded_get_provider(name):  # noqa: ANN001
        if name != "fake":
            raise AssertionError(
                f"cloud provider {name!r} resolved for a day-split fallback "
                "(R7 over R5 violated)"
            )
        return fake

    monkeypatch.setattr(prov, "get_provider", _guarded_get_provider)
    assert original_get_provider is not None  # sanity: symbol existed.

    result = _run_terminal(rec_dir)

    # Heuristic produced the tasks; provider consulted exactly once (on-device).
    assert len(fake.calls) == 1
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["task_1", "task_2"]
    assert persisted["summary"]["source"] == "idle_gap_heuristic"
    assert result.tasks_persisted == 2


# ---------------------------------------------------------------------------
# Terminal wiring: a real provider result / a None result do NOT run heuristic.
# ---------------------------------------------------------------------------


def test_real_provider_result_used_heuristic_not_run(tmp_path, monkeypatch):
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))
    _install_consent(monkeypatch, ConsentPolicy())

    result = _run_terminal(rec_dir)

    persisted = json.loads((rec_dir / "tasks.json").read_text())
    # The provider's named task, NOT the mechanical task_N of the heuristic.
    assert [t["name"] for t in persisted["tasks"]] == ["Implement auth module"]
    assert "source" not in persisted["summary"]
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert [s.name for s in ledger.read_task_segments()] == ["Implement auth module"]
    assert result.tasks_persisted == 1


def test_provider_none_is_fail_open_heuristic_not_run(tmp_path, monkeypatch):
    """None (ran, empty) → no tasks; the heuristic is NOT a backstop for empty."""
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(None))
    _install_consent(monkeypatch, ConsentPolicy())

    result = _run_terminal(rec_dir)

    assert result.routed is True
    assert result.destination == "local"
    assert result.tasks_persisted == 0
    assert not (rec_dir / "tasks.json").exists()
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.read_task_segments() == []
    # LOCAL_DONE marking still happened (terminal not blocked by the miss).
    assert result.n_local_done == 1


# ---------------------------------------------------------------------------
# No degradation path uploads for a LOCAL recording.
# ---------------------------------------------------------------------------


def test_heuristic_fallback_never_uploads(tmp_path, monkeypatch):
    import screencap.terminal_stage as ts
    import screencap.upload as upload

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="gemini", summary_cloud_consent=True,
    ))

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("upload seam invoked during a heuristic fallback")

    monkeypatch.setattr(upload, "upload_recording", _boom)
    monkeypatch.setattr(upload, "request_signed_urls", _boom)
    monkeypatch.setattr(
        ts.CloudCopyProducer, "produce",
        lambda self, **k: (_ for _ in ()).throw(
            AssertionError("cloud copy produced during a heuristic fallback")
        ),
    )

    result = _run_terminal(rec_dir)

    assert result.destination == "local"
    assert result.tasks_persisted == 2  # heuristic still produced tasks.
    assert result.sentinel_uploaded is False
    assert result.n_uploaded == 0
