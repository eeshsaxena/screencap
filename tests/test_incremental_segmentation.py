"""Tests for U6 — incremental segmentation of a LIVE ambient day (R6, R7, KTD3/4).

Scope (the U6 test scenarios), all over a LOCAL recording so nothing can upload:

* A mid-recording pass produces agent task rows; a second pass over the same
  footage REFRESHES the agent rows without duplicating (idempotent scoped replace).
* The carve-out invariant (the KEY test): a protected user / user-edited span
  survives every pass AND no fresh ``source='agent'`` span overlaps it — dropping
  overlapping agent tasks, and clearing a stale agent row a user has since marked.
* Fail-CLOSED R11 (privacy lane, Vision-free): a mid-write / partial DB read never
  yields a task covering a masked interval — the blocked-interval derivation fails
  closed, so no content from the masked span reaches the provider or a task.
* No on-device model / provider unavailable → the idle-gap heuristic still names
  tasks (Covers AE5 / AE2).
* Provider error → prior tasks unchanged (fail-open on the PROVIDER).
* A concurrent terminal-stage finalize (flock held) → the incremental pass SKIPS
  (non-blocking flock), leaving prior tasks intact.

Fixtures mirror ``tests/test_terminal_stage_degradation.py`` (a LOCAL recording
dir with recording.db + ledger + on-disk chunk artifacts, with two idle-separated
action-event clusters so the idle-gap heuristic has something to split), but drive
``run_incremental_segmentation`` (the U6 live entry) rather than the full
``run_terminal_stage`` finalize.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from screencap.segmentation.consent import ConsentPolicy
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Two clusters of action events split by a > rest_threshold (120s default) gap,
# so the idle-gap heuristic yields exactly two tasks over the whole recording.
_CLUSTER_A = (10.0, 12.0, 15.0)
_CLUSTER_B = (400.0, 402.0, 405.0)


def _make_local_recording(
    tmp_path: Path, *, destination: str = "local", n_chunks: int = 1,
    name: str = "ambient-rec",
) -> Path:
    """Create a LOCAL recording dir with recording.db + ledger + chunk artifacts.

    Writes real v2 manifests + events the LocalActivitySource reads, plus
    ``action_event`` rows in two idle-separated clusters for the heuristic, and a
    benign (UNKNOWN → ALLOW) window + surviving screenshot rows per chunk so the
    fail-closed R11 strip has canonical coverage (mirrors the U4/U7 fixtures).
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
        (rec_dir / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 256)
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
    # NB: chunks_expected is NOT frozen — a LIVE ambient recording keeps growing;
    # the incremental pass segments over whatever completed manifests exist.

    (rec_dir / ".recording_intent").write_text(json.dumps({
        "version": 2,
        "destination": destination,
        "retention_policy": "keep_forever",
        "retention_params": {},
        "show_on_website": False,
    }))
    (rec_dir / ".recording_id").write_text(name)
    return rec_dir


def _canned_tasks(session_start: float = 1000.0) -> dict:
    """A validated-shape provider tasks dict spanning two adjacent halves."""
    return {
        "tasks": [
            {"start_ts": session_start, "end_ts": session_start + 1800.0,
             "name": "Implement auth module", "derived_name": "implement-auth-module",
             "description": "Wrote auth.py", "category": "development",
             "apps_used": ["VS Code"], "confidence": "high"},
            {"start_ts": session_start + 1800.0, "end_ts": session_start + 3600.0,
             "name": "Coordinate PR review", "derived_name": "coordinate-pr-review",
             "description": "Pinged team", "category": "communication",
             "apps_used": ["Slack"], "confidence": "medium"},
        ],
        "summary": {"overview": "Built auth then coordinated review.",
                    "primary_focus": "development", "time_breakdown": {},
                    "key_accomplishments": []},
        "tags": ["python", "auth"],
    }


class _FakeProvider:
    """A canned provider — records the summaries it was handed, returns a fixed result."""

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
    """Wire a fake provider into the day-split routing seam.

    The builder now receives the SCR-275 per-recording context
    (``recording_dir`` / ``stop_event`` / ``is_live``); the fake ignores it.
    """
    import screencap.segmentation.routing as routing

    monkeypatch.setattr(
        routing, "build_day_split_provider", lambda *a, **k: provider,
    )


def _install_consent(monkeypatch, policy: ConsentPolicy) -> None:
    """Force ``ConsentPolicy.from_config`` + ``get_llm_cloud_provider`` to a policy."""
    import screencap.config as config
    import screencap.segmentation.consent as consent

    monkeypatch.setattr(
        consent.ConsentPolicy, "from_config", classmethod(lambda cls: policy),
    )
    monkeypatch.setattr(
        config, "get_llm_cloud_provider", lambda: policy.cloud_provider,
    )


def _ledger(rec_dir: Path):
    from screencap.pipeline_state import PipelineLedger

    return PipelineLedger(rec_dir / "recording.db")


def _run_incremental(rec_dir: Path, **kw):
    from screencap import terminal_stage as ts

    return ts.run_incremental_segmentation(rec_dir, **kw)


# ---------------------------------------------------------------------------
# Mid-recording pass produces agent rows; a second pass refreshes, not duplicates.
# ---------------------------------------------------------------------------


def test_midrecording_pass_produces_agent_tasks(tmp_path, monkeypatch):
    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))

    result = _run_incremental(rec_dir)

    assert result.routed is True
    assert result.destination == "local"
    assert result.tasks_persisted == 2

    segs = _ledger(rec_dir).read_task_segments()
    assert [s.name for s in segs] == ["Implement auth module", "Coordinate PR review"]
    assert all(s.source == "agent" for s in segs)
    assert all(s.edited is False for s in segs)
    assert [s.task_index for s in segs] == [0, 1]

    # tasks.json written mid-recording too (the app-readable mirror).
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert [t["name"] for t in persisted["tasks"]] == [
        "Implement auth module", "Coordinate PR review",
    ]


def test_second_pass_refreshes_agent_rows_without_duplicating(tmp_path, monkeypatch):
    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))

    _run_incremental(rec_dir)
    _run_incremental(rec_dir)  # second pass over more footage — REPLACE, not append.

    segs = _ledger(rec_dir).read_task_segments()
    assert len(segs) == 2  # not 4
    assert [s.task_index for s in segs] == [0, 1]
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert len(persisted["tasks"]) == 2


# ---------------------------------------------------------------------------
# THE carve-out invariant (KTD3, R8): user/edited spans survive AND no fresh
# agent span overlaps a protected one.
# ---------------------------------------------------------------------------


def test_user_span_survives_and_no_agent_span_overlaps_it(tmp_path, monkeypatch):
    """U2 KTD-7 TRIM: a user span in the MIDDLE of an agent block trims that block
    to abut the curated span cleanly (splitting it into two abutting pieces),
    instead of DROPPING the whole block. The no-overlap invariant still holds."""
    from screencap.pipeline_state import TaskSegmentRow

    rec_dir = _make_local_recording(tmp_path)
    ledger = _ledger(rec_dir)
    # A user task in the MIDDLE of the FIRST canned agent block [1000, 2800).
    ledger.insert_task_segment(TaskSegmentRow(
        task_index=0, start_ts=1500.0, end_ts=2000.0, name="My standup meeting",
    ))
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))

    _run_incremental(rec_dir)

    segs = ledger.read_task_segments()
    user_rows = [s for s in segs if s.source == "user"]
    agent_rows = [s for s in segs if s.source == "agent"]

    # The user row survived untouched.
    assert [s.name for s in user_rows] == ["My standup meeting"]
    assert user_rows[0].start_ts == 1500.0 and user_rows[0].end_ts == 2000.0

    # KTD-7: the overlapped block was TRIMMED to abut the user span (two pieces),
    # not dropped; the second (non-overlapping) block is untouched.
    assert sorted((s.start_ts, s.end_ts) for s in agent_rows) == [
        (1000.0, 1500.0), (2000.0, 2800.0), (2800.0, 4600.0),
    ]
    assert [
        s.name for s in agent_rows if s.start_ts < 2800.0
    ] == ["Implement auth module", "Implement auth module"]

    # INVARIANT: no agent span overlaps the protected user span.
    for a in agent_rows:
        assert not (a.start_ts < 2000.0 and 1500.0 < a.end_ts), (
            f"agent span [{a.start_ts},{a.end_ts}) overlaps protected user span"
        )

    # A second pass keeps the invariant and does not duplicate.
    _run_incremental(rec_dir)
    segs2 = ledger.read_task_segments()
    assert [s.name for s in segs2 if s.source == "user"] == ["My standup meeting"]
    agent2 = sorted((s.start_ts, s.end_ts) for s in segs2 if s.source == "agent")
    assert agent2 == [(1000.0, 1500.0), (2000.0, 2800.0), (2800.0, 4600.0)]


def test_edited_agent_span_is_protected_from_overlap(tmp_path, monkeypatch):
    """A user-EDITED agent row (edited=1, re-homed to the HIGH range) is protected
    just like a user row: the next pass drops any fresh agent span overlapping it."""
    rec_dir = _make_local_recording(tmp_path)
    ledger = _ledger(rec_dir)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))

    _run_incremental(rec_dir)  # agent rows at index 0 [1000,2800), 1 [2800,4600).
    # The user edits the first agent task (rename) — marks it edited, re-homes it.
    ledger.update_task_segment(0, name="Renamed by me", mark_edited=True)

    _run_incremental(rec_dir)  # provider returns the same two; index-0 overlaps.

    segs = ledger.read_task_segments()
    edited = [s for s in segs if s.edited]
    agent_unedited = [s for s in segs if s.source == "agent" and not s.edited]

    # The edited row survived with the user's name.
    assert [s.name for s in edited] == ["Renamed by me"]
    # No unedited agent span overlaps the protected edited span [1000,2800).
    for a in agent_unedited:
        assert not (a.start_ts < 2800.0 and 1000.0 < a.end_ts), (
            f"agent span [{a.start_ts},{a.end_ts}) overlaps protected edited span"
        )
    # Only the non-overlapping second task remains as a fresh agent row.
    assert [s.name for s in agent_unedited] == ["Coordinate PR review"]


def test_stale_agent_row_over_newly_marked_user_span_is_cleared(tmp_path, monkeypatch):
    """A user marks a span AFTER an agent pass wrote a row overlapping it; the next
    pass must CLEAR that stale agent row so no agent span overlaps the user span."""
    from screencap.pipeline_state import TaskSegmentRow

    rec_dir = _make_local_recording(tmp_path)
    ledger = _ledger(rec_dir)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))

    _run_incremental(rec_dir)  # 2 agent rows spanning [1000,2800) + [2800,4600).
    # Now the user marks an all-day span overlapping BOTH agent rows.
    ledger.insert_task_segment(TaskSegmentRow(
        task_index=0, start_ts=900.0, end_ts=5000.0, name="All-day session",
    ))

    _run_incremental(rec_dir)  # provider returns the same two → both carved out.

    segs = ledger.read_task_segments()
    assert [s.source for s in segs] == ["user"]  # only the user row survives
    assert [s for s in segs if s.source == "agent"] == []


# ---------------------------------------------------------------------------
# Fail-CLOSED R11 (privacy lane, Vision-free): a partial DB read never yields a
# task covering a masked interval.
# ---------------------------------------------------------------------------


def _leaky_full_span_task() -> dict:
    """A provider result naming the WHOLE recording as one task [1000, 4600).

    It covers the sub-interval [2000, 3000) — if the R11 strip under-blocks, a task
    covering masked content is persisted (the leak the fail-closed strip prevents).
    """
    return {
        "tasks": [
            {"start_ts": 1000.0, "end_ts": 4600.0, "name": "Whole session",
             "derived_name": "whole-session", "category": "development",
             "apps_used": ["VS Code"], "confidence": "high"},
        ],
        "summary": {"overview": "One long session.", "primary_focus": "development",
                    "time_breakdown": {}, "key_accomplishments": []},
        "tags": [],
    }


@pytest.mark.privacy
def test_baseline_leaky_task_persisted_without_fail_closed(tmp_path, monkeypatch):
    """Proof-first: with a NORMAL (fully-covered) DB the provider IS handed a
    summary and its whole-span task IS persisted covering [2000,3000) — so the
    fail-closed test's absence of that task is caused by the strip, not by the
    provider never producing one."""
    rec_dir = _make_local_recording(tmp_path)
    provider = _FakeProvider(_leaky_full_span_task())
    _install_provider(monkeypatch, provider)
    _install_consent(monkeypatch, ConsentPolicy())

    _run_incremental(rec_dir)

    assert len(provider.calls) == 1  # the provider WAS handed a summary.
    segs = _ledger(rec_dir).read_task_segments()
    # A task covering the (to-be-masked) interval [2000,3000) is persisted.
    assert any(s.start_ts <= 2500.0 <= s.end_ts for s in segs)


@pytest.mark.privacy
def test_partial_db_fail_closed_yields_no_task_over_masked_interval(
    tmp_path, monkeypatch,
):
    """A mid-write / partial canonical read of the live recording.db makes the R11
    blocked-interval derivation RAISE; the strip must fail CLOSED (all content
    treated as blocked). So the summary carries no activity, the provider is never
    handed masked content, and no task covering the masked interval is persisted."""
    import screencap.backfill.skip_intervals as skip_intervals

    rec_dir = _make_local_recording(tmp_path)
    provider = _FakeProvider(_leaky_full_span_task())
    _install_provider(monkeypatch, provider)
    _install_consent(monkeypatch, ConsentPolicy())

    # Simulate a partial/ambiguous read of the concurrently-written DB: the
    # blocked-interval derivation raises. ``derive_skip_intervals`` is imported at
    # call time inside the strip, so patching the module attribute takes effect.
    def _partial_read(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated partial canonical read (mid-write DB)")

    monkeypatch.setattr(skip_intervals, "derive_skip_intervals", _partial_read)

    result = _run_incremental(rec_dir)

    # Fail-closed: everything is treated as blocked → the summary has no activity →
    # the provider is never handed masked content → no task persisted.
    assert provider.calls == []
    assert result.tasks_persisted == 0
    segs = _ledger(rec_dir).read_task_segments()
    assert not any(s.start_ts <= 2500.0 <= s.end_ts for s in segs)
    assert segs == []


# ---------------------------------------------------------------------------
# No on-device model / provider unavailable → idle-gap heuristic (AE5 / AE2).
# ---------------------------------------------------------------------------


def test_provider_unavailable_falls_back_to_idle_gap_heuristic(tmp_path, monkeypatch):
    """Covers AE5 / AE2: a CLI-only / pre-macOS-26 install (no on-device model)
    still fills today's Journal mid-recording via the idle-gap heuristic.

    U2 day-diary: the idle-gap heuristic's mechanical ``task_N`` names never
    surface as diary block names (R2 / KTD-10). Consolidation folds the two
    mechanical clusters into two honest UNNAMED blocks (empty name + a
    ``name_fallback`` marker); the mechanical signal is carried by the recording
    outcome, not by a mechanical name on the block."""
    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(PROVIDER_UNAVAILABLE))
    _install_consent(monkeypatch, ConsentPolicy())  # no cloud configured.

    result = _run_incremental(rec_dir)

    assert result.tasks_persisted == 2
    segs = _ledger(rec_dir).read_task_segments()
    # Honest UNNAMED blocks — NOT the mechanical ``task_1`` / ``task_2`` names.
    assert [s.name for s in segs] == ["", ""]
    assert all(
        json.loads(s.metadata).get("name_fallback") == "mechanical" for s in segs
    )
    assert all(s.source == "agent" for s in segs)
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert persisted["summary"]["source"] == "idle_gap_heuristic"
    # Boundaries reflect the two seeded clusters.
    assert persisted["tasks"][0]["start_ts"] == 1000.0 + _CLUSTER_A[0]
    assert persisted["tasks"][1]["start_ts"] == 1000.0 + _CLUSTER_B[0]


# ---------------------------------------------------------------------------
# Provider error → prior tasks unchanged (fail-open on the PROVIDER).
# ---------------------------------------------------------------------------


def test_provider_error_leaves_prior_tasks_unchanged(tmp_path, monkeypatch):
    rec_dir = _make_local_recording(tmp_path)
    # First pass with a working provider seeds two agent tasks.
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))
    _run_incremental(rec_dir)
    before = _ledger(rec_dir).read_task_segments()
    assert len(before) == 2

    # Second pass: the provider builder RAISES → fail-open, tasks untouched.
    import screencap.segmentation.routing as routing

    def _raise(*a, **k):
        raise RuntimeError("provider build exploded")

    monkeypatch.setattr(routing, "build_day_split_provider", _raise)

    result = _run_incremental(rec_dir)

    assert result.tasks_persisted == 0
    after = _ledger(rec_dir).read_task_segments()
    assert [s.name for s in after] == [s.name for s in before]  # unchanged.


def test_provider_none_leaves_prior_tasks_unchanged(tmp_path, monkeypatch):
    """A genuine empty provider result (ran, produced nothing) is fail-open: it
    does NOT clear existing agent rows (distinct from the carve-out clearing)."""
    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))
    _run_incremental(rec_dir)

    _install_provider(monkeypatch, _FakeProvider(None))
    _install_consent(monkeypatch, ConsentPolicy())
    result = _run_incremental(rec_dir)

    assert result.tasks_persisted == 0
    after = _ledger(rec_dir).read_task_segments()
    assert len(after) == 2  # prior agent rows intact.


# ---------------------------------------------------------------------------
# Concurrent terminal-stage finalize (flock held) → incremental pass skips.
# ---------------------------------------------------------------------------


def test_incremental_pass_skips_when_flock_held(tmp_path, monkeypatch):
    """A concurrent finalize/upload holds the per-recording terminal flock; the
    non-blocking incremental pass must raise TerminalStageBusy and write nothing
    rather than racing the holder's own segmentation (AE12)."""
    import screencap.terminal_stage as ts

    rec_dir = _make_local_recording(tmp_path)
    # Seed prior agent tasks so we can assert they are untouched by the skip.
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))
    _run_incremental(rec_dir)
    before = _ledger(rec_dir).read_task_segments()

    # A different provider that, if run, would rewrite the tasks — proving the
    # skip prevented any write.
    _install_provider(monkeypatch, _FakeProvider(_leaky_full_span_task()))

    # Hold the terminal flock (as a live finalize would), then attempt the pass.
    with ts.terminal_lock(rec_dir.name):
        with pytest.raises(ts.TerminalStageBusy):
            _run_incremental(rec_dir, non_blocking=True)

    # Prior tasks are untouched — the pass wrote nothing while the flock was held.
    after = _ledger(rec_dir).read_task_segments()
    assert [s.name for s in after] == [s.name for s in before]


# ---------------------------------------------------------------------------
# SCR-275 U4 wiring: the per-recording context reaches the day-split provider,
# and the on-device heuristic-first pipeline runs end-to-end via a fake helper.
# ---------------------------------------------------------------------------


@pytest.mark.privacy
def test_incremental_pass_threads_recording_context(tmp_path, monkeypatch):
    """The tick path hands recording_dir + stop_event + is_live=True to the
    day-split builder (the KTD-1 transport)."""
    import threading

    import screencap.segmentation.routing as routing

    rec_dir = _make_local_recording(tmp_path)
    captured: dict = {}
    provider = _FakeProvider(_canned_tasks())

    def _build(recording_dir=None, stop_event=None, is_live=False, manifests=None):
        captured.update(
            recording_dir=recording_dir, stop_event=stop_event, is_live=is_live,
            manifests=manifests,
        )
        return provider

    monkeypatch.setattr(routing, "build_day_split_provider", _build)
    ev = threading.Event()

    result = _run_incremental(rec_dir, stop_event=ev)

    assert result.tasks_persisted == 2
    assert captured["recording_dir"] == rec_dir
    assert captured["stop_event"] is ev
    assert captured["is_live"] is True
    # The already-loaded chunk manifests ride the same transport (no re-glob).
    assert captured["manifests"]


@pytest.mark.privacy
def test_finalize_threads_context_with_is_live_false(tmp_path, monkeypatch):
    import screencap.segmentation.routing as routing
    from screencap import terminal_stage as ts

    rec_dir = _make_local_recording(tmp_path)
    captured: dict = {}
    provider = _FakeProvider(_canned_tasks())

    def _build(recording_dir=None, stop_event=None, is_live=False, manifests=None):
        captured.update(recording_dir=recording_dir, is_live=is_live)
        return provider

    monkeypatch.setattr(routing, "build_day_split_provider", _build)

    ts.run_terminal_stage(rec_dir)

    assert captured["recording_dir"] == rec_dir
    assert captured["is_live"] is False


@pytest.mark.privacy
def test_preset_stop_event_interrupts_incremental_pass(tmp_path, monkeypatch):
    """A quiesce already in progress halts the pass before any work (KTD-7)."""
    import threading

    from screencap import terminal_stage as ts

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))
    ev = threading.Event()
    ev.set()

    with pytest.raises(ts.TerminalStageInterrupted):
        _run_incremental(rec_dir, stop_event=ev)

    assert _ledger(rec_dir).read_task_segments() == []


@pytest.mark.privacy
def test_ondevice_pipeline_end_to_end_via_fake_helper(tmp_path, monkeypatch):
    """Full wiring proof: terminal stage → routing → OnDeviceProvider(context) →
    heuristic-first pipeline → fake helper verbs → model-named rows persisted.

    Exercises the REAL R11 strip derivation over the fixture's recording.db
    (the unit suite injects a predicate; this path does not).
    """
    import sys

    import screencap.segmentation.routing as routing
    from screencap import config

    rec_dir = _make_local_recording(tmp_path)

    helper = tmp_path / "fake_helper.py"
    helper.write_text(
        "#!" + sys.executable + "\n"
        + "import json, sys\n"
        + "req = json.load(sys.stdin)\n"
        + "task = req.get('task')\n"
        + "if task == 'arbitrate':\n"
        + "    print(json.dumps({'status': 'ok', 'result': {'merges': []}}))\n"
        + "elif task == 'name-window':\n"
        + "    print(json.dumps({'status': 'ok', 'result':\n"
        + "        {'name': 'Deep work', 'category': 'development'}}))\n"
        + "elif task == 'day-summary':\n"
        + "    print(json.dumps({'status': 'ok', 'result':\n"
        + "        {'overview': 'Focused day', 'tags': ['focus']}}))\n"
        + "else:\n"
        + "    print(json.dumps({'status': 'unavailable',\n"
        + "                      'reason': 'unexpected-legacy-call'}))\n"
    )
    helper.chmod(0o755)
    monkeypatch.setenv("SCREENCAP_ONDEVICE_HELPER", str(helper))
    monkeypatch.setenv("SCREENCAP_ONDEVICE_HELPER_TIMEOUT", "10")
    monkeypatch.setattr(config, "get_llm_provider", lambda: "on-device")
    monkeypatch.setattr(routing, "_downloaded_model_installed", lambda: False)

    result = _run_incremental(rec_dir)

    assert result.tasks_persisted >= 1
    segs = _ledger(rec_dir).read_task_segments()
    assert [s.name for s in segs] == ["Deep work"] * len(segs)
    assert all(s.category == "development" for s in segs)
    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert persisted["summary"]["overview"] == "Focused day"
    assert persisted["tags"] == ["focus"]
    # Per-task provenance marks the model source in the returned/persisted dict
    # metadata path: tasks.json rewrites source to the row-ownership 'agent'
    # (the U2/U6 classifier reads the mix BEFORE persist), so here we assert
    # the names prove the model path ran (mechanical would be task_1 ...).
    assert all(t["name"] == "Deep work" for t in persisted["tasks"])
