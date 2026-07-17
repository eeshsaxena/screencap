"""Tests for U4 — the local session-segmentation stage in run_terminal_stage.

Scope (the U4 test scenarios):

* A LOCAL recording produces a persisted tasks store (tasks.json + the
  pipeline_task_segments ledger table) after terminal runs — using a FAKE
  provider returning canned tasks (never the real on-device / Gemini backend).
* No upload path is invoked for a LOCAL recording (AE1) — asserted against the
  upload seam (``upload_recording`` / ``request_signed_urls``).
* The local tasks store is excluded from ``list_recording_files`` and rejected
  by ``assert_uploadable`` — a cloud/both recording never enumerates it.
* Re-entering terminal for the same recording does not duplicate tasks
  (idempotent replace).
* A provider returning ``None`` (and an unavailable provider that raises) leaves
  terminal completion intact (strictly fail-open) and persists no store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures — a LOCAL recording dir with the ledger schema + on-disk chunk
# artifacts (manifests/events) the local ActivitySource reads.
# ---------------------------------------------------------------------------


def _make_local_recording(
    tmp_path: Path, *, destination: str = "local", n_chunks: int = 2,
    name: str = "rec",
) -> Path:
    """Create a recording dir with recording.db + ledger schema + chunk files.

    Writes real v2 manifests + events JSONL so the local ActivitySource yields
    activity the summary builder can consume. Seeds + freezes the ledger like
    the terminal-stage tests do.
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
    # The event timestamps (per chunk offset) — the same values forwarded to the
    # U3 strip as coverage_timestamps, so they need canonical window coverage
    # (else the fail-closed uncovered-gap residual strips them). The screenshot
    # rows mirror what a real recording carries on disk; events are deliberately
    # NOT orphan-cross-checked against them.
    event_offsets = (10.0, 20.0, 30.0)
    for i in range(n_chunks):
        cs = base + i * chunk_dur
        crud.insert_action_event(session, recording, 1000.0 + i * 5 + 1, {
            "name": "click", "mouse_x": 10.0, "mouse_y": 20.0,
            "mouse_button_name": "left", "mouse_pressed": True,
        })
        # A benign (UNKNOWN → ALLOW) window_event at the chunk start so it covers
        # the whole chunk span (derive_skip_intervals require_canonical) and the
        # ALLOW frames pass through — mirrors the coverage requirement in
        # tests/segmentation/test_privacy_strip.py.
        crud.insert_window_event(session, recording, cs + 1, {
            "app_bundle_id": "com.example.unknownbenign",
            "window_id": f"w{i}",
            "title": f"file_{i}.py",
            "app_name": "Editor",
        })
        # A surviving screenshot row at each event timestamp so the SCR-191
        # orphan cross-check sees coverage (an empty screenshot table would flag
        # every forwarded ts as an orphan and strip it).
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


def _canned_tasks(session_start: float = 1000.0) -> dict:
    """A validated-shape tasks dict, as a provider's ``segment`` would return."""
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
    """A canned LLMProvider — records calls, returns a fixed result."""

    def __init__(self, result: dict | None) -> None:
        self._result = result
        self.calls: list[dict] = []

    def segment(self, activity_summary: dict) -> dict | None:
        self.calls.append(activity_summary)
        return self._result


@pytest.fixture(autouse=True)
def _isolate_run_dir(tmp_path, monkeypatch):
    """Point the terminal-stage lock dir at a per-test tmp dir."""
    import screencap.terminal_stage as ts

    monkeypatch.setattr(ts, "_RUN_DIR", tmp_path / "run")


def _install_provider(monkeypatch, provider) -> None:
    """Wire a fake provider into the day-split routing seam.

    Since U8, ``_segment_local_tasks`` builds the day-split provider via
    ``screencap.segmentation.routing.build_day_split_provider()`` (an on-device
    chain, the downloaded backend, or a LOCAL BYO endpoint); patch that seam to
    return the fake provider directly.
    """
    import screencap.segmentation.routing as routing

    monkeypatch.setattr(routing, "build_day_split_provider", lambda: provider)


def _run_terminal(rec_dir: Path):
    from screencap import terminal_stage as ts

    return ts.run_terminal_stage(rec_dir)


# ---------------------------------------------------------------------------
# Happy path — a LOCAL recording persists a tasks store.
# ---------------------------------------------------------------------------


def test_local_recording_persists_tasks_store(tmp_path, monkeypatch):
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    fake = _FakeProvider(_canned_tasks())
    _install_provider(monkeypatch, fake)

    result = _run_terminal(rec_dir)

    # The provider was called with the built activity summary.
    assert len(fake.calls) == 1
    assert "summary" in fake.calls[0]

    # tasks.json written with the canned tasks.
    tasks_path = rec_dir / "tasks.json"
    assert tasks_path.is_file()
    persisted = json.loads(tasks_path.read_text())
    assert [t["name"] for t in persisted["tasks"]] == [
        "Implement auth module", "Coordinate PR review",
    ]

    # Ledger table populated with one row per task, ordered by task_index.
    ledger = PipelineLedger(rec_dir / "recording.db")
    segs = ledger.read_task_segments()
    assert [s.name for s in segs] == [
        "Implement auth module", "Coordinate PR review",
    ]
    assert [s.task_index for s in segs] == [0, 1]
    assert segs[0].category == "development"
    assert segs[0].confidence == "high"
    # metadata carries the extra provider fields as JSON.
    assert json.loads(segs[0].metadata)["derived_name"] == "implement-auth-module"

    # Terminal reports the count and completed the LOCAL route.
    assert result.tasks_persisted == 2
    assert result.routed is True
    assert result.destination == "local"


# ---------------------------------------------------------------------------
# AE1 — no upload path is invoked for a LOCAL recording.
# ---------------------------------------------------------------------------


def test_local_recording_never_uploads(tmp_path, monkeypatch):
    import screencap.terminal_stage as ts
    import screencap.upload as upload

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))

    # Trip-wire the upload seam: any call is a failure.
    def _boom(*a, **k):  # noqa: ANN001
        raise AssertionError("upload seam invoked for a LOCAL recording")

    monkeypatch.setattr(upload, "upload_recording", _boom)
    monkeypatch.setattr(upload, "request_signed_urls", _boom)
    # The cloud producer must never be reached for a LOCAL route either.
    monkeypatch.setattr(
        ts.CloudCopyProducer, "produce",
        lambda self, **k: (_ for _ in ()).throw(
            AssertionError("cloud copy produced for a LOCAL recording")
        ),
    )

    result = _run_terminal(rec_dir)

    assert result.destination == "local"
    assert result.tasks_persisted == 2
    assert result.sentinel_uploaded is False
    assert result.n_uploaded == 0


# ---------------------------------------------------------------------------
# Never-upload — the local tasks store is excluded / rejected at the seam.
# ---------------------------------------------------------------------------


def test_tasks_store_excluded_from_upload_enumeration(tmp_path, monkeypatch):
    from screencap.upload import list_recording_files

    # A cloud/both recording that happens to have a tasks.json must never
    # enumerate it for upload.
    rec_dir = _make_local_recording(tmp_path, destination="cloud")
    (rec_dir / "tasks.json").write_text(json.dumps(_canned_tasks()))

    files = list_recording_files(rec_dir)
    names = {f.name for f in files}
    assert "tasks.json" not in names
    # recording.db stays excluded too (control).
    assert "recording.db" not in names
    # a normal artifact IS enumerated (control — the denylist is narrow).
    assert any(n.endswith("_manifest.json") for n in names)


def test_tasks_store_rejected_by_assert_uploadable(tmp_path):
    from screencap.upload import FileInfo, assert_uploadable

    p = tmp_path / "tasks.json"
    p.write_text("{}")
    fi = FileInfo(name="tasks.json", path=p, content_type="application/json", size=2)
    with pytest.raises(ValueError, match="local-only"):
        assert_uploadable(fi)


# ---------------------------------------------------------------------------
# Idempotency — re-entering terminal does not duplicate tasks.
# ---------------------------------------------------------------------------


def test_reentry_does_not_duplicate_tasks(tmp_path, monkeypatch):
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(_canned_tasks()))

    _run_terminal(rec_dir)
    _run_terminal(rec_dir)  # second entry — must REPLACE, not append.

    ledger = PipelineLedger(rec_dir / "recording.db")
    segs = ledger.read_task_segments()
    assert len(segs) == 2  # not 4
    assert [s.task_index for s in segs] == [0, 1]

    persisted = json.loads((rec_dir / "tasks.json").read_text())
    assert len(persisted["tasks"]) == 2


# ---------------------------------------------------------------------------
# Fail-open — a provider returning None leaves terminal completion intact.
# ---------------------------------------------------------------------------


def test_provider_returns_none_is_fail_open(tmp_path, monkeypatch):
    from screencap.pipeline_state import PipelineLedger

    rec_dir = _make_local_recording(tmp_path)
    _install_provider(monkeypatch, _FakeProvider(None))

    result = _run_terminal(rec_dir)

    # Terminal completed the LOCAL route; no tasks store written.
    assert result.routed is True
    assert result.destination == "local"
    assert result.tasks_persisted == 0
    assert not (rec_dir / "tasks.json").exists()
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.read_task_segments() == []
    # LOCAL_DONE marking still happened (terminal not blocked by the miss).
    assert result.n_local_done == 2


def test_unavailable_provider_is_fail_open(tmp_path, monkeypatch):
    """A provider builder that RAISES fails open — terminal never blocks (U8 seam)."""
    import screencap.segmentation.routing as routing

    rec_dir = _make_local_recording(tmp_path)

    def _raise():
        raise RuntimeError("provider build failed unexpectedly")

    monkeypatch.setattr(routing, "build_day_split_provider", _raise)

    result = _run_terminal(rec_dir)

    assert result.routed is True
    assert result.tasks_persisted == 0
    assert not (rec_dir / "tasks.json").exists()
