"""Day-diary block search index — the ``diary_fts`` sibling table in ``content_index.db``.

Covers U6 of the day-diary plan (R5 / KTD-8):

* AE2 (search half): a block is found weeks later (a DIFFERENT recording) by a term
  present only in its topic BULLETS, landing on its ``(recording, block_id)`` pointer
  + ms span.
* Idempotence: a re-consolidation REPLACES a recording's diary rows, so a block that
  vanished / was renamed / lost bullets disappears from results (whole-recording
  delete-then-insert).
* Fallback: FTS5 unavailable → the escaped-LIKE fallback returns the same block.
* Default-on (P2): the diary write is INDEPENDENT of the default-off
  ``content_index_enabled`` flag — a block is searchable with OCR indexing OFF.
* Narrative never indexed (KTD-8): a distinctive narrative term never matches.
* Pointer-only: a hit carries ids + span + a bounded snippet, never a media path.
* Deletion methods (the seam U5 calls): whole-recording + overlapping-interval diary
  purge, and the whole-recording delete cascade.
* Purge-race safety (P1 — the load-bearing security requirement): the diary write
  RE-READS the block rows UNDER ``content_index_write_lock()`` immediately before the
  write, so a concurrent retroactive-disable purge is NEVER resurrected — proven under
  BOTH orderings (write-then-purge and purge-then-write) plus a concurrent race.

Privacy-marked + Vision-free (raw sqlite + the pipeline ledger / FTS — no OCR/Vision),
so the CI ``pytest -m privacy`` lane runs them and the Linux backstop lane can too.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

import screencap.content_index as ci
from screencap.content_index import (
    ContentIndex,
    IndexState,
    content_index_write_lock,
    write_recording_diary,
)
from screencap.pipeline_state import (
    PipelineLedger,
    TaskSegmentRow,
    ensure_pipeline_state_schema,
)

pytestmark = pytest.mark.privacy

# Hard upper bound on any single lock acquisition so a deadlock fails FAST.
_LOCK_TIMEOUT_S = 20.0


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_store(tmp_path, monkeypatch):
    """Point the global content-index path (store DB + the write-lock file) at a
    per-test temp dir so no test EVER touches the real ``~/.screencap`` store.

    ``write_recording_diary`` / ``content_index_write_lock`` derive both the store
    path and the flock path from ``default_index_path()``; redirecting it here keeps
    the whole surface hermetic.
    """
    store = tmp_path / "content_index.db"
    monkeypatch.setattr(ci, "default_index_path", lambda: store)
    return store


def _seed_recording(rec_dir: Path, blocks: list[tuple]) -> None:
    """Create a ``recording.db`` with one recording row + the given diary BLOCKS.

    ``blocks`` is a list of ``(block_id, name, bullets, start_ts, end_ts)`` — the
    same shape a consolidation pass persists. Bullets ride ``metadata`` JSON under
    the ``bullets`` key (the U3 field the wire projector reads).
    """
    rec_dir.mkdir(parents=True, exist_ok=True)
    db = rec_dir / "recording.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, 1000.0, 2.0)")
        conn.commit()
    ensure_pipeline_state_schema(db)
    _replace_blocks(rec_dir, blocks)


def _replace_blocks(rec_dir: Path, blocks: list[tuple]) -> None:
    """Rewrite the recording's agent block rows (a re-consolidation)."""
    rows = [
        TaskSegmentRow(
            task_index=i,
            start_ts=float(start_ts),
            end_ts=float(end_ts),
            name=name,
            metadata=json.dumps({"bullets": list(bullets)}) if bullets else None,
            block_id=block_id,
        )
        for i, (block_id, name, bullets, start_ts, end_ts) in enumerate(blocks)
    ]
    PipelineLedger(rec_dir / "recording.db").replace_task_segments(rows)


def _delete_block_task_row(rec_dir: Path, block_id: str) -> None:
    """Delete a block's task rows from ``recording.db`` (the retroactive purge's
    row-side delete — the recording.db half of a retroactive-disable cascade)."""
    with sqlite3.connect(str(rec_dir / "recording.db")) as conn:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(
            "DELETE FROM pipeline_task_segments WHERE block_id = ?", (block_id,)
        )
        conn.commit()


def _diary_hits(store_path: Path, term: str, *, recording: str | None = None):
    with ContentIndex(store_path) as store:
        return store.search_diary(term, recording=recording, limit=500).hits


def _run_with_watchdog(fn) -> None:
    """Run ``fn`` on a thread bounded by ``_LOCK_TIMEOUT_S`` so a lock deadlock
    fails fast (a join timeout) instead of hanging the CI lane."""
    err: list[BaseException] = []

    def _target():
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 — re-raised on the main thread
            err.append(e)

    t = threading.Thread(target=_target)
    t.start()
    t.join(timeout=_LOCK_TIMEOUT_S)
    assert not t.is_alive(), "lock acquisition deadlocked (watchdog timeout)"
    if err:
        raise err[0]


# --------------------------------------------------------------------------
# AE2 — fuzzy topic recall by bullet text, cross-recording ("weeks later")
# --------------------------------------------------------------------------


def test_search_finds_block_by_bullet_text_across_recordings(_hermetic_store, tmp_path):
    """AE2 (search half): a term present ONLY in a block's bullets finds that block
    in a DIFFERENT recording (a later day) — pointer + span, ranked."""
    morning = tmp_path / "2026-06-01_0900"
    _seed_recording(
        morning,
        [(
            "blk-morning",
            "Kick technique review",
            ["broke down the spinning back kick chamber", "compared stance depth"],
            10.0,
            120.0,
        )],
    )
    # An unrelated LATER recording so the search is genuinely cross-recording.
    other = tmp_path / "2026-06-20_1400"
    _seed_recording(
        other,
        [("blk-other", "Invoice triage", ["reconciled the quarterly invoice"], 5.0, 60.0)],
    )

    write_recording_diary("2026-06-01_0900", morning / "recording.db")
    write_recording_diary("2026-06-20_1400", other / "recording.db")

    hits = _diary_hits(_hermetic_store, "spinning")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.recording == "2026-06-01_0900"
    assert hit.block_id == "blk-morning"
    assert hit.start_ms == 10_000 and hit.end_ms == 120_000
    assert "spinning" in hit.snippet.lower()

    # The block name is searchable too (R5 covers name AND bullets).
    assert {h.block_id for h in _diary_hits(_hermetic_store, "kick")} == {"blk-morning"}
    # A recording filter scopes the search.
    assert _diary_hits(_hermetic_store, "spinning", recording="2026-06-20_1400") == []


# --------------------------------------------------------------------------
# Idempotence — a re-consolidation REPLACES a recording's rows
# --------------------------------------------------------------------------


def test_reconsolidation_drops_vanished_block(_hermetic_store, tmp_path):
    """A block that disappears on re-consolidation disappears from search (whole-
    recording delete-then-insert)."""
    rec = tmp_path / "rec"
    _seed_recording(
        rec,
        [
            ("blk-a", "Design systems audit", ["tokenized the color ramp"], 10.0, 60.0),
            ("blk-b", "Payroll run", ["approved the March cycle"], 70.0, 120.0),
        ],
    )
    write_recording_diary("rec", rec / "recording.db")
    assert {h.block_id for h in _diary_hits(_hermetic_store, "payroll")} == {"blk-b"}

    # Re-consolidate: blk-b is gone (merged away / dropped). Re-write.
    _replace_blocks(
        rec,
        [("blk-a", "Design systems audit", ["tokenized the color ramp"], 10.0, 60.0)],
    )
    write_recording_diary("rec", rec / "recording.db")

    assert _diary_hits(_hermetic_store, "payroll") == [], "stale block survived re-consolidation"
    assert {h.block_id for h in _diary_hits(_hermetic_store, "tokenized")} == {"blk-a"}


def test_reconsolidation_replaces_changed_text(_hermetic_store, tmp_path):
    """Same ``block_id``, new name+bullets → the OLD text stops matching, the NEW
    text matches (a re-carve that renamed/re-bulleted a block)."""
    rec = tmp_path / "rec"
    _seed_recording(rec, [("blk", "Old name", ["obsolete detail"], 10.0, 60.0)])
    write_recording_diary("rec", rec / "recording.db")
    assert {h.block_id for h in _diary_hits(_hermetic_store, "obsolete")} == {"blk"}

    _replace_blocks(rec, [("blk", "Fresh name", ["current detail"], 10.0, 60.0)])
    write_recording_diary("rec", rec / "recording.db")

    assert _diary_hits(_hermetic_store, "obsolete") == []
    assert {h.block_id for h in _diary_hits(_hermetic_store, "current")} == {"blk"}


# --------------------------------------------------------------------------
# Fallback — FTS5 unavailable → escaped-LIKE returns the same hits
# --------------------------------------------------------------------------


def test_like_fallback_returns_same_hits(_hermetic_store, tmp_path, monkeypatch):
    """With FTS5 compiled out, the escaped-LIKE fallback finds the same block and
    reports ``index_degraded`` (never a silent empty)."""
    monkeypatch.setattr(ContentIndex, "_probe_fts5", staticmethod(lambda conn: False))

    rec = tmp_path / "rec"
    _seed_recording(
        rec, [("blk", "Kick technique review", ["spinning back kick"], 10.0, 60.0)]
    )
    write_recording_diary("rec", rec / "recording.db")

    with ContentIndex(_hermetic_store) as store:
        assert not store.fts_available  # confirm we exercised the LIKE path
        res = store.search_diary("spinning")
    assert res.index_state is IndexState.INDEX_DEGRADED
    assert {h.block_id for h in res.hits} == {"blk"}
    assert res.hits[0].start_ms == 10_000 and res.hits[0].end_ms == 60_000


# --------------------------------------------------------------------------
# Default-on (P2) — independent of the default-off content_index_enabled flag
# --------------------------------------------------------------------------


def test_diary_write_independent_of_content_index_enabled(
    _hermetic_store, tmp_path, monkeypatch
):
    """The terminal-stage hook writes diary rows even with OCR content indexing OFF:
    diary block search is core / always-on (R5), NOT gated by the
    ``content_index_enabled`` flag that gates only the ``content_fts`` pass."""
    from screencap import config
    from screencap.terminal_stage import _index_diary_blocks

    # Force the OCR content-index flag OFF (env wins over the dev's config.toml),
    # then prove the diary still indexes — the write is independent of the flag.
    monkeypatch.setenv("SCREENCAP_CONTENT_INDEX", "0")
    assert config.get_content_index_enabled() is False

    rec = tmp_path / "rec"
    _seed_recording(
        rec, [("blk", "Design systems audit", ["tokenized the color ramp"], 10.0, 60.0)]
    )
    # The exact hook terminal_stage calls after consolidation (fail-open wrapper).
    _index_diary_blocks(rec)

    assert {h.block_id for h in _diary_hits(_hermetic_store, "tokenized")} == {"blk"}


# --------------------------------------------------------------------------
# Narrative never indexed (KTD-8)
# --------------------------------------------------------------------------


def test_narrative_text_is_never_indexed(_hermetic_store, tmp_path):
    """Only block name + bullets enter the index — a distinctive narrative-only word
    is never searchable (KTD-8)."""
    rec = tmp_path / "rec"
    _seed_recording(
        rec, [("blk", "Kick technique review", ["spinning back kick"], 10.0, 60.0)]
    )
    # A narrative row exists with a term found NOWHERE in the block name/bullets.
    PipelineLedger(rec / "recording.db").set_day_narrative(
        "Today you drilled a rare capoeira flourish worth remembering.",
        "fp-1",
        None,
    )
    write_recording_diary("rec", rec / "recording.db")

    assert _diary_hits(_hermetic_store, "capoeira") == [], "narrative text leaked into the index"
    # …while the block's own bullet term is found.
    assert {h.block_id for h in _diary_hits(_hermetic_store, "spinning")} == {"blk"}


# --------------------------------------------------------------------------
# Pointer-only shape + empty-store guard
# --------------------------------------------------------------------------


def test_hit_is_pointer_only(_hermetic_store, tmp_path):
    """A hit exposes ONLY ids + span + a bounded snippet (no path / no bytes)."""
    from screencap.content_index import DiaryHit

    rec = tmp_path / "rec"
    _seed_recording(rec, [("blk", "Audit", ["invoice reconciliation notes"], 1.0, 2.0)])
    write_recording_diary("rec", rec / "recording.db")

    hit = _diary_hits(_hermetic_store, "invoice")[0]
    # The dataclass has exactly the pointer-only fields — no media path can ride it.
    assert set(DiaryHit.__dataclass_fields__) == {
        "recording", "block_id", "start_ms", "end_ms", "snippet", "score",
    }
    assert hit.recording == "rec" and hit.block_id == "blk"


def test_empty_blocks_do_not_create_a_store(_hermetic_store, tmp_path):
    """Nothing to index AND no store yet → no empty store is materialised."""
    rec = tmp_path / "rec"
    _seed_recording(rec, [])  # no blocks
    written = write_recording_diary("rec", rec / "recording.db")
    assert written == 0
    assert not _hermetic_store.exists()


# --------------------------------------------------------------------------
# Deletion methods the U5 purge cascade calls
# --------------------------------------------------------------------------


def test_delete_recording_diary_interval_removes_overlapping_blocks(_hermetic_store, tmp_path):
    """The retroactive-disable primitive (U5 seam): a block whose ms span OVERLAPS
    the disabled interval is removed; a non-overlapping block survives."""
    rec = tmp_path / "rec"
    _seed_recording(
        rec,
        [
            ("blk-early", "Morning email", ["triaged inbox"], 100.0, 200.0),
            ("blk-late", "Kick drills", ["spinning back kick"], 300.0, 400.0),
        ],
    )
    write_recording_diary("rec", rec / "recording.db")

    with content_index_write_lock(), ContentIndex(_hermetic_store) as store:
        # Disable [350s, +inf): overlaps blk-late only.
        store.delete_recording_diary_interval("rec", 350_000, None)

    assert _diary_hits(_hermetic_store, "spinning") == []
    assert {h.block_id for h in _diary_hits(_hermetic_store, "triaged")} == {"blk-early"}

    # The whole-recording purge + the full delete cascade both clear everything.
    with ContentIndex(_hermetic_store) as store:
        store.delete_recording_diary("rec")
    assert _diary_hits(_hermetic_store, "triaged") == []


def test_delete_recording_cascades_into_diary(_hermetic_store, tmp_path):
    """A full-recording delete removes its diary block rows too (R13)."""
    rec = tmp_path / "rec"
    _seed_recording(rec, [("blk", "Audit", ["design tokens"], 1.0, 2.0)])
    write_recording_diary("rec", rec / "recording.db")
    assert _diary_hits(_hermetic_store, "tokens")

    with ContentIndex(_hermetic_store) as store:
        store.delete_recording("rec")
    assert _diary_hits(_hermetic_store, "tokens") == []


# --------------------------------------------------------------------------
# P1 purge-race safety — the barrier under BOTH orderings + a concurrent race
# --------------------------------------------------------------------------


def _purge_diary_like_scrub_worker(
    store_path: Path, rec_dir: Path, recording: str, block_id: str,
    start_ms: int, end_ms: int | None,
) -> None:
    """Mirror the scrub-worker retroactive-disable ordering for the diary sink.

    scrub_worker deletes the source rows FIRST (lock-free), THEN takes
    ``content_index_write_lock()`` and deletes the index rows. Here the source is
    ``recording.db``'s block rows: delete them lock-free, then purge the diary rows
    under the lock — exactly the ordering the P1 barrier relies on.
    """
    _delete_block_task_row(rec_dir, block_id)  # 1. lock-free source delete
    with content_index_write_lock(), ContentIndex(store_path) as store:  # 2. under-lock
        if store.available:
            store.delete_recording_diary_interval(recording, start_ms, end_ms)


def test_write_then_purge_block_absent(_hermetic_store, tmp_path):
    """Ordering 1 (write-then-purge): the write lands, the purge then removes the
    disabled block → it is absent; the other block survives."""
    rec = tmp_path / "rec"
    _seed_recording(
        rec,
        [
            ("blk-keep", "Design audit", ["color tokens"], 100.0, 200.0),
            ("blk-drop", "Kick drills", ["spinning back kick"], 300.0, 400.0),
        ],
    )
    _run_with_watchdog(lambda: write_recording_diary("rec", rec / "recording.db"))
    assert {h.block_id for h in _diary_hits(_hermetic_store, "spinning")} == {"blk-drop"}

    _run_with_watchdog(lambda: _purge_diary_like_scrub_worker(
        _hermetic_store, rec, "rec", "blk-drop", 300_000, None,
    ))
    assert _diary_hits(_hermetic_store, "spinning") == [], "purged block survived"
    assert {h.block_id for h in _diary_hits(_hermetic_store, "tokens")} == {"blk-keep"}


def test_purge_then_write_block_not_resurrected(_hermetic_store, tmp_path):
    """Ordering 2 (purge-then-write — the load-bearing case): a purge deletes the
    block's SOURCE rows and its diary rows; a later consolidation write RE-READS the
    source under the lock, sees the block gone, and does NOT resurrect it.

    This is the barrier: because ``write_recording_diary`` re-reads ``recording.db``
    UNDER ``content_index_write_lock()`` immediately before writing, a purge that
    completed first wins. A write that had cached the pre-purge rows would resurrect
    the disabled block here."""
    rec = tmp_path / "rec"
    _seed_recording(
        rec,
        [
            ("blk-keep", "Design audit", ["color tokens"], 100.0, 200.0),
            ("blk-drop", "Kick drills", ["spinning back kick"], 300.0, 400.0),
        ],
    )
    # A prior write put BOTH blocks in the index.
    _run_with_watchdog(lambda: write_recording_diary("rec", rec / "recording.db"))
    assert {h.block_id for h in _diary_hits(_hermetic_store, "spinning")} == {"blk-drop"}

    # Retroactive disable of blk-drop (source rows + diary rows).
    _run_with_watchdog(lambda: _purge_diary_like_scrub_worker(
        _hermetic_store, rec, "rec", "blk-drop", 300_000, None,
    ))
    # A consolidation re-run writes again — the under-lock re-read sees blk-drop gone.
    _run_with_watchdog(lambda: write_recording_diary("rec", rec / "recording.db"))

    assert _diary_hits(_hermetic_store, "spinning") == [], "in-flight-purged block resurrected"
    assert {h.block_id for h in _diary_hits(_hermetic_store, "tokens")} == {"blk-keep"}


def test_source_reread_happens_under_the_write_lock(_hermetic_store, tmp_path):
    """The barrier is a RE-READ UNDER the lock: prove the rows are read while
    ``content_index_write_lock()`` is held (an in-flight purge racing the write is
    therefore serialized against it), and that a purge running AT that read instant
    is reflected."""
    rec = tmp_path / "rec"
    _seed_recording(
        rec,
        [
            ("blk-keep", "Design audit", ["color tokens"], 100.0, 200.0),
            ("blk-drop", "Kick drills", ["spinning back kick"], 300.0, 400.0),
        ],
    )
    _run_with_watchdog(lambda: write_recording_diary("rec", rec / "recording.db"))

    observed = {"locked_at_read": None}

    def _reader_that_purges_in_flight(db_path):
        # Executed by write_recording_diary UNDER the lock, just before the write.
        observed["locked_at_read"] = ci._WRITE_LOCK.locked()
        # A retroactive disable lands WHILE the write holds the lock: delete the
        # block's SOURCE rows lock-free (the recorder purge's screenshot-unlink
        # analog — recording.db writes are not under the content-index lock), then
        # read live so the re-read reflects the purge.
        _delete_block_task_row(rec, "blk-drop")
        return ci._read_diary_source_rows(db_path)

    _run_with_watchdog(lambda: write_recording_diary(
        "rec", rec / "recording.db", rows_reader=_reader_that_purges_in_flight,
    ))

    assert observed["locked_at_read"] is True, "source re-read did NOT happen under the lock"
    assert _diary_hits(_hermetic_store, "spinning") == [], "in-flight purge was resurrected"
    assert {h.block_id for h in _diary_hits(_hermetic_store, "tokens")} == {"blk-keep"}


def test_concurrent_write_and_purge_converge_without_deadlock(_hermetic_store, tmp_path):
    """A write and a purge contending on the SAME lock complete within the watchdog
    bound and CONVERGE to purged — the shared lock serializes them without deadlock,
    whatever the interleaving."""
    rec = tmp_path / "rec"
    _seed_recording(
        rec,
        [
            ("blk-keep", "Design audit", ["color tokens"], 100.0, 200.0),
            ("blk-drop", "Kick drills", ["spinning back kick"], 300.0, 400.0),
        ],
    )
    _run_with_watchdog(lambda: write_recording_diary("rec", rec / "recording.db"))

    barrier = threading.Barrier(2, timeout=_LOCK_TIMEOUT_S)
    errors: list[BaseException] = []

    def _writer():
        try:
            barrier.wait()
            write_recording_diary("rec", rec / "recording.db")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def _purger():
        try:
            barrier.wait()
            _purge_diary_like_scrub_worker(
                _hermetic_store, rec, "rec", "blk-drop", 300_000, None,
            )
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    tw = threading.Thread(target=_writer)
    tp = threading.Thread(target=_purger)
    tw.start()
    tp.start()
    tw.join(timeout=_LOCK_TIMEOUT_S)
    tp.join(timeout=_LOCK_TIMEOUT_S)

    assert not tw.is_alive() and not tp.is_alive(), "writer/purge deadlocked"
    assert not errors, f"contention raised: {errors!r}"
    # The block's SOURCE rows were deleted by the purge, so whatever the ordering the
    # re-read/replace can never leave it queryable.
    assert _diary_hits(_hermetic_store, "spinning") == []
