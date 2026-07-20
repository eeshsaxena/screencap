"""Day narrative generation + gating + staleness (day diary, U4, R8/R9).

Two layers, all Vision/OCR-free and privacy-marked so the CI privacy lane runs
them:

* the PURE narrative core (:mod:`screencap.segmentation.narrative`) — the
  closed-block staleness fingerprint (KTD-6), the evidence-bound digest
  (names + bullets + rollups only), the model-narrator seam, and the honest
  heuristic fallback + its sanitizer chokepoint;
* the terminal-stage wiring (:func:`screencap.terminal_stage._run_day_narrative`)
  over a real ``recording.db`` — per-recording outcome GATING (AE3: mechanical-only
  writes no row; a thin produced day writes a short narrative; a mixed day yields a
  partial covering only the produced recording) and the KTD-6 regenerate-only-on-
  change cadence.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Pure core fixtures.
# ---------------------------------------------------------------------------


def _block(key, name, *, bullets=(), start=0.0, end=600.0, thread=None, is_open=False):
    from screencap.segmentation.narrative import NarrativeBlock

    return NarrativeBlock(
        key=key, name=name, bullets=tuple(bullets),
        start_ts=float(start), end_ts=float(end), thread_id=thread, is_open=is_open,
    )


class _ScriptedNarrator:
    """A stub day narrator: scripted CallResults over ``call_day_narrative``."""

    def __init__(self, *, results=None, text="A written day."):
        from screencap.segmentation.providers.ondevice import CallResult

        self._results = list(results or [])
        self._text = text
        self.calls: list[dict] = []
        self._CallResult = CallResult

    def call_day_narrative(self, digest_payload):
        assert digest_payload.get("stripped") is True, (
            "narrative payloads must carry the strip attestation"
        )
        self.calls.append(digest_payload)
        if self._results:
            return self._results.pop(0)
        return self._CallResult(self._text, None)


# ---------------------------------------------------------------------------
# Staleness fingerprint (KTD-6) — CLOSED blocks only.
# ---------------------------------------------------------------------------


def test_growing_open_block_alone_does_not_change_fingerprint():
    """The live ``is_open`` trailing block is EXCLUDED, so it growing each tick
    never regenerates the narrative (KTD-6)."""
    from screencap.segmentation.narrative import narrative_fingerprint

    closed = _block("blk_a", "Payroll run", start=0, end=600)
    fp1 = narrative_fingerprint(
        [closed, _block("blk_b", "Live work", start=600, end=1200, is_open=True)]
    )
    fp2 = narrative_fingerprint(
        [closed, _block("blk_b", "Live work", start=600, end=99999, is_open=True)]
    )
    assert fp1 is not None
    assert fp1 == fp2


def test_block_rename_changes_fingerprint():
    """A closed block rename changes the fingerprint → the narrative regenerates."""
    from screencap.segmentation.narrative import narrative_fingerprint

    fp1 = narrative_fingerprint([_block("blk_a", "Payroll run")])
    fp2 = narrative_fingerprint([_block("blk_a", "Payroll reconciliation")])
    assert fp1 != fp2


def test_new_closed_block_changes_fingerprint():
    fp1 = _fp([_block("blk_a", "Payroll run")])
    fp2 = _fp([_block("blk_a", "Payroll run"), _block("blk_b", "Email triage", start=700, end=900)])
    assert fp1 != fp2


def test_fingerprint_none_when_no_closed_named_block():
    """Only an open block (or only unnamed blocks) → no closed named block → the
    fingerprint is None and the caller writes no narrative row (R9)."""
    from screencap.segmentation.narrative import narrative_fingerprint

    assert narrative_fingerprint(
        [_block("blk_a", "Live work", is_open=True)]
    ) is None
    assert narrative_fingerprint([_block("blk_a", "")]) is None  # honest unnamed


def _fp(blocks):
    from screencap.segmentation.narrative import narrative_fingerprint

    return narrative_fingerprint(blocks)


# ---------------------------------------------------------------------------
# Evidence-bound digest (names + bullets + rollups ONLY) + narrator seam.
# ---------------------------------------------------------------------------


def test_digest_is_exactly_names_bullets_rollups():
    """The narrator sees ONLY the block projection — names, minutes, bullets, and
    thread rollups — never raw evidence (R9/KTD-4)."""
    from screencap.segmentation.narrative import build_narrative

    blocks = [
        _block("blk_a", "Diary schema", bullets=("Sketched the block table",),
               start=0, end=1800, thread="thr_1"),
        _block("blk_b", "Diary schema", start=3600, end=5400, thread="thr_1"),
    ]
    narrator = _ScriptedNarrator()
    build_narrative(blocks, narrator=narrator)

    assert len(narrator.calls) == 1
    digest = narrator.calls[0]
    assert digest["kind"] == "day-narrative"
    entries = digest["timeline"]
    assert [e["name"] for e in entries] == ["Diary schema", "Diary schema"]
    # Only the allowed keys ride each entry — no raw evidence leaks in.
    allowed = {"name", "minutes", "bullets", "thread"}
    for e in entries:
        assert set(e) <= allowed
    # The thread rollup rides (2 sittings), computed at read time.
    assert entries[0]["thread"] == {"total_minutes": 60, "sittings": 2}


def test_model_narrator_success_records_no_fallback_reason():
    from screencap.segmentation.narrative import build_narrative

    res = build_narrative(
        [_block("blk_a", "Payroll run")], narrator=_ScriptedNarrator(text="You ran payroll.")
    )
    assert res.text == "You ran payroll."
    assert res.reason is None  # a model narrated — no degrade marker.


def test_no_model_falls_to_heuristic_with_observable_marker():
    """No narrator → the honest heuristic + the KTD-10 observable ``no-model``
    marker (never a silent degrade)."""
    from screencap.segmentation.narrative import REASON_NO_MODEL, build_narrative

    res = build_narrative([_block("blk_a", "Payroll run")], narrator=None)
    assert "Payroll run" in res.text
    assert res.reason == REASON_NO_MODEL


def test_bullet_free_day_yields_names_only_narrative():
    """Evidence bound (R9/AE3): with no bullets the heuristic asserts only names,
    never invented detail."""
    from screencap.segmentation.narrative import build_narrative

    res = build_narrative(
        [_block("blk_a", "Payroll run"), _block("blk_b", "Email triage", start=700, end=1200)],
        narrator=None,
    )
    assert res.text == "Payroll run. Email triage."


def test_bullets_enrich_the_heuristic_when_present():
    from screencap.segmentation.narrative import build_narrative

    res = build_narrative(
        [_block("blk_a", "Diary schema", bullets=("Sketched the table", "Wrote the migration"))],
        narrator=None,
    )
    assert res.text == "Diary schema: Sketched the table; Wrote the migration."


def test_injected_markup_is_sanitized_in_narrative_text():
    """A prompt-injected block name/bullet cannot carry markup into the day view —
    the sanitizer chokepoint (``sanitize_answer``) neutralizes it."""
    from screencap.segmentation.narrative import build_narrative

    res = build_narrative(
        [_block("blk_a", "Payroll <script>alert(1)</script>",
                bullets=("<img src=x onerror=1>",))],
        narrator=None,
    )
    assert "<script>" not in res.text
    assert "<img" not in res.text
    # Escaped losslessly (HTML-escape, not deletion) so the prose survives.
    assert "&lt;" in res.text


# ---------------------------------------------------------------------------
# Terminal-stage wiring: per-recording GATING + KTD-6 cadence (real recording.db).
# ---------------------------------------------------------------------------


def _make_recording(rec_dir: Path, started: float = 1000.0) -> None:
    """Minimal recording.db: a ``recording`` row so the ledger resolves an id, then
    the pipeline schema (the narrative table)."""
    from screencap.pipeline_state import ensure_pipeline_state_schema

    rec_dir.mkdir(parents=True, exist_ok=True)
    db = rec_dir / "recording.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, ?, 2.0)", (started,))
        conn.commit()
    ensure_pipeline_state_schema(db)


def _seed_blocks(rec_dir: Path, blocks: list[dict]) -> None:
    """Write consolidated diary block rows into ``pipeline_task_segments``."""
    from screencap.pipeline_state import PipelineLedger, TaskSegmentRow

    rows = [
        TaskSegmentRow(
            task_index=i,
            start_ts=float(b["start"]),
            end_ts=float(b["end"]),
            name=b["name"],
            metadata=(json.dumps({"bullets": b["bullets"]}) if b.get("bullets") else None),
            block_id=b.get("block_id", f"blk_{i}"),
            thread_id=b.get("thread"),
            is_open=b.get("is_open", False),
        )
        for i, b in enumerate(blocks)
    ]
    PipelineLedger(rec_dir / "recording.db").replace_task_segments(rows)


def _outcome(rec_dir: Path, reason: str) -> None:
    from screencap.pipeline_state import PipelineLedger

    PipelineLedger(rec_dir / "recording.db").set_recording_outcome(reason)


def _run_narrative(rec_dir: Path):
    """Run the terminal-stage narrative pass over ``rec_dir`` (heuristic narrator)."""
    from screencap import terminal_stage
    from screencap.pipeline_state import PipelineLedger

    ledger = PipelineLedger(rec_dir / "recording.db")
    terminal_stage._run_day_narrative(rec_dir, ledger, is_live=False)
    return ledger.get_day_narrative()


@pytest.fixture(autouse=True)
def _no_model_narrator(monkeypatch):
    """Force the heuristic narrator in the wiring tests (no provider construction /
    subprocess), so the gating + cadence assertions are deterministic."""
    from screencap import terminal_stage

    monkeypatch.setattr(terminal_stage, "_build_narrator", lambda _dir: None)


def test_mechanical_only_recording_writes_no_narrative(tmp_path):
    """AE3: a mechanical-only recording contributes NO narrative row."""
    rec = tmp_path / "rec-mech"
    _make_recording(rec)
    _seed_blocks(rec, [{"name": "task_1", "start": 0, "end": 300, "block_id": "blk_0"}])
    _outcome(rec, "mechanical_only")

    assert _run_narrative(rec) is None


def test_nothing_to_name_recording_writes_no_narrative(tmp_path):
    rec = tmp_path / "rec-nada"
    _make_recording(rec)
    _outcome(rec, "nothing_to_name")
    assert _run_narrative(rec) is None


def test_thin_produced_day_writes_short_narrative(tmp_path):
    """A thin produced day → a short, evidence-bound narrative row (R8/R9)."""
    rec = tmp_path / "rec-thin"
    _make_recording(rec)
    _seed_blocks(rec, [{"name": "Payroll run", "start": 0, "end": 600, "block_id": "blk_0"}])
    _outcome(rec, "produced_tasks")

    row = _run_narrative(rec)
    assert row is not None
    assert row.narrative == "Payroll run."
    assert row.reason == "no-model"  # observable heuristic marker (KTD-10)


def test_mixed_day_partial_narrative_only_covers_produced_recording(tmp_path):
    """AE3 (narrative half): across a MIXED day the produced recording gets a
    narrative and the mechanical one gets none — the day view's partial narrative
    is the union of the surviving produced rows."""
    produced = tmp_path / "rec-produced"
    _make_recording(produced)
    _seed_blocks(produced, [{"name": "Design review", "start": 0, "end": 900, "block_id": "blk_0"}])
    _outcome(produced, "produced_tasks")

    mechanical = tmp_path / "rec-mechanical"
    _make_recording(mechanical)
    _seed_blocks(mechanical, [{"name": "task_1", "start": 0, "end": 120, "block_id": "blk_0"}])
    _outcome(mechanical, "mechanical_only")

    prod_row = _run_narrative(produced)
    mech_row = _run_narrative(mechanical)

    assert prod_row is not None and "Design review" in prod_row.narrative
    assert mech_row is None


def test_unchanged_closed_set_skips_regeneration(tmp_path, monkeypatch):
    """KTD-6: a second pass over an UNCHANGED closed block set does NOT regenerate."""
    from screencap.segmentation import narrative as narrative_mod

    calls = _count_build_narrative(monkeypatch, narrative_mod)

    rec = tmp_path / "rec"
    _make_recording(rec)
    _seed_blocks(rec, [{"name": "Payroll run", "start": 0, "end": 600, "block_id": "blk_0"}])
    _outcome(rec, "produced_tasks")

    _run_narrative(rec)
    _run_narrative(rec)
    assert calls["n"] == 1  # generated once, skipped the unchanged second pass.


def test_rename_regenerates_narrative(tmp_path, monkeypatch):
    from screencap.segmentation import narrative as narrative_mod

    calls = _count_build_narrative(monkeypatch, narrative_mod)

    rec = tmp_path / "rec"
    _make_recording(rec)
    _seed_blocks(rec, [{"name": "Payroll run", "start": 0, "end": 600, "block_id": "blk_0"}])
    _outcome(rec, "produced_tasks")
    _run_narrative(rec)

    # A rename (same block_id) changes the closed-set fingerprint → regenerate.
    _seed_blocks(rec, [{"name": "Payroll reconciliation", "start": 0, "end": 600, "block_id": "blk_0"}])
    row = _run_narrative(rec)
    assert calls["n"] == 2
    assert row.narrative == "Payroll reconciliation."


def test_growing_open_block_does_not_regenerate_each_tick(tmp_path, monkeypatch):
    """KTD-6: a still-growing open trailing block alone does NOT trigger a
    regeneration (the fingerprint excludes it)."""
    from screencap.segmentation import narrative as narrative_mod

    calls = _count_build_narrative(monkeypatch, narrative_mod)

    rec = tmp_path / "rec"
    _make_recording(rec)
    _seed_blocks(rec, [
        {"name": "Payroll run", "start": 0, "end": 600, "block_id": "blk_0"},
        {"name": "Live work", "start": 600, "end": 700, "block_id": "blk_1", "is_open": True},
    ])
    _outcome(rec, "produced_tasks")
    _run_narrative(rec)

    # The open block grows; the closed set is unchanged → no regeneration.
    _seed_blocks(rec, [
        {"name": "Payroll run", "start": 0, "end": 600, "block_id": "blk_0"},
        {"name": "Live work", "start": 600, "end": 5000, "block_id": "blk_1", "is_open": True},
    ])
    _run_narrative(rec)
    assert calls["n"] == 1


def _count_build_narrative(monkeypatch, narrative_mod):
    """Wrap ``narrative.build_narrative`` with a call counter (terminal_stage
    imports it by-name inside the function, so patching the module attr is seen)."""
    counter = {"n": 0}
    real = narrative_mod.build_narrative

    def _counting(*a, **k):
        counter["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(narrative_mod, "build_narrative", _counting)
    return counter
