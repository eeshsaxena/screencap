"""Privacy strip (R11 / U3) — the single chokepoint before ANY model.

``build_activity_summary`` gains an OPT-IN ``blocked_source`` parameter. When
supplied, any timeline entry / event / transcript segment whose timestamp falls
inside a blocked/masked interval is dropped ALLOW-only, fail-closed, BEFORE it
reaches the summary handed to a provider (local on-device OR cloud). When
omitted (the default), output is byte-identical to the pre-strip cloud path —
so the existing cloud caller (which passes no ``blocked_source``) is preserved.

These tests are the privacy-lane guard for that chokepoint. They are Vision-free
(a callable predicate for the stripping semantics; a real raw-sqlite
``recording.db`` for the Path-derivation integration path — no OCR / Apple
Vision), so they run on the CI ``-m privacy`` lane.

Proof-first: ``test_blocked_entry_leaks_without_strip`` shows an entry inside a
blocked interval DOES reach the summary with no strip; the strip tests then show
it removed.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from screencap.segmentation.activity_summary import build_activity_summary
from tests.segmentation import _fixtures

# CI runs only ``pytest -m privacy`` (there is no general pytest lane). The strip
# is privacy-load-bearing, so mark the module so it runs as a CI guard rather
# than rotting as dev-only. Vision-free → runs on both CI lanes.
pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Local fixtures (own — U3 must NOT edit tests/segmentation/_fixtures.py)
# ---------------------------------------------------------------------------

# Two chunks spanning [1000, 8200). Window switches at 1100 (VSCode), 2000
# (Chrome/Gmail), 3000 (VSCode), 4700 (VSCode), 6000 (Slack).
_MANIFESTS: list[dict] = [
    {"format_version": 2, "chunk_index": 0, "chunk_start": 1000.0,
     "chunk_end": 4600.0, "stats": {}, "blocked_intervals": []},
    {"format_version": 2, "chunk_index": 1, "chunk_start": 4600.0,
     "chunk_end": 8200.0, "stats": {}, "blocked_intervals": []},
]

_EVENTS_0: list[dict] = [
    {"type": "window.switch", "timestamp": 1100.0,
     "app_bundle_id": "com.microsoft.VSCode", "window_title": "main.py"},
    {"type": "key.type", "timestamp": 1110.0, "text": "def foo():"},
    # The blocked window: Gmail/Chrome span [2000, 3000).
    {"type": "window.switch", "timestamp": 2000.0,
     "app_bundle_id": "com.google.Chrome", "window_title": "Gmail",
     "domain": "mail.google.com"},
    {"type": "key.type", "timestamp": 2010.0, "text": "secret password"},
    {"type": "window.switch", "timestamp": 3000.0,
     "app_bundle_id": "com.microsoft.VSCode", "window_title": "auth.py"},
    {"type": "key.type", "timestamp": 3010.0, "text": "class Auth:"},
]

_EVENTS_1: list[dict] = [
    {"type": "window.switch", "timestamp": 4700.0,
     "app_bundle_id": "com.microsoft.VSCode", "window_title": "auth.py"},
    {"type": "window.switch", "timestamp": 6000.0,
     "app_bundle_id": "com.tinyspeck.slackmacgap", "window_title": "#dev — Slack"},
]

_TRANSCRIPT_0: dict = {
    "segments": [
        # abs_ts = 1000 (chunk_start) + start. 50 → 1050 (ALLOW window),
        # 1200 → 2200 (inside the blocked [2000,3000) span).
        {"start": 50.0, "end": 55.0, "text": "Working in the editor"},
        {"start": 1200.0, "end": 1205.0, "text": "Reading a private email"},
    ]
}


def _source() -> _fixtures.InMemorySource:
    return _fixtures.InMemorySource(
        events_by_chunk={0: _EVENTS_0, 1: _EVENTS_1},
        transcripts_by_chunk={0: _TRANSCRIPT_0},
    )


def _apps(result: dict) -> list[str]:
    return [e["title"] for e in result["summary"]["timeline"]]


# A blocked interval covering the Gmail/Chrome span [2000, 3000).
def _blocks_email(ts: float) -> bool:
    return 2000.0 <= ts < 3000.0


# ---------------------------------------------------------------------------
# DB builder (raw sqlite, mirroring tests/backfill/test_skip_intervals.py) —
# for the Path → recording.db derivation integration path. Vision-free.
# ---------------------------------------------------------------------------


def _make_db(path: Path, windows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        db.execute("INSERT INTO recording VALUES (1, 0.0, 2.0)")
        db.execute(
            """CREATE TABLE window_event (
                id INTEGER PRIMARY KEY,
                recording_id INTEGER,
                timestamp REAL,
                app_bundle_id TEXT,
                window_id TEXT,
                title TEXT,
                state TEXT,
                app_name TEXT,
                browser_url TEXT
            )"""
        )
        for i, w in enumerate(windows, start=1):
            db.execute(
                "INSERT INTO window_event "
                "(id, recording_id, timestamp, app_bundle_id, window_id, title, "
                "state, app_name, browser_url) VALUES (?, 1, ?, ?, ?, ?, NULL, ?, ?)",
                (i, w["ts"], w.get("bundle"), w.get("window_id", f"w{i}"),
                 w.get("title"), w.get("app_name"), w.get("url")),
            )
        db.commit()


# ===========================================================================
# Proof-first: an entry inside a blocked interval LEAKS without the strip
# ===========================================================================


def test_blocked_entry_leaks_without_strip():
    """RED baseline: with no ``blocked_source`` the Gmail entry is present."""
    result = build_activity_summary("rec", _MANIFESTS, _source())
    assert "Gmail" in _apps(result), (
        "baseline: blocked-window entry should be present when no strip is applied"
    )
    # And its typed text (the sensitive content) reaches the summary.
    gmail = [e for e in result["summary"]["timeline"] if e["title"] == "Gmail"]
    assert gmail and gmail[0].get("typed") == ["secret password"]


# ===========================================================================
# Covers AE1 — an entry inside a MASK/EXCLUDE interval is stripped
# ===========================================================================


def test_blocked_entry_stripped_with_predicate():
    """A window.switch inside a blocked interval is absent from the summary."""
    result = build_activity_summary(
        "rec", _MANIFESTS, _source(), blocked_source=_blocks_email,
    )
    titles = _apps(result)
    assert "Gmail" not in titles, "blocked Gmail entry leaked past the strip"
    # ALLOW windows survive.
    assert "main.py" in titles
    assert "auth.py" in titles
    assert "#dev — Slack" in titles


def test_blocked_typed_text_and_raw_events_stripped():
    """Stripping is at the event boundary → raw-fallback data is stripped too."""
    result = build_activity_summary(
        "rec", _MANIFESTS, _source(), blocked_source=_blocks_email,
    )
    # No merged entry carries the blocked window's typed text.
    all_typed = [t for e in result["entries"] for t in e.get("typed", [])]
    assert "secret password" not in all_typed
    # The raw-fallback window list (used by the idle-gap fallback) excludes it.
    raw_titles = [w["title"] for w in result["raw_window_events"]]
    assert "Gmail" not in raw_titles
    # No raw timestamp lands inside the blocked span.
    assert not any(2000.0 <= ts < 3000.0 for ts in result["raw_timestamps"])


def test_blocked_transcript_segment_stripped():
    """A transcript segment whose absolute timestamp is blocked is dropped."""
    result = build_activity_summary(
        "rec", _MANIFESTS, _source(), blocked_source=_blocks_email,
    )
    snippets = result["summary"].get("transcript", [])
    texts = [s["text"] for s in snippets]
    assert "Working in the editor" in texts        # ALLOW (abs_ts 1050)
    assert "Reading a private email" not in texts   # blocked (abs_ts 2200)


# ===========================================================================
# Fail-closed — ambiguous / unreadable read excludes rather than includes
# ===========================================================================


def test_fail_closed_missing_recording_db_strips_everything(tmp_path):
    """A recording dir with no ``recording.db`` → every entry stripped.

    ``derive_skip_intervals(require_canonical=True)`` raises on a missing DB;
    the strip maps that to the all-blocked sentinel (fail-closed), so an
    unreadable recording never leaks content to the model.
    """
    empty_dir = tmp_path / "rec-no-db"
    empty_dir.mkdir()
    result = build_activity_summary(
        "rec", _MANIFESTS, _source(), blocked_source=empty_dir,
    )
    # No window activity survives → the builder returns None (nothing to send).
    assert result is None


def test_fail_closed_coverage_gap_treated_as_blocked(tmp_path):
    """A window with no covering ``window_event`` row is an uncovered gap.

    The recording.db has ONLY a late surviving window (t=5000); the earlier
    ALLOW windows (1100/2000/3000/4700) have no covering row, so their frames
    are uncovered-gap intervals (fail-closed) and are stripped. This proves an
    ambiguous/coverage-gap read excludes rather than includes.
    """
    rec_dir = tmp_path / "rec-gap"
    rec_dir.mkdir()
    # Earliest surviving window is at 5000 → everything before it is an
    # uncovered gap. Slack (6000) is covered by the surviving benign window.
    _make_db(rec_dir / "recording.db", windows=[
        {"ts": 5000.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    result = build_activity_summary(
        "rec", _MANIFESTS, _source(), blocked_source=rec_dir,
    )
    titles = _apps(result) if result else []
    # Everything before the surviving window is uncovered-gap-stripped.
    assert "Gmail" not in titles
    assert "main.py" not in titles
    # The window covered by the surviving benign row survives.
    assert "#dev — Slack" in titles


def test_real_db_blocks_sensitive_app_span(tmp_path):
    """Integration: a sensitive-app span in a real recording.db strips its entry.

    A 1Password window (bundle-id classified → EXCLUDE) covers the [2000, 3000)
    span; the Gmail entry there is derived-blocked and stripped, while a benign
    (UNKNOWN) app in an adjacent span survives. Proves the Path →
    ``derive_skip_intervals`` wiring, not just an injected predicate.

    Note: under the default PUBLIC classifier VSCode is ``TEXT_REDACT`` (in
    ``SCRUB_BLOCK_ACTIONS`` → blocked), so a genuinely-ALLOW window must use an
    UNKNOWN/benign bundle id to prove passthrough.
    """
    rec_dir = tmp_path / "rec-real"
    rec_dir.mkdir()
    _make_db(rec_dir / "recording.db", windows=[
        {"ts": 1100.0, "bundle": "com.example.unknownbenign", "title": "main.py"},
        {"ts": 2000.0, "bundle": "com.1password.1password", "title": "Vault"},
        {"ts": 3000.0, "bundle": "com.example.unknownbenign", "title": "auth.py"},
        {"ts": 4700.0, "bundle": "com.example.unknownbenign", "title": "auth.py"},
        {"ts": 6000.0, "bundle": "com.example.unknownbenign", "title": "Notes"},
    ])
    result = build_activity_summary(
        "rec", _MANIFESTS, _source(), blocked_source=rec_dir,
    )
    titles = _apps(result) if result else []
    assert "Gmail" not in titles, "sensitive [2000,3000) span did not strip Gmail"
    assert "main.py" in titles    # benign window in the [1100,2000) span survives


# ===========================================================================
# All-ALLOW passthrough + default-off cloud-behavior preservation
# ===========================================================================


def test_all_allow_predicate_passthrough():
    """An all-ALLOW recording (predicate never blocks) leaves content unchanged.

    The strip ran (a ``blocked_source`` was applied) but blocked nothing, so the
    content matches the no-strip build while the summary is authoritatively
    marked ``stripped=True`` (the on-device provider's fail-closed gate relies on
    this). The no-strip build carries no marker.
    """
    stripped = build_activity_summary(
        "rec", _MANIFESTS, _source(), blocked_source=lambda _ts: False,
    )
    plain = build_activity_summary("rec", _MANIFESTS, _source())
    assert stripped.pop("stripped") is True
    assert "stripped" not in plain
    assert stripped == plain


def test_default_off_preserves_cloud_behavior():
    """With ``blocked_source`` omitted, output is byte-identical to no-strip.

    This is the cloud-behavior-preservation guard: the cloud caller passes no
    ``blocked_source``, so the strip is a no-op and the golden shape holds.
    Compared against the shared golden fixture used by the characterization test.
    """
    # Direct comparison: the same builder call with the param omitted vs. a
    # freshly-built no-arg call must match exactly.
    a = build_activity_summary(
        "test-rec", _fixtures.MANIFESTS, _fixtures.default_source(),
    )
    b = build_activity_summary(
        "test-rec", _fixtures.MANIFESTS, _fixtures.default_source(),
        blocked_source=None,
    )
    assert a == b

    # And the omitted-param output still matches the frozen golden shape, so the
    # default path did not perturb the shipped artifact.
    import json

    golden = json.loads(
        (Path(__file__).resolve().parent / "golden_segmentation.json").read_text()
    )["activity_summary"]
    assert json.loads(json.dumps(a, sort_keys=True)) == golden


# ===========================================================================
# The activity summary carries text only — never frame/image bytes (R11 doc)
# ===========================================================================


def test_summary_carries_no_image_bytes():
    """Sanity guard: the summary is JSON-serializable text (no bytes/frames)."""
    import json

    result = build_activity_summary(
        "rec", _MANIFESTS, _source(), blocked_source=_blocks_email,
    )
    # A bytes value anywhere would raise here; the summary is text/number only.
    dumped = json.dumps(result["summary"])
    assert "\\u0000" not in dumped  # no raw binary smuggled as a string
