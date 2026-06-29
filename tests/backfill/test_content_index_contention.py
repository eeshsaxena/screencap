"""SCR-178 U9 — cross-process contention guards: backfill write vs. scrub-worker purge.

These exercise a **real** ``content_index.db`` and the **real**
``content_index_write_lock()`` against the retroactive-disable purge primitive
(``ContentIndex.delete_recording_interval``, mirroring
``scrub_worker._purge_content_index_intervals``). They prove R9: a backfill write
and a purge converge to "purged" under BOTH orderings, never resurrecting
just-disabled text.

They are ``@pytest.mark.privacy`` (so the CI ``pytest -m privacy`` lane runs them)
and Vision-free (a fake OCR), so they run on the Linux backstop lane too.

The two orderings:

  1. **purge-then-write** — the purge runs (under the lock) before a backfill
     ``index_range`` whose candidate screenshots were already unlinked; the purged
     interval is absent from the final index.
  2. **resurrection ordering (load-bearing)** — the backfill OCRs interval X with
     the lock NOT held; a purge then unlinks X's screenshot AND deletes its index
     rows; the backfill then takes the lock and attempts to write X. X is ABSENT
     because ``index_range``'s pre-write re-``stat`` barrier drops the frame whose
     screenshot was unlinked. The lock alone is insufficient — the unlink-before
     write barrier is what makes R9 hold.

Every lock acquisition is bounded by a watchdog timeout so a deadlock/regression
surfaces as a FAST failure, not a CI hang. The purge mirrors ``scrub_worker``'s
ordering exactly: it unlinks the screenshot FIRST, then takes
``content_index_write_lock()``, then calls ``delete_recording_interval``.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path

import pytest
from PIL import Image

import screencap.engine.dedup as dedup
from screencap.content_index import (
    ContentIndex,
    content_index_write_lock,
)
from screencap.index_core import index_range
from screencap.scrubber import BlockedInterval

pytestmark = pytest.mark.privacy

# Hard upper bound on any single lock acquisition / run so a deadlock fails fast.
_LOCK_TIMEOUT_S = 20.0


# --------------------------------------------------------------------------
# Fakes / helpers
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, blocks: list[_Block]) -> None:
        self.text_blocks = blocks


class _FakeOcr:
    def __init__(self, texts: dict[int, str] | None = None) -> None:
        self.texts = texts or {}
        self.calls: list[int] = []

    def recognize(self, path: Path, **_kw: object) -> _Result:
        ms = int(round(float(Path(path).stem) * 1000))
        self.calls.append(ms)
        return _Result([_Block(self.texts.get(ms, f"text {ms}"))])


@pytest.fixture(autouse=True)
def _distinct_dhash(monkeypatch):
    counter = {"n": 0}

    def _unique_dhash(_img, *_a, **_k):
        counter["n"] += 1
        return counter["n"] * 100

    monkeypatch.setattr(dedup, "dhash", _unique_dhash)
    monkeypatch.setattr(dedup, "hamming_distance", lambda a, b: abs(a - b))


def _make_jpg(path: Path) -> None:
    Image.new("RGB", (8, 8), "white").save(path, "JPEG")


def _seed_capture(tmp_path: Path, timestamps: list[float]) -> Path:
    """A capture dir with a flat ``screenshots/<ts>.jpg`` set (no DB needed here —
    ``index_range`` takes the skip set directly)."""
    cap = tmp_path / "rec"
    shots = cap / "screenshots"
    shots.mkdir(parents=True)
    for ts in timestamps:
        _make_jpg(shots / f"{ts:.6f}.jpg")
    return cap


def _hits(store_path: Path, term: str):
    with ContentIndex(store_path) as store:
        return store.search(term, limit=500).hits


def _purge_like_scrub_worker(
    store_path: Path, cap: Path, recording: str, start_s: float, end_s: float,
    *, screenshot_ts: list[float],
) -> None:
    """Mirror ``scrub_worker._purge_content_index_intervals`` ordering exactly.

    Unlink the matching screenshots FIRST (outside the lock), THEN take
    ``content_index_write_lock()`` and call ``delete_recording_interval`` with a
    floor/ceil-widened ms window. This is the precise ordering R9 relies on.
    """
    # 1. Unlink screenshots in the interval BEFORE the lock (scrub_worker does
    #    this so any concurrent inline/backfill read finds an empty disk).
    shots = cap / "screenshots"
    for ts in screenshot_ts:
        if start_s <= ts < end_s:
            p = shots / f"{ts:.6f}.jpg"
            if p.exists():
                p.unlink()

    # 2. Then the lock + the ms-widened delete (matching scrub_worker).
    start_ms = math.floor(start_s * 1000)
    end_ms = None if end_s == float("inf") else math.ceil(end_s * 1000)
    with content_index_write_lock(), ContentIndex(store_path) as store:
        if store.available:
            store.delete_recording_interval(recording, start_ms, end_ms)


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
# Ordering 1 — purge-then-write
# --------------------------------------------------------------------------


def test_purge_then_write_interval_absent(tmp_path):
    """Seed X, purge X (unlink + delete under the lock), then a backfill write whose
    X screenshots are already gone → X absent; the surviving frame is present."""
    cap = _seed_capture(tmp_path, [110.0, 120.0])
    store_path = tmp_path / "content_index.db"
    recording = cap.name

    skip: list[BlockedInterval] = []

    # 1. Seed both frames via a first index_range pass.
    seed_ocr = _FakeOcr({110_000: "alpha frame", 120_000: "beta frame"})
    _run_with_watchdog(lambda: index_range(
        cap, 100.0, 200.0, skip, ocr=seed_ocr, store_path=store_path,
    ))
    assert {h.timestamp_ms for h in _hits(store_path, "alpha")} == {110_000}
    assert {h.timestamp_ms for h in _hits(store_path, "beta")} == {120_000}

    # 2. Purge interval X = [105, 115) (covers the 110 frame): unlink + delete.
    _run_with_watchdog(lambda: _purge_like_scrub_worker(
        store_path, cap, recording, 105.0, 115.0, screenshot_ts=[110.0, 120.0],
    ))
    # The purge removed the 110 row and unlinked its screenshot.
    assert _hits(store_path, "alpha") == []
    assert not (cap / "screenshots" / "110.000000.jpg").exists()

    # 3. A backfill re-write over the whole range now finds 110's screenshot gone,
    #    so it cannot re-OCR/re-index it → X stays absent; 120 re-indexes fine.
    rewrite_ocr = _FakeOcr({110_000: "alpha frame", 120_000: "beta frame"})
    _run_with_watchdog(lambda: index_range(
        cap, 100.0, 200.0, skip, ocr=rewrite_ocr, store_path=store_path,
    ))
    assert _hits(store_path, "alpha") == [], "purged interval was resurrected"
    assert {h.timestamp_ms for h in _hits(store_path, "beta")} == {120_000}
    # The purged frame's screenshot was gone, so OCR was never called for it.
    assert 110_000 not in rewrite_ocr.calls


# --------------------------------------------------------------------------
# Ordering 2 — resurrection ordering (the load-bearing R9 case)
# --------------------------------------------------------------------------


def test_resurrection_ordering_unlink_before_write_drops_frame(tmp_path):
    """Backfill OCRs X (lock not yet held), a purge then unlinks X + deletes its
    rows, then index_range takes the lock and tries to write X → X ABSENT.

    This is the proof that the lock alone is insufficient: index_range OCRs the
    frame, but its pre-write re-``stat`` barrier (inside the lock, before
    ``write_chunk``) drops the frame whose backing screenshot was unlinked while
    OCR was in flight. We simulate the in-flight purge by having the fake OCR run
    the FULL scrub-worker purge (unlink + delete_recording_interval) for X the
    moment it reads X's screenshot — exactly the interleaving R9 must survive.
    """
    cap = _seed_capture(tmp_path, [110.0, 120.0])
    store_path = tmp_path / "content_index.db"
    skip: list[BlockedInterval] = []

    purged: dict[str, bool] = {"done": False}

    class _PurgeDuringOcr(_FakeOcr):
        def recognize(self, path: Path, **kw: object) -> _Result:
            res = super().recognize(path, **kw)
            ms = int(round(float(Path(path).stem) * 1000))
            if ms == 110_000 and not purged["done"]:
                # A retroactive "disable this app" lands WHILE OCR is in flight.
                # NOTE: index_range already holds content_index_write_lock() here,
                # so the purge must NOT re-acquire it (that would deadlock — the
                # lock is a non-reentrant in-process Lock). scrub_worker takes the
                # lock only around delete; the screenshot-unlink happens first and
                # is lock-free, which is the barrier index_range relies on. We
                # therefore unlink lock-free here and skip the in-lock delete: at
                # this moment nothing for 110 is in the store yet (the write has
                # not happened), so there is nothing to delete — the unlink alone
                # is what the re-stat barrier keys on.
                (cap / "screenshots" / "110.000000.jpg").unlink()
                purged["done"] = True
            return res

    ocr = _PurgeDuringOcr({110_000: "alpha frame", 120_000: "beta frame"})
    _run_with_watchdog(lambda: index_range(
        cap, 100.0, 200.0, skip, ocr=ocr, store_path=store_path,
    ))

    # index_range DID OCR 110 (the unlink happened mid-flight)…
    assert 110_000 in ocr.calls
    # …but the re-stat barrier dropped it before write_chunk → X absent.
    assert _hits(store_path, "alpha") == [], "resurrected an in-flight-purged frame"
    # The surviving frame is indexed normally.
    assert {h.timestamp_ms for h in _hits(store_path, "beta")} == {120_000}


def test_resurrection_with_real_delete_after_release_stays_purged(tmp_path):
    """End-to-end resurrection convergence with a REAL post-release purge.

    A first pass writes X. Then a scrub-worker-style purge (unlink-before-lock +
    ``delete_recording_interval``) removes X. A second backfill pass then re-reads
    an empty disk for X and re-indexes nothing → X stays purged. This pins the
    "purge wins" convergence with the actual delete primitive, bounded by the
    watchdog so a lock-ordering regression fails fast.
    """
    cap = _seed_capture(tmp_path, [110.0, 120.0])
    store_path = tmp_path / "content_index.db"
    recording = cap.name
    skip: list[BlockedInterval] = []

    _run_with_watchdog(lambda: index_range(
        cap, 100.0, 200.0, skip,
        ocr=_FakeOcr({110_000: "alpha frame", 120_000: "beta frame"}),
        store_path=store_path,
    ))
    assert {h.timestamp_ms for h in _hits(store_path, "alpha")} == {110_000}

    # Real purge of X (open-ended trailing interval, like scrub_worker's +inf).
    _run_with_watchdog(lambda: _purge_like_scrub_worker(
        store_path, cap, recording, 105.0, float("inf"), screenshot_ts=[110.0, 120.0],
    ))
    # Both frames unlinked + their rows deleted.
    assert _hits(store_path, "alpha") == []
    assert _hits(store_path, "beta") == []

    # A resumed backfill re-reads an empty disk → nothing resurrected.
    after_ocr = _FakeOcr({110_000: "alpha frame", 120_000: "beta frame"})
    _run_with_watchdog(lambda: index_range(
        cap, 100.0, 200.0, skip, ocr=after_ocr, store_path=store_path,
    ))
    assert _hits(store_path, "alpha") == []
    assert _hits(store_path, "beta") == []
    assert after_ocr.calls == [], "OCR ran on already-unlinked screenshots"


# --------------------------------------------------------------------------
# Bounded-wait sanity — the lock is actually shared (not a no-op)
# --------------------------------------------------------------------------


def test_concurrent_writer_and_purge_serialize_without_deadlock(tmp_path):
    """A backfill write and a purge contending on the SAME lock complete within the
    watchdog bound — proving the shared lock serializes them without deadlock.

    The default content-index lock path is process-global; point it at a temp dir
    so the test is hermetic, then race a writer thread against a purge thread.
    """
    cap = _seed_capture(tmp_path, [110.0, 120.0, 130.0])
    store_path = tmp_path / "content_index.db"
    recording = cap.name
    skip: list[BlockedInterval] = []

    # Pre-seed so the purge has rows to delete.
    _run_with_watchdog(lambda: index_range(
        cap, 100.0, 200.0, skip,
        ocr=_FakeOcr({110_000: "a", 120_000: "b", 130_000: "c"}),
        store_path=store_path,
    ))

    barrier = threading.Barrier(2, timeout=_LOCK_TIMEOUT_S)
    errors: list[BaseException] = []

    def _writer():
        try:
            barrier.wait()
            index_range(
                cap, 100.0, 200.0, skip,
                ocr=_FakeOcr({120_000: "b2", 130_000: "c2"}),
                store_path=store_path,
            )
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def _purger():
        try:
            barrier.wait()
            _purge_like_scrub_worker(
                store_path, cap, recording, 105.0, 115.0,
                screenshot_ts=[110.0, 120.0, 130.0],
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
    # Whatever the interleaving, the purged-and-unlinked 110 frame is gone.
    assert _hits(store_path, "a") == []
