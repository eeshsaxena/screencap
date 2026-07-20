"""Block consolidation + thread linking tests (day diary, U2).

Drives the pure grouping / clamp / thread / trim / identity core of
:mod:`screencap.segmentation.consolidate` over in-memory fine-task fixtures (no
recording.db, no provider) plus a scripted block-namer fake for the one model
touch. All Vision/OCR-free and privacy-marked so the CI privacy lane runs them.

Covers AE1 (many same-work fragments -> one block), AE2 (two sittings -> one
thread), stable identity across re-carves, protection TRIM (KTD-7), midnight
clamp (KTD-5), the honest-unnamed fallback (KTD-10), the staleness cadence
(KTD-6), and live/finalize parity.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Fixture helpers: fine task dicts as the segmentation pipeline emits them.
# ---------------------------------------------------------------------------

_MODEL = "ondevice_model"
_MECH = "idle_gap_heuristic"


def _frag(
    start, end, name, *, category="development", apps=("com.apple.dt.Xcode",),
    source=_MODEL,
):
    """One fine task fragment dict (the shape ``_model_task`` emits)."""
    return {
        "start_ts": float(start),
        "end_ts": float(end),
        "name": name,
        "category": category,
        "apps_used": list(apps),
        "source": source,
    }


# ---------------------------------------------------------------------------
# Grouping core (test-first): adjacency + app/topic affinity + gap tolerance.
# ---------------------------------------------------------------------------


def test_eleven_same_work_fragments_group_into_one_block():
    """AE1: eleven adjacent same-work fragments consolidate to ONE block span."""
    from screencap.segmentation.consolidate import group_fragments

    base = 1000.0
    frags = [
        _frag(base + i * 180, base + i * 180 + 150,
              "Brainstorming diary schema", apps=("com.figma.Desktop",))
        for i in range(11)
    ]
    blocks = group_fragments(frags)

    assert len(blocks) == 1
    assert blocks[0].start_ts == frags[0]["start_ts"]
    assert blocks[0].end_ts == frags[-1]["end_ts"]
    assert len(blocks[0].fragments) == 11


def test_distinct_work_fragments_stay_separate():
    """Adjacent fragments of DIFFERENT work (apps + names) are NOT merged."""
    from screencap.segmentation.consolidate import group_fragments

    frags = [
        _frag(1000.0, 2800.0, "Implement auth module",
              category="development", apps=("com.microsoft.VSCode",)),
        _frag(2800.0, 4600.0, "Coordinate PR review",
              category="communication", apps=("com.tinyspeck.slackmacgap",)),
    ]
    blocks = group_fragments(frags)

    assert len(blocks) == 2
    assert [b.fragments[0].name for b in blocks] == [
        "Implement auth module", "Coordinate PR review",
    ]


def test_mechanical_fragments_never_group():
    """Unnamed mechanical fragments carry no positive same-work evidence -> never
    merged, even when adjacent and sharing an app (they stay distinct sittings)."""
    from screencap.segmentation.consolidate import group_fragments

    frags = [
        _frag(1010.0, 1015.0, "task_1", category=None,
              apps=("com.example.unknownbenign",), source=_MECH),
        _frag(1400.0, 1405.0, "task_2", category=None,
              apps=("com.example.unknownbenign",), source=_MECH),
    ]
    blocks = group_fragments(frags)

    assert len(blocks) == 2


# ---------------------------------------------------------------------------
# Scripted block namer (no subprocess): the ``call_name_window`` verb seam.
# ---------------------------------------------------------------------------


class _ScriptedNamer:
    """A stub block namer: scripted CallResults over ``call_name_window``."""

    def __init__(self, *, results=None, name="Consolidated work", category="development"):
        from screencap.segmentation.providers.ondevice import CallResult

        self._results = list(results or [])
        self._name = name
        self._category = category
        self.calls: list[dict] = []
        self._CallResult = CallResult

    def call_name_window(self, digest_payload):
        assert digest_payload.get("stripped") is True, (
            "block naming payloads must carry the strip attestation"
        )
        self.calls.append(digest_payload)
        if self._results:
            return self._results.pop(0)
        return self._CallResult((self._name, self._category), None)


def _rows_from_dicts(task_dicts):
    """Turn consolidate()'s output dicts into prior TaskSegmentRow rows."""
    import json

    from screencap.pipeline_state import TaskSegmentRow

    rows = []
    for i, t in enumerate(task_dicts):
        meta = {k: t[k] for k in ("apps_used", "derived_name", "description") if k in t}
        rows.append(TaskSegmentRow(
            task_index=i, start_ts=t["start_ts"], end_ts=t["end_ts"],
            name=t["name"], category=t.get("category"),
            metadata=json.dumps(meta) if meta else None,
            block_id=t.get("block_id"), thread_id=t.get("thread_id"),
        ))
    return rows


# ---------------------------------------------------------------------------
# AE1: eleven same-work fragments -> ONE named block.
# ---------------------------------------------------------------------------


def test_ae1_eleven_fragments_consolidate_to_one_named_block():
    from screencap.segmentation.consolidate import consolidate

    base = 1000.0
    frags = [
        _frag(base + i * 180, base + i * 180 + 150,
              "Brainstorming diary schema", apps=("com.figma.Desktop",))
        for i in range(11)
    ]
    namer = _ScriptedNamer(name="Day diary schema design")
    out = consolidate(frags, namer=namer, recording_name="rec")

    assert len(out) == 1
    assert out[0]["name"] == "Day diary schema design"
    assert out[0]["start_ts"] == frags[0]["start_ts"]
    assert out[0]["end_ts"] == frags[-1]["end_ts"]
    assert out[0]["block_id"]
    assert len(namer.calls) == 1  # one model call for the one merged block


# ---------------------------------------------------------------------------
# AE2: two sittings of the same work -> two blocks sharing ONE thread_id;
# an unrelated middle block gets none.
# ---------------------------------------------------------------------------


def test_ae2_two_sittings_share_a_thread_middle_block_gets_none():
    from screencap.segmentation.consolidate import consolidate

    frags = [
        # Morning sitting.
        _frag(10_000.0, 13_000.0, "Kick technique practice",
              category="learning", apps=("com.apple.QuickTimePlayerX",)),
        # Unrelated middle block.
        _frag(20_000.0, 22_000.0, "Email triage",
              category="communication", apps=("com.google.Chrome",)),
        # Afternoon sitting of the SAME morning work (far apart -> own block).
        _frag(30_000.0, 33_000.0, "Kick technique practice",
              category="learning", apps=("com.apple.QuickTimePlayerX",)),
    ]
    out = consolidate(frags, recording_name="rec")

    assert len(out) == 3  # three separate blocks (gaps exceed GROUP_GAP_S)
    kick = [t for t in out if t["name"] == "Kick technique practice"]
    email = [t for t in out if t["name"] == "Email triage"]
    assert len(kick) == 2
    assert kick[0]["thread_id"] and kick[0]["thread_id"] == kick[1]["thread_id"]
    assert "thread_id" not in email[0]


# ---------------------------------------------------------------------------
# Identity: re-run preserves ids; a new chunk extends the last block's id.
# ---------------------------------------------------------------------------


def test_rerun_on_unchanged_evidence_preserves_every_block_id():
    from screencap.segmentation.consolidate import consolidate

    frags = [
        _frag(1000.0, 2800.0, "Implement auth module",
              apps=("com.microsoft.VSCode",)),
        _frag(2800.0, 4600.0, "Coordinate PR review",
              category="communication", apps=("com.tinyspeck.slackmacgap",)),
    ]
    first = consolidate(frags, recording_name="rec")
    prior = _rows_from_dicts(first)
    second = consolidate(frags, prior_rows=prior, recording_name="rec")

    assert [t["block_id"] for t in second] == [t["block_id"] for t in first]


def test_adding_a_new_chunk_extends_last_block_without_changing_its_id():
    from screencap.segmentation.consolidate import consolidate

    base = 1000.0
    frags = [
        _frag(base + i * 180, base + i * 180 + 150, "Writing the plan",
              apps=("com.apple.Notes",))
        for i in range(3)
    ]
    first = consolidate(frags, recording_name="rec")
    assert len(first) == 1
    prior = _rows_from_dicts(first)

    # A fourth adjacent same-work chunk lands: the block extends, id unchanged.
    frags.append(_frag(base + 3 * 180, base + 3 * 180 + 150, "Writing the plan",
                       apps=("com.apple.Notes",)))
    second = consolidate(frags, prior_rows=prior, recording_name="rec")

    assert len(second) == 1
    assert second[0]["block_id"] == first[0]["block_id"]
    assert second[0]["end_ts"] > first[0]["end_ts"]


# ---------------------------------------------------------------------------
# Protection TRIM (KTD-7): a user-curated span trims its neighbour, not drops it.
# ---------------------------------------------------------------------------


def test_protected_span_trims_neighbouring_block_not_drops_it():
    from screencap.segmentation.consolidate import consolidate

    # One long agent block; the user curated a span at its tail edge.
    frags = [
        _frag(1000.0, 4000.0, "Refactoring the pipeline",
              apps=("com.microsoft.VSCode",)),
    ]
    protected = [(3000.0, 4000.0)]  # user block abutting the agent block's end
    out = consolidate(frags, protected_spans=protected, recording_name="rec")

    assert len(out) == 1
    # The agent block was TRIMMED to abut [3000,4000), not dropped.
    assert out[0]["start_ts"] == 1000.0
    assert out[0]["end_ts"] == 3000.0
    assert out[0]["name"] == "Refactoring the pipeline"


def test_protected_span_in_the_middle_splits_into_two_abutting_blocks():
    from screencap.segmentation.consolidate import consolidate

    frags = [
        _frag(1000.0, 4000.0, "Refactoring the pipeline",
              apps=("com.microsoft.VSCode",)),
    ]
    protected = [(2000.0, 2500.0)]  # user block in the MIDDLE
    out = consolidate(frags, protected_spans=protected, recording_name="rec")

    spans = sorted((t["start_ts"], t["end_ts"]) for t in out)
    assert spans == [(1000.0, 2000.0), (2500.0, 4000.0)]  # abut, not fragmented away
    # Split assigns distinct block ids without disturbing each other.
    assert len({t["block_id"] for t in out}) == 2


# ---------------------------------------------------------------------------
# Midnight clamp (KTD-5): a 23:40-00:20 stretch -> two blocks, thread not spanning.
# ---------------------------------------------------------------------------


def test_midnight_stretch_splits_into_two_blocks_thread_not_spanning():
    from screencap.segmentation.consolidate import consolidate

    # tz_offset 0 -> local midnight == a UTC-day boundary. Day 3 midnight = 3*86400.
    midnight = 3 * 86400
    frags = [
        _frag(midnight - 1200, midnight + 1200, "Late-night writing",
              apps=("com.apple.Notes",)),  # 23:40 -> 00:20
    ]
    out = consolidate(frags, tz_offset_s=0, recording_name="rec")

    spans = sorted((t["start_ts"], t["end_ts"]) for t in out)
    assert spans == [
        (float(midnight - 1200), float(midnight)),
        (float(midnight), float(midnight + 1200)),
    ]
    # The two midnight-split pieces belong to different days -> not threaded.
    assert all("thread_id" not in t for t in out)


# ---------------------------------------------------------------------------
# Fallback (KTD-10): a merged block with no model -> honest unnamed + marker.
# ---------------------------------------------------------------------------


def test_merged_block_without_a_model_is_honestly_unnamed():
    from screencap.segmentation.consolidate import UNNAMED, consolidate

    base = 1000.0
    frags = [
        _frag(base + i * 180, base + i * 180 + 150, "Design review",
              apps=("com.figma.Desktop",))
        for i in range(4)
    ]
    out = consolidate(frags, namer=None, recording_name="rec")  # no model

    assert len(out) == 1
    assert out[0]["name"] == UNNAMED
    assert out[0]["name"] != "task_1"
    assert out[0]["name_fallback"] == "no-model"


def test_mechanical_single_fragment_block_is_honestly_unnamed():
    from screencap.segmentation.consolidate import UNNAMED, consolidate

    frags = [
        _frag(1000.0, 1500.0, "task_1", category=None,
              apps=("com.example.unknownbenign",), source=_MECH),
    ]
    out = consolidate(frags, recording_name="rec")

    assert out[0]["name"] == UNNAMED
    assert out[0]["name"] != "task_1"
    assert out[0]["name_fallback"] == "mechanical"


# ---------------------------------------------------------------------------
# Cadence (KTD-6): unchanged fingerprint skips; a curation edit forces a re-run.
# ---------------------------------------------------------------------------


def test_consolidation_fingerprint_stable_then_shifts_on_curation(tmp_path):
    import json

    from screencap.engine.db import create_db
    from screencap.pipeline_state import (
        PipelineLedger,
        TaskSegmentRow,
        ensure_pipeline_state_schema,
    )
    from screencap.segmentation.consolidate import consolidation_fingerprint

    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    db_path = rec_dir / "recording.db"
    create_db(str(db_path))
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO recording (timestamp, monitor_width, monitor_height, "
        "pixel_ratio, platform) VALUES (?, ?, ?, ?, ?)",
        (1000.0, 1920, 1080, 2.0, "darwin"),
    )
    conn.commit()
    conn.close()
    ensure_pipeline_state_schema(db_path)
    (rec_dir / "chunk_0000_manifest.json").write_text(json.dumps({
        "format_version": 2, "chunk_index": 0,
        "chunk_start": 1000.0, "chunk_end": 4600.0,
    }))

    key1 = consolidation_fingerprint(rec_dir)
    key2 = consolidation_fingerprint(rec_dir)
    assert key1 is not None and key1 == key2  # unchanged -> skip

    # A user curation edit shifts the kept-curation count -> forces a re-run.
    PipelineLedger(db_path).insert_task_segment(
        TaskSegmentRow(task_index=0, start_ts=1500.0, end_ts=2000.0, name="Mine"),
    )
    key3 = consolidation_fingerprint(rec_dir)
    assert key3 is not None and key3 != key1


# ---------------------------------------------------------------------------
# Live/finalize parity: the same evidence through both paths yields identical
# blocks (is_open aside), because both share one consolidation body.
# ---------------------------------------------------------------------------


def _build_local_recording(tmp_path, name):
    """A minimal LOCAL recording dir (recording.db + ledger + one chunk)."""
    import json

    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    rec_dir = tmp_path / name
    rec_dir.mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()
    recording = crud.insert_recording(session, {
        "timestamp": 1000.0, "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080, "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5, "double_click_distance_pixels": 5.0,
    })
    cs, ce = 1000.0, 4600.0
    crud.insert_window_event(session, recording, cs + 1, {
        "app_bundle_id": "com.example.unknownbenign", "window_id": "w0",
        "title": "file.py", "app_name": "Editor",
    })
    for off in (10.0, 20.0, 30.0):
        crud.insert_action_event(session, recording, cs + off, {
            "name": "click", "mouse_x": 10.0, "mouse_y": 20.0,
            "mouse_button_name": "left", "mouse_pressed": True,
        })
        crud.insert_screenshot(session, recording, cs + off, {
            "image_path": f"screenshots/{cs + off}.jpg",
        })
    session.close()
    engine.dispose()

    (rec_dir / "chunk_0000.mp4").write_bytes(b"\x00" * 1024)
    (rec_dir / "chunk_0000_manifest.json").write_text(json.dumps({
        "format_version": 2, "chunk_index": 0, "chunk_start": cs, "chunk_end": ce,
        "stats": {"total_events": 3, "total_window_switches": 1},
        "blocked_intervals": [],
    }))
    (rec_dir / "events_0000.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"_meta": True, "format_version": 2},
        {"type": "window.switch", "timestamp": cs + 10.0,
         "app_bundle_id": "com.microsoft.VSCode", "window_title": "file.py"},
        {"type": "key.type", "timestamp": cs + 20.0, "text": "def foo():"},
        {"type": "mouse.singleclick", "timestamp": cs + 30.0},
    ]) + "\n")

    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    ledger.seed_chunk(0)
    ledger.mark_staged(0)
    ledger.freeze_chunks_expected(1)
    (rec_dir / ".recording_intent").write_text(json.dumps({
        "version": 2, "destination": "local", "retention_policy": "keep_forever",
        "retention_params": {}, "show_on_website": False,
    }))
    (rec_dir / ".recording_id").write_text(name)
    return rec_dir


class _SameWorkProvider:
    """Returns two SAME-work fine fragments consolidation MERGES into one block."""

    def segment(self, activity_summary):
        return {
            "tasks": [
                {"start_ts": 1010.0, "end_ts": 1200.0, "name": "Diary schema design",
                 "category": "development", "apps_used": ["com.figma.Desktop"],
                 "source": _MODEL},
                {"start_ts": 1300.0, "end_ts": 1500.0, "name": "Diary schema design",
                 "category": "development", "apps_used": ["com.figma.Desktop"],
                 "source": _MODEL},
            ],
            "summary": {"overview": "x", "primary_focus": "development",
                        "time_breakdown": {}, "key_accomplishments": []},
            "tags": [],
        }


def test_live_and_finalize_paths_yield_identical_blocks(tmp_path, monkeypatch):
    import screencap.segmentation.routing as routing
    import screencap.terminal_stage as ts
    from screencap.pipeline_state import PipelineLedger

    monkeypatch.setattr(ts, "_RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(
        routing, "build_day_split_provider", lambda *a, **k: _SameWorkProvider(),
    )
    # Patch the on-device block namer seam with a scripted fake (Vision-free).
    monkeypatch.setattr(
        ts, "_build_block_namer", lambda rec: _ScriptedNamer(name="Diary schema design"),
    )

    live_dir = _build_local_recording(tmp_path, "live-rec")
    fin_dir = _build_local_recording(tmp_path, "fin-rec")

    ts.run_incremental_segmentation(live_dir)
    ts.run_terminal_stage(fin_dir)

    live = PipelineLedger(live_dir / "recording.db").read_task_segments()
    fin = PipelineLedger(fin_dir / "recording.db").read_task_segments()

    # Both fold the two same-work fragments into ONE named block over the stretch.
    assert [(s.name, s.start_ts, s.end_ts) for s in live] == [
        ("Diary schema design", 1010.0, 1500.0),
    ]
    assert [(s.name, s.start_ts, s.end_ts) for s in fin] == [
        ("Diary schema design", 1010.0, 1500.0),
    ]
    # is_open is the ONLY difference: set on the live trailing block, clear at
    # finalize.
    assert live[0].is_open is True
    assert fin[0].is_open is False

