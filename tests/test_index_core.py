"""Tests for the SCR-178 U1 shared content-index core (``index_core``).

``index_range`` is the pure, dependency-injected helper both the live
``chunk_processor`` pass and the (future) backfill engine call, so the
skip/dedup/OCR/write rules live in ONE place. These tests exercise it directly
with a fake OCR engine, a temp dir of ``<ts>.jpg`` screenshots, and a temp
``ContentIndex`` store — no Apple Vision / recording pipeline needed.

The final ``test_do_index_chunk_content_matches_snapshot`` is the
characterization/integration guard: it asserts the live
``chunk_processor._do_index_chunk_content`` delegate still produces the same rows
the inline pre-refactor body did (behavior-preserving extraction).
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import screencap.content_index as content_index
import screencap.engine.dedup as dedup
from screencap.content_index import ContentIndex
from screencap.index_core import IndexRangeResult, index_range
from screencap.privacy.policy import PrivacyAction
from screencap.scrubber import BlockedInterval

# --------------------------------------------------------------------------
# Stubs / helpers
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, blocks: list[_Block]) -> None:
        self.text_blocks = blocks


class _FakeOcr:
    """Records every path it was asked to recognise; returns ts-keyed text."""

    def __init__(self, texts: dict[int, str] | None = None) -> None:
        self.texts = texts or {}
        self.calls: list[int] = []

    def recognize(self, path: Path, **_kw: object) -> _Result:
        ms = int(round(float(path.stem) * 1000))
        self.calls.append(ms)
        return _Result([_Block(self.texts.get(ms, f"generic screen text {ms}"))])


def _make_jpg(path: Path) -> None:
    Image.new("RGB", (8, 8), "white").save(path, "JPEG")


def _make_screenshots(capture_dir: Path, timestamps: list[float]) -> None:
    shots = capture_dir / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    for ts in timestamps:
        _make_jpg(shots / f"{ts:.6f}.jpg")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Distinct-per-frame dHash (nothing deduped) + a redirected store path."""
    counter = {"n": 0}

    def _unique_dhash(_img, *_a, **_k):
        counter["n"] += 1
        return counter["n"] * 100  # well beyond the dedup threshold → never deduped

    monkeypatch.setattr(dedup, "dhash", _unique_dhash)
    monkeypatch.setattr(dedup, "hamming_distance", lambda a, b: abs(a - b))

    store_path = tmp_path / "content_index.db"
    monkeypatch.setattr(content_index, "default_index_path", lambda: store_path)
    return SimpleNamespace(tmp_path=tmp_path, store_path=store_path)


def _search(store_path: Path, term: str):
    with ContentIndex(store_path) as store:
        return store.search(term, limit=100).hits


# --------------------------------------------------------------------------
# index_range — happy path / time-scoping
# --------------------------------------------------------------------------


def test_happy_path_writes_all_in_range_frames(env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0, 130.0, 140.0, 150.0])
    ocr = _FakeOcr({
        110_000: "alpha one",
        120_000: "beta two",
        130_000: "gamma three",
        140_000: "delta four",
        150_000: "epsilon five",
    })

    res = index_range(
        cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path,
        stop_event=threading.Event(),
    )

    assert isinstance(res, IndexRangeResult)
    assert res.rows_written == 5
    assert res.completed_range is True
    assert {h.timestamp_ms for h in _search(env.store_path, "alpha")} == {110_000}
    assert {h.recording for h in _search(env.store_path, "alpha")} == {"rec"}


def test_frames_outside_range_excluded(env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    # 90 and 250 are out of [100, 200); only 120/150 are in.
    _make_screenshots(cap, [90.0, 120.0, 150.0, 250.0])
    ocr = _FakeOcr()

    res = index_range(cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path)

    assert res.rows_written == 2
    assert sorted(ocr.calls) == [120_000, 150_000]


def test_empty_range_does_not_create_store_when_absent(env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [250.0, 300.0])  # all outside [100, 200)
    ocr = _FakeOcr()

    # ``index_range`` returns at the ``not candidates`` early return for an
    # out-of-range chunk — before any store touch — so no empty PII store is
    # created.
    res = index_range(cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path)

    assert res.rows_written == 0
    assert res.completed_range is True
    assert ocr.calls == []  # nothing OCR'd
    assert not env.store_path.exists()  # no empty store created


def test_empty_textless_chunk_does_not_create_store_when_absent(env, tmp_path):
    """In-range frames that all OCR to empty text → no store created (empty-store guard).

    Distinct from the out-of-range case: here there ARE in-range, non-skipped
    candidate frames that reach OCR, but every recognise returns empty text, so
    ``frames`` ends up empty. With no pre-existing store file the write must be
    skipped entirely rather than create an empty PII store.
    """
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0, 130.0])
    # Every frame OCRs to empty text → nothing survives to write.
    ocr = _FakeOcr({110_000: "", 120_000: "", 130_000: ""})

    res = index_range(cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path)

    # Frames were OCR'd (so we exercised the loop), but none produced text.
    assert sorted(ocr.calls) == [110_000, 120_000, 130_000]
    assert res.rows_written == 0
    assert res.completed_range is True
    assert not env.store_path.exists()  # empty-store guard held: no store created


def test_no_screenshots_dir_is_noop(env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()  # no screenshots/ subdir
    ocr = _FakeOcr()

    res = index_range(cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path)

    assert res == IndexRangeResult(rows_written=0, completed_range=True)
    assert ocr.calls == []


# --------------------------------------------------------------------------
# index_range — store unavailable (SCR-192)
# --------------------------------------------------------------------------


def test_store_unavailable_signals_distinctly(env, tmp_path):
    """An un-openable store must NOT report a clean completion (SCR-192).

    When there ARE in-range frames to write but the store can't be opened
    (here: a symlinked db path, which ``ContentIndex`` refuses), the loop still
    runs to completion — so ``completed_range`` is True — but nothing is
    written. ``store_available`` must be False so the backfill leaves the unit
    PENDING instead of latching it DONE with zero rows. The empty-range
    short-circuits (no store needed) keep ``store_available=True``.
    """
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0])
    ocr = _FakeOcr({110_000: "alpha one", 120_000: "beta two"})

    # Make the store path a symlink → ContentIndex._open raises _StoreUnavailable
    # → store.available is False. frames are non-empty, so the store IS opened.
    env.store_path.symlink_to(tmp_path / "real_target.db")

    res = index_range(cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path)

    # The frame loop completed, but the unavailable store swallowed every write.
    assert res.completed_range is True
    assert res.store_available is False
    assert res.rows_written == 0


def test_empty_range_reports_store_available(env, tmp_path):
    """The no-store-needed short-circuit reports store_available=True (SCR-192).

    Contrast with the unavailable case: an out-of-range chunk never opens the
    store, so there is no unavailability to report — the unit should still be
    eligible to mark DONE.
    """
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [250.0, 300.0])  # all outside [100, 200)
    ocr = _FakeOcr()

    res = index_range(cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path)

    assert res.completed_range is True
    assert res.store_available is True


# --------------------------------------------------------------------------
# index_range — dedup / cap
# --------------------------------------------------------------------------


def test_dedups_near_identical_consecutive_frames(env, tmp_path, monkeypatch):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0, 130.0])
    # Constant hash → every frame is a near-dup of the previous (hamming 0 ≤ thr).
    monkeypatch.setattr(dedup, "dhash", lambda *_a, **_k: 42)
    ocr = _FakeOcr()

    res = index_range(cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path)

    assert res.rows_written == 1
    assert ocr.calls == [110_000]  # only the first distinct frame OCR'd
    assert {h.timestamp_ms for h in _search(env.store_path, "screen")} == {110_000}


def test_max_frames_cap_truncates_candidates(env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0, 130.0, 140.0, 150.0])
    ocr = _FakeOcr()

    res = index_range(
        cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path, max_frames=2
    )

    # Only the first 2 in-range candidates are OCR'd/written; the cap is
    # deterministic, so it is still a complete pass.
    assert res.rows_written == 2
    assert ocr.calls == [110_000, 120_000]
    assert res.completed_range is True


# --------------------------------------------------------------------------
# index_range — skip intervals
# --------------------------------------------------------------------------


# SCR-178: skip enforcement is a privacy invariant — mark so CI's privacy lane
# runs it (CI has no general pytest lane). Vision-free (fake OCR, direct
# index_range call — no ChunkProcessor), so it runs on the Vision-free lane too.
@pytest.mark.privacy
def test_skipped_frames_never_reach_ocr(env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 150.0, 190.0])
    ocr = _FakeOcr({
        110_000: "ordinary alpha",
        150_000: "PASSWORD hunter2",  # inside the skip interval
        190_000: "ordinary beta",
    })
    skip = [
        BlockedInterval(
            start=140.0, end=160.0, action=PrivacyAction.EXCLUDE, reason="secure_field",
        )
    ]

    index_range(cap, 100.0, 200.0, skip, ocr=ocr, store_path=env.store_path)

    # The skipped frame is never passed to OCR, and never indexed.
    assert 150_000 not in ocr.calls
    assert sorted(ocr.calls) == [110_000, 190_000]
    assert _search(env.store_path, "hunter2") == []
    assert {h.timestamp_ms for h in _search(env.store_path, "ordinary")} == {110_000, 190_000}


# --------------------------------------------------------------------------
# index_range — budget / stop bail → completed_range=False
# --------------------------------------------------------------------------


def test_stop_event_mid_loop_bails_partial(env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0, 130.0])
    stop = threading.Event()

    class _StopAfterFirst(_FakeOcr):
        def recognize(self, path, **kw):
            r = super().recognize(path, **kw)
            stop.set()  # trip the stop event right after the first OCR
            return r

    ocr = _StopAfterFirst()

    res = index_range(
        cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path, stop_event=stop
    )

    assert res.completed_range is False
    assert res.rows_written == 1  # only the pre-stop frame written
    assert ocr.calls == [110_000]
    assert {h.timestamp_ms for h in _search(env.store_path, "screen")} == {110_000}


def test_wall_clock_budget_bails_partial(env, tmp_path, monkeypatch):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0, 130.0])

    # Make perf_counter jump past the budget after the first frame's OCR. The loop
    # checks the budget at the TOP, so: t0 (start), t1 (frame1 check, under), then
    # the OCR runs, then t2 (frame2 check, over budget) → bail.
    import screencap.index_core as index_core

    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(index_core.time, "perf_counter", lambda: next(ticks, 100.0))
    ocr = _FakeOcr()

    res = index_range(
        cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path, budget_s=1.0
    )

    assert res.completed_range is False
    assert res.rows_written == 1
    assert ocr.calls == [110_000]


def test_full_pass_reports_completed(env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0])
    ocr = _FakeOcr()

    res = index_range(cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path)

    assert res.completed_range is True
    assert res.rows_written == 2


# --------------------------------------------------------------------------
# index_range — R9 unlink-before-write barrier
# --------------------------------------------------------------------------


@pytest.mark.privacy  # SCR-178 R9 barrier — a privacy invariant; run it on CI.
def test_unlinked_frame_dropped_before_write(env, tmp_path):
    """A screenshot unlinked between OCR and the pre-write re-stat is dropped.

    Models the scrub_worker purge race: OCR reads a frame (outside the lock in
    production), the purge unlinks it, and the pre-``write_chunk`` re-``stat``
    barrier must drop it so the just-disabled text is never resurrected.
    """
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 150.0])
    shots = cap / "screenshots"
    ocr = _FakeOcr({110_000: "survives alpha", 150_000: "purged secret"})

    # Unlink the 150.0 screenshot as soon as it is OCR'd (simulating a concurrent
    # purge), so the pre-write re-stat finds it gone.
    real_recognize = ocr.recognize

    def _recognize_then_unlink(path, **kw):
        result = real_recognize(path, **kw)
        if int(round(float(path.stem) * 1000)) == 150_000:
            (shots / f"{150.0:.6f}.jpg").unlink()
        return result

    ocr.recognize = _recognize_then_unlink  # type: ignore[method-assign]

    res = index_range(cap, 100.0, 200.0, [], ocr=ocr, store_path=env.store_path)

    # Both were OCR'd, but the unlinked one is dropped from the written set.
    assert sorted(ocr.calls) == [110_000, 150_000]
    assert res.rows_written == 1
    assert _search(env.store_path, "secret") == []
    assert {h.timestamp_ms for h in _search(env.store_path, "survives")} == {110_000}


# --------------------------------------------------------------------------
# Characterization / integration: the live delegate matches the pre-refactor body
# --------------------------------------------------------------------------


def _make_cp(capture_dir: Path):
    """A ChunkProcessor wired for indexing (masking context present)."""
    import multiprocessing

    from screencap.chunk_processor import ChunkProcessor
    from screencap.chunk_scrubber import ChunkScrubber

    cp = ChunkProcessor(
        capture_dir,
        multiprocessing.Queue(),
        multiprocessing.Queue(),
        recording_name="test",
        upload_enabled=False,
        auto_delete=False,
    )
    cp._content_index_enabled = True
    # Non-None evaluator/classifier sentinels → has_masking_context True, so the
    # fail-closed guard passes and the index path runs.
    cp._chunk_scrubber = ChunkScrubber(
        capture_dir, enabled=False, pipeline=None, anonymizer=None,
        evaluator=object(), classifier=object(), pixel_ratio=2.0,
    )
    return cp


def test_do_index_chunk_content_matches_snapshot(env, tmp_path, monkeypatch):
    """The live delegate produces the same rows the inline body did (snapshot).

    Snapshot = the exact set of (timestamp_ms, search-term) rows the pre-refactor
    inline ``_do_index_chunk_content`` produced for this fixture: in-range,
    non-skipped, distinct-hash frames, with the MASK_WINDOW frame excluded.
    """
    import screencap.redaction.ocr as ocr_mod
    from screencap.scrubber import ScrubResult

    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 150.0, 190.0, 250.0])

    fake = _FakeOcr({
        110_000: "ordinary alpha",
        150_000: "BANK balance statement",  # MASK_WINDOW → excluded
        190_000: "ordinary beta",
        250_000: "out of range gamma",  # outside [100, 200)
    })
    # VisionOcr() is constructed with no args inside the delegate.
    monkeypatch.setattr(ocr_mod, "VisionOcr", lambda: fake)

    scrub = ScrubResult()
    scrub.blocked_intervals = [
        BlockedInterval(
            start=140.0, end=160.0, action=PrivacyAction.MASK_WINDOW, reason="bank",
        )
    ]

    cp = _make_cp(cap)
    cp._index_chunk_content(0, 100.0, 200.0, scrub)

    # Exactly the in-range, non-masked frames are searchable; nothing else.
    assert {h.timestamp_ms for h in _search(env.store_path, "ordinary")} == {110_000, 190_000}
    assert _search(env.store_path, "balance") == []  # masked frame excluded
    assert _search(env.store_path, "gamma") == []  # out-of-range frame excluded
