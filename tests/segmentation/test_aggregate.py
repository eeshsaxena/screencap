"""U2 — the deterministic, coverage-qualified aggregation layer
(``screencap.segmentation.aggregate.aggregate_window``).

Covers AE2: durations/counts come from authoritative event data, never
estimated — and a static-focus span (reading / lunch / idle) is reported as
UNCOVERED rather than silently credited to the last-focused app.

The load-bearing rule under test: ``_compute_dominant_app`` credits the whole
inter-event gap to the last-focused app, so a static-focus span becomes "active"
in that app. This layer instead credits ACTIVE time only where a focus span is
corroborated by ``action_event`` timestamps and/or on-disk ``screenshots/*.jpg``,
and surfaces the uncorroborated remainder as an explicit uncovered descriptor.

Vision-free (raw sqlite + tiny jpeg headers, no OCR), and only reads local
event data, so it is safe on the CI privacy lane; marked privacy so the
coverage guarantee runs as a CI guard.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from screencap.segmentation import aggregate

pytestmark = pytest.mark.privacy

# A fixed base epoch so windows are exact and readable.
_BASE = 1_770_000_000.0  # some Unix second in 2026


def _make_recording_db(
    rec_dir: Path,
    *,
    started: float,
    end: float,
    windows: list[dict],
    actions: list[dict] | None = None,
    screenshots: list[float] | None = None,
) -> None:
    """Build a minimal recording dir (recording + window_event + action_event)
    plus optional flat ``screenshots/*.jpg`` files.

    ``windows`` items: ``{ts, bundle, app_name?, title?}`` — one per focus change.
    ``actions`` items: ``{ts, name?}`` — ``name`` defaults to ``"click"``; a
    ``"move"`` name is written but excluded from active/count corroboration
    (mirrors the manifest's ``name != 'move'`` filter). A trailing ``click`` at
    ``end`` is always inserted so the recording has a duration.
    """
    rec_dir.mkdir(parents=True, exist_ok=True)
    db = rec_dir / "recording.db"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, ?, 2.0)", (started,))
        conn.execute(
            """CREATE TABLE window_event (
                id INTEGER PRIMARY KEY, recording_id INTEGER, timestamp REAL,
                app_bundle_id TEXT, window_id TEXT, title TEXT, state TEXT,
                app_name TEXT, browser_url TEXT
            )"""
        )
        for i, w in enumerate(windows, start=1):
            # Non-null title + populated element_state below: a GENUINE recording
            # populates these columns, so the fail-closed NULL-column ambiguity
            # residual (ambiguous_title / ambiguous_secure_field) does not fire and
            # these plain ALLOW apps survive the FIX A strip. A NULL title/state is
            # the retroactive-deletion / partial-data case the residual guards, not
            # a normal recording.
            conn.execute(
                "INSERT INTO window_event (id, recording_id, timestamp, app_bundle_id, "
                "window_id, title, state, app_name, browser_url) "
                "VALUES (?, 1, ?, ?, ?, ?, NULL, ?, NULL)",
                (i, w["ts"], w.get("bundle"), f"w{i}",
                 w.get("title") or f"{w.get('app_name') or 'Window'} — main",
                 w.get("app_name")),
            )
        conn.execute(
            """CREATE TABLE action_event (
                id INTEGER PRIMARY KEY, recording_id INTEGER, name TEXT,
                timestamp REAL, key_char TEXT, element_state TEXT
            )"""
        )
        rows = list(actions or [])
        rows.append({"ts": end, "name": "click"})
        for j, a in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO action_event (id, recording_id, name, timestamp, element_state) "
                "VALUES (?, 1, ?, ?, 'normal')",
                (j, a.get("name", "click"), a["ts"]),
            )
        # A real recording.db ALWAYS carries a ``screenshot`` table (the engine's
        # metadata create_all makes every table), with one row per captured
        # frame. Its presence enables the SCR-191 orphan cross-check inside
        # derive_skip_intervals — a table-less fixture silently disables that
        # pass and hides orphan-flagging bugs (the divergence that let action
        # events be falsely orphan-flagged in production while these tests
        # stayed green).
        conn.execute(
            """CREATE TABLE screenshot (
                id INTEGER PRIMARY KEY, recording_id INTEGER,
                timestamp REAL, image_path TEXT
            )"""
        )
        for k, ts in enumerate(screenshots or [], start=1):
            conn.execute(
                "INSERT INTO screenshot (id, recording_id, timestamp, image_path) "
                "VALUES (?, 1, ?, ?)",
                (k, ts, f"screenshots/{ts}.jpg"),
            )
        conn.commit()

    if screenshots:
        shots = rec_dir / "screenshots"
        shots.mkdir(exist_ok=True)
        for ts in screenshots:
            (shots / f"{ts}.jpg").write_bytes(b"\xff\xd8\xff")  # tiny jpeg header


def _app(result: aggregate.WindowAggregate, bundle: str) -> aggregate.AppActivity | None:
    return next((a for a in result.apps if a.app == bundle), None)


# --- dense activity: the app gets covered active time ----------------------


def test_dense_activity_yields_covered_active_time(tmp_path):
    """A focus span densely corroborated by action events reports covered active
    time for that app (AE2 — computed, not estimated)."""
    rec = tmp_path / "dense"
    start = _BASE
    end = _BASE + 300
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{"ts": start, "bundle": "com.salesforce.app", "app_name": "Salesforce"}],
        # a click every 10s across the whole span → fully corroborated
        actions=[{"ts": start + t} for t in range(0, 300, 10)],
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    sf = _app(result, "com.salesforce.app")
    assert sf is not None, "Salesforce must appear with covered active time"
    # Most of the 300s window is covered (dense clicks); the layer never claims
    # the exact 300s wall-clock, but covered time is a large fraction of it.
    assert sf.covered_active_ms > 150_000
    assert sf.covered_active_ms <= 300_000
    assert sf.event_count >= 25  # ~30 clicks minus the trailing one at `end`
    # It carries backing source pointers (recording + interval).
    assert sf.sources, "covered active time must carry backing source pointers"
    assert all(s.recording == "dense" for s in sf.sources)


def test_screenshots_alone_corroborate_active_time(tmp_path):
    """Screenshots present in a focus span corroborate active time even with no
    action events (screenshot-presence is a valid coverage signal)."""
    rec = tmp_path / "shots"
    start = _BASE
    end = _BASE + 120
    # A plain ALLOW app (TextEdit) — NOT a browser, which classifies MASK_WINDOW
    # under PUBLIC mode and would be (correctly) stripped by the FIX A privacy pass,
    # confounding this coverage-signal test.
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{"ts": start, "bundle": "com.apple.TextEdit", "app_name": "TextEdit"}],
        actions=[],  # no user actions in-span (trailing click at end only)
        screenshots=[start + 10, start + 20, start + 30, start + 40],
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    editor = _app(result, "com.apple.TextEdit")
    assert editor is not None
    assert editor.covered_active_ms > 0, "screenshots alone must corroborate coverage"


# --- static-focus gap: reported UNCOVERED, not credited to the app ---------


def test_static_focus_gap_is_uncovered_not_active_time(tmp_path):
    """THE load-bearing test. A long static-focus gap with NO action events and NO
    screenshots is reported as uncovered — NOT credited as active time in the
    last-focused app (the ``_compute_dominant_app`` over-count bug)."""
    rec = tmp_path / "lunch"
    start = _BASE
    end = _BASE + 3600  # a full hour of Salesforce focus, but the user is at lunch
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{"ts": start, "bundle": "com.salesforce.app", "app_name": "Salesforce"}],
        actions=[],       # nothing happened
        screenshots=[],   # no frames captured in the gap
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    sf = _app(result, "com.salesforce.app")
    # The naive layer would report ~3600s of Salesforce. The honest layer must
    # not: the hour is uncovered, not active app time.
    if sf is not None:
        assert sf.covered_active_ms == 0, (
            "a static-focus lunch span must not be credited as active app time"
        )
    # And the uncovered hour is surfaced explicitly.
    assert result.uncovered_ms > 3_000_000, "the idle hour must be surfaced as uncovered"
    assert result.uncovered_spans, "uncovered spans must be enumerated for the caller"


# --- app / task filter -----------------------------------------------------


def test_app_filter_narrows_to_one_app(tmp_path):
    """The optional ``app`` filter narrows the result to matching apps only."""
    rec = tmp_path / "multi"
    start = _BASE
    end = _BASE + 200
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[
            {"ts": start, "bundle": "com.salesforce.app", "app_name": "Salesforce"},
            # A plain ALLOW app (TextEdit) so the filter — not the FIX A privacy
            # mask — is what narrows the result. (A browser would be MASK_WINDOW.)
            {"ts": start + 100, "bundle": "com.apple.TextEdit", "app_name": "TextEdit"},
        ],
        actions=(
            [{"ts": start + t} for t in range(0, 100, 10)]
            + [{"ts": start + t} for t in range(100, 200, 10)]
        ),
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), app="salesforce", recordings_dir=tmp_path
    )
    assert [a.app for a in result.apps] == ["com.salesforce.app"]
    assert _app(result, "com.apple.TextEdit") is None


# --- FIX A: blocked (MASK_WINDOW/EXCLUDE) apps are stripped, fail-closed ----


def _make_masked_recording_db(
    rec_dir: Path,
    *,
    started: float,
    end: float,
    masked_bundle: str = "com.1password.1password",
    masked_app_name: str = "1Password",
    actions: list[dict] | None = None,
    screenshots: list[float] | None = None,
) -> None:
    """A recording whose sole focused window is a masked password-manager window.

    Under a real PUBLIC classifier the whole open-ended span of a 1Password window
    classifies into ``SCRUB_BLOCK_ACTIONS`` (PASSWORD_MANAGER), so the app's NAME +
    the credited active time must NOT appear in the aggregate — the aggregate flow
    must apply the same fail-closed strip the point flow does."""
    rec_dir.mkdir(parents=True, exist_ok=True)
    db = rec_dir / "recording.db"
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        conn.execute("INSERT INTO recording VALUES (1, ?, 2.0)", (started,))
        conn.execute(
            """CREATE TABLE window_event (
                id INTEGER PRIMARY KEY, recording_id INTEGER, timestamp REAL,
                app_bundle_id TEXT, window_id TEXT, title TEXT, state TEXT,
                app_name TEXT, browser_url TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO window_event (id, recording_id, timestamp, app_bundle_id, "
            "window_id, title, state, app_name, browser_url) "
            "VALUES (1, 1, ?, ?, 'w1', 'Vault', NULL, ?, NULL)",
            (started, masked_bundle, masked_app_name),
        )
        conn.execute(
            """CREATE TABLE action_event (
                id INTEGER PRIMARY KEY, recording_id INTEGER, name TEXT,
                timestamp REAL, key_char TEXT, element_state TEXT
            )"""
        )
        rows = list(actions or [])
        rows.append({"ts": end, "name": "click"})
        for j, a in enumerate(rows, start=1):
            # element_state populated so the secure-field ambiguity residual does
            # NOT fire — this proves the app is stripped by the CANONICAL
            # PASSWORD_MANAGER block (1Password), not the fail-closed residual.
            conn.execute(
                "INSERT INTO action_event (id, recording_id, name, timestamp, element_state) "
                "VALUES (?, 1, ?, ?, 'normal')",
                (j, a.get("name", "click"), a["ts"]),
            )
        # Real schema: the ``screenshot`` table always exists (see
        # _make_recording_db) — its absence would silently disable the SCR-191
        # orphan cross-check for this fixture.
        conn.execute(
            """CREATE TABLE screenshot (
                id INTEGER PRIMARY KEY, recording_id INTEGER,
                timestamp REAL, image_path TEXT
            )"""
        )
        for k, ts in enumerate(screenshots or [], start=1):
            conn.execute(
                "INSERT INTO screenshot (id, recording_id, timestamp, image_path) "
                "VALUES (?, 1, ?, ?)",
                (k, ts, f"screenshots/{ts}.jpg"),
            )
        conn.commit()
    if screenshots:
        shots = rec_dir / "screenshots"
        shots.mkdir(exist_ok=True)
        for ts in screenshots:
            (shots / f"{ts}.jpg").write_bytes(b"\xff\xd8\xff")


def test_masked_app_name_and_time_are_stripped_from_aggregate(tmp_path):
    """THE FIX A privacy assertion: a MASK_WINDOW/EXCLUDE app's NAME and its credited
    active time must be ABSENT from the aggregate. The window_event for a 1Password
    window classifies into SCRUB_BLOCK_ACTIONS, so the aggregate must drop that focus
    span before deriving per-app names/durations (mirroring the point flow's
    fail-closed strip). Otherwise the masked app's name + timing egress to cloud."""
    rec = tmp_path / "masked"
    start = _BASE
    end = _BASE + 300
    _make_masked_recording_db(
        rec,
        started=start,
        end=end,
        # dense activity so, absent the strip, the app WOULD accrue covered time.
        actions=[{"ts": start + t} for t in range(0, 300, 10)],
        screenshots=[start + t for t in range(0, 300, 30)],
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    # The masked app must not appear among the aggregate's apps at all.
    app_ids = [a.app for a in result.apps]
    assert "com.1password.1password" not in app_ids, (
        "a masked app's bundle id must be stripped from the aggregate"
    )
    assert _app(result, "com.1password.1password") is None
    # And no covered active time is credited to the masked span.
    assert result.covered_active_ms == 0, (
        "a masked focus span must not be credited as active time (fail-closed)"
    )
    # Even the unattributed "screen time recorded" total must not carry the
    # masked session's duration (blocked intervals are subtracted from it).
    assert result.recorded_ms == 0, (
        "a fully-masked recording's timing must not surface in recorded_ms"
    )


def test_aggregate_fails_closed_when_blocked_intervals_underivable(tmp_path, monkeypatch):
    """Fail-closed: when a recording's blocked intervals cannot be derived, the whole
    recording's contribution is DROPPED (never credited with possibly-masked time).

    We make the strip derivation raise (mirroring a CanonicalDerivationError under
    ``require_canonical=True``) and assert the ordinary ALLOW recording — which WOULD
    otherwise accrue covered TextEdit time — contributes nothing."""
    rec = tmp_path / "allow-but-underivable"
    _make_recording_db(
        rec,
        started=_BASE,
        end=_BASE + 100,
        windows=[{"ts": _BASE, "bundle": "com.apple.TextEdit", "app_name": "TextEdit"}],
        actions=[{"ts": _BASE + t} for t in range(0, 100, 10)],
    )

    def _boom(*_a, **_k):
        raise RuntimeError("canonical block set underivable")

    # The aggregate strip derives its skip intervals via derive_skip_intervals;
    # force it to raise so the fail-closed drop path is exercised.
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.derive_skip_intervals", _boom
    )

    result = aggregate.aggregate_window(
        int(_BASE * 1000), int((_BASE + 100) * 1000), recordings_dir=tmp_path
    )
    assert result.apps == [], "an underivable recording must contribute no apps (fail-closed)"
    assert result.covered_active_ms == 0


# --- coverage clamp: a dead recording contributes nothing -------------------


def test_dead_recording_adds_no_phantom_uncovered_time(tmp_path):
    """REGRESSION (recall-aggregate-timings). A recording whose events all ended
    BEFORE the window must contribute nothing: without the coverage clamp, its
    last focus row was carried into a phantom span across the whole window, and
    every dead recording in the library added ``window_ms`` of fake "uncovered"
    time (254 h of "uncovered" in a 15 h day on a real library)."""
    dead = tmp_path / "dead"
    # Recording ran (and ended) well before the queried window.
    _make_recording_db(
        dead,
        started=_BASE - 7200,
        end=_BASE - 6600,
        windows=[{"ts": _BASE - 7200, "bundle": "com.apple.TextEdit", "app_name": "TextEdit"}],
        actions=[{"ts": _BASE - 7200 + t} for t in range(0, 600, 10)],
    )
    result = aggregate.aggregate_window(
        int(_BASE * 1000), int((_BASE + 3600) * 1000), recordings_dir=tmp_path
    )
    assert result.apps == [], "a dead recording must not surface any app"
    assert result.uncovered_ms == 0, (
        "a dead recording must not add phantom uncovered time to the window"
    )
    assert result.recorded_ms == 0


def test_live_recording_contribution_clamps_to_its_coverage_end(tmp_path):
    """A recording that stops mid-window contributes only up to its last captured
    evidence — the time after it stopped is 'not recorded', never 'idle in the
    last-focused app'."""
    rec = tmp_path / "stops-early"
    start = _BASE
    rec_end = _BASE + 600  # recording stops 10 min in
    _make_recording_db(
        rec,
        started=start,
        end=rec_end,
        windows=[{"ts": start, "bundle": "com.apple.TextEdit", "app_name": "TextEdit"}],
        actions=[{"ts": start + t} for t in range(0, 600, 10)],
    )
    # Query a 2 h window — 110 min of it have no recording at all.
    result = aggregate.aggregate_window(
        int(start * 1000), int((start + 7200) * 1000), recordings_dir=tmp_path
    )
    assert result.recorded_ms <= 600_000
    editor = _app(result, "com.apple.TextEdit")
    assert editor is not None
    assert editor.focus_ms <= 600_000, (
        "focus must clamp to the recording's coverage end, not the window end"
    )
    assert result.uncovered_ms <= 600_000, (
        "time after the recording stopped must not be counted as uncovered/idle"
    )


# --- encrypted-corpus stills corroborate coverage ---------------------------


def test_encrypted_stills_corroborate_active_time(tmp_path):
    """REGRESSION (recall-aggregate-timings). Under ``corpus_encrypted`` every
    still is ``*.jpg.enc``; the frame-presence coverage signal must survive —
    a ``.jpg``-only glob silently halves the credited active time on the shipped
    default posture."""
    rec = tmp_path / "encshots"
    start = _BASE
    end = _BASE + 120
    shot_ts = [start + 10, start + 20, start + 30, start + 40]
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{"ts": start, "bundle": "com.apple.TextEdit", "app_name": "TextEdit"}],
        actions=[],
        screenshots=shot_ts,
    )
    # Re-write the stills in the encrypted-corpus form (name-only encryption
    # stand-in; the aggregate never reads the bytes). The DB image_path rows keep
    # the same timestamps, mirroring a real encrypted capture.
    shots = rec / "screenshots"
    for ts in shot_ts:
        (shots / f"{ts}.jpg").rename(shots / f"{ts}.jpg.enc")
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    editor = _app(result, "com.apple.TextEdit")
    assert editor is not None
    assert editor.covered_active_ms > 0, (
        "encrypted .jpg.enc stills must corroborate coverage like plaintext .jpg"
    )


# --- focus figures: wall-clock focus rides alongside active time ------------


def test_focus_time_and_display_name_surface_alongside_active(tmp_path):
    """The per-app figure carries wall-clock focus (the number a person means by
    'time in that app') next to the stricter corroborated-active figure, plus the
    human display name and longest-focused window titles for narration. A
    static reading tail counts toward focus but NOT toward active (the honest
    coverage model is unchanged)."""
    rec = tmp_path / "focus"
    start = _BASE
    end = _BASE + 600
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{
            "ts": start, "bundle": "com.apple.TextEdit",
            "app_name": "TextEdit", "title": "notes.txt — TextEdit",
        }],
        # 100 s of clicks, then a 500 s untouched reading tail.
        actions=[{"ts": start + t} for t in range(0, 100, 10)],
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    editor = _app(result, "com.apple.TextEdit")
    assert editor is not None
    # Focus covers the whole recorded span; active only the corroborated part.
    assert editor.focus_ms == 600_000
    assert editor.covered_active_ms < editor.focus_ms
    assert editor.display_name == "TextEdit"
    assert editor.top_titles == ["notes.txt — TextEdit"]
    assert result.recorded_ms == 600_000


def test_blocked_sliver_clips_span_instead_of_erasing_it(tmp_path, monkeypatch):
    """REGRESSION (recall-aggregate-timings). A small blocked interval (e.g. an
    uncovered-gap residual at a span edge) must CLIP the overlapped part of a
    focus span, not erase the whole span — the old any-overlap drop erased
    minutes of legitimate activity for a 10 s residual. A fully-masked span
    still vanishes entirely (its blocked interval covers all of it) — pinned by
    ``test_masked_app_name_and_time_are_stripped_from_aggregate``."""
    rec = tmp_path / "sliver"
    start = _BASE
    end = _BASE + 300
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{"ts": start, "bundle": "com.apple.TextEdit", "app_name": "TextEdit"}],
        actions=[{"ts": start + t} for t in range(0, 300, 10)],
    )

    class _Sliver:
        start = _BASE
        end = _BASE + 10

    def _fake_derive(*_a, **_k):
        return [_Sliver()]

    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.derive_skip_intervals", _fake_derive
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    editor = _app(result, "com.apple.TextEdit")
    assert editor is not None, "a 10 s sliver must not erase the whole span"
    # The clipped span keeps everything but the blocked sliver.
    assert editor.focus_ms == 290_000
    assert editor.covered_active_ms > 200_000


# --- empty window: honest zero, not a fabricated figure --------------------


def test_empty_window_returns_zero_with_coverage_note(tmp_path):
    """An empty window (no recordings intersect it) returns zeroed apps and a
    coverage note — never a fabricated figure."""
    result = aggregate.aggregate_window(
        int(_BASE * 1000), int((_BASE + 100) * 1000), recordings_dir=tmp_path
    )
    assert result.apps == []
    assert result.covered_active_ms == 0
    assert result.coverage_note, "an empty window must carry an explicit coverage note"


# --- coverage descriptor rides the figure ----------------------------------


def test_result_carries_coverage_descriptor(tmp_path):
    """The returned figure carries its coverage descriptor so a caller can narrate
    '≈X over covered spans' rather than presenting an exact wall-clock number."""
    rec = tmp_path / "mixed"
    start = _BASE
    end = _BASE + 600
    # 5 min of clicks, then 5 min of nothing (idle).
    _make_recording_db(
        rec,
        started=start,
        end=end,
        windows=[{"ts": start, "bundle": "com.salesforce.app", "app_name": "Salesforce"}],
        actions=[{"ts": start + t} for t in range(0, 300, 10)],
    )
    result = aggregate.aggregate_window(
        int(start * 1000), int(end * 1000), recordings_dir=tmp_path
    )
    # window is 600s; covered < window (the idle tail is uncovered).
    assert result.window_ms == 600_000
    assert result.covered_active_ms < result.window_ms
    assert result.uncovered_ms > 0
    # covered + uncovered accounts for the window (no double-counting / no
    # phantom time beyond the window).
    assert result.covered_active_ms + result.uncovered_ms <= result.window_ms
    # A human-readable coverage note is present for narration.
    assert isinstance(result.coverage_note, str) and result.coverage_note
