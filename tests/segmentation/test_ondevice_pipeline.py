"""U4 orchestrator + naming cache tests (SCR-275, KTD-1/4/6/7/9/10).

Drives :func:`screencap.segmentation.ondevice_pipeline.run_heuristic_pipeline`
with a SCRIPTED fake provider (no subprocess, Vision-free): candidate windows →
arbitration → cached naming loop → assembly → day summary, returning the
preserved tri-state (tasks dict | ``None`` | ``PROVIDER_UNAVAILABLE``).

The recording fixture is a real ``recording.db`` (for the naming cache, the
per-chunk settled state, and the scrub-generation guard) plus in-memory
events/manifests; the R11 strip is injected as a predicate (the same
``blocked_source`` seam ``build_window_digests`` exposes) — the full
recording-dir strip derivation is covered by the wiring tests in
``tests/test_incremental_segmentation.py``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from screencap.segmentation.provider import PROVIDER_UNAVAILABLE
from screencap.segmentation.providers.ondevice import CallResult

pytestmark = pytest.mark.privacy

_REST = 120.0  # explicit rest_threshold so config never matters
_BASE = 1000.0
_CHUNK = 3600.0

# Default cluster layout: three >=60s activity clusters in chunk 0, separated by
# idle gaps > _REST → three heuristic candidate windows.
_CHUNK0_CLUSTERS = (1010.0, 1310.0, 1610.0)


def _never_blocked(_ts: float) -> bool:
    return False


# ---------------------------------------------------------------------------
# Fixture: a recording dir with a real recording.db + in-memory events/manifests
# ---------------------------------------------------------------------------


def _cluster_events(t: float, tag: str) -> list[dict]:
    """One 80s activity cluster: a window.switch + two typed events."""
    return [
        {"type": "window.switch", "timestamp": t,
         "app_bundle_id": "com.microsoft.VSCode",
         "window_title": f"{tag}.py — project"},
        {"type": "key.type", "timestamp": t + 40.0, "text": f"editing {tag}"},
        {"type": "key.type", "timestamp": t + 80.0, "text": f"saving {tag}"},
    ]


class _Rec:
    """A recording fixture: dir + recording.db + in-memory chunks."""

    def __init__(self, rec_dir: Path) -> None:
        self.dir = rec_dir
        self.manifests: list[dict] = []
        self.events_by_chunk: dict[int, list[dict]] = {}
        self.transcripts_by_chunk: dict[int, dict] = {}

    @property
    def db_path(self) -> Path:
        return self.dir / "recording.db"

    def source(self):
        from tests.segmentation._fixtures import InMemorySource

        return InMemorySource(
            dict(self.events_by_chunk), dict(self.transcripts_by_chunk),
        )

    @property
    def session_start(self) -> float:
        return self.manifests[0]["chunk_start"]

    @property
    def session_end(self) -> float:
        return self.manifests[-1]["chunk_end"]

    def ledger(self):
        from screencap.pipeline_state import PipelineLedger

        return PipelineLedger(self.db_path)

    def add_chunk(
        self, index: int, clusters: list[float], *, staged: bool = True,
    ) -> None:
        cs = _BASE + index * _CHUNK
        ce = cs + _CHUNK
        self.manifests.append({
            "format_version": 2, "chunk_index": index,
            "chunk_start": cs, "chunk_end": ce,
            "stats": {"total_events": 3 * len(clusters),
                      "total_window_switches": len(clusters)},
            "blocked_intervals": [],
        })
        self.manifests.sort(key=lambda m: m["chunk_index"])
        events: list[dict] = []
        for j, t in enumerate(clusters):
            events.extend(_cluster_events(t, f"c{index}_{j}"))
        self.events_by_chunk[index] = events
        ledger = self.ledger()
        ledger.seed_chunk(index)
        if staged:
            ledger.mark_staged(index)

    def cache_rows(self) -> list[tuple]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(
                "SELECT window_start, window_end, digest_hash, name, category "
                "FROM ondevice_window_names ORDER BY window_start"
            ).fetchall()
        finally:
            conn.close()


def _make_rec(
    tmp_path: Path, chunk_clusters: dict[int, list[float]] | None = None,
    *, name: str = "rec",
) -> _Rec:
    from screencap.engine.db import create_db
    from screencap.pipeline_state import ensure_pipeline_state_schema

    rec_dir = tmp_path / name
    rec_dir.mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"
    create_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO recording (timestamp, monitor_width, monitor_height, "
        "pixel_ratio, platform) VALUES (?, ?, ?, ?, ?)",
        (_BASE, 1920, 1080, 2.0, "darwin"),
    )
    conn.commit()
    conn.close()
    ensure_pipeline_state_schema(db_path)

    rec = _Rec(rec_dir)
    if chunk_clusters is None:
        chunk_clusters = {0: list(_CHUNK0_CLUSTERS)}
    for idx in sorted(chunk_clusters):
        rec.add_chunk(idx, chunk_clusters[idx])
    return rec


# ---------------------------------------------------------------------------
# Scripted provider (no subprocess)
# ---------------------------------------------------------------------------


_OK_DAY = CallResult({"overview": "A focused day.", "tags": ["focus"]}, None)


class _ScriptedProvider:
    """A stub OnDeviceProvider: scripted CallResults, records every call."""

    def __init__(
        self,
        *,
        arbitrate: CallResult | None = None,
        name_results: list[CallResult] | None = None,
        name_fn=None,
        day: CallResult | None = None,
        name_prefix: str = "",
    ) -> None:
        self._arbitrate = arbitrate
        self._name_results = list(name_results or [])
        self._name_fn = name_fn
        self._day = day
        self._prefix = name_prefix
        self.arbitrate_calls: list[list[str]] = []
        self.name_calls: list[dict] = []
        self.day_calls: list[list[dict]] = []
        self.last_unavailable_reason: str | None = None

    def call_arbitrate(self, window_lines, *, stripped):
        assert stripped is True, "arbitration must carry the strip attestation"
        self.arbitrate_calls.append(list(window_lines))
        if self._arbitrate is not None:
            return self._arbitrate
        return CallResult([], None)

    def call_name_window(self, digest_payload):
        assert digest_payload.get("stripped") is True, (
            "naming payloads must carry the strip attestation"
        )
        self.name_calls.append(digest_payload)
        if self._name_fn is not None:
            return self._name_fn(digest_payload)
        if self._name_results:
            return self._name_results.pop(0)
        return CallResult(
            (f"{self._prefix}Named {len(self.name_calls)}", "development"), None,
        )

    def call_day_summary(self, task_rows):
        self.day_calls.append(list(task_rows))
        if self._day is not None:
            return self._day
        return _OK_DAY


def _run(provider, rec: _Rec, **kw):
    from screencap.segmentation.ondevice_pipeline import run_heuristic_pipeline

    kw.setdefault("is_live", False)
    kw.setdefault("rest_threshold", _REST)
    kw.setdefault("blocked_source", _never_blocked)
    return run_heuristic_pipeline(
        provider, rec.dir, rec.source(), list(rec.manifests),
        session_start=rec.session_start, session_end=rec.session_end,
        **kw,
    )


def _names(result: dict) -> list[str]:
    return [t["name"] for t in result["tasks"]]


def _sources(result: dict) -> list[str]:
    return [t["source"] for t in result["tasks"]]


def _commit_tasks(rec: _Rec, result: dict) -> None:
    """Persist the pass's tasks as unedited agent rows (the terminal-stage sink)."""
    from screencap.pipeline_state import TaskSegmentRow

    rows = [
        TaskSegmentRow(
            task_index=i, start_ts=t["start_ts"], end_ts=t["end_ts"],
            name=t["name"], category=t.get("category"),
        )
        for i, t in enumerate(result["tasks"])
    ]
    rec.ledger().replace_task_segments(rows)


# ---------------------------------------------------------------------------
# AE1: a many-window day, every payload bounded, no context failures
# ---------------------------------------------------------------------------


def test_many_window_day_all_model_named(tmp_path):
    rec = _make_rec(tmp_path, {0: list(_CHUNK0_CLUSTERS), 1: [4700.0]})
    provider = _ScriptedProvider()

    result = _run(provider, rec)

    assert isinstance(result, dict)
    assert len(result["tasks"]) == 4
    assert _sources(result) == ["ondevice_model"] * 4
    assert _names(result) == [f"Named {i}" for i in (1, 2, 3, 4)]
    assert len(provider.name_calls) == 4
    assert len(provider.day_calls) == 1
    assert provider.last_unavailable_reason is None
    # Every naming payload fits the input budget (KTD-5: ~3 chars/token Latin).
    for payload in provider.name_calls:
        body = {k: v for k, v in payload.items() if k != "stripped"}
        assert len(json.dumps(body)) <= 700 * 3
    # Tasks tile the windows in order, with categories + summary + tags.
    starts = [t["start_ts"] for t in result["tasks"]]
    assert starts == sorted(starts)
    assert all(t["category"] == "development" for t in result["tasks"])
    assert result["summary"]["overview"] == "A focused day."
    assert result["tags"] == ["focus"]


# ---------------------------------------------------------------------------
# AE2: one naming failure → partial mix
# ---------------------------------------------------------------------------


def test_one_naming_failure_yields_partial_mix(tmp_path):
    rec = _make_rec(tmp_path)
    ok = CallResult(("Real work", "development"), None)
    provider = _ScriptedProvider(
        name_results=[ok, CallResult(None, "respond-failed"), ok],
    )

    result = _run(provider, rec)

    assert isinstance(result, dict)
    assert _sources(result) == [
        "ondevice_model", "idle_gap_heuristic", "ondevice_model",
    ]
    assert result["tasks"][1]["name"] == "task_2"
    assert result["tasks"][1]["derived_name"] == "task-2"
    # The dominant per-window failure reason rides the provider seam (KTD-8).
    assert provider.last_unavailable_reason == "respond-failed"


# ---------------------------------------------------------------------------
# AE3: arbitration failure / invalid merge list → heuristic boundaries stand
# ---------------------------------------------------------------------------


def test_arbitration_failure_keeps_heuristic_boundaries(tmp_path):
    rec = _make_rec(tmp_path)
    provider = _ScriptedProvider(arbitrate=CallResult(None, "respond-failed"))

    result = _run(provider, rec)

    assert isinstance(result, dict)
    assert len(result["tasks"]) == 3  # heuristic boundaries stood
    assert len(provider.name_calls) == 3  # naming proceeded for every window
    assert _sources(result) == ["ondevice_model"] * 3


@pytest.mark.parametrize("merges", [
    [[0, 2]],          # non-contiguous
    [[0, 1], [1, 2]],  # overlapping
    [[1, 5]],          # out of range
    [[-1, 0]],         # negative
])
def test_invalid_merge_list_keeps_heuristic_boundaries(tmp_path, merges):
    rec = _make_rec(tmp_path)
    provider = _ScriptedProvider(arbitrate=CallResult(merges, None))

    result = _run(provider, rec)

    assert isinstance(result, dict)
    assert len(result["tasks"]) == 3
    assert len(provider.name_calls) == 3


def test_valid_merge_is_applied_and_named_once(tmp_path):
    rec = _make_rec(tmp_path)
    provider = _ScriptedProvider(arbitrate=CallResult([[1, 2]], None))

    result = _run(provider, rec)

    assert isinstance(result, dict)
    assert len(result["tasks"]) == 2
    merged = result["tasks"][1]
    assert merged["start_ts"] == 1310.0
    assert merged["end_ts"] == 1690.0
    assert len(provider.name_calls) == 2  # window A + the merged BC, once each


# ---------------------------------------------------------------------------
# Zero model names → PROVIDER_UNAVAILABLE with the dominant reason
# ---------------------------------------------------------------------------


def test_zero_model_names_returns_unavailable_with_reason(tmp_path):
    rec = _make_rec(tmp_path)
    provider = _ScriptedProvider(
        name_fn=lambda payload: CallResult(None, "guardrail"),
    )

    result = _run(provider, rec)

    assert result is PROVIDER_UNAVAILABLE
    assert provider.last_unavailable_reason == "guardrail"
    assert provider.day_calls == []  # no summary without any named task
    assert rec.cache_rows() == []  # failures are never cached


# ---------------------------------------------------------------------------
# Budget + stop (KTD-7)
# ---------------------------------------------------------------------------


def test_budget_exhaustion_goes_mechanical_for_remaining_windows(
    tmp_path, monkeypatch,
):
    from screencap.segmentation import ondevice_pipeline

    rec = _make_rec(tmp_path, {0: list(_CHUNK0_CLUSTERS), 1: [4700.0]})
    clock = {"t": 0.0}
    monkeypatch.setattr(ondevice_pipeline, "_monotonic", lambda: clock["t"])

    def _name(_payload):
        clock["t"] += 100.0
        n = len(provider.name_calls)
        return CallResult((f"Named {n}", "development"), None)

    provider = _ScriptedProvider(name_fn=_name)
    result = _run(provider, rec, budget_s=250.0)

    assert isinstance(result, dict)
    assert len(provider.name_calls) == 3  # 0s, 100s, 200s; 300s > 250s budget
    assert _sources(result) == ["ondevice_model"] * 3 + ["idle_gap_heuristic"]
    assert result["tasks"][3]["name"] == "task_4"


def test_stop_signal_exits_promptly(tmp_path):
    rec = _make_rec(tmp_path, {0: list(_CHUNK0_CLUSTERS), 1: [4700.0]})
    stop = threading.Event()

    def _name(_payload):
        n = len(provider.name_calls)
        if n >= 2:
            stop.set()
        return CallResult((f"Named {n}", "development"), None)

    provider = _ScriptedProvider(name_fn=_name)
    result = _run(provider, rec, stop_event=stop)

    assert isinstance(result, dict)
    assert len(provider.name_calls) == 2  # no further spawns after the stop
    assert _sources(result) == ["ondevice_model"] * 2 + ["idle_gap_heuristic"] * 2
    # Day summary is skipped under a stop → synthesized fallback.
    assert provider.day_calls == []
    assert result["summary"]["overview"] == "Recording with 4 tasks."


def test_stopped_pass_reruns_to_convergence_via_cache(tmp_path):
    rec = _make_rec(tmp_path, {0: list(_CHUNK0_CLUSTERS), 1: [4700.0]})
    stop = threading.Event()

    def _name(_payload):
        n = len(p1.name_calls)
        if n >= 2:
            stop.set()
        return CallResult((f"Named {n}", "development"), None)

    p1 = _ScriptedProvider(name_fn=_name)
    first = _run(p1, rec, stop_event=stop)
    assert _sources(first).count("ondevice_model") == 2

    # Re-run after "unlock": only the un-named windows call the model.
    p2 = _ScriptedProvider(name_prefix="P2 ")
    second = _run(p2, rec)
    assert isinstance(second, dict)
    assert len(p2.name_calls) == 2
    assert _names(second)[:2] == _names(first)[:2]  # cached, byte-identical
    assert _sources(second) == ["ondevice_model"] * 4


# ---------------------------------------------------------------------------
# Single-window day skips arbitration
# ---------------------------------------------------------------------------


def test_single_window_day_skips_arbitration(tmp_path):
    rec = _make_rec(tmp_path, {0: [1010.0]})
    provider = _ScriptedProvider(arbitrate=CallResult([[0, 0]], None))

    result = _run(provider, rec)

    assert isinstance(result, dict)
    assert provider.arbitrate_calls == []
    assert len(provider.name_calls) == 1
    assert len(provider.day_calls) == 1


# ---------------------------------------------------------------------------
# Day-summary failure → synthesized summary, tasks unaffected
# ---------------------------------------------------------------------------


def test_day_summary_failure_synthesizes_summary(tmp_path):
    rec = _make_rec(tmp_path)
    provider = _ScriptedProvider(day=CallResult(None, "respond-failed"))

    result = _run(provider, rec)

    assert isinstance(result, dict)
    assert len(result["tasks"]) == 3
    assert _sources(result) == ["ondevice_model"] * 3
    assert result["summary"]["overview"] == "Recording with 3 tasks."
    assert result["summary"]["primary_focus"] == "development"
    assert result["tags"] == []


# ---------------------------------------------------------------------------
# AE4 + cache semantics
# ---------------------------------------------------------------------------


def test_second_pass_with_one_new_window_makes_one_naming_call(tmp_path):
    rec = _make_rec(tmp_path)
    p1 = _ScriptedProvider()
    first = _run(p1, rec)
    assert len(p1.name_calls) == 3

    rec.add_chunk(1, [4700.0])
    p2 = _ScriptedProvider(name_prefix="P2 ")
    second = _run(p2, rec)

    assert len(p2.name_calls) == 1  # exactly the new window
    assert len(p2.day_calls) == 1
    assert _names(second)[:3] == _names(first)  # prior names byte-identical
    assert _names(second)[3] == "P2 Named 1"


def test_digest_change_invalidates_only_that_window(tmp_path):
    rec = _make_rec(tmp_path)
    p1 = _ScriptedProvider()
    first = _run(p1, rec)

    # Edit ONLY cluster B's typed content (timestamps unchanged → same spans).
    for evt in rec.events_by_chunk[0]:
        if evt.get("timestamp") == 1350.0:
            evt["text"] = "totally different content"
    p2 = _ScriptedProvider(name_prefix="P2 ")
    second = _run(p2, rec)

    assert len(p2.name_calls) == 1  # only window B re-named
    names = _names(second)
    assert names[0] == _names(first)[0]
    assert names[2] == _names(first)[2]
    assert names[1] == "P2 Named 1"


def test_pending_chunk_window_named_but_not_cached_until_settled(tmp_path):
    rec = _make_rec(tmp_path)
    rec.add_chunk(1, [4700.0], staged=False)  # pending: not stage-complete

    p1 = _ScriptedProvider()
    _run(p1, rec)
    assert len(p1.name_calls) == 4  # every window named this pass

    # Still pending → the chunk-1 window is a miss again (named, not cached).
    p2 = _ScriptedProvider(name_prefix="P2 ")
    _run(p2, rec)
    assert len(p2.name_calls) == 1

    # Settle it → one more naming call (cache write), then zero.
    rec.ledger().mark_staged(1)
    p3 = _ScriptedProvider(name_prefix="P3 ")
    _run(p3, rec)
    assert len(p3.name_calls) == 1
    p4 = _ScriptedProvider(name_prefix="P4 ")
    _run(p4, rec)
    assert p4.name_calls == []


def test_merged_window_renames_once_then_never_flaps(tmp_path):
    rec = _make_rec(tmp_path)
    p1 = _ScriptedProvider(arbitrate=CallResult([[1, 2]], None))
    first = _run(p1, rec)
    assert len(p1.name_calls) == 2

    # Same merge next pass → identical spans → all cache hits, zero calls.
    p2 = _ScriptedProvider(arbitrate=CallResult([[1, 2]], None))
    second = _run(p2, rec)
    assert p2.name_calls == []
    assert _names(second) == _names(first)


def test_partial_budget_pass_then_fresh_pass_reuses_cached_names(
    tmp_path, monkeypatch,
):
    """Interrupted-pass resume (the crash simulation): cached names survive."""
    from screencap.segmentation import ondevice_pipeline

    rec = _make_rec(tmp_path, {0: list(_CHUNK0_CLUSTERS), 1: [4700.0]})
    clock = {"t": 0.0}
    monkeypatch.setattr(ondevice_pipeline, "_monotonic", lambda: clock["t"])

    def _name(_payload):
        clock["t"] += 100.0
        n = len(p1.name_calls)
        return CallResult((f"Named {n}", "development"), None)

    p1 = _ScriptedProvider(name_fn=_name)
    _run(p1, rec, budget_s=150.0)
    assert len(p1.name_calls) == 2

    clock["t"] = 0.0
    p2 = _ScriptedProvider(name_prefix="P2 ")
    second = _run(p2, rec)
    assert len(p2.name_calls) == 2  # the two un-named windows only
    assert _names(second)[:2] == ["Named 1", "Named 2"]


# ---------------------------------------------------------------------------
# Boundary pinning (KTD-6)
# ---------------------------------------------------------------------------


def test_pinning_keeps_prior_boundaries_and_names_under_flipped_merges(tmp_path):
    from screencap.segmentation.ondevice_pipeline import compute_pinned_before_ts

    rec = _make_rec(tmp_path)
    # Tick 1 (live, nothing committed): arbitration merges A+B.
    p1 = _ScriptedProvider(arbitrate=CallResult([[0, 1]], None))
    first = _run(p1, rec, is_live=True)
    assert [
        (t["start_ts"], t["end_ts"]) for t in first["tasks"]
    ] == [(1010.0, 1390.0), (1610.0, 1690.0)]
    _commit_tasks(rec, first)
    assert compute_pinned_before_ts(rec.dir) == 1690.0

    # Tick 2: two new windows; the arbitrator "flips" and wants [[0, 1]] again —
    # with pinning that merge applies to the NEW tail only, never the prefix.
    rec.add_chunk(1, [4700.0, 5100.0])
    p2 = _ScriptedProvider(
        arbitrate=CallResult([[0, 1]], None), name_prefix="P2 ",
    )
    second = _run(p2, rec, is_live=True, pinned_before_ts=1690.0)

    assert isinstance(second, dict)
    # Arbitration saw ONLY the two tail windows.
    assert len(p2.arbitrate_calls) == 1
    assert len(p2.arbitrate_calls[0]) == 2
    # Prefix boundaries: the committed merged AB span survives even though the
    # heuristic re-proposes A and B separately; C's boundary survives too.
    spans = [(t["start_ts"], t["end_ts"]) for t in second["tasks"]]
    assert spans == [(1010.0, 1390.0), (1610.0, 1690.0), (4700.0, 5180.0)]
    # AB's name is byte-identical (cache hit); C re-names once (it was the
    # uncached trailing window on tick 1); the merged tail names fresh.
    assert _names(second)[0] == _names(first)[0]
    assert len(p2.name_calls) == 2  # C + the merged DE window


def test_compute_pinned_ignores_user_and_edited_rows(tmp_path):
    from screencap.pipeline_state import TaskSegmentRow
    from screencap.segmentation.ondevice_pipeline import compute_pinned_before_ts

    rec = _make_rec(tmp_path)
    assert compute_pinned_before_ts(rec.dir) is None

    ledger = rec.ledger()
    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=1010.0, end_ts=1390.0, name="a"),
    ])
    ledger.insert_task_segment(
        TaskSegmentRow(task_index=0, start_ts=5000.0, end_ts=6000.0, name="user"),
    )
    assert compute_pinned_before_ts(rec.dir) == 1390.0


# ---------------------------------------------------------------------------
# Cache table lifecycle + scrub (KTD-10)
# ---------------------------------------------------------------------------


def test_cache_table_absent_created_on_first_use(tmp_path):
    rec = _make_rec(tmp_path)
    conn = sqlite3.connect(str(rec.db_path))
    conn.execute("DROP TABLE ondevice_window_names")
    conn.execute("DROP TABLE scrub_generation")
    conn.commit()
    conn.close()

    provider = _ScriptedProvider()
    result = _run(provider, rec)

    assert isinstance(result, dict)
    assert len(rec.cache_rows()) == 3  # recreated + written


def test_scrub_purges_overlapping_cache_rows_and_bumps_generation(tmp_path):
    from screencap.enforcement.scrub_worker import ScrubWorker

    rec = _make_rec(tmp_path)
    ledger = rec.ledger()
    ledger.store_window_name(1000.0, 1100.0, "h1", "Before", "development")
    ledger.store_window_name(1290.0, 1350.0, "h2", "Overlapping", "development")
    ledger.store_window_name(1500.0, 1600.0, "h3", "After", "development")
    gen_before = ledger.read_scrub_generation()

    # A target window active [1300, 1400): benign → target → benign.
    conn = sqlite3.connect(str(rec.db_path))
    for ts, bundle in ((1200.0, "com.apple.dt.Xcode"),
                      (1300.0, "com.spotify.client"),
                      (1400.0, "com.apple.dt.Xcode")):
        conn.execute(
            "INSERT INTO window_event (recording_id, timestamp, "
            "recording_timestamp, app_bundle_id, app_name, title) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (ts, ts, bundle, bundle.split(".")[-1], "t"),
        )
    conn.commit()
    conn.close()

    worker = ScrubWorker(
        disable_q=None, recording_db_path=rec.db_path, capture_dir=rec.dir,
    )
    entry = worker._handle({
        "kind": "app", "bundle_id": "com.spotify.client", "app_name": "Spotify",
        "root_domain": None, "ts_unix": 2000.0, "source": "menubar",
    })
    assert entry["scrub_status"] == "completed"

    rows = rec.cache_rows()
    assert [r[3] for r in rows] == ["Before", "After"]
    assert ledger.read_scrub_generation() == gen_before + 1


def test_scrub_fails_open_when_cache_tables_absent(tmp_path):
    from screencap.enforcement.scrub_worker import ScrubWorker

    rec = _make_rec(tmp_path)
    conn = sqlite3.connect(str(rec.db_path))
    conn.execute("DROP TABLE ondevice_window_names")
    conn.execute("DROP TABLE scrub_generation")
    conn.execute(
        "INSERT INTO window_event (recording_id, timestamp, "
        "recording_timestamp, app_bundle_id, app_name, title) "
        "VALUES (1, 1300.0, 1300.0, 'com.spotify.client', 'Spotify', 't')",
    )
    conn.commit()
    conn.close()

    worker = ScrubWorker(
        disable_q=None, recording_db_path=rec.db_path, capture_dir=rec.dir,
    )
    entry = worker._handle({
        "kind": "app", "bundle_id": "com.spotify.client", "app_name": "Spotify",
        "root_domain": None, "ts_unix": 2000.0, "source": "menubar",
    })
    assert entry["scrub_status"] == "completed"


def _seed_spotify_target(db_path: Path) -> None:
    """Insert benign → Spotify → benign window events → target interval [1300, 1400)."""
    conn = sqlite3.connect(str(db_path))
    for ts, bundle in ((1200.0, "com.apple.dt.Xcode"),
                       (1300.0, "com.spotify.client"),
                       (1400.0, "com.apple.dt.Xcode")):
        conn.execute(
            "INSERT INTO window_event (recording_id, timestamp, "
            "recording_timestamp, app_bundle_id, app_name, title) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (ts, ts, bundle, bundle.split(".")[-1], "t"),
        )
    conn.commit()
    conn.close()


def _segment_rows(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT name, source, edited FROM pipeline_task_segments "
            "ORDER BY start_ts"
        ).fetchall()
    finally:
        conn.close()


def test_scrub_purges_overlapping_task_segments_preserving_user_and_edited(tmp_path):
    """SCR-280: a retroactive disable drops AGENT task rows (and tasks.json
    entries) whose span overlaps the scrubbed interval, but PRESERVES
    ``source='user'`` and user-edited rows — the ledger's protection predicate."""
    from screencap.enforcement.scrub_worker import ScrubWorker
    from screencap.pipeline_state import TaskSegmentRow

    rec = _make_rec(tmp_path)
    ledger = rec.ledger()
    # Three agent rows: before / overlapping / after the target interval, plus a
    # fourth agent row we will mark edited so it moves into the protected range.
    ledger.replace_task_segments([
        TaskSegmentRow(task_index=0, start_ts=1000.0, end_ts=1100.0, name="Before"),
        TaskSegmentRow(task_index=1, start_ts=1290.0, end_ts=1350.0, name="Overlapping"),
        TaskSegmentRow(task_index=2, start_ts=1500.0, end_ts=1600.0, name="After"),
        TaskSegmentRow(task_index=3, start_ts=1310.0, end_ts=1330.0, name="Edited"),
    ])
    # A user row overlapping the target — must survive.
    ledger.insert_task_segment(
        TaskSegmentRow(task_index=0, start_ts=1300.0, end_ts=1400.0, name="User Task")
    )
    # Re-home the "Edited" agent row into the protected HIGH range (edited=1).
    ledger.update_task_segment(3, mark_edited=True)

    # tasks.json mirror: agent Overlapping (dropped) + agent Before (kept) +
    # a user entry overlapping the target (kept).
    (rec.dir / "tasks.json").write_text(json.dumps({"tasks": [
        {"name": "Before", "start_ts": 1000.0, "end_ts": 1100.0,
         "source": "agent", "edited": False},
        {"name": "Overlapping", "start_ts": 1290.0, "end_ts": 1350.0,
         "source": "agent", "edited": False},
        {"name": "User Task", "start_ts": 1300.0, "end_ts": 1400.0,
         "source": "user", "edited": False},
    ]}))

    _seed_spotify_target(rec.db_path)

    worker = ScrubWorker(
        disable_q=None, recording_db_path=rec.db_path, capture_dir=rec.dir,
    )
    entry = worker._handle({
        "kind": "app", "bundle_id": "com.spotify.client", "app_name": "Spotify",
        "root_domain": None, "ts_unix": 2000.0, "source": "menubar",
    })
    assert entry["scrub_status"] == "completed"

    # Ledger: only the unedited agent "Overlapping" row is gone.
    names = {name for name, _, _ in _segment_rows(rec.db_path)}
    assert names == {"Before", "After", "Edited", "User Task"}

    # tasks.json: the overlapping agent entry is dropped; the agent Before entry
    # and the protected user entry survive.
    kept = json.loads((rec.dir / "tasks.json").read_text())["tasks"]
    assert [t["name"] for t in kept] == ["Before", "User Task"]


def test_scrub_task_segment_purge_fails_open_when_table_absent(tmp_path):
    """SCR-280: a pre-U4 recording.db with no pipeline_task_segments table still
    completes the scrub (fail-open, mirroring the on-device cache purge)."""
    from screencap.enforcement.scrub_worker import ScrubWorker

    rec = _make_rec(tmp_path)
    conn = sqlite3.connect(str(rec.db_path))
    conn.execute("DROP TABLE pipeline_task_segments")
    conn.commit()
    conn.close()
    _seed_spotify_target(rec.db_path)

    worker = ScrubWorker(
        disable_q=None, recording_db_path=rec.db_path, capture_dir=rec.dir,
    )
    entry = worker._handle({
        "kind": "app", "bundle_id": "com.spotify.client", "app_name": "Spotify",
        "root_domain": None, "ts_unix": 2000.0, "source": "menubar",
    })
    assert entry["scrub_status"] == "completed"


def _bump_generation(db_path: Path) -> None:
    """Simulate a concurrent scrub committing between naming calls."""
    from screencap.pipeline_state import bump_scrub_generation

    conn = sqlite3.connect(str(db_path))
    bump_scrub_generation(conn)
    conn.commit()
    conn.close()


def test_mid_pass_scrub_trips_staleness_guard(tmp_path):
    """A scrub landing mid-pass → zero pre-scrub cache writes survive, and the
    pass returns the unavailable sentinel with the distinct ``stale-scrub``
    reason (not a real unavailability — the terminal stage short-circuits)."""
    rec = _make_rec(tmp_path)

    def _name(_payload):
        n = len(provider.name_calls)
        if n == 1:
            _bump_generation(rec.db_path)
        return CallResult((f"Named {n}", "development"), None)

    provider = _ScriptedProvider(name_fn=_name)
    result = _run(provider, rec)

    assert result is PROVIDER_UNAVAILABLE  # nothing reaches the sinks
    assert provider.last_unavailable_reason == "stale-scrub"
    assert rec.cache_rows() == []  # this pass's rows were deleted


def test_mid_pass_generation_bump_gates_subsequent_cache_writes(tmp_path):
    """Each store re-checks the scrub generation AT WRITE TIME: a bump landing
    mid-pass means every later store commits nothing (the window stays
    named-but-uncached), not merely that finalize cleans up afterwards."""
    rec = _make_rec(tmp_path)
    rows_seen_by_third_call: list[int] = []

    def _name(_payload):
        n = len(provider.name_calls)
        if n == 2:
            # Bump BETWEEN window 1's committed store and window 2's store.
            _bump_generation(rec.db_path)
        if n == 3:
            # Window 2's store was gated at write time — only window 1's row
            # exists while the pass is still running.
            rows_seen_by_third_call.append(len(rec.cache_rows()))
        return CallResult((f"Named {n}", "development"), None)

    provider = _ScriptedProvider(name_fn=_name)
    result = _run(provider, rec)

    assert rows_seen_by_third_call == [1]
    assert result is PROVIDER_UNAVAILABLE  # the end-of-pass guard still trips
    assert provider.last_unavailable_reason == "stale-scrub"
    assert rec.cache_rows() == []  # window 1's pre-bump row was deleted


# ---------------------------------------------------------------------------
# Output hygiene (KTD-9)
# ---------------------------------------------------------------------------


def test_oversized_and_out_of_vocab_output_is_sanitized(tmp_path):
    rec = _make_rec(tmp_path, {0: [1010.0]})
    provider = _ScriptedProvider(
        name_results=[CallResult(("  " + "X" * 200 + "  ", "hacking"), None)],
        day=CallResult(
            {"overview": "  Fine day.  ",
             "tags": ["Python", "BAD TAG!!", "ok-tag", "python"]},
            None,
        ),
    )

    result = _run(provider, rec)

    task = result["tasks"][0]
    assert task["name"] == "X" * 80
    assert task["category"] == "other"
    assert result["summary"]["overview"] == "Fine day."
    assert result["tags"] == ["python", "ok-tag"]
    # The sanitized (not raw) values are what got cached.
    rows = rec.cache_rows()
    assert rows[0][3] == "X" * 80
    assert rows[0][4] == "other"


def test_context_window_failure_halves_digest_then_retries(tmp_path):
    rec = _make_rec(tmp_path, {0: [1010.0]})
    # A transcript-heavy window: halving has real content to shed.
    rec.transcripts_by_chunk[0] = {"segments": [
        {"start": 15.0 + k * 15.0, "end": 20.0 + k * 15.0,
         "text": f"talking about part {k} of the work"}
        for k in range(4)
    ]}
    sizes: list[int] = []

    def _name(payload):
        body = {k: v for k, v in payload.items() if k != "stripped"}
        sizes.append(len(json.dumps(body)))
        if len(sizes) < 3:
            return CallResult(None, "context-window")
        return CallResult(("Fits now", "research"), None)

    provider = _ScriptedProvider(name_fn=_name)
    result = _run(provider, rec)

    assert isinstance(result, dict)
    assert result["tasks"][0]["name"] == "Fits now"
    assert len(sizes) == 3
    assert sizes[1] < sizes[0]  # each retry shrank the payload


def test_stop_mid_halving_loop_exits_promptly(tmp_path):
    """KTD-7 inside the halving loop: a stop landing between context-window
    retries halts the loop at its next iteration — no further model calls,
    the window goes mechanical with the ``stopped`` pseudo-reason."""
    rec = _make_rec(tmp_path, {0: [1010.0]})
    # A transcript-heavy window so halving has plenty of retries left.
    rec.transcripts_by_chunk[0] = {"segments": [
        {"start": 15.0 + k * 15.0, "end": 20.0 + k * 15.0,
         "text": f"talking about part {k} of the work"}
        for k in range(4)
    ]}
    stop = threading.Event()

    def _name(_payload):
        stop.set()  # the stop lands mid-loop, after the first naming call
        return CallResult(None, "context-window")

    provider = _ScriptedProvider(name_fn=_name)
    result = _run(provider, rec, stop_event=stop)

    assert result is PROVIDER_UNAVAILABLE  # sole window went mechanical
    assert len(provider.name_calls) == 1  # NO halving retry after the stop
    assert provider.last_unavailable_reason == "stopped"


def test_context_window_exhausts_halvings_then_mechanical(tmp_path):
    rec = _make_rec(tmp_path, {0: [1010.0]})
    provider = _ScriptedProvider(
        name_fn=lambda payload: CallResult(None, "context-window"),
    )

    result = _run(provider, rec)

    assert result is PROVIDER_UNAVAILABLE  # sole window went mechanical
    assert provider.last_unavailable_reason == "context-window"
    # Bounded: the original call + at most 3 halvings.
    assert len(provider.name_calls) <= 4
