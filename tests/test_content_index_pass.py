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


def _make_cp(capture_dir: Path, *, enabled: bool = True):
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
