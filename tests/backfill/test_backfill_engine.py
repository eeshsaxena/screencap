"""Tests for the SCR-178 U4 backfill engine (``run_backfill``).

The engine enumerates existing recordings + chunks, seeds the U3 ledger, drives
U1's ``index_range`` with U2's re-derived skip set per unit, emits progress, and
honors a global budget + cancel flag — strictly fail-open.

Every test injects a FAKE OCR, a temp ``store_path``, and a temp ``ledger`` (so
no Apple Vision / production paths are touched), and scaffolds tiny fixture
recordings (``screenshots/*.jpg`` + a minimal raw ``recording.db``, optionally a
chunk manifest). Fixture/classifier conventions mirror
``tests/backfill/test_skip_intervals.py`` and ``tests/test_index_core.py``:

  * ``com.1password.1password`` is bundle-id-classified sensitive → blocked in
    PUBLIC mode (its frames must never be indexed);
  * ``com.example.unknownbenign`` is ALLOW → its frames are indexed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import screencap.engine.dedup as dedup
from screencap.backfill.engine import BackfillSummary, run_backfill
from screencap.backfill.ledger import BackfillLedger, RunState
from screencap.content_index import ContentIndex

# --------------------------------------------------------------------------
# Fakes / fixtures
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, blocks: list[_Block]) -> None:
        self.text_blocks = blocks


class _FakeOcr:
    """Returns deterministic per-frame text; records the timestamps it OCR'd."""

    def __init__(self, texts: dict[int, str] | None = None) -> None:
        self.texts = texts or {}
        self.calls: list[int] = []

    def recognize(self, path: Path, **_kw: object) -> _Result:
        ms = int(round(float(path.stem) * 1000))
        self.calls.append(ms)
        return _Result([_Block(self.texts.get(ms, f"screen text {ms}"))])


@pytest.fixture(autouse=True)
def _distinct_dhash(monkeypatch):
    """Make every frame a distinct dHash so nothing is deduped in these tests."""
    counter = {"n": 0}

    def _unique_dhash(_img, *_a, **_k):
        counter["n"] += 1
        return counter["n"] * 100

    monkeypatch.setattr(dedup, "dhash", _unique_dhash)
    monkeypatch.setattr(dedup, "hamming_distance", lambda a, b: abs(a - b))


def _make_jpg(path: Path) -> None:
    Image.new("RGB", (8, 8), "white").save(path, "JPEG")


def _make_recording(
    rec_dir: Path,
    *,
    windows: list[dict],
    screenshot_ts: list[float],
    manifest: tuple[float, float] | None = None,
    intent: str | None = None,
) -> None:
    """Scaffold a fixture recording dir.

    ``windows`` items: ``{ts, bundle, title?, url?}`` (raw NULLs preserved).
    ``manifest`` ``(start, end)`` writes one ``chunk_0000_manifest.json``; omit
    to exercise the synthesized-window fallback. ``intent`` writes a
    ``.recording_intent`` destination.
    """
    rec_dir.mkdir(parents=True, exist_ok=True)
    shots = rec_dir / "screenshots"
    shots.mkdir(exist_ok=True)
    for ts in screenshot_ts:
        _make_jpg(shots / f"{ts:.6f}.jpg")

    db = rec_dir / "recording.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, 0.0, 2.0)")
        conn.execute(
            "CREATE TABLE window_event ("
            "id INTEGER PRIMARY KEY, recording_id INTEGER, timestamp REAL, "
            "app_bundle_id TEXT, window_id TEXT, title TEXT, state TEXT, "
            "app_name TEXT, browser_url TEXT)"
        )
        for i, w in enumerate(windows, start=1):
            conn.execute(
                "INSERT INTO window_event (id, recording_id, timestamp, app_bundle_id, "
                "window_id, title, state, app_name, browser_url) "
                "VALUES (?, 1, ?, ?, ?, ?, NULL, ?, ?)",
                (i, w["ts"], w.get("bundle"), w.get("window_id", f"w{i}"),
                 w.get("title"), w.get("app_name"), w.get("url")),
            )
        conn.execute(
            "CREATE TABLE action_event ("
            "id INTEGER PRIMARY KEY, recording_id INTEGER, name TEXT, "
            "timestamp REAL, key_char TEXT, element_state TEXT)"
        )
        conn.commit()

    if manifest is not None:
        start, end = manifest
        (rec_dir / "chunk_0000_manifest.json").write_text(
            json.dumps(
                {
                    "format_version": 2,
                    "chunk_index": 0,
                    "chunk_start": start,
                    "chunk_end": end,
                }
            )
        )
    if intent is not None:
        (rec_dir / ".recording_intent").write_text(
            json.dumps({"destination": intent})
        )


def _hits(store_path: Path, term: str):
    with ContentIndex(store_path) as store:
        return store.search(term, limit=200).hits


@pytest.fixture
def env(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    store_path = tmp_path / "content_index.db"
    ledger = BackfillLedger(tmp_path / "backfill_state.db")
    return SimpleNamespace(
        recordings=recordings,
        store_path=store_path,
        ledger=ledger,
        tmp_path=tmp_path,
    )


def _run(env, ocr, **kw):
    return run_backfill(
        recordings_dir=env.recordings,
        ledger=env.ledger,
        ocr=ocr,
        store_path=env.store_path,
        **kw,
    )


# --------------------------------------------------------------------------
# R8 — happy path
# --------------------------------------------------------------------------


def test_happy_path_two_recordings_indexed(env):
    _make_recording(
        env.recordings / "rec_a",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "A"}],
        screenshot_ts=[110.0, 120.0],
        manifest=(100.0, 200.0),
    )
    _make_recording(
        env.recordings / "rec_b",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "B"}],
        screenshot_ts=[110.0, 120.0],
        manifest=(100.0, 200.0),
    )
    ocr = _FakeOcr({
        110_000: "alpha apple",
        120_000: "alpha apple",  # distinct dhash → both kept; same text both recs
    })

    summary = _run(env, ocr)

    assert isinstance(summary, BackfillSummary)
    assert summary.state == RunState.COMPLETED
    recs = {h.recording for h in _hits(env.store_path, "alpha")}
    assert recs == {"rec_a", "rec_b"}
    assert summary.done == 2 and summary.total == 2


def test_synthesized_window_when_no_manifest(env):
    """No chunk manifest → engine synthesizes a whole-recording window."""
    _make_recording(
        env.recordings / "rec",
        windows=[{"ts": 50.0, "bundle": "com.example.unknownbenign", "title": "X"}],
        screenshot_ts=[100.0, 110.0],
        manifest=None,
    )
    ocr = _FakeOcr({100_000: "synth text", 110_000: "synth text"})

    summary = _run(env, ocr)

    assert summary.state == RunState.COMPLETED
    assert {h.recording for h in _hits(env.store_path, "synth")} == {"rec"}


# --------------------------------------------------------------------------
# R2 / R3 — blocked interval + uncovered gap absent
# --------------------------------------------------------------------------


def test_blocked_window_frames_absent(env):
    """A sensitive (1Password) window's frames are never indexed; ALLOW are."""
    _make_recording(
        env.recordings / "rec",
        windows=[
            {"ts": 100.0, "bundle": "com.1password.1password", "title": "Vault"},
            {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
        ],
        screenshot_ts=[150.0, 250.0],  # 150 in blocked span, 250 in ALLOW span
        manifest=(100.0, 300.0),
    )
    ocr = _FakeOcr({150_000: "secret vault text", 250_000: "benign note text"})

    summary = _run(env, ocr)

    assert summary.state == RunState.COMPLETED
    assert _hits(env.store_path, "secret") == []
    assert {h.timestamp_ms for h in _hits(env.store_path, "benign")} == {250_000}
    # The blocked frame was never even handed to OCR.
    assert 150_000 not in ocr.calls


def test_uncovered_gap_orphan_frame_absent(env):
    """An orphan screenshot BEFORE the earliest window_event is skipped."""
    _make_recording(
        env.recordings / "rec",
        windows=[{"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "N"}],
        screenshot_ts=[100.0, 250.0],  # 100 has no covering window (gap); 250 ok
        manifest=(50.0, 300.0),
    )
    ocr = _FakeOcr({100_000: "orphan gap text", 250_000: "covered text"})

    summary = _run(env, ocr)

    assert summary.state == RunState.COMPLETED
    assert _hits(env.store_path, "orphan") == []
    assert {h.timestamp_ms for h in _hits(env.store_path, "covered")} == {250_000}
    assert 100_000 not in ocr.calls


# --------------------------------------------------------------------------
# DONE-gating + resume
# --------------------------------------------------------------------------


def test_done_gating_budget_bail_leaves_pending_then_resumes(env):
    """A mid-chunk budget bail leaves the unit PENDING; a resume indexes the rest."""
    _make_recording(
        env.recordings / "rec",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "X"}],
        screenshot_ts=[110.0, 120.0, 130.0, 140.0],
        manifest=(100.0, 200.0),
    )

    # OCR that stops the run after the first frame (forces a mid-chunk bail so
    # index_range returns completed_range=False → unit stays PENDING).
    stop = threading.Event()

    class _StopAfterFirst(_FakeOcr):
        def recognize(self, path, **kw):
            res = super().recognize(path, **kw)
            stop.set()  # next per-frame check bails
            return res

    ocr1 = _StopAfterFirst({
        110_000: "first frame", 120_000: "second frame",
        130_000: "third frame", 140_000: "fourth frame",
    })
    summary1 = _run(env, ocr1, stop_event=stop)

    assert summary1.state == RunState.CANCELLED
    assert env.ledger.next_pending() == ("rec", 0)  # still PENDING (not DONE)

    # Resume with a fresh OCR + no stop → re-OCRs the whole range (whole-range
    # replace), all frames now present.
    ocr2 = _FakeOcr({
        110_000: "first frame", 120_000: "second frame",
        130_000: "third frame", 140_000: "fourth frame",
    })
    summary2 = _run(env, ocr2)

    assert summary2.state == RunState.COMPLETED
    assert env.ledger.next_pending() is None
    got = {h.timestamp_ms for h in _hits(env.store_path, "frame")}
    assert got == {110_000, 120_000, 130_000, 140_000}


def test_resume_skips_already_done_recording(env):
    """Cancel after recording 1 → resume indexes only recording 2."""
    _make_recording(
        env.recordings / "rec_a",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "A"}],
        screenshot_ts=[110.0],
        manifest=(100.0, 200.0),
    )
    # rec_b has two frames so a stop set during its first OCR bails it mid-chunk.
    _make_recording(
        env.recordings / "rec_b",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "B"}],
        screenshot_ts=[110.0, 120.0],
        manifest=(100.0, 200.0),
    )

    # Cancel after rec_a completes: stop set on rec_b's first frame so its chunk
    # bails (completed_range=False → rec_b stays PENDING).
    stop = threading.Event()

    class _StopOnRecB(_FakeOcr):
        def recognize(self, path, **kw):
            res = super().recognize(path, **kw)
            # rec_b's screenshots live under .../rec_b/screenshots/...
            if "rec_b" in str(path):
                stop.set()
            return res

    ocr1 = _StopOnRecB({110_000: "shared text", 120_000: "shared text"})
    summary1 = _run(env, ocr1, stop_event=stop)
    assert summary1.state == RunState.CANCELLED
    # rec_a done, rec_b still pending.
    assert env.ledger.next_pending() == ("rec_b", 0)

    # Resume — rec_a is terminal so it is NOT re-OCR'd; only rec_b is processed.
    ocr2 = _FakeOcr({110_000: "shared text", 120_000: "shared text"})
    summary2 = _run(env, ocr2)
    assert summary2.state == RunState.COMPLETED
    # rec_a's frame (110000 under rec_a) is not re-OCR'd; rec_b's two frames are.
    assert ocr2.calls == [110_000, 120_000]  # only rec_b
    assert {h.recording for h in _hits(env.store_path, "shared")} == {"rec_a", "rec_b"}


# --------------------------------------------------------------------------
# Budget → PAUSED
# --------------------------------------------------------------------------


def test_tiny_budget_returns_paused_then_completes(env):
    """A tiny global budget pauses with a pending remainder, not COMPLETED."""
    for name in ("rec_a", "rec_b"):
        _make_recording(
            env.recordings / name,
            windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": name}],
            screenshot_ts=[110.0],
            manifest=(100.0, 200.0),
        )

    # budget_s=0 → between-unit budget check trips immediately, before the first
    # unit is processed → PAUSED with everything still PENDING.
    ocr = _FakeOcr({110_000: "budget text"})
    summary = _run(env, ocr, budget_s=0.0)

    assert summary.state == RunState.PAUSED
    assert summary.done == 0
    assert summary.total == 2
    assert env.ledger.next_pending() is not None

    # Follow-up with no budget completes it.
    ocr2 = _FakeOcr({110_000: "budget text"})
    summary2 = _run(env, ocr2)
    assert summary2.state == RunState.COMPLETED
    assert {h.recording for h in _hits(env.store_path, "budget")} == {"rec_a", "rec_b"}


# --------------------------------------------------------------------------
# Cancel
# --------------------------------------------------------------------------


def test_cancel_at_start_returns_cancelled_and_resumable(env):
    """stop_event set before start → CANCELLED promptly, run-state CANCELLED."""
    _make_recording(
        env.recordings / "rec",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "X"}],
        screenshot_ts=[110.0],
        manifest=(100.0, 200.0),
    )
    stop = threading.Event()
    stop.set()
    ocr = _FakeOcr({110_000: "never indexed"})

    summary = _run(env, ocr, stop_event=stop)

    assert summary.state == RunState.CANCELLED
    assert env.ledger.run_state() == RunState.CANCELLED
    assert env.ledger.is_cancelled() is True
    assert ocr.calls == []  # never reached OCR
    assert _hits(env.store_path, "never") == []

    # Resumable: a fresh run with no stop completes.
    ocr2 = _FakeOcr({110_000: "now indexed"})
    summary2 = _run(env, ocr2)
    assert summary2.state == RunState.COMPLETED
    assert {h.timestamp_ms for h in _hits(env.store_path, "now")} == {110_000}


# --------------------------------------------------------------------------
# SKIPPED — missing screenshots / recording.db
# --------------------------------------------------------------------------


def test_missing_screenshots_dir_marked_skipped(env):
    """A recording dir with a synthesized-unit-but-evicted screenshots → SKIPPED.

    We seed a unit via a manifest, then remove ``screenshots/`` to simulate a
    retention-evicted recording: its unit is SKIPPED (not FAILED) and the run
    continues over the other recording.
    """
    rec_evicted = env.recordings / "rec_evicted"
    _make_recording(
        rec_evicted,
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "E"}],
        screenshot_ts=[110.0],
        manifest=(100.0, 200.0),
    )
    # Evict the screenshots after scaffolding (manifest still seeds the unit).
    for p in (rec_evicted / "screenshots").glob("*.jpg"):
        p.unlink()
    (rec_evicted / "screenshots").rmdir()

    _make_recording(
        env.recordings / "rec_ok",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "K"}],
        screenshot_ts=[110.0],
        manifest=(100.0, 200.0),
    )
    ocr = _FakeOcr({110_000: "ok text"})

    summary = _run(env, ocr)

    assert summary.state == RunState.COMPLETED
    assert summary.skipped == 1
    assert summary.done == 1
    assert {h.recording for h in _hits(env.store_path, "ok")} == {"rec_ok"}


def test_missing_db_marked_skipped(env):
    """A recording dir with screenshots but no recording.db → SKIPPED."""
    rec = env.recordings / "rec_nodb"
    _make_recording(
        rec,
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "N"}],
        screenshot_ts=[110.0],
        manifest=(100.0, 200.0),
    )
    (rec / "recording.db").unlink()
    ocr = _FakeOcr({110_000: "nodb text"})

    summary = _run(env, ocr)

    assert summary.state == RunState.COMPLETED
    assert summary.skipped == 1
    assert _hits(env.store_path, "nodb") == []


# --------------------------------------------------------------------------
# FAILED isolation
# --------------------------------------------------------------------------


def test_failed_recording_isolated(env, monkeypatch):
    """derive_skip_intervals raising for one recording → that unit FAILED only."""
    _make_recording(
        env.recordings / "rec_bad",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Bad"}],
        screenshot_ts=[110.0],
        manifest=(100.0, 200.0),
    )
    _make_recording(
        env.recordings / "rec_good",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Good"}],
        screenshot_ts=[110.0],
        manifest=(100.0, 200.0),
    )

    from screencap.backfill import skip_intervals as si

    orig = si.derive_skip_intervals

    def _raise_for_bad(db_path, **kw):
        if "rec_bad" in str(db_path):
            raise RuntimeError("boom")
        return orig(db_path, **kw)

    monkeypatch.setattr(si, "derive_skip_intervals", _raise_for_bad)

    ocr = _FakeOcr({110_000: "good content"})
    summary = _run(env, ocr)

    assert summary.state == RunState.COMPLETED  # run did not abort
    assert summary.failed == 1
    assert summary.done == 1
    assert {h.recording for h in _hits(env.store_path, "good")} == {"rec_good"}


# --------------------------------------------------------------------------
# R9 — resurrection ordering (unlink before write barrier)
# --------------------------------------------------------------------------


def test_purge_unlink_during_ocr_not_resurrected(env):
    """A screenshot unlinked while OCR is in flight is dropped from the index.

    The fake OCR unlinks the backing file when first read, exercising U1's
    re-stat barrier inside ``index_range``: the frame must be absent from the
    final index.
    """
    _make_recording(
        env.recordings / "rec",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "X"}],
        screenshot_ts=[110.0, 120.0],
        manifest=(100.0, 200.0),
    )

    class _UnlinkOnRead(_FakeOcr):
        def recognize(self, path, **kw):
            res = super().recognize(path, **kw)
            if int(round(float(path.stem) * 1000)) == 110_000:
                Path(path).unlink()  # simulate a concurrent purge unlink
            return res

    ocr = _UnlinkOnRead({110_000: "purged frame", 120_000: "surviving frame"})
    summary = _run(env, ocr)

    assert summary.state == RunState.COMPLETED
    assert _hits(env.store_path, "purged") == []  # dropped by the re-stat barrier
    assert {h.timestamp_ms for h in _hits(env.store_path, "surviving")} == {120_000}


# --------------------------------------------------------------------------
# progress_cb — opaque index, no recording name
# --------------------------------------------------------------------------


def test_progress_cb_called_with_opaque_index_no_recording_name(env):
    rec_names = ("rec_alpha", "rec_beta")
    for name in rec_names:
        _make_recording(
            env.recordings / name,
            windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": name}],
            screenshot_ts=[110.0],
            manifest=(100.0, 200.0),
        )

    calls: list[tuple] = []

    def _cb(done, skipped, failed, total, unit_index):
        calls.append((done, skipped, failed, total, unit_index))

    ocr = _FakeOcr({110_000: "progress text"})
    _run(env, ocr, progress_cb=_cb)

    assert calls, "progress_cb should be called at least once"
    # Five-arg shape, all ints, indices are opaque ordinals (0,1,...).
    for call in calls:
        assert len(call) == 5
        for arg in call:
            assert isinstance(arg, int)
        # No recording directory name leaked into any arg.
        for name in rec_names:
            assert name not in {str(a) for a in call}
    indices = [c[4] for c in calls]
    assert indices == sorted(indices)  # monotonic ordinals
    assert set(indices) <= {0, 1}
