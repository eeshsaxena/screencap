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


class _ReasonProvider(_FakeProvider):
    """A canned provider carrying the U3/U6 ``last_unavailable_reason`` seam."""

    def __init__(self, result, reason: str | None = None) -> None:  # noqa: ANN001
        super().__init__(result)
        self.last_unavailable_reason = reason


class _SequenceProvider:
    """A provider whose ``segment`` walks scripted ``(result, reason)`` pairs.

    The retry-observing seam for the stale-scrub finalize tests: each call
    consumes the next pair (the last repeats) and records the reason on the
    ``last_unavailable_reason`` attribute like the real on-device provider.
    """

    def __init__(self, script: "list[tuple]") -> None:
        self._script = list(script)
        self.calls: list[dict] = []
        self.last_unavailable_reason: str | None = None

    def segment(self, activity_summary: dict):  # noqa: ANN201
        self.calls.append(activity_summary)
        if len(self._script) > 1:
            result, reason = self._script.pop(0)
        else:
            result, reason = self._script[0]
        self.last_unavailable_reason = reason
        return result


@pytest.fixture(autouse=True)
def _isolate_run_dir(tmp_path, monkeypatch):
    """Point the terminal-stage lock dir at a per-test tmp dir."""
    import screencap.terminal_stage as ts

    monkeypatch.setattr(ts, "_RUN_DIR", tmp_path / "run")


def _install_provider(monkeypatch, provider) -> None:
    """Wire a fake provider into the U8 day-split routing seam."""
    import screencap.segmentation.routing as routing

    monkeypatch.setattr(
        routing, "build_day_split_provider", lambda *a, **k: provider,
    )


def _install_consent(monkeypatch, policy: ConsentPolicy) -> None:
    """Force ``ConsentPolicy.from_config`` to return a specific policy.

    Also mirrors the policy's ``cloud_provider`` into ``config.get_llm_cloud_provider``
    so the SUMMARY cloud fallback (U5) — which resolves the provider *name* via that
    config getter, exactly like ``recall._cloud_fallback`` — sees a consistent
    configured/unconfigured cloud provider rather than the test host's real config.
    """
    import screencap.config as config
    import screencap.segmentation.consent as consent

    monkeypatch.setattr(
        consent.ConsentPolicy, "from_config", classmethod(lambda cls: policy),
    )
    monkeypatch.setattr(
        config, "get_llm_cloud_provider", lambda: policy.cloud_provider,
    )


def _install_cloud_provider(monkeypatch, provider) -> None:
    """Wire ``get_provider`` to return ``provider`` for the SUMMARY cloud fallback.

    Patches the ``get_provider`` symbol imported by ``_summary_cloud_fallback``
    (``screencap.segmentation.provider.get_provider``) so the consented SUMMARY
    cloud path resolves to the given fake cloud backend rather than a live one.
    """
    import screencap.segmentation.provider as prov

    monkeypatch.setattr(prov, "get_provider", lambda name: provider)


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


@pytest.mark.privacy
def test_resolve_prose_unavailable_consented_reaches_cloud():
    """DIARY_PROSE (U3) mirrors SUMMARY: consented + provider + unavailable → cloud
    (gated on the summary consent row, KTD-4/R14) — NOT day-split's never-cloud."""
    from screencap.segmentation.degrade import resolve_prose

    policy = ConsentPolicy(cloud_provider="gemini", summary_cloud_consent=True)
    assert resolve_prose(PROVIDER_UNAVAILABLE, policy).action is DegradeAction.CLOUD


@pytest.mark.privacy
def test_resolve_prose_unavailable_no_consent_falls_to_heuristic_never_none():
    """No consent (or no provider) → the honest app-level HEURISTIC, never NONE —
    a diary block always carries at least app-level bullets (R6/AE3)."""
    from screencap.segmentation.degrade import resolve_prose

    no_consent = ConsentPolicy(cloud_provider="gemini", summary_cloud_consent=False)
    assert (
        resolve_prose(PROVIDER_UNAVAILABLE, no_consent).action
        is DegradeAction.HEURISTIC
    )
    no_provider = ConsentPolicy(cloud_provider=None, summary_cloud_consent=True)
    assert (
        resolve_prose(PROVIDER_UNAVAILABLE, no_provider).action
        is DegradeAction.HEURISTIC
    )


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
    assert [t["name"] for t in persisted["tasks"]] == ["", ""]  # U2: mechanical task_N never surfaces; honest UNNAMED blocks
    assert persisted["summary"]["source"] == "idle_gap_heuristic"
    # Boundaries reflect the two seeded clusters (base=1000 + offsets).
    assert persisted["tasks"][0]["start_ts"] == 1000.0 + _CLUSTER_A[0]
    assert persisted["tasks"][1]["start_ts"] == 1000.0 + _CLUSTER_B[0]

    ledger = PipelineLedger(rec_dir / "recording.db")
    segs = ledger.read_task_segments()
    assert [s.name for s in segs] == ["", ""]  # U2: mechanical task_N never surfaces; honest UNNAMED blocks
    assert [s.task_index for s in segs] == [0, 1]

    assert result.tasks_persisted == 2
    assert result.routed is True
    assert result.destination == "local"


def test_unavailable_summary_consent_off_stays_heuristic_never_cloud(
    tmp_path, monkeypatch,
):
    """R7 over R5 / R6: on-device day-split unavailable, but SUMMARY cloud consent
    OFF → day-split stays heuristic and NO cloud provider is ever resolved.

    This pins the two invariants together: (1) the day-split *boundaries* come
    from the idle-gap heuristic, never cloud (KTD6); (2) with the SUMMARY cloud
    consent row OFF, the net-new SUMMARY cloud fallback (U5) does not fire, so
    ``get_provider`` is never called with a cloud name at all."""
    import screencap.segmentation.provider as prov

    rec_dir = _make_local_recording(tmp_path)
    fake = _FakeProvider(PROVIDER_UNAVAILABLE)
    _install_provider(monkeypatch, fake)
    # A cloud provider is configured, but SUMMARY consent is OFF → no cloud path.
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="gemini",
        summary_cloud_consent=False,
        recall_cloud_consent=True,
    ))

    # Trip-wire: get_provider is only allowed to hand back our single fake (the
    # on-device backend under test). With SUMMARY consent OFF, no cloud backend
    # may be resolved for day-split OR for a summary fallback.
    original_get_provider = prov.get_provider

    def _guarded_get_provider(name):  # noqa: ANN001
        if name != "fake":
            raise AssertionError(
                f"cloud provider {name!r} resolved with SUMMARY consent off "
                "(R7 over R5 / R6 violated)"
            )
        return fake

    monkeypatch.setattr(prov, "get_provider", _guarded_get_provider)
    assert original_get_provider is not None  # sanity: symbol existed.

    result = _run_terminal(rec_dir)

    # Heuristic produced the tasks; provider consulted exactly once (on-device).
    assert len(fake.calls) == 1
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["", ""]  # U2: mechanical task_N never surfaces; honest UNNAMED blocks
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
# U5 — the net-new consented SUMMARY cloud fallback (R6/R8/KTD4).
#
# When on-device day-split is UNAVAILABLE and the SUMMARY cloud consent row is on
# with a BYO cloud provider configured, the recording is named by that cloud
# provider over the SAME already-stripped summary — never frames, never for the
# day-split *boundary* decision (which stays heuristic when cloud declines).
# ---------------------------------------------------------------------------


def _cloud_named_tasks() -> dict:
    """A validated cloud-provider tasks dict, distinguishable from heuristic/on-device."""
    return {
        "tasks": [
            {"start_ts": 1000.0, "end_ts": 2800.0,
             "name": "Cloud-named session", "derived_name": "cloud-named-session",
             "description": "Named by the BYO cloud provider", "category": "development",
             "apps_used": ["VS Code"], "confidence": "high"},
        ],
        "summary": {"overview": "A cloud summary.", "primary_focus": "development",
                    "time_breakdown": {}, "key_accomplishments": []},
        "tags": ["cloud"],
    }


@pytest.mark.privacy
def test_summary_cloud_fallback_invoked_with_stripped_summary_no_frames(
    tmp_path, monkeypatch,
):
    """R6/R8/KTD4: on-device unavailable + SUMMARY consent on + cloud configured →
    the BYO cloud provider's segment() is invoked with the ALLOW-only stripped
    summary (never frames), and its named tasks are persisted (not the heuristic)."""
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True,
    ))
    cloud = _FakeProvider(_cloud_named_tasks())
    _install_cloud_provider(monkeypatch, cloud)

    result = _run_terminal(rec_dir)

    # The cloud provider WAS invoked exactly once with the stripped summary.
    assert len(cloud.calls) == 1
    handed = cloud.calls[0]
    assert isinstance(handed, dict)
    # The payload is the ALLOW-only text summary, authoritatively marked stripped.
    assert handed.get("stripped") is True
    # No frame bytes / image paths of any kind reach the cloud payload.
    blob = json.dumps(handed)
    for marker in ("\\xff\\xd8\\xff", "\\x89PNG", ".jpg", ".png", "screenshots/"):
        assert marker not in blob, f"frame reference {marker!r} leaked to cloud payload"

    # The cloud provider's named task is persisted — NOT the mechanical heuristic.
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["Cloud-named session"]
    assert persisted["summary"].get("source") != "idle_gap_heuristic"
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert [s.name for s in ledger.read_task_segments()] == ["Cloud-named session"]
    assert result.tasks_persisted == 1
    # LOCAL recording: the cloud NAMING never triggers an upload.
    assert result.destination == "local"
    assert result.n_uploaded == 0
    assert result.sentinel_uploaded is False


def _cloud_split_tasks() -> dict:
    """A validated cloud tasks dict that SPLITS the day into two named segments."""
    return {
        "tasks": [
            {"start_ts": 1000.0, "end_ts": 1600.0,
             "name": "Morning research", "derived_name": "morning-research",
             "description": "Reading docs", "category": "research",
             "apps_used": ["Safari"], "confidence": "high"},
            {"start_ts": 2200.0, "end_ts": 2800.0,
             "name": "Afternoon build", "derived_name": "afternoon-build",
             "description": "Writing code", "category": "development",
             "apps_used": ["VS Code"], "confidence": "high"},
        ],
        "summary": {"overview": "A cloud-split day.", "primary_focus": "development",
                    "time_breakdown": {}, "key_accomplishments": []},
        "tags": ["cloud"],
    }


@pytest.mark.privacy
def test_no_on_device_cloud_splits_and_labels_the_day(tmp_path, monkeypatch):
    """Point 2 / R2: with no on-device model but a cloud provider configured
    (consent on by default — KTD1), the day is SPLIT INTO MULTIPLE cloud-labeled
    tasks by the cloud model, not collapsed to the mechanical idle-gap heuristic.

    The default-on gate itself is proven at the policy level in
    ``tests/segmentation/test_consent.py``; this proves the terminal wiring turns a
    cloud segmentation into persisted multi-segment day tasks (boundaries + labels),
    over the ALLOW-only stripped summary, with no upload."""
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    # A policy mirroring the shipped default: cloud configured, consent on.
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True,
    ))
    cloud = _FakeProvider(_cloud_split_tasks())
    _install_cloud_provider(monkeypatch, cloud)

    result = _run_terminal(rec_dir)

    # The cloud model split the day into its own multiple named segments...
    assert len(cloud.calls) == 1
    assert cloud.calls[0].get("stripped") is True  # ALLOW-only text, never frames
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    names = [t["name"] for t in persisted["tasks"]]
    assert names == ["Morning research", "Afternoon build"]
    # ...NOT the mechanical idle-gap heuristic.
    assert persisted["summary"].get("source") != "idle_gap_heuristic"
    assert names != ["task_1", "task_2"]
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert [s.name for s in ledger.read_task_segments()] == [
        "Morning research", "Afternoon build",
    ]
    assert result.tasks_persisted == 2
    # LOCAL recording: cloud splitting never triggers an upload.
    assert result.destination == "local"
    assert result.n_uploaded == 0


@pytest.mark.privacy
def test_summary_cloud_declines_falls_through_to_heuristic(tmp_path, monkeypatch):
    """Fail-open: consent on + cloud configured, but the cloud provider returns
    None (ran, no usable tasks) → the recording falls through to the idle-gap
    heuristic; it is never left unnamed just because cloud declined."""
    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True,
    ))
    cloud = _FakeProvider(None)  # ran, produced nothing.
    _install_cloud_provider(monkeypatch, cloud)

    result = _run_terminal(rec_dir)

    assert len(cloud.calls) == 1  # the cloud path WAS tried first.
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    # Fell through to the heuristic's mechanical names.
    assert [t["name"] for t in persisted["tasks"]] == ["", ""]  # U2: mechanical task_N never surfaces; honest UNNAMED blocks
    assert persisted["summary"]["source"] == "idle_gap_heuristic"
    assert result.tasks_persisted == 2


@pytest.mark.privacy
def test_summary_cloud_unavailable_falls_through_to_heuristic(tmp_path, monkeypatch):
    """Fail-open: a cloud provider that returns PROVIDER_UNAVAILABLE (could not run
    — e.g. no stored key) leaves the recording to the idle-gap heuristic; never a
    crash, never left unnamed."""
    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="gemini-cli", summary_cloud_consent=True,
    ))
    _install_cloud_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))

    result = _run_terminal(rec_dir)

    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["", ""]  # U2: mechanical task_N never surfaces; honest UNNAMED blocks
    assert persisted["summary"]["source"] == "idle_gap_heuristic"
    assert result.tasks_persisted == 2


@pytest.mark.privacy
def test_summary_cloud_provider_raising_fails_open_to_heuristic(tmp_path, monkeypatch):
    """The SUMMARY cloud fallback never raises: a cloud provider whose segment()
    raises degrades to the heuristic, and terminal completion is never blocked."""
    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True,
    ))

    class _RaisingCloud:
        def segment(self, activity_summary):  # noqa: ANN001, ANN201
            raise RuntimeError("cloud api exploded")

    _install_cloud_provider(monkeypatch, _RaisingCloud())

    result = _run_terminal(rec_dir)

    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["", ""]  # U2: mechanical task_N never surfaces; honest UNNAMED blocks
    assert result.tasks_persisted == 2


def test_summary_cloud_not_invoked_when_provider_ran_with_tasks(tmp_path, monkeypatch):
    """When the on-device day-split provider RAN and produced tasks (USE_PROVIDER),
    the SUMMARY cloud fallback is never reached — cloud is a fallback only for the
    on-device-UNAVAILABLE state."""
    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True,
    ))
    cloud = _FakeProvider(_cloud_named_tasks())
    _install_cloud_provider(monkeypatch, cloud)

    result = _run_terminal(rec_dir)

    assert cloud.calls == []  # the on-device result short-circuits any cloud path.
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["Implement auth module"]
    assert result.tasks_persisted == 1


def test_summary_cloud_not_invoked_when_provider_ran_empty(tmp_path, monkeypatch):
    """A genuine None (provider ran, produced nothing) is fail-open — neither the
    heuristic NOR the SUMMARY cloud fallback is a backstop for a real empty result."""
    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(None))
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True,
    ))
    cloud = _FakeProvider(_cloud_named_tasks())
    _install_cloud_provider(monkeypatch, cloud)

    result = _run_terminal(rec_dir)

    assert cloud.calls == []  # None is not routed to cloud.
    assert result.tasks_persisted == 0
    assert not (rec_dir / "tasks.json").exists()


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
    # Consent is ON here, so the SUMMARY cloud fallback WILL resolve the cloud
    # provider — wire a cloud backend that declines (None), so the recording falls
    # through to the idle-gap heuristic. This keeps the test's subject (no upload,
    # no cloud copy) while exercising the net-new SUMMARY path without a live call.
    _install_cloud_provider(monkeypatch, _FakeProvider(None))

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


# ---------------------------------------------------------------------------
# SCR-272 U5 — masked frames ride ONLY on the consented SUMMARY cloud task,
# gated on the frames opt-in AND a vision-capable provider.
# ---------------------------------------------------------------------------


class _FramesCloudProvider:
    """A VISION-capable canned cloud provider that records the frames it is handed.

    ``supports_frames`` True (the Gemini shape); ``segment`` accepts the typed
    ``masked_frames`` kwarg and records it, so a test can assert exactly which
    frames rode along (or that none did)."""

    supports_frames = True

    def __init__(self, result) -> None:  # noqa: ANN001
        self._result = result
        self.calls: list[dict] = []
        self.masked_frames_seen: tuple = ()

    def segment(self, activity_summary: dict, *, masked_frames: tuple = ()):  # noqa: ANN201
        self.calls.append(activity_summary)
        self.masked_frames_seen = tuple(masked_frames)
        return self._result


def _write_stills(rec_dir: Path, tss) -> None:
    """Write DISTINCT gradient JPEGs at the given epoch-second timestamps.

    Distinct phases keep the frame-egress dedup from collapsing them; each ts also
    matches a ``screenshot`` row seeded by ``_make_local_recording`` at ``cs+off``
    so the structural orphan cross-check sees on-disk coverage."""
    from PIL import Image

    shots = rec_dir / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    for i, ts in enumerate(tss):
        size = 32
        img = Image.new("RGB", (size, size))
        px = img.load()
        for x in range(size):
            for y in range(size):
                v = int(255 * (((x + i * 6) % size) / size))
                px[x, y] = (v, v, v)
        img.save(shots / f"{ts}.jpg", "JPEG", quality=85)


def _stub_vision(monkeypatch) -> None:
    """Vision-free residual detector + plaintext stills (no corpus key needed)."""
    monkeypatch.setattr(
        "screencap.segmentation.frame_egress._build_default_detector",
        lambda: (lambda _b: []),
    )
    import screencap.config as config

    monkeypatch.setattr(config, "get_corpus_encrypted", lambda: False)


# The three in-window screenshot timestamps ``_make_local_recording`` seeds a row
# for (chunk_start=1000 + event_offsets 10/20/30); the session window is
# [1000, 4600). 5000 is deliberately AFTER session_end to prove window scoping.
_IN_WINDOW_STILLS = [1010.0, 1020.0, 1030.0]
_OUT_OF_WINDOW_STILL = 5000.0


@pytest.mark.privacy
def test_summary_cloud_gate_on_vision_attaches_scoped_frames(tmp_path, monkeypatch):
    """Gate ON + a vision-capable cloud provider + on-disk ALLOW frames → the SUMMARY
    cloud provider's segment() is handed masked frames scoped to the SESSION WINDOW
    (a still after session_end never ships), each carrying the masked marker."""
    rec_dir = _make_local_recording(tmp_path)
    _write_stills(rec_dir, [*_IN_WINDOW_STILLS, _OUT_OF_WINDOW_STILL])
    _stub_vision(monkeypatch)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="gemini", summary_cloud_consent=True, frames_cloud_consent=True,
    ))
    cloud = _FramesCloudProvider(_cloud_named_tasks())
    _install_cloud_provider(monkeypatch, cloud)

    _run_terminal(rec_dir)

    assert len(cloud.calls) == 1  # the cloud provider named the session
    frames = cloud.masked_frames_seen
    assert frames, "gate ON + vision provider must attach masked frames"
    assert all(f.masked is True for f in frames)
    tss = {f.timestamp_ms for f in frames}
    # Every attached frame is inside the session window; the out-of-window still
    # (5000s, after session_end) is NEVER attached.
    assert tss <= {1_010_000, 1_020_000, 1_030_000}
    assert 5_000_000 not in tss


@pytest.mark.privacy
def test_summary_cloud_gate_off_carries_zero_frames(tmp_path, monkeypatch):
    """AE1: frames opt-in OFF + a vision-capable cloud provider + SUMMARY resolves
    CLOUD → the cloud provider names the session but rides ZERO frames (connecting a
    provider / cloud-tasks-on is NOT the frames consent)."""
    rec_dir = _make_local_recording(tmp_path)
    _write_stills(rec_dir, _IN_WINDOW_STILLS)
    _stub_vision(monkeypatch)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="gemini", summary_cloud_consent=True, frames_cloud_consent=False,
    ))
    cloud = _FramesCloudProvider(_cloud_named_tasks())
    _install_cloud_provider(monkeypatch, cloud)

    _run_terminal(rec_dir)

    assert len(cloud.calls) == 1  # the session WAS named by cloud...
    assert cloud.masked_frames_seen == ()  # ...but with ZERO frames (AE1).


@pytest.mark.privacy
def test_summary_cloud_nonvision_provider_is_text_only(tmp_path, monkeypatch):
    """Gate ON + a NON-vision cloud provider → text only, no frames, no error: the
    frame egress is never even attempted (trip-wired) and naming still succeeds."""
    rec_dir = _make_local_recording(tmp_path)
    _write_stills(rec_dir, _IN_WINDOW_STILLS)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True, frames_cloud_consent=True,
    ))
    # A plain _FakeProvider has no supports_frames attr → getattr False.
    cloud = _FakeProvider(_cloud_named_tasks())
    _install_cloud_provider(monkeypatch, cloud)

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("frame egress attempted for a non-vision provider")

    monkeypatch.setattr(
        "screencap.segmentation.frame_egress.produce_egress_frames", _boom
    )

    _run_terminal(rec_dir)

    assert len(cloud.calls) == 1
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["Cloud-named session"]


@pytest.mark.privacy
def test_day_split_heuristic_builds_no_frames(tmp_path, monkeypatch):
    """Regression: the DAY_SPLIT boundary path (idle-gap heuristic, no cloud) builds
    NO frame evidence — frames ride ONLY on the consented SUMMARY cloud task."""
    rec_dir = _make_local_recording(tmp_path)
    _write_stills(rec_dir, _IN_WINDOW_STILLS)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    _install_consent(monkeypatch, ConsentPolicy())  # no cloud → heuristic boundaries.

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("frame egress attempted on the day-split/heuristic path")

    monkeypatch.setattr(
        "screencap.segmentation.frame_egress.produce_egress_frames", _boom
    )

    _run_terminal(rec_dir)

    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["", ""]  # U2: mechanical task_N never surfaces; honest UNNAMED blocks


@pytest.mark.privacy
def test_on_device_available_builds_no_frames(tmp_path, monkeypatch):
    """AE3: an on-device model produced day-split tasks (USE_PROVIDER) → the SUMMARY
    cloud fallback is never reached, so NO masked frames are built at all — even with
    the frames opt-in on and a vision provider configured."""
    rec_dir = _make_local_recording(tmp_path)
    _write_stills(rec_dir, _IN_WINDOW_STILLS)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))  # on-device RAN
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="gemini", summary_cloud_consent=True, frames_cloud_consent=True,
    ))

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("frame egress attempted while on-device produced tasks")

    monkeypatch.setattr(
        "screencap.segmentation.frame_egress.produce_egress_frames", _boom
    )

    _run_terminal(rec_dir)

    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["Implement auth module"]


@pytest.mark.privacy
def test_summary_cloud_frames_on_device_target_builds_nothing(monkeypatch):
    """AE3 (unit): ``_summary_cloud_frames`` for a NON-cloud target returns () and
    never attempts frame egress — ``frames_may_attach`` is false off the CLOUD row."""
    import screencap.terminal_stage as ts
    from screencap.segmentation.consent import ExecutionTarget

    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("produce_egress_frames called for a non-CLOUD target")

    monkeypatch.setattr(
        "screencap.segmentation.frame_egress.produce_egress_frames", _boom
    )

    class _Vision:
        supports_frames = True

    policy = ConsentPolicy(cloud_provider="gemini", frames_cloud_consent=True)
    frames = ts._summary_cloud_frames(
        Path("/nonexistent"), (0.0, 10.0), _Vision(), policy, ExecutionTarget.ON_DEVICE
    )
    assert frames == ()


# ---------------------------------------------------------------------------
# SCR-275 U6 (KTD-1/KTD-8) — partial success as a first-class outcome, the
# distinct unavailable-reason detail, and the ``stopped`` quiesce special-case.
# ---------------------------------------------------------------------------


def _mixed_source_tasks() -> dict:
    """An on-device pipeline result mixing model and mechanical task sources.

    ``source`` values mirror ``ondevice_pipeline.SOURCE_MODEL`` /
    ``SOURCE_MECHANICAL`` — the per-task provenance the U4 orchestrator emits and
    the terminal stage must read BEFORE ``_persist_local_tasks`` rewrites
    row-level ownership to ``agent``.
    """
    return {
        "tasks": [
            {"start_ts": 1000.0, "end_ts": 2000.0,
             "name": "Draft launch email", "derived_name": "draft-launch-email",
             "category": "communication", "source": "ondevice_model"},
            {"start_ts": 2000.0, "end_ts": 2800.0,
             "name": "task_2", "derived_name": "task-2",
             "category": "other", "source": "idle_gap_heuristic"},
        ],
        "summary": {"overview": "A partly-named day.", "primary_focus": "communication",
                    "time_breakdown": {}, "key_accomplishments": []},
        "tags": [],
    }


def _all_model_tasks() -> dict:
    """An on-device pipeline result whose every task is model-named."""
    return {
        "tasks": [
            {"start_ts": 1000.0, "end_ts": 2800.0,
             "name": "Write design doc", "derived_name": "write-design-doc",
             "category": "writing", "source": "ondevice_model"},
        ],
        "summary": {"overview": "Wrote the doc.", "primary_focus": "writing",
                    "time_breakdown": {}, "key_accomplishments": []},
        "tags": [],
    }


@pytest.mark.privacy
def test_partial_pass_records_partial_with_detail_and_skips_cloud_fallback(
    tmp_path, monkeypatch,
):
    """KTD-1/KTD-8: a mixed-source pass is a REAL provider result — it records
    ``produced_tasks_partial`` (with the dominant per-window failure reason as
    detail) and must NOT consult the consented cloud-summary fallback, which
    fires only on true PROVIDER_UNAVAILABLE."""
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(
        monkeypatch, _ReasonProvider(_mixed_source_tasks(), reason="context-window"),
    )
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True,
    ))
    cloud = _FakeProvider(_cloud_named_tasks())
    _install_cloud_provider(monkeypatch, cloud)

    result = _run_terminal(rec_dir)

    assert cloud.calls == []  # partial ≠ unavailable → no cloud-summary fallback.
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["Draft launch email", ""]  # U2: the mechanical fill is an UNNAMED block
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.get_recording_outcome() == "produced_tasks_partial"
    assert ledger.get_recording_outcome_detail() == "context-window"
    assert result.tasks_persisted == 2


@pytest.mark.privacy
def test_all_model_pass_records_produced_with_no_detail(tmp_path, monkeypatch):
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _ReasonProvider(_all_model_tasks(), reason=None))
    _install_consent(monkeypatch, ConsentPolicy())

    _run_terminal(rec_dir)

    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.get_recording_outcome() == "produced_tasks"
    assert ledger.get_recording_outcome_detail() is None


@pytest.mark.privacy
def test_unavailable_sentinel_records_mechanical_with_distinct_detail(
    tmp_path, monkeypatch,
):
    """R8: the zero-model-names sentinel path still consults the consented
    cloud-summary fallback, then the heuristic — and the outcome carries the
    DISTINCT unavailable reason as detail (context-window ≠ intelligence-off)."""
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(
        monkeypatch, _ReasonProvider(PROVIDER_UNAVAILABLE, reason="context-window"),
    )
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True,
    ))
    cloud = _FakeProvider(None)  # consulted, declines → heuristic.
    _install_cloud_provider(monkeypatch, cloud)

    _run_terminal(rec_dir)

    assert len(cloud.calls) == 1  # true unavailability DOES consult cloud.
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["", ""]  # U2: mechanical task_N never surfaces; honest UNNAMED blocks
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.get_recording_outcome() == "mechanical_only"
    assert ledger.get_recording_outcome_detail() == "context-window"


@pytest.mark.privacy
def test_stopped_sentinel_skips_fallbacks_and_stays_provisional(tmp_path, monkeypatch):
    """The ``stopped`` pseudo-reason marks a quiesce-interrupted pre-work pass —
    NOT a real unavailability. The terminal stage must consult NEITHER the
    cloud-summary fallback nor the heuristic (nothing is persisted), and the
    outcome maps through the picker's provisional live branch (``in_progress``)
    so the re-run on resume records the real outcome."""
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(
        monkeypatch, _ReasonProvider(PROVIDER_UNAVAILABLE, reason="stopped"),
    )
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True,
    ))
    cloud = _FakeProvider(_cloud_named_tasks())
    _install_cloud_provider(monkeypatch, cloud)

    _run_terminal(rec_dir)

    assert cloud.calls == []  # a quiesce interrupt never consults cloud.
    assert not (rec_dir / "tasks.json").exists()  # no mechanical overwrite either.
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.get_recording_outcome() == "in_progress"
    assert ledger.get_recording_outcome_detail() is None


@pytest.mark.privacy
def test_quiesced_local_finalize_raises_instead_of_completing(tmp_path, monkeypatch):
    """SCR-279: a ``storage.lock`` that quiesces DURING the local naming pass must
    halt the finalize, not complete it. The stopped arm records a provisional
    ``in_progress`` and returns; the LOCAL branch's post-segmentation ``_check_stop``
    then raises :class:`TerminalStageInterrupted` — so retention is skipped and the
    recording is left for the startup/unlock resume to re-run (rather than settling
    as a completed-but-``in_progress`` recording the Journal shows "still
    processing…" forever)."""
    import threading

    from screencap.pipeline_state import PipelineLedger
    from screencap.terminal_stage import TerminalStageInterrupted

    rec_dir = _make_local_recording(tmp_path)

    # A provider that TRIPS the quiesce mid-pass (as the on-device pipeline does on
    # detecting the stop_event) and reports the ``stopped`` pseudo-reason.
    stop_event = threading.Event()

    class _QuiescingProvider(_ReasonProvider):
        def segment(self, activity_summary):  # noqa: ANN001, ANN201
            stop_event.set()
            return super().segment(activity_summary)

    _install_provider(
        monkeypatch, _QuiescingProvider(PROVIDER_UNAVAILABLE, reason="stopped"),
    )
    _install_consent(monkeypatch, ConsentPolicy())

    # Retention must NOT run once the finalize is interrupted.
    from screencap import terminal_stage as ts

    retention_calls: list[object] = []
    real_retention = ts._apply_retention
    monkeypatch.setattr(
        ts, "_apply_retention",
        lambda *a, **k: retention_calls.append(a) or real_retention(*a, **k),
    )

    with pytest.raises(TerminalStageInterrupted):
        ts.run_terminal_stage(rec_dir, stop_event=stop_event)

    assert retention_calls == []  # interrupted before retention.
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.get_recording_outcome() == "in_progress"  # re-runs on resume.


@pytest.mark.privacy
def test_stopped_sentinel_keeps_prior_partial_and_its_detail(tmp_path, monkeypatch):
    """Monotonic: a stopped pass never downgrades a recorded partial — and never
    clobbers its stored detail with NULL."""
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    ledger = PipelineLedger(rec_dir / "recording.db")
    ledger.set_recording_outcome("produced_tasks_partial", detail="context-window")
    _install_provider(
        monkeypatch, _ReasonProvider(PROVIDER_UNAVAILABLE, reason="stopped"),
    )
    _install_consent(monkeypatch, ConsentPolicy())

    _run_terminal(rec_dir)

    assert ledger.get_recording_outcome() == "produced_tasks_partial"
    assert ledger.get_recording_outcome_detail() == "context-window"


@pytest.mark.privacy
def test_live_sequence_produced_then_mixed_recomputes_to_partial(
    tmp_path, monkeypatch,
):
    """KTD-8 live recompute: an early all-model live pass records produced; a
    later live tick whose grown window set includes one mechanical task moves
    the outcome to partial (produced → partial IS allowed on live)."""
    from screencap import terminal_stage as ts
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_consent(monkeypatch, ConsentPolicy())
    ledger = PipelineLedger(rec_dir / "recording.db")

    _install_provider(monkeypatch, _ReasonProvider(_all_model_tasks(), reason=None))
    ts.run_incremental_segmentation(rec_dir)
    assert ledger.get_recording_outcome() == "produced_tasks"

    _install_provider(
        monkeypatch, _ReasonProvider(_mixed_source_tasks(), reason="decoding-failure"),
    )
    ts.run_incremental_segmentation(rec_dir)
    assert ledger.get_recording_outcome() == "produced_tasks_partial"
    assert ledger.get_recording_outcome_detail() == "decoding-failure"
    # The mixed pass IS persisted (partial keeps its model names + mechanical fill).
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["Draft launch email", ""]  # U2: the mechanical fill is an UNNAMED block


@pytest.mark.privacy
def test_mechanical_pass_never_overwrites_partial_rows(tmp_path, monkeypatch):
    """The monotonic row gate extends to partial: once a recording holds a
    partially-model-named task set, a later fully-mechanical pass must not
    overwrite those rows, and the reason stays partial."""
    from screencap import terminal_stage as ts
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_consent(monkeypatch, ConsentPolicy())
    ledger = PipelineLedger(rec_dir / "recording.db")

    _install_provider(
        monkeypatch, _ReasonProvider(_mixed_source_tasks(), reason="context-window"),
    )
    ts.run_incremental_segmentation(rec_dir)
    assert ledger.get_recording_outcome() == "produced_tasks_partial"
    rows_before = [s.name for s in ledger.read_task_segments()]
    assert rows_before == ["Draft launch email", ""]  # U2: the mechanical fill is an UNNAMED block

    # Next live tick degrades all the way to the heuristic (provider unavailable).
    _install_provider(
        monkeypatch, _ReasonProvider(PROVIDER_UNAVAILABLE, reason="respond-failed"),
    )
    ts.run_incremental_segmentation(rec_dir)

    assert [s.name for s in ledger.read_task_segments()] == rows_before
    assert ledger.get_recording_outcome() == "produced_tasks_partial"
    # The stored detail explains the RECORDED partial — the mechanical pass's
    # fresh failure reason must not have replaced it.
    assert ledger.get_recording_outcome_detail() == "context-window"


@pytest.mark.privacy
def test_finalize_partial_pass_never_overwrites_produced_rows(tmp_path, monkeypatch):
    """Finalize is strictly monotonic (partial never downgrades produced) — so
    a finalize PARTIAL pass over a ``produced_tasks`` prior must ALSO skip
    persisting its rows: the recorded reason and the stored rows stay in
    agreement (no produced label above a partly-mechanical list). A LIVE
    partial pass keeps persisting (the deliberate recompute — covered by
    ``test_live_sequence_produced_then_mixed_recomputes_to_partial``)."""
    from screencap import terminal_stage as ts
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_consent(monkeypatch, ConsentPolicy())
    ledger = PipelineLedger(rec_dir / "recording.db")

    _install_provider(monkeypatch, _ReasonProvider(_all_model_tasks(), reason=None))
    ts.run_incremental_segmentation(rec_dir)
    assert ledger.get_recording_outcome() == "produced_tasks"
    rows_before = [s.name for s in ledger.read_task_segments()]
    assert rows_before == ["Write design doc"]

    _install_provider(
        monkeypatch, _ReasonProvider(_mixed_source_tasks(), reason="decoding-failure"),
    )
    _run_terminal(rec_dir)  # finalize with a mixed-source provider result

    assert [s.name for s in ledger.read_task_segments()] == rows_before
    assert ledger.get_recording_outcome() == "produced_tasks"
    assert ledger.get_recording_outcome_detail() is None


# ---------------------------------------------------------------------------
# SCR-275 KTD-10 — the distinct ``stale-scrub`` halt at finalize.
# ---------------------------------------------------------------------------


@pytest.mark.privacy
def test_stale_scrub_at_finalize_retries_once_over_settled_rows(
    tmp_path, monkeypatch,
):
    """A staleness-guard trip at finalize gets ONE immediate same-call retry:
    the scrub that tripped it has committed, so the post-scrub rows are
    settled and the retry succeeds without waiting for a resume."""
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    provider = _SequenceProvider([
        (PROVIDER_UNAVAILABLE, "stale-scrub"),
        (_all_model_tasks(), None),
    ])
    _install_provider(monkeypatch, provider)
    _install_consent(monkeypatch, ConsentPolicy())

    _run_terminal(rec_dir)

    assert len(provider.calls) == 2  # exactly one same-call retry
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == ["Write design doc"]
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.get_recording_outcome() == "produced_tasks"


@pytest.mark.privacy
def test_stale_scrub_second_trip_stays_provisional(tmp_path, monkeypatch):
    """A second staleness trip takes the provisional path: no further retry,
    neither the cloud-summary fallback nor the heuristic runs, nothing is
    persisted, and the outcome settles as ``in_progress`` (re-runs on the
    startup/unlock reconcile) — never ``nothing_to_name``."""
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    provider = _SequenceProvider([
        (PROVIDER_UNAVAILABLE, "stale-scrub"),
        (PROVIDER_UNAVAILABLE, "stale-scrub"),
    ])
    _install_provider(monkeypatch, provider)
    _install_consent(monkeypatch, ConsentPolicy(
        cloud_provider="openai", summary_cloud_consent=True,
    ))
    cloud = _FakeProvider(_cloud_named_tasks())
    _install_cloud_provider(monkeypatch, cloud)

    _run_terminal(rec_dir)

    assert len(provider.calls) == 2  # one retry, then the provisional path
    assert cloud.calls == []  # a halted pass never consults cloud
    assert not (rec_dir / "tasks.json").exists()  # no mechanical fallback either
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.get_recording_outcome() == "in_progress"
    assert ledger.get_recording_outcome_detail() is None
