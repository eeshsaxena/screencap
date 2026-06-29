"""SCR-178 U9 — CI-safe privacy guards for the content-index backfill.

These guards lock in the backfill's load-bearing privacy invariants and — per the
institutional learning in
``docs/solutions/workflow-issues/privacy-guards-deselected-on-ci-silently-rot-2026-06-10.md``
— **every guard here runs on CI**. They are ``@pytest.mark.privacy`` (the repo's
only CI pytest lane is ``pytest -m privacy``, run BOTH on macOS+Vision and on a
Vision-free Linux backstop) AND they use a FAKE OCR, so they exercise the real
skip/index predicate WITHOUT any Apple Vision dependency. The rot this repo got
burned by (SCR-110) was a guard that was *both* privacy-marked *and* Vision-only,
so it only ran on a dev Mac; the fix is privacy-marked + Vision-free so the Linux
backstop lane always runs it.

Each skip condition is asserted **independently** (no single boolean collapsing
them) so disabling one skip source in a fixture can never silently mask another:

  * a ``SCRUB_BLOCK_ACTIONS`` window (sensitive bundle) — canonical block,
  * an uncovered-gap orphan screenshot — fail-closed coverage gap,
  * a null-column ambiguity window — fail-closed classification ambiguity.

The real-scrub guard drives the ACTUAL masking pipeline (``mask_screenshots``,
which EXCLUDE→deletes / MASK_WINDOW→masks a flat ``screenshots/`` dir) and is
itself Vision-free for those actions, then backfills — proving the production
"scrub-on capture, then backfill" path never indexes masked-window text.

Fixture conventions mirror ``tests/backfill/test_backfill_engine.py`` and
``tests/backfill/test_skip_intervals.py``:
  * ``com.1password.1password`` / ``com.robinhood.Robinhood`` — bundle-id
    classified sensitive → blocked in PUBLIC mode (never indexed);
  * ``com.example.unknownbenign`` — ALLOW → indexed;
  * ``com.example.unknownapp`` with ``title IS NULL`` — title-ambiguous under
    PUBLIC → skipped.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import screencap.engine.dedup as dedup
from screencap.backfill.engine import run_backfill
from screencap.backfill.ledger import BackfillLedger, RunState
from screencap.content_index import ContentIndex

pytestmark = pytest.mark.privacy


# --------------------------------------------------------------------------
# Fakes / fixtures (fake OCR — NO Apple Vision)
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, blocks: list[_Block]) -> None:
        self.text_blocks = blocks


class _FakeOcr:
    """Records every screenshot path/timestamp it is asked to recognise.

    The whole point of the predicate guards is that a skipped frame is NEVER
    handed to OCR, so ``calls`` (the ms timestamps actually OCR'd) is the load
    bearing assertion surface — distinct from "did the text reach the store".
    """

    def __init__(self, texts: dict[int, str] | None = None) -> None:
        self.texts = texts or {}
        self.calls: list[int] = []
        self.paths: list[Path] = []

    def recognize(self, path: Path, **_kw: object) -> _Result:
        self.paths.append(Path(path))
        ms = int(round(float(Path(path).stem) * 1000))
        self.calls.append(ms)
        return _Result([_Block(self.texts.get(ms, f"screen text {ms}"))])


@pytest.fixture(autouse=True)
def _distinct_dhash(monkeypatch):
    """Make every frame a distinct dHash so dedup never hides a skip assertion."""
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
    actions: list[dict] | None = None,
    manifest: tuple[float, float] | None = None,
    intent: str | None = None,
) -> None:
    """Scaffold a fixture recording dir (flat ``screenshots/`` + raw recording.db).

    ``windows`` items: ``{ts, bundle, title?, url?}`` — any omitted key is stored
    as a raw NULL (the ambiguity tests depend on the NULL distinction surviving).
    ``actions`` items: ``{ts, element_state?}`` for secure-field / null-state spans.
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
        for i, a in enumerate(actions or [], start=1):
            es = a.get("element_state")
            es_raw = json.dumps(es) if isinstance(es, dict) else es
            conn.execute(
                "INSERT INTO action_event (id, recording_id, name, timestamp, "
                "key_char, element_state) VALUES (?, 1, 'press', ?, ?, ?)",
                (i, a["ts"], a.get("key_char"), es_raw),
            )
        conn.commit()

    if manifest is not None:
        start, end = manifest
        (rec_dir / "chunk_0000_manifest.json").write_text(
            json.dumps({"format_version": 2, "chunk_index": 0,
                        "chunk_start": start, "chunk_end": end})
        )
    if intent is not None:
        (rec_dir / ".recording_intent").write_text(
            json.dumps({"version": 2, "destination": intent,
                        "retention_policy": "keep_forever", "retention_params": {}})
        )


def _hits(store_path: Path, term: str):
    with ContentIndex(store_path) as store:
        return store.search(term, limit=500).hits


@pytest.fixture
def env(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    return SimpleNamespace(
        recordings=recordings,
        store_path=tmp_path / "content_index.db",
        ledger=BackfillLedger(tmp_path / "backfill_state.db"),
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


def _secure_state() -> dict:
    return {"AXRole": "AXSecureTextField"}


# --------------------------------------------------------------------------
# Guard 1 — ALLOW-only across all three skip sources at once
# --------------------------------------------------------------------------


def test_all_three_skip_sources_never_reach_ocr_or_store(env):
    """One recording carrying all three skip sources: none reach OCR or the store.

    (a) a SCRUB_BLOCK_ACTIONS window (1Password), (b) an uncovered-gap orphan
    screenshot (before the earliest surviving window), and (c) a null-column
    ambiguity window (unknown app, title IS NULL under PUBLIC). The single ALLOW
    frame IS indexed.
    """
    _make_recording(
        env.recordings / "rec",
        windows=[
            # 1Password span [200, 300): blocked (SCRUB_BLOCK_ACTIONS).
            {"ts": 200.0, "bundle": "com.1password.1password", "title": "Vault"},
            # unknown app with NULL title, span [300, 400): title-ambiguous → skip.
            {"ts": 300.0, "bundle": "com.example.unknownapp", "title": None},
            # benign ALLOW span [400, +inf).
            {"ts": 400.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
        ],
        # 150 → uncovered gap (before earliest window @200); 250 → blocked;
        # 350 → ambiguity; 450 → ALLOW.
        screenshot_ts=[150.0, 250.0, 350.0, 450.0],
        manifest=(100.0, 500.0),
    )
    ocr = _FakeOcr({
        150_000: "ORPHAN gap text",
        250_000: "SECRET vault text",
        350_000: "AMBIGUOUS title text",
        450_000: "BENIGN allowed text",
    })

    summary = _run(env, ocr)
    assert summary.state == RunState.COMPLETED

    # NONE of the three skipped timestamps were ever handed to OCR.
    assert 150_000 not in ocr.calls, "uncovered-gap frame reached OCR"
    assert 250_000 not in ocr.calls, "blocked-window frame reached OCR"
    assert 350_000 not in ocr.calls, "ambiguity frame reached OCR"
    # And the ALLOW frame WAS.
    assert 450_000 in ocr.calls

    # None of the skipped text reached the store; only the ALLOW text did.
    assert _hits(env.store_path, "ORPHAN") == []
    assert _hits(env.store_path, "SECRET") == []
    assert _hits(env.store_path, "AMBIGUOUS") == []
    assert {h.timestamp_ms for h in _hits(env.store_path, "BENIGN")} == {450_000}


# --------------------------------------------------------------------------
# Guard 2 — independence: each skip source fires ON ITS OWN
# --------------------------------------------------------------------------


def test_only_blocked_window_still_skips_it(env):
    """A fixture with ONLY a blocked window (no gap, no ambiguity) still skips it."""
    _make_recording(
        env.recordings / "rec",
        windows=[
            {"ts": 100.0, "bundle": "com.robinhood.Robinhood", "title": "Trade"},
            {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
        ],
        screenshot_ts=[150.0, 250.0],
        manifest=(100.0, 300.0),
    )
    ocr = _FakeOcr({150_000: "TRADE secret", 250_000: "BENIGN ok"})

    summary = _run(env, ocr)
    assert summary.state == RunState.COMPLETED
    assert 150_000 not in ocr.calls
    assert _hits(env.store_path, "TRADE") == []
    assert {h.timestamp_ms for h in _hits(env.store_path, "BENIGN")} == {250_000}


def test_only_ambiguity_window_still_skips_it(env):
    """A fixture with ONLY a null-title ambiguity window (no block, no gap) skips it.

    The ALLOW window covers the recording start (so there is no uncovered gap) and
    no SCRUB_BLOCK_ACTIONS window exists — proving the ambiguity predicate fires
    independently of the other two.
    """
    _make_recording(
        env.recordings / "rec",
        windows=[
            {"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
            # unknown app, NULL title → title-ambiguous under PUBLIC.
            {"ts": 200.0, "bundle": "com.example.unknownapp", "title": None},
        ],
        screenshot_ts=[150.0, 250.0],
        manifest=(100.0, 300.0),
    )
    ocr = _FakeOcr({150_000: "BENIGN ok", 250_000: "AMBIGUOUS title text"})

    summary = _run(env, ocr)
    assert summary.state == RunState.COMPLETED
    assert 250_000 not in ocr.calls
    assert _hits(env.store_path, "AMBIGUOUS") == []
    assert {h.timestamp_ms for h in _hits(env.store_path, "BENIGN")} == {150_000}


def test_only_uncovered_gap_still_skips_it(env):
    """A fixture with ONLY an uncovered-gap orphan (no block, no ambiguity) skips it.

    The earliest surviving window is at 200 (the covering rows were retroactively
    deleted); a screenshot at 150 precedes it → uncovered gap. The 250 frame, under
    a benign intact window, is ALLOW.
    """
    _make_recording(
        env.recordings / "rec",
        windows=[{"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"}],
        screenshot_ts=[150.0, 250.0],
        manifest=(100.0, 300.0),
    )
    ocr = _FakeOcr({150_000: "ORPHAN gap text", 250_000: "BENIGN ok"})

    summary = _run(env, ocr)
    assert summary.state == RunState.COMPLETED
    assert 150_000 not in ocr.calls
    assert _hits(env.store_path, "ORPHAN") == []
    assert {h.timestamp_ms for h in _hits(env.store_path, "BENIGN")} == {250_000}


def test_only_secure_field_null_state_still_skips_it(env):
    """A fixture with ONLY an element_state-NULL action span still skips that span.

    A benign window covers the whole range; the sole skip source is an
    ``element_state IS NULL`` action_event (a secure field could have been there),
    proving the secure-field ambiguity predicate fires on its own.
    """
    _make_recording(
        env.recordings / "rec",
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Notes"}],
        screenshot_ts=[150.0, 250.0],
        actions=[{"ts": 250.0, "element_state": None, "key_char": "x"}],
        manifest=(100.0, 300.0),
    )
    ocr = _FakeOcr({150_000: "BENIGN ok", 250_000: "MAYBESECURE field text"})

    summary = _run(env, ocr)
    assert summary.state == RunState.COMPLETED
    assert 250_000 not in ocr.calls
    assert _hits(env.store_path, "MAYBESECURE") == []
    assert {h.timestamp_ms for h in _hits(env.store_path, "BENIGN")} == {150_000}


# --------------------------------------------------------------------------
# Guard 3 — Vision-FREE real-scrub path (production scrub-on, then backfill)
# --------------------------------------------------------------------------


def test_real_scrub_then_backfill_masked_text_absent(env):
    """Drive the ACTUAL masking pipeline, THEN backfill — masked text never indexed.

    Vision-FREE: ``mask_screenshots`` EXCLUDE→deletes and MASK_WINDOW→masks a flat
    ``screenshots/`` dir without Apple Vision (Vision is only for OCR_FALLBACK,
    which is not exercised here). So this guard runs on the CI Linux backstop lane.
    The blocked window's screenshot is physically deleted by the real scrub; the
    backfill then re-derives the same block interval and never OCRs it, while the
    ALLOW frame survives masking and IS indexed.
    """
    from screencap.privacy.classify import DefaultContextClassifier
    from screencap.privacy.policy import DefaultPolicyEvaluator
    from screencap.scrubber import (
        ScrubResult,
        build_scrub_context,
        find_blocked_interval,
        mask_screenshots,
    )

    rec = env.recordings / "rec"
    # Window events sit within ``mask_screenshots``'s 5s association delta of their
    # screenshots so the real masking pipeline associates each frame to its window
    # (the EXCLUDE/MASK_WINDOW routing keys on associate_screenshot, not the
    # interval span the backfill's skip set uses).
    _make_recording(
        rec,
        windows=[
            # 1Password span [148, 248): EXCLUDE → the real scrub DELETES its frame.
            {"ts": 148.0, "bundle": "com.1password.1password", "title": "Vault"},
            # benign ALLOW span [248, +inf): kept.
            {"ts": 248.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
        ],
        screenshot_ts=[150.0, 250.0],  # 150 in EXCLUDE span; 250 ALLOW
        manifest=(100.0, 300.0),
    )

    # --- Run the REAL masking pipeline over the flat screenshots dir ----------
    from screencap.privacy.policy import parse_privacy_config

    cfg = parse_privacy_config({"privacy": {"mode": "public"}})
    evaluator = DefaultPolicyEvaluator(cfg)
    classifier = DefaultContextClassifier(app_classes=cfg.app_classes)

    ctx = build_scrub_context(
        rec / "recording.db", evaluator, classifier, time_range=(100.0, 300.0),
    )
    # Sanity: the masking context proves the 1Password span is EXCLUDE.
    blocked = find_blocked_interval(150.0, ctx.blocked_intervals)
    assert blocked is not None and blocked.action.name == "EXCLUDE"

    mask_screenshots(
        rec / "screenshots", ctx, db_path=rec / "recording.db", result=ScrubResult(),
    )
    # The real scrub deleted the EXCLUDE frame from disk.
    assert not (rec / "screenshots" / "150.000000.jpg").exists()
    assert (rec / "screenshots" / "250.000000.jpg").exists()

    # --- Now backfill the scrubbed recording ---------------------------------
    ocr = _FakeOcr({150_000: "SECRET vault text", 250_000: "BENIGN allowed text"})
    summary = _run(env, ocr, max_frames_per_recording=240)

    assert summary.state == RunState.COMPLETED
    # The masked/blocked window's text never reached OCR or the store…
    assert 150_000 not in ocr.calls
    assert _hits(env.store_path, "SECRET") == []
    # …while the ALLOW frame's text is searchable.
    assert {h.timestamp_ms for h in _hits(env.store_path, "BENIGN")} == {250_000}


# --------------------------------------------------------------------------
# Guard 4 — EventBus / progress payload is recording-name-free
# --------------------------------------------------------------------------


def test_progress_payload_is_recording_name_free(env):
    """No backfill progress arg contains the (distinctive) recording dir name.

    Complements U5's daemon-level guard at the engine level: the engine emits an
    opaque ordinal unit index, never the recording directory name (which encodes
    timing/context and would leak to same-EUID EventBus subscribers, incl. MCP).
    """
    distinctive = "rec_SECRETNAME_2026_marker"
    _make_recording(
        env.recordings / distinctive,
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "X"}],
        screenshot_ts=[150.0],
        manifest=(100.0, 200.0),
    )

    captured: list[tuple] = []

    def _cb(*args):
        captured.append(args)

    ocr = _FakeOcr({150_000: "progress text"})
    _run(env, ocr, progress_cb=_cb)

    assert captured, "progress_cb should fire at least once"
    for call in captured:
        for arg in call:
            assert distinctive not in str(arg), f"recording name leaked: {arg!r}"
            # Every arg is an int (opaque counters/ordinal), never a name string.
            assert isinstance(arg, int)


# --------------------------------------------------------------------------
# Guard 5 — read-only / local-only: only the store + ledger are written
# --------------------------------------------------------------------------


def test_recording_db_unchanged_and_only_store_and_ledger_written(env):
    """The backfill never mutates recording.db and writes ONLY the store + ledger.

    Asserts (a) recording.db content + mtime are unchanged after a run (read-only
    against recordings), and (b) the only filesystem paths created outside the
    recordings tree are the store_path and the ledger DB (no upload artifact, no
    sidecar in the recording dir).
    """
    rec = env.recordings / "rec"
    _make_recording(
        rec,
        windows=[{"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "X"}],
        screenshot_ts=[150.0],
        manifest=(100.0, 200.0),
    )
    db = rec / "recording.db"
    db_bytes_before = db.read_bytes()
    db_mtime_before = db.stat().st_mtime_ns

    # Snapshot the entire recording-dir tree before the run.
    def _tree(root: Path) -> set[Path]:
        return {p for p in root.rglob("*")}

    rec_tree_before = _tree(env.recordings)

    ocr = _FakeOcr({150_000: "readonly text"})
    summary = _run(env, ocr)
    assert summary.state == RunState.COMPLETED

    # recording.db is byte-for-byte unchanged and its mtime did not move.
    assert db.read_bytes() == db_bytes_before
    assert db.stat().st_mtime_ns == db_mtime_before

    # No new file appeared anywhere under the recordings tree (no .scrub_failed,
    # no -scrubbed copy, no manifest rewrite, no upload artifact).
    assert _tree(env.recordings) == rec_tree_before

    # The store was created (local-only write); the ledger DB exists.
    assert env.store_path.exists()
    assert (env.tmp_path / "backfill_state.db").exists()
    # And the only thing indexed is the ALLOW frame.
    assert {h.timestamp_ms for h in _hits(env.store_path, "readonly")} == {150_000}
