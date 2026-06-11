"""Tests for the SCR-118 U2 content-index OCR pass + scrub_worker purge.

Covers the chunk-processor index method (``_index_chunk_content``): time-scoping,
secure-field / EXCLUDE skipping, dHash dedup, idempotency, fail-open behavior,
and the config-flag / Vision-absent gates — plus the retroactive-disable purge
in ``ScrubWorker._purge_content_index_intervals``.

The index pass is exercised directly with a stubbed OCR engine, a patched dHash
(for deterministic dedup), and a redirected content-index path, so no real Apple
Vision or recording pipeline is needed.
"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import screencap.content_index as content_index
import screencap.engine.dedup as dedup
import screencap.privacy.ocr as ocr_mod
from screencap.content_index import ContentIndex
from screencap.scrubber import BlockedInterval, ScrubResult

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
    """Stand-in for VisionOcr — returns text keyed by the frame's ms timestamp."""

    texts: dict[int, str] = {}

    def recognize(self, path: Path, **_kw: object) -> _Result:
        ms = int(round(float(path.stem) * 1000))
        return _Result([_Block(_FakeOcr.texts.get(ms, f"generic screen text {ms}"))])


def _make_jpg(path: Path) -> None:
    """Write a minimal valid JPEG (content irrelevant — dHash is patched)."""
    Image.new("RGB", (8, 8), "white").save(path, "JPEG")


def _make_screenshots(capture_dir: Path, timestamps: list[float]) -> None:
    shots = capture_dir / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    for ts in timestamps:
        _make_jpg(shots / f"{ts:.6f}.jpg")


@pytest.fixture
def index_env(tmp_path, monkeypatch):
    """Wire OCR stub, a distinct-per-frame dHash, and a redirected store path."""
    _FakeOcr.texts = {}
    monkeypatch.setattr(ocr_mod, "VisionOcr", _FakeOcr)

    # Distinct hash per call → nothing deduped (override per-test for dedup).
    counter = {"n": 0}

    def _unique_dhash(_img, *_a, **_k):
        counter["n"] += 1
        return counter["n"] * 100  # well beyond the dedup threshold → never deduped

    monkeypatch.setattr(dedup, "dhash", _unique_dhash)
    monkeypatch.setattr(dedup, "hamming_distance", lambda a, b: abs(a - b))

    store_path = tmp_path / "content_index.db"
    monkeypatch.setattr(content_index, "default_index_path", lambda: store_path)
    return SimpleNamespace(tmp_path=tmp_path, store_path=store_path)


def _make_cp(capture_dir: Path, *, enabled: bool = True, masking: bool = True):
    from screencap.chunk_processor import ChunkProcessor

    cp = ChunkProcessor(
        capture_dir,
        multiprocessing.Queue(),
        multiprocessing.Queue(),
        recording_name="test",
        upload_enabled=False,
        auto_delete=False,
    )
    cp._content_index_enabled = enabled
    # The index pass FAILS CLOSED when masking classification is unavailable
    # (no evaluator/classifier → blocked_intervals can't represent masked-app
    # skips). With a real scrub pipeline these are set; the test CP is built
    # without one, so stand in non-None sentinels to exercise the index path.
    # ``masking=False`` leaves them None to assert the fail-closed gate.
    if masking:
        cp._masking_classifier = object()
        cp._masking_evaluator = object()
    return cp


def _search(store_path: Path, term: str):
    with ContentIndex(store_path) as store:
        return store.search(term, limit=100).hits


# --------------------------------------------------------------------------
# Index pass behavior
# --------------------------------------------------------------------------


def test_index_pass_writes_time_scoped_frames(index_env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    # Two in-window frames + one outside [100, 200).
    _make_screenshots(cap, [120.0, 150.0, 250.0])

    cp = _make_cp(cap)
    cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())

    hits = _search(index_env.store_path, "screen")
    assert {h.timestamp_ms for h in hits} == {120_000, 150_000}
    # Keyed on the recording DIRECTORY name.
    assert {h.recording for h in hits} == {"rec"}


def test_index_pass_skips_excluded_intervals(index_env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 150.0, 190.0])
    _FakeOcr.texts = {
        110_000: "ordinary content alpha",
        150_000: "PASSWORD hunter2 secret",  # inside the EXCLUDE interval
        190_000: "ordinary content beta",
    }

    # Secure-field / EXCLUDE-app intervals both carry action=EXCLUDE.
    from screencap.privacy.policy import PrivacyAction

    scrub = ScrubResult()
    scrub.blocked_intervals = [
        BlockedInterval(start=140.0, end=160.0, action=PrivacyAction.EXCLUDE, reason="secure_field"),
    ]

    cp = _make_cp(cap)
    cp._index_chunk_content(0, 100.0, 200.0, scrub)

    assert _search(index_env.store_path, "secret") == []
    assert _search(index_env.store_path, "hunter2") == []
    ts = {h.timestamp_ms for h in _search(index_env.store_path, "ordinary")}
    assert ts == {110_000, 190_000}


def test_index_pass_skips_all_masked_actions_not_just_exclude(index_env, tmp_path):
    """Frames in ANY policy-flagged interval are skipped — not only EXCLUDE.

    blocked_intervals is built with the full SCRUB_BLOCK_ACTIONS set; a
    MASK_WINDOW frame (banking/email/chat) must not be indexed even though it is
    not EXCLUDE, or its unmasked on-screen text leaks into the local index.
    """
    from screencap.privacy.policy import PrivacyAction

    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 150.0, 190.0])
    _FakeOcr.texts = {
        110_000: "ordinary alpha",
        150_000: "BANK balance statement",  # inside a MASK_WINDOW interval
        190_000: "ordinary beta",
    }

    scrub = ScrubResult()
    scrub.blocked_intervals = [
        BlockedInterval(start=140.0, end=160.0, action=PrivacyAction.MASK_WINDOW, reason="bank"),
    ]

    cp = _make_cp(cap)
    cp._index_chunk_content(0, 100.0, 200.0, scrub)

    assert _search(index_env.store_path, "balance") == []
    assert _search(index_env.store_path, "BANK") == []
    assert {h.timestamp_ms for h in _search(index_env.store_path, "ordinary")} == {110_000, 190_000}


@pytest.mark.parametrize(
    "action_name",
    ["EXCLUDE", "MASK_WINDOW", "MASK_REGION", "TEXT_REDACT", "OCR_FALLBACK"],
)
def test_index_pass_skips_every_block_action(index_env, tmp_path, action_name):
    """A frame inside a BlockedInterval of ANY SCRUB_BLOCK_ACTIONS member is
    skipped — not just EXCLUDE/MASK_WINDOW. ``blocked_intervals`` is built with
    the full set, so each must keep its (unmasked, local) on-screen text out of
    the index, or it leaks content the cloud copy would have masked/redacted.
    """
    from screencap.privacy.policy import PrivacyAction

    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 150.0, 190.0])
    _FakeOcr.texts = {
        110_000: "ordinary alpha",
        150_000: "SENSITIVE redacted payload",  # inside the blocked interval
        190_000: "ordinary beta",
    }

    scrub = ScrubResult()
    scrub.blocked_intervals = [
        BlockedInterval(
            start=140.0, end=160.0,
            action=getattr(PrivacyAction, action_name), reason=action_name.lower(),
        ),
    ]

    cp = _make_cp(cap)
    cp._index_chunk_content(0, 100.0, 200.0, scrub)

    assert _search(index_env.store_path, "redacted") == []
    assert _search(index_env.store_path, "SENSITIVE") == []
    assert {h.timestamp_ms for h in _search(index_env.store_path, "ordinary")} == {110_000, 190_000}


def test_index_pass_fails_closed_without_masking_classifier(index_env, tmp_path):
    """When masking classification is unavailable (evaluator/classifier None),
    blocked_intervals cannot represent the masked-app skips, so the pass must
    index NOTHING — never ingest potentially masked-app on-screen text.

    Mirrors the #2 fail-closed gate: even an empty blocked_intervals (the shape
    a no-context scrub produces) must not let unfiltered frames through.
    """
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 150.0, 190.0])
    _FakeOcr.texts = {
        110_000: "would be indexed alpha",
        150_000: "would be indexed beta",
        190_000: "would be indexed gamma",
    }

    # masking=False → classifier/evaluator stay None (the unavailable case). The
    # empty blocked_intervals here would otherwise let every frame through.
    cp = _make_cp(cap, masking=False)
    cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())

    # Fail-closed: the pass returned before touching the store, so no PII store
    # was created (assert BEFORE _search, which would create one on open).
    assert not index_env.store_path.exists()
    assert _search(index_env.store_path, "indexed") == []


def test_reprocess_drops_now_skipped_frame(index_env, tmp_path):
    """A re-process where a frame becomes policy-skipped must clear its stale row.

    Range-replace (write_chunk) clears the whole chunk range, so a frame indexed
    in pass 1 that is EXCLUDE/MASK-skipped in pass 2 (e.g. a tightened policy)
    does not survive as a stale, less-redacted row.
    """
    from screencap.privacy.policy import PrivacyAction

    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [120.0, 150.0])
    _FakeOcr.texts = {120_000: "keep me", 150_000: "now sensitive secret"}

    cp = _make_cp(cap)
    cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())  # pass 1: both indexed
    assert _search(index_env.store_path, "secret")  # present after pass 1

    # Pass 2 with the 150.0 frame now inside a blocked interval.
    scrub = ScrubResult()
    scrub.blocked_intervals = [
        BlockedInterval(start=145.0, end=155.0, action=PrivacyAction.EXCLUDE, reason="x"),
    ]
    cp._index_chunk_content(0, 100.0, 200.0, scrub)

    assert _search(index_env.store_path, "secret") == []  # stale row cleared
    assert {h.timestamp_ms for h in _search(index_env.store_path, "keep")} == {120_000}


def test_index_pass_dedups_near_identical_frames(index_env, tmp_path, monkeypatch):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0, 130.0])

    # Constant hash → every frame is a near-dup of the previous one.
    monkeypatch.setattr(dedup, "dhash", lambda *_a, **_k: 42)

    cp = _make_cp(cap)
    cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())

    hits = _search(index_env.store_path, "screen")
    # Only the first distinct frame indexed.
    assert {h.timestamp_ms for h in hits} == {110_000}


def test_index_pass_idempotent_on_reprocess(index_env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0])

    cp = _make_cp(cap)
    cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())
    cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())  # --force / recovery

    hits = _search(index_env.store_path, "screen")
    assert sorted(h.timestamp_ms for h in hits) == [110_000, 120_000]


# --------------------------------------------------------------------------
# Gating + fail-open
# --------------------------------------------------------------------------


def test_index_pass_noop_when_flag_disabled(index_env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0])

    cp = _make_cp(cap, enabled=False)
    cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())

    # Disabled → store never even created.
    assert not index_env.store_path.exists()


def test_index_pass_noop_when_vision_absent(index_env, tmp_path, monkeypatch):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0])

    def _raise() -> None:
        raise ImportError("pyobjc-framework-Vision not installed")

    monkeypatch.setattr(ocr_mod, "VisionOcr", lambda: _raise())

    cp = _make_cp(cap)
    cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())  # must not raise
    assert not index_env.store_path.exists()


def test_index_pass_fail_open_on_ocr_error(index_env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0, 120.0])

    class _BoomOcr:
        def recognize(self, *_a, **_k):
            raise RuntimeError("OCR exploded")

    import screencap.privacy.ocr as ocr_module

    _orig = ocr_module.VisionOcr
    ocr_module.VisionOcr = _BoomOcr
    try:
        cp = _make_cp(cap)
        # Per-frame recognize errors are swallowed; the pass completes with no
        # rows and never raises into the chunk lifecycle (AE1 fail-open).
        cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())
    finally:
        ocr_module.VisionOcr = _orig

    assert _search(index_env.store_path, "screen") == []


def test_index_pass_swallows_store_errors(index_env, tmp_path, monkeypatch):
    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [110.0])

    def _boom(_path):
        raise RuntimeError("store path resolution failed")

    monkeypatch.setattr(content_index, "default_index_path", _boom)

    cp = _make_cp(cap)
    # The outer wrapper must swallow everything — no raise into _process_chunk.
    cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())


def test_index_pass_noop_without_screenshots_dir(index_env, tmp_path):
    cap = tmp_path / "rec"
    cap.mkdir()  # no screenshots/ subdir

    cp = _make_cp(cap)
    cp._index_chunk_content(0, 100.0, 200.0, ScrubResult())
    assert not index_env.store_path.exists()


# --------------------------------------------------------------------------
# scrub_worker retroactive-disable purge
# --------------------------------------------------------------------------


def test_scrub_worker_purges_disabled_intervals(index_env, tmp_path):
    from screencap.privacy.scrub_worker import ScrubWorker

    # Seed the store as if a recording had been indexed.
    with ContentIndex(index_env.store_path) as store:
        store.write_frames(
            "rec",
            [
                content_index.IndexFrame(timestamp_ms=110_000, text="keep before"),
                content_index.IndexFrame(timestamp_ms=150_000, text="purge inside"),
                content_index.IndexFrame(timestamp_ms=900_000, text="keep trailing was active"),
            ],
        )

    stub = SimpleNamespace(_capture_dir=Path("/whatever/rec"))
    # Intervals are float unix SECONDS; the trailing one runs to +inf.
    intervals = [(140.0, 160.0), (800.0, float("inf"))]
    ScrubWorker._purge_content_index_intervals(stub, intervals)

    remaining = {h.timestamp_ms for h in _search(index_env.store_path, "keep")}
    assert remaining == {110_000}
    assert _search(index_env.store_path, "purge") == []


def test_scrub_worker_purge_skips_when_no_store(index_env, tmp_path):
    from screencap.privacy.scrub_worker import ScrubWorker

    stub = SimpleNamespace(_capture_dir=Path("/whatever/rec"))
    # No store on disk → purge must be a no-op and must NOT create one.
    ScrubWorker._purge_content_index_intervals(stub, [(0.0, 100.0)])
    assert not index_env.store_path.exists()


# --------------------------------------------------------------------------
# SCR-134: inline-write vs retroactive-disable-purge mutual exclusion
# --------------------------------------------------------------------------


def test_index_write_and_purge_serialize_on_shared_lock(index_env, tmp_path):
    """SCR-134: the inline index write and the retroactive-disable purge must take
    one shared lock, so an in-flight write can never re-insert just-purged text.

    The earlier design closed the race only via a documented "re-disable
    self-heals" recovery — which never ran for the content index (a re-disable
    computes empty intervals from the already-deleted window_event rows). The fix
    removes the race outright: holding ``content_index_write_lock`` must block BOTH
    a concurrent purge AND a concurrent index pass until released, proving each
    path acquires it. Both racers are threads of the one recorder process, so the
    in-process lock is what excludes them here.
    """
    import threading

    from screencap.content_index import content_index_write_lock
    from screencap.privacy.scrub_worker import ScrubWorker

    # Seed the store so the purge has both a store to open and a row to delete.
    with ContentIndex(index_env.store_path) as store:
        store.write_frames(
            "rec",
            [content_index.IndexFrame(timestamp_ms=150_000, text="resurrected secret")],
        )

    cap = tmp_path / "rec"
    cap.mkdir()
    _make_screenshots(cap, [120.0])
    cp = _make_cp(cap)
    purge_stub = SimpleNamespace(_capture_dir=cap)

    contended_ops = {
        "purge": lambda: ScrubWorker._purge_content_index_intervals(
            purge_stub, [(0.0, float("inf"))]
        ),
        "index": lambda: cp._index_chunk_content(0, 100.0, 200.0, ScrubResult()),
    }

    for label, op in contended_ops.items():
        started = threading.Event()
        done = threading.Event()

        def _run(op=op, started=started, done=done):
            started.set()
            op()
            done.set()

        with content_index_write_lock():
            worker = threading.Thread(target=_run)
            worker.start()
            assert started.wait(2.0), f"{label} thread never started"
            # While we hold the write lock the contended op must not complete.
            assert not done.wait(0.4), f"{label} ran while the write lock was held"
        worker.join(5.0)
        assert done.is_set(), f"{label} never finished after the lock was released"
