"""Tests for the SCR-178 U2 skip-set re-derivation (the privacy-load-bearing unit).

The backfill re-derives the canonical ``SCRUB_BLOCK_ACTIONS`` skip set from the
**intact** local ``recording.db`` and unions it with two fail-closed residuals:

* **uncovered-gap** intervals — a screenshot timestamp with no covering
  ``window_event`` (a retroactive "disable this app" deleted the rows), and
* **ambiguity** intervals — a window whose classification genuinely depends on a
  NULL policy-relevant column (``browser_url`` / ``title`` / ``element_state``),
  detected via **raw SQL on the columns** (the ``WindowContext`` loader coerces
  ``title`` NULL → ``''`` and would hide the distinction).

Every test uses a real classifier + evaluator and a real sqlite ``recording.db``
built with raw SQL (mirroring ``tests/test_scrubber_class.py``). The invariant
under test is fail-closed *superset-ness*: the re-derived set always blocks every
timestamp the live per-chunk path would block, plus the residual.
"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
from pathlib import Path

import pytest

from screencap.backfill.skip_intervals import (
    AMBIGUOUS_BROWSER_URL,
    AMBIGUOUS_SECURE_FIELD,
    AMBIGUOUS_TITLE,
    ORPHAN_SCREENSHOT,
    UNCOVERED_GAP,
    CanonicalDerivationError,
    build_classifier_evaluator,
    derive_skip_intervals,
)
from screencap.privacy.actions import PrivacyAction
from screencap.privacy.policy import (
    FrameMetadata,
    PrivacyMode,
)
from screencap.scrubber import (
    BlockedInterval,
    ScrubContext,
    build_scrub_context,
    find_blocked_interval,
)

# SCR-178: this whole module is the privacy-load-bearing skip-set derivation.
# CI runs only ``pytest -m privacy`` (there is no general pytest lane), so mark the
# module so the fail-closed skip logic actually runs as a CI guard rather than
# rotting as dev-only documentation (see SCR-110 / the privacy-guards-rot doc). The
# tests are Vision-free (real classifier + raw sqlite, no OCR), so they run on both
# the Vision-backed and the Vision-free CI lanes.
pytestmark = pytest.mark.privacy

# --------------------------------------------------------------------------
# DB fixture builders (raw SQL, mirroring tests/test_scrubber_class.py)
# --------------------------------------------------------------------------


def _make_db(
    path: Path,
    *,
    windows: list[dict],
    actions: list[dict] | None = None,
    with_browser_url: bool = True,
    pixel_ratio: float = 2.0,
) -> None:
    """Build a minimal recording.db with window_event (+ optional action_event).

    ``windows`` items: ``{ts, bundle, title, url, window_id}`` (any omitted →
    column NULL, *not* empty string — the raw NULL is the point of the
    ambiguity tests). ``with_browser_url=False`` simulates a legacy schema
    predating the ``browser_url`` column.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        db.execute("INSERT INTO recording VALUES (1, 0.0, ?)", (pixel_ratio,))

        url_col = ", browser_url TEXT" if with_browser_url else ""
        db.execute(
            f"""CREATE TABLE window_event (
                id INTEGER PRIMARY KEY,
                recording_id INTEGER,
                timestamp REAL,
                app_bundle_id TEXT,
                window_id TEXT,
                title TEXT,
                state TEXT,
                app_name TEXT{url_col}
            )"""
        )
        for i, w in enumerate(windows, start=1):
            cols = ["id", "recording_id", "timestamp", "app_bundle_id",
                    "window_id", "title", "state", "app_name"]
            vals = [i, 1, w["ts"], w.get("bundle"), w.get("window_id", f"w{i}"),
                    w.get("title"), None, w.get("app_name")]
            if with_browser_url:
                cols.append("browser_url")
                vals.append(w.get("url"))
            placeholders = ", ".join("?" * len(cols))
            db.execute(
                f"INSERT INTO window_event ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )

        db.execute(
            """CREATE TABLE action_event (
                id INTEGER PRIMARY KEY,
                recording_id INTEGER,
                name TEXT,
                timestamp REAL,
                key_char TEXT,
                element_state TEXT
            )"""
        )
        for i, a in enumerate(actions or [], start=1):
            es = a.get("element_state")
            es_raw = json.dumps(es) if isinstance(es, dict) else es
            db.execute(
                "INSERT INTO action_event (id, recording_id, name, timestamp, key_char, element_state) "
                "VALUES (?, 1, ?, ?, ?, ?)",
                (i, a.get("name", "press"), a["ts"], a.get("key_char"), es_raw),
            )
        db.commit()


def _public():
    classifier, evaluator = build_classifier_evaluator(mode=PrivacyMode.PUBLIC)
    return classifier, evaluator


def _internal():
    classifier, evaluator = build_classifier_evaluator(mode=PrivacyMode.INTERNAL)
    return classifier, evaluator


def _blocked_at(ts: float, intervals: list[BlockedInterval]) -> BlockedInterval | None:
    return find_blocked_interval(ts, intervals)


def _secure_state() -> dict:
    return {"AXRole": "AXSecureTextField"}


# --------------------------------------------------------------------------
# Happy path — intact DB reproduces the canonical scrub set
# --------------------------------------------------------------------------


def test_happy_path_sensitive_blocks_benign_allows(tmp_path):
    db = tmp_path / "recording.db"
    # 1password (sensitive, bundle-id classified) then VSCode (benign editor).
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.1password.1password", "title": "Vault"},
        {"ts": 200.0, "bundle": "com.microsoft.VSCode", "title": "Editor"},
    ])
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    # The 1password span [100, 200) is blocked (EXCLUDE).
    blocked = _blocked_at(150.0, intervals)
    assert blocked is not None
    assert blocked.action == PrivacyAction.EXCLUDE
    # VSCode under PUBLIC is TEXT_REDACT — which IS in SCRUB_BLOCK_ACTIONS, so it
    # is also blocked (canonical). Pick a benign UNKNOWN app to prove ALLOW.
    db.unlink()
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    assert _blocked_at(150.0, intervals) is None


def test_matches_fresh_scrub_canonical_set(tmp_path):
    """The re-derived canonical portion equals build_scrub_context's set."""
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.1password.1password", "title": "Vault"},
        {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    classifier, evaluator = _public()
    tr = (0.0, 1000.0)
    ctx = build_scrub_context(
        db, evaluator, classifier, time_range=tr,
    )
    derived = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=tr,
    )
    # Every canonical interval is present (by start) in the derived superset.
    canon_starts = {round(iv.start, 6) for iv in ctx.blocked_intervals}
    derived_starts = {round(iv.start, 6) for iv in derived}
    assert canon_starts <= derived_starts


# --------------------------------------------------------------------------
# Coverage gap — deleted window_event rows leave an orphan screenshot
# --------------------------------------------------------------------------


def test_uncovered_gap_skipped(tmp_path):
    db = tmp_path / "recording.db"
    # The earliest SURVIVING window is at t=200 (a retroactive "disable this
    # app" deleted the rows that covered the start of the recording). A screenshot
    # at 100 falls BEFORE any surviving window → it has no covering window_event.
    _make_db(db, windows=[
        {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator,
        time_range=(0.0, 1000.0),
        screenshot_timestamps=[100.0, 250.0],
    )
    # 100 precedes the earliest surviving window (200) → uncovered_gap skip.
    gap = _blocked_at(100.0, intervals)
    assert gap is not None
    assert gap.reason == UNCOVERED_GAP
    # 250 is covered by the surviving t=200 window (benign) → allowed.
    assert _blocked_at(250.0, intervals) is None


def test_uncovered_gap_biased_outward_no_leak(tmp_path):
    """The gap interval is biased so the adjacent ALLOW window can't leak across.

    The earliest surviving window is at t=200 (the rows covering the recording
    start were retroactively deleted). The orphan screenshot at 100 must be
    skipped, and the gap must extend OUTWARD past 100 (not collapse to a
    zero-width point) so a frame a hair before/after 100 is still inside the
    skip — and the gap must NOT reach the surviving window at 200.
    """
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator,
        time_range=(0.0, 1000.0),
        screenshot_timestamps=[100.0],
    )
    gap = _blocked_at(100.0, intervals)
    assert gap is not None and gap.reason == UNCOVERED_GAP
    assert gap.start < 100.0 < gap.end  # strictly outward on both sides
    assert gap.end < 200.0  # does not leak into the surviving window's span


# --------------------------------------------------------------------------
# Ambiguity — null policy-relevant column, raw-SQL detection
# --------------------------------------------------------------------------


def test_ambiguous_browser_url_null_internal(tmp_path):
    """Known browser with browser_url IS NULL under a URL-routed (INTERNAL) policy."""
    db = tmp_path / "recording.db"
    # Chrome with NO url. Under INTERNAL: browser_unverified → ALLOW. But some
    # URL could route to banking/email (blocked) → null is ambiguous → skip.
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.google.Chrome", "title": None, "url": None},
        {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    classifier, evaluator = _internal()
    # Sanity: live classification of the null-url Chrome window is ALLOW.
    meta = FrameMetadata(bundle_id="com.google.Chrome", domain=None, browser_url=None)
    assert evaluator.evaluate(classifier.classify(meta), meta).action == PrivacyAction.ALLOW

    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    amb = _blocked_at(150.0, intervals)
    assert amb is not None
    assert amb.reason == AMBIGUOUS_BROWSER_URL


def test_intact_browser_url_classifies_normally_no_ambiguity(tmp_path):
    """A known browser WITH a benign url is not flagged ambiguous (intact value)."""
    db = tmp_path / "recording.db"
    url = "https://github.com/foo"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.google.Chrome", "title": "GH", "url": url},
        {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "x"},
    ])
    classifier, evaluator = _internal()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    # github under INTERNAL → code_editor_terminal → ALLOW. Intact, not ambiguous.
    hit = _blocked_at(150.0, intervals)
    assert hit is None or hit.reason != AMBIGUOUS_BROWSER_URL


def test_bundle_id_sensitive_blocks_not_via_ambiguity(tmp_path):
    """A bundle-id-classified sensitive app (intact) blocks via normal classification."""
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.robinhood.Robinhood", "title": None, "url": None},
    ])
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    blocked = _blocked_at(150.0, intervals)
    assert blocked is not None
    # Blocked via canonical EXCLUDE (banking), NOT via an ambiguity reason —
    # app_bundle_id is never nulled, so banking needs no ambiguity handling.
    assert blocked.reason != AMBIGUOUS_BROWSER_URL


def test_ambiguous_title_null_raw_sql(tmp_path):
    """title IS NULL is detected on the column directly (loader NULL→'' would hide it).

    An unknown app whose title is NULL: live → UNKNOWN → ALLOW. But some title
    ("...login..."/"...bank...") would classify AUTH_FLOW/BANKING → blocked. So
    a NULL title under a title-dependent policy is ambiguous → skip. The
    WindowContext loader coerces NULL → '' which classifies identically to a
    genuine empty title, so the detection MUST query the raw column.
    """
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.example.unknownapp", "title": None, "url": None},
        {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    amb = _blocked_at(150.0, intervals)
    assert amb is not None
    assert amb.reason == AMBIGUOUS_TITLE
    # The benign window with a NON-null title is not title-ambiguous.
    hit = _blocked_at(250.0, intervals)
    assert hit is None or hit.reason != AMBIGUOUS_TITLE


def test_empty_string_title_not_treated_as_null(tmp_path):
    """A genuine empty-string title is a real value, not a NULL → no ambiguity."""
    db = tmp_path / "recording.db"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, pixel_ratio REAL)")
        conn.execute("INSERT INTO recording VALUES (1, 2.0)")
        conn.execute(
            "CREATE TABLE window_event (id INTEGER PRIMARY KEY, recording_id INTEGER, "
            "timestamp REAL, app_bundle_id TEXT, window_id TEXT, title TEXT, "
            "state TEXT, app_name TEXT, browser_url TEXT)"
        )
        # Explicit empty-string title (NOT NULL).
        conn.execute(
            "INSERT INTO window_event (id, recording_id, timestamp, app_bundle_id, "
            "window_id, title, browser_url) VALUES (1, 1, 100.0, 'com.example.unknownapp', "
            "'w1', '', NULL)"
        )
        conn.execute(
            "CREATE TABLE action_event (id INTEGER PRIMARY KEY, recording_id INTEGER, "
            "name TEXT, timestamp REAL, key_char TEXT, element_state TEXT)"
        )
        conn.commit()
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    hit = _blocked_at(150.0, intervals)
    assert hit is None or hit.reason != AMBIGUOUS_TITLE


# --------------------------------------------------------------------------
# Secure field — intact AXSecureTextField + element_state NULL spans
# --------------------------------------------------------------------------


def test_secure_field_intact_blocks_with_hold_window(tmp_path):
    """An intact AXSecureTextField action span is skipped (incl. hold-window)."""
    db = tmp_path / "recording.db"
    _make_db(
        db,
        windows=[{"ts": 0.0, "bundle": "com.example.unknownbenign", "title": "Notes"}],
        actions=[{"ts": 300.0, "element_state": _secure_state()}],
    )
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    # At the secure-field timestamp itself.
    assert _blocked_at(300.0, intervals) is not None
    # Within the hold window (default 1.0s) after.
    assert _blocked_at(300.5, intervals) is not None


def test_element_state_null_span_skipped(tmp_path):
    """An action_event with element_state IS NULL where a secure field could have
    been is skipped (fail-closed ambiguity)."""
    db = tmp_path / "recording.db"
    _make_db(
        db,
        windows=[{"ts": 0.0, "bundle": "com.example.unknownbenign", "title": "Notes"}],
        actions=[{"ts": 400.0, "element_state": None, "key_char": "x"}],
    )
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    amb = _blocked_at(400.0, intervals)
    assert amb is not None
    assert amb.reason == AMBIGUOUS_SECURE_FIELD
    # The skip spans a hold window AFTER the event (end = ts + hold), mirroring the
    # intact-secure-field case — guards against an accidental zero-width interval.
    assert _blocked_at(400.5, intervals) is not None


# --------------------------------------------------------------------------
# Mode selection per .recording_intent
# --------------------------------------------------------------------------


def _write_intent(
    rec_dir: Path, destination: str, privacy_mode: str | None = None,
) -> None:
    intent = {"version": 2, "destination": destination,
              "retention_policy": "keep_forever", "retention_params": {}}
    if privacy_mode is not None:
        intent["privacy_mode"] = privacy_mode
    (rec_dir / ".recording_intent").write_text(json.dumps(intent))


@pytest.mark.parametrize("destination", ["cloud", "both"])
def test_mode_cloud_intent_is_public(tmp_path, destination):
    rec = tmp_path / "rec"
    rec.mkdir()
    _write_intent(rec, destination, privacy_mode="internal")
    _, evaluator = build_classifier_evaluator(recording_dir=rec)
    assert evaluator.config.mode == PrivacyMode.PUBLIC


def test_mode_local_intent_uses_frozen_capture_mode(tmp_path, monkeypatch):
    """SCR-190: the frozen capture-time mode wins over a relaxed current config.

    Recorded under PUBLIC (strict), global config later relaxed to INTERNAL.
    The backfill must re-derive under the frozen PUBLIC mode so it can never
    block LESS than capture time did (the relaxed-since-capture under-block hole).
    """
    rec = tmp_path / "rec"
    rec.mkdir()
    _write_intent(rec, "local", privacy_mode="public")
    import screencap.config as cfg
    from screencap.privacy.policy import PrivacyConfig as PC

    monkeypatch.setattr(cfg, "get_privacy_config", lambda: PC(mode=PrivacyMode.INTERNAL))
    _, evaluator = build_classifier_evaluator(recording_dir=rec)
    assert evaluator.config.mode == PrivacyMode.PUBLIC


def test_mode_local_intent_missing_privacy_mode_fails_closed(tmp_path, monkeypatch):
    """SCR-190: a local intent with no frozen privacy_mode → fail-closed to PUBLIC.

    Never fall back to the (possibly relaxed) current config mode, which would
    reintroduce the under-block hole for legacy/corrupt intents.
    """
    rec = tmp_path / "rec"
    rec.mkdir()
    _write_intent(rec, "local")  # no privacy_mode field
    import screencap.config as cfg
    from screencap.privacy.policy import PrivacyConfig as PC

    monkeypatch.setattr(cfg, "get_privacy_config", lambda: PC(mode=PrivacyMode.INTERNAL))
    _, evaluator = build_classifier_evaluator(recording_dir=rec)
    assert evaluator.config.mode == PrivacyMode.PUBLIC


def test_mode_local_intent_internal_capture_mode_preserved(tmp_path, monkeypatch):
    """A genuinely-INTERNAL capture is re-derived under INTERNAL, not over-blocked."""
    rec = tmp_path / "rec"
    rec.mkdir()
    _write_intent(rec, "local", privacy_mode="internal")
    import screencap.config as cfg
    from screencap.privacy.policy import PrivacyConfig as PC

    monkeypatch.setattr(cfg, "get_privacy_config", lambda: PC(mode=PrivacyMode.PUBLIC))
    _, evaluator = build_classifier_evaluator(recording_dir=rec)
    assert evaluator.config.mode == PrivacyMode.INTERNAL


def test_mode_missing_intent_falls_back_public(tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    rec.mkdir()  # no .recording_intent
    import screencap.config as cfg
    from screencap.privacy.policy import PrivacyConfig as PC

    monkeypatch.setattr(cfg, "get_privacy_config", lambda: PC(mode=PrivacyMode.INTERNAL))
    _, evaluator = build_classifier_evaluator(recording_dir=rec)
    # Absent/unreadable intent → strictest fail-closed default.
    assert evaluator.config.mode == PrivacyMode.PUBLIC


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------


def test_empty_db_no_rows_in_range_empty(tmp_path):
    db = tmp_path / "recording.db"
    _make_db(db, windows=[])
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator,
        time_range=(0.0, 1000.0), screenshot_timestamps=[],
    )
    assert intervals == []


def test_legacy_schema_missing_browser_url_no_throw(tmp_path):
    db = tmp_path / "recording.db"
    _make_db(
        db,
        windows=[{"ts": 100.0, "bundle": "com.google.Chrome", "title": "x"}],
        with_browser_url=False,
    )
    classifier, evaluator = _internal()
    # Must not raise even though browser_url column is absent.
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    assert isinstance(intervals, list)


def test_missing_db_returns_empty(tmp_path):
    db = tmp_path / "nonexistent.db"
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator,
        time_range=(0.0, 1000.0), screenshot_timestamps=[150.0],
    )
    # No DB → no canonical/ambiguity, but the orphan screenshot is an uncovered
    # gap (fail-closed): nothing can prove it ALLOW.
    assert all(iv.reason == UNCOVERED_GAP for iv in intervals)
    assert _blocked_at(150.0, intervals) is not None


def test_trailing_open_interval_preserved(tmp_path):
    db = tmp_path / "recording.db"
    # A sensitive app is the LAST window → its interval ends at +inf.
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
        {"ts": 200.0, "bundle": "com.1password.1password", "title": "Vault"},
    ])
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
    )
    last = max(intervals, key=lambda iv: iv.start)
    assert math.isinf(last.end)
    # A far-future timestamp is still inside the open interval.
    assert _blocked_at(10_000.0, intervals) is not None


def test_returns_sorted_by_start(tmp_path):
    db = tmp_path / "recording.db"
    _make_db(
        db,
        windows=[
            {"ts": 100.0, "bundle": "com.1password.1password", "title": "V"},
            {"ts": 300.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
        ],
        actions=[{"ts": 350.0, "element_state": _secure_state()}],
    )
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator,
        time_range=(0.0, 1000.0), screenshot_timestamps=[800.0],
    )
    starts = [iv.start for iv in intervals]
    assert starts == sorted(starts)


# --------------------------------------------------------------------------
# Parity — coverage-equivalence with the live per-chunk path
# --------------------------------------------------------------------------


def test_parity_superset_of_live_blocks(tmp_path):
    """Every timestamp the live per-chunk path blocks is blocked by the derived
    set, and every ALLOW timestamp stays ALLOW, for an identical intact DB."""
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.1password.1password", "title": "Vault"},
        {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
        {"ts": 300.0, "bundle": "com.robinhood.Robinhood", "title": "Trade"},
    ])
    classifier, evaluator = _public()
    tr = (0.0, 1000.0)

    # The live path's skip set (canonical).
    live = build_scrub_context(
        db, evaluator, classifier, time_range=tr,
    ).blocked_intervals
    derived = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=tr,
        screenshot_timestamps=[150.0, 250.0, 350.0],
    )

    sample_ts = [120.0, 150.0, 250.0, 320.0, 350.0]
    for ts in sample_ts:
        if find_blocked_interval(ts, live) is not None:
            # Superset: live-blocked ⊆ derived-blocked.
            assert find_blocked_interval(ts, derived) is not None, ts

    # The benign window at [200, 300) stays ALLOW under derivation (covered,
    # intact, non-ambiguous).
    assert find_blocked_interval(250.0, derived) is None


# --------------------------------------------------------------------------
# SCR-191 — orphan-screenshot cross-check (flat file outlived its DB row)
# --------------------------------------------------------------------------


def _add_screenshot_table(db: Path, rows: list[dict]) -> None:
    """Add a ``screenshot`` table with ``rows`` (``{ts, image_path?}``).

    Omitting ``image_path`` leaves it NULL (a surviving row whose file path the
    purge could not match — the cross-check must still treat the ts as covered).
    """
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE screenshot ("
            "id INTEGER PRIMARY KEY, recording_id INTEGER, timestamp REAL, image_path TEXT)"
        )
        for i, r in enumerate(rows, start=1):
            ip = r.get("image_path", f"screenshots/{r['ts']:.6f}.jpg")
            conn.execute(
                "INSERT INTO screenshot (id, recording_id, timestamp, image_path) "
                "VALUES (?, 1, ?, ?)",
                (i, r["ts"], ip),
            )
        conn.commit()


def test_orphan_screenshot_with_purged_row_is_skipped(tmp_path):
    """A flat screenshot whose ``screenshot`` row was purged is skipped.

    Mid-recording retroactive disable deleted the covering window rows AND the
    screenshot row, but the flat file outlived its row (table-vs-flat-dir
    divergence). It floats under the adjacent ALLOW window's open-ended span, so
    the uncovered-gap pass (only-before-first-window) misses it — the orphan
    cross-check must catch it.
    """
    db = tmp_path / "recording.db"
    # One ALLOW window spanning the whole range; its open-ended span would cover
    # a frame at 250.0 if we only looked at window coverage.
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    # Surviving screenshot rows at 110 and 200 — but NOT 250 (its row was purged).
    _add_screenshot_table(db, rows=[{"ts": 110.0}, {"ts": 200.0}])
    classifier, evaluator = _public()

    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
        screenshot_timestamps=[110.0, 200.0, 250.0],
    )

    # 250.0 is an orphan (flat file present, no surviving row) → skipped.
    orphan = find_blocked_interval(250.0, intervals)
    assert orphan is not None and orphan.reason == ORPHAN_SCREENSHOT
    # Frames with surviving rows stay ALLOW (not over-skipped).
    assert find_blocked_interval(110.0, intervals) is None
    assert find_blocked_interval(200.0, intervals) is None


def test_orphan_check_tight_pad_spares_neighbours(tmp_path):
    """The orphan pad is tight — a surviving neighbour ~1s away is untouched."""
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    _add_screenshot_table(db, rows=[{"ts": 200.0}, {"ts": 201.0}])
    classifier, evaluator = _public()

    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
        screenshot_timestamps=[200.0, 200.5, 201.0],
    )

    # 200.5 is the orphan; its 200.0 / 201.0 neighbours survived → not skipped.
    assert find_blocked_interval(200.5, intervals) is not None
    assert find_blocked_interval(200.0, intervals) is None
    assert find_blocked_interval(201.0, intervals) is None


def test_null_image_path_row_still_covers_frame(tmp_path):
    """A surviving row with NULL image_path still covers its frame by timestamp."""
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    _add_screenshot_table(db, rows=[{"ts": 150.0, "image_path": None}])
    classifier, evaluator = _public()

    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
        screenshot_timestamps=[150.0],
    )
    # Covered by the timestamp-column fallback → not flagged orphan.
    assert find_blocked_interval(150.0, intervals) is None


def test_no_screenshot_table_disables_orphan_check(tmp_path):
    """Legacy schema (no ``screenshot`` table) → orphan cross-check is a no-op."""
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    classifier, evaluator = _public()

    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
        screenshot_timestamps=[150.0],
    )
    # No table → cannot cross-check → frame is NOT flagged orphan (the divergence
    # the check guards requires a purge, which only exists where the table does).
    assert find_blocked_interval(150.0, intervals) is None


# --------------------------------------------------------------------------
# SCR-198 — partial canonical read (build_scrub_context silent-empty)
# --------------------------------------------------------------------------


def _make_canonical_empty_partial_read(monkeypatch):
    """Simulate a partial recording.db read: ``build_scrub_context`` empties the
    canonical set and signals it via ``canonical_ok=False`` *without raising*.

    The separate raw-SQL ambiguity read in ``derive_skip_intervals`` opens its
    own connection on the real DB and is untouched, reproducing the exact SCR-198
    shape: an empty canonical set while ``window_starts`` is still populated (so
    the uncovered-gap pass adds nothing for a covered frame).
    """
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.build_scrub_context",
        lambda *a, **k: ScrubContext(canonical_ok=False),
    )


def test_partial_canonical_read_fail_open_by_default(tmp_path, monkeypatch):
    """Default (backfill) stays fail-open: a partial canonical read does NOT raise.

    A masked 1password window spans [100, 200). With the canonical pass emptied
    by a partial read, the frame at 150 silently under-blocks — the fail-open
    posture the backfill must keep (better to miss-index a benign frame than to
    over-skip every frame on a transient read error).
    """
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.1password.1password", "title": "Vault"},
        {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    classifier, evaluator = _public()
    _make_canonical_empty_partial_read(monkeypatch)

    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
        screenshot_timestamps=[150.0],
    )
    # No raise; the canonical hole means the masked frame is (under-)allowed. The
    # ambiguity read still saw window 100 → 150 is "covered" → no gap residual.
    assert find_blocked_interval(150.0, intervals) is None


def test_partial_canonical_read_require_canonical_raises(tmp_path, monkeypatch):
    """Fail-closed caller: ``require_canonical=True`` raises on a partial read.

    Same setup as the fail-open test, but the fail-closed ``frame.nearest`` caller
    refuses the under-blocked set rather than trusting it (SCR-198).
    """
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.1password.1password", "title": "Vault"},
        {"ts": 200.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    classifier, evaluator = _public()
    _make_canonical_empty_partial_read(monkeypatch)

    with pytest.raises(CanonicalDerivationError):
        derive_skip_intervals(
            db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
            screenshot_timestamps=[150.0],
            require_canonical=True,
        )


def test_require_canonical_clean_allow_does_not_raise(tmp_path):
    """A genuine all-ALLOW recording (clean read, empty canonical) must NOT raise.

    The whole point of the ``canonical_ok`` signal: an empty canonical set from a
    *successful* read is the legitimate shape of an all-ALLOW recording and must
    survive ``require_canonical=True`` (else the common happy path would break).
    """
    db = tmp_path / "recording.db"
    _make_db(db, windows=[
        {"ts": 100.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    classifier, evaluator = _public()
    intervals = derive_skip_intervals(
        db, classifier=classifier, evaluator=evaluator, time_range=(0.0, 1000.0),
        screenshot_timestamps=[150.0],
        require_canonical=True,
    )
    # No raise, and the benign covered frame stays ALLOW.
    assert find_blocked_interval(150.0, intervals) is None


def test_require_canonical_missing_db_raises_at_seam(tmp_path):
    """Missing recording.db + require_canonical raises *at the seam* (SCR-198).

    A missing DB is the most extreme canonical failure: build_scrub_context is
    never called. The fail-closed contract must hold here without depending on the
    uncovered-gap pass (which only fires when screenshot_timestamps is supplied),
    so the raise is self-contained — even with no screenshot_timestamps.
    """
    classifier, evaluator = _public()
    missing = tmp_path / "nonexistent" / "recording.db"
    with pytest.raises(CanonicalDerivationError):
        derive_skip_intervals(
            missing, classifier=classifier, evaluator=evaluator,
            time_range=(0.0, 1000.0),
            require_canonical=True,
        )


def test_missing_db_fail_open_by_default_no_raise(tmp_path):
    """Default (backfill) over a missing DB still returns gracefully (no raise)."""
    classifier, evaluator = _public()
    missing = tmp_path / "nonexistent" / "recording.db"
    intervals = derive_skip_intervals(
        missing, classifier=classifier, evaluator=evaluator,
        time_range=(0.0, 1000.0),
        screenshot_timestamps=[150.0],
    )
    # Fail-open: every supplied frame is an uncovered gap (no window events).
    assert find_blocked_interval(150.0, intervals) is not None
