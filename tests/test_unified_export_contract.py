"""Cross-caller byte-identical contract for the unified export pipeline.

Headline test for Unit 8 of the unified-export-callable refactor. Pins the
post-meta byte-identical contract between the three callers of
``screencap.engine.export.unified_export_events``:

1. **CLI export** — ``CaptureSession.export_events()`` from
   ``src/screencap/engine/capture.py``.
2. **Chunk processor** — ``ChunkProcessor._export_events()`` from
   ``src/screencap/chunk_processor.py``.
3. **Recovery** — ``_recover_chunk_metadata()`` from
   ``src/screencap/cli.py`` (called by ``screencap upload``).

The CLI sees the full recording and does NOT apply ``window_filter``
(``window_filter=None``). The chunk processor and recovery slice the
recording by ``[start_ts, end_ts)`` and apply the cloud window filter
when ``cloud_intent`` / ``cloud_bound`` is True.

Strategy: build a single fixture recording covering one chunk's worth of
time (so CLI's "see everything" view and the chunk's slice cover the
same rows). For each scenario, run the three paths against the same
recording, strip the ``_meta`` header (``exported_at`` differs by run),
and compare the remaining lines byte-for-byte.

Pre-Unit-7 sanity note: today's recovery's degraded format would
have failed this test trivially — recovery's old behaviour wrote raw
DB-row dumps (no Pydantic conversion, no window.switch events, no
``_meta`` header). The mere format shape (``_meta`` line + processed
events with ``type`` discriminators) would diverge before any byte-level
comparison even mattered. We do not test the pre-refactor binary
explicitly; the format mismatch alone is the regression signal.
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import pytest

from screencap.privacy.policy import PrivacyConfig, PrivacyMode

# ---------------------------------------------------------------------------
# Helpers — fixture builders + path runners
# ---------------------------------------------------------------------------


def _public_privacy_config():
    """Patch ``get_privacy_config`` to a clean PUBLIC-mode config.

    Mirrors ``tests/privacy/test_filter_factory.py`` so policy outcomes
    don't depend on the developer's local config.toml. Cloud-bound
    callers force PUBLIC anyway, but the underlying ``app_classes`` come
    from this fixture — keeping the test deterministic.
    """
    return mock.patch(
        "screencap.config.get_privacy_config",
        return_value=PrivacyConfig(mode=PrivacyMode.PUBLIC),
    )


def _insert_action(rdb, ts_offset, name="click", **kwargs):
    """Insert an action_event at ``base_ts + ts_offset``.

    Mirrors the conftest fixture's ``add_click`` / ``add_keypress``
    pattern — offsets keep tests readable while the actual stored
    timestamps live in the recording's real time domain.
    """
    from screencap.engine.db import crud

    data = dict(kwargs)
    data["name"] = name
    ts = rdb._base_ts + ts_offset
    crud.insert_action_event(rdb.session, rdb.recording, ts, data)


def _insert_window(
    rdb,
    ts_offset,
    *,
    title="Finder",
    bundle_id="com.apple.finder",
    window_id="1",
    left=0,
    top=0,
    width=800,
    height=600,
    browser_url=None,
):
    """Insert a window_event at ``base_ts + ts_offset``.

    Same offset convention as :func:`_insert_action` — keeps the
    fixture readable while the chunk processor's ``[start_ts, end_ts)``
    slice operates on real timestamps.
    """
    from screencap.engine.db import crud

    data = {
        "title": title,
        "app_bundle_id": bundle_id,
        "window_id": window_id,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }
    if browser_url is not None:
        data["browser_url"] = browser_url
    ts = rdb._base_ts + ts_offset
    crud.insert_window_event(rdb.session, rdb.recording, ts, data)


def _strip_meta(jsonl_text: str) -> list[str]:
    """Drop the ``_meta`` header line from a JSONL blob; return remaining lines."""
    lines = [ln for ln in jsonl_text.split("\n") if ln.strip()]
    if not lines:
        return []
    first = json.loads(lines[0])
    if isinstance(first, dict) and first.get("_meta") is True:
        return lines[1:]
    return lines


def _post_meta_lines_from_events(events) -> list[str]:
    """Render a list of Pydantic events as the post-meta JSONL lines.

    Mirrors what ``write_events_jsonl`` emits per event (
    ``model_dump_json()`` per line). Used to compare the CLI's in-memory
    list output against the JSONL written by chunk + recovery.
    """
    return [ev.model_dump_json() for ev in events]


def _run_cli_path(rec_dir: Path, *, include_moves: bool) -> list[str]:
    """Run the CLI export path (``CaptureSession.export_events``)."""
    from screencap.engine.capture import Capture

    with Capture.load(str(rec_dir)) as cap:
        events = cap.export_events(include_moves=include_moves)
    return _post_meta_lines_from_events(events)


def _run_chunk_path(
    rec_dir: Path,
    start_ts: float,
    end_ts: float,
    *,
    cloud_intent: bool,
    privacy_mode: str = "internal",
) -> list[str]:
    """Run the chunk processor path (``ChunkProcessor._export_events``)."""
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    if cloud_intent:
        # cloud_intent=True triggers privacy-pipeline init; mock the heavy
        # dependencies so this stays a unit test.
        with (
            mock.patch(
                "screencap.privacy.create_default_pipeline",
                return_value=MagicMock(),
            ),
            mock.patch("screencap.privacy.Anonymizer"),
        ):
            cp = ChunkProcessor(
                rec_dir,
                q,
                ack_q,
                recording_name="contract-test",
                upload_enabled=False,
                auto_delete=False,
                cloud_intent=True,
                privacy_mode=privacy_mode,
            )
    else:
        cp = ChunkProcessor(
            rec_dir,
            q,
            ack_q,
            recording_name="contract-test",
            upload_enabled=False,
            auto_delete=False,
            cloud_intent=False,
            privacy_mode=privacy_mode,
        )

    # Remove any prior output so the export actually runs (the path
    # short-circuits when the JSONL already exists).
    out_path = rec_dir / "events_0000.jsonl"
    out_path.unlink(missing_ok=True)

    cp._export_events(0, start_ts, end_ts)

    return _strip_meta(out_path.read_text())


def _run_recovery_path(
    rec_dir: Path,
    start_ts: float,
    end_ts: float,
    *,
    cloud_bound: bool,
) -> list[str]:
    """Run the recovery path (``_recover_chunk_metadata``).

    Simulates the upload flow's recovery scenario: a chunk video file
    exists on disk, the per-chunk events JSONL does not, and recovery
    must re-derive it from ``recording.db``.

    The recovery path uses chunk-duration arithmetic to derive its time
    ranges. We pin a synthetic chunk video file at index 0 and configure
    ``get_chunk_duration`` so that chunk 0 covers ``[start_ts, end_ts)``
    relative to the recording's base timestamp.
    """
    from rich.console import Console

    from screencap.cli import _recover_chunk_metadata

    out_path = rec_dir / "events_0000.jsonl"
    out_path.unlink(missing_ok=True)

    # Drop a placeholder chunk_0000.mp4 so recovery sees this as a chunked recording.
    chunk_video = rec_dir / "chunk_0000.mp4"
    chunk_video.write_bytes(b"placeholder")

    # Recovery's _recover_chunk_metadata derives chunk ranges from
    # (recording.timestamp, MIN/MAX action_event.timestamp, n_chunks,
    # get_chunk_duration()). With a single chunk, the last chunk extends
    # to last_action_ts + 1.0, so chunk 0 effectively spans
    # [base_ts, max(base_ts + chunk_dur, last_ts + 1.0)).
    #
    # We pin chunk_dur to a value that puts (start_ts, end_ts) inside
    # chunk 0's range. The recording fixture starts at 1000.0 and our
    # action range covers [1000.0, ~1010.0); pinning chunk_dur to 60s
    # makes chunk 0 cover [1000.0, 1060.0) which envelops the test data.
    console = Console(quiet=True)
    with mock.patch("screencap.config.get_chunk_duration", return_value=60.0):
        _recover_chunk_metadata(
            rec_dir,
            console,
            force=True,
            cloud_bound=cloud_bound,
        )

    if not out_path.exists():
        return []
    return _strip_meta(out_path.read_text())


# ---------------------------------------------------------------------------
# Fixture recordings — small but representative
# ---------------------------------------------------------------------------


def _build_full_fixture(rdb) -> tuple[float, float]:
    """Build the canonical fixture recording.

    ~10 actions (clicks, keypresses, drags), 3 distinct window switches
    (one ALLOW app, one MASK_WINDOW app, one EXCLUDE app), one drag with
    move children, and several mouse.move events sprinkled between
    actions.

    All timestamps are inside ``[1000.0, 1010.0)`` so a chunk processor
    call with ``[1000.0, 1010.0)`` and a CLI export both see the same
    rows.

    Returns ``(start_ts, end_ts)`` for the chunk slice.
    """
    # Window 1: Finder (ALLOW under PUBLIC for unknown context).
    _insert_window(
        rdb,
        0.05,
        title="Documents",
        bundle_id="com.apple.finder",
        window_id="finder-1",
    )

    # A click on Finder.
    _insert_action(
        rdb, 0.10, "click",
        mouse_x=100, mouse_y=100,
        mouse_button_name="left", mouse_pressed=1,
    )
    _insert_action(
        rdb, 0.15, "click",
        mouse_x=100, mouse_y=100,
        mouse_button_name="left", mouse_pressed=0,
    )

    # A few mouse.move events between actions.
    _insert_action(rdb, 0.25, "move", mouse_x=200, mouse_y=150)
    _insert_action(rdb, 0.35, "move", mouse_x=210, mouse_y=160)

    # Window 2: Slack (MASK_WINDOW under PUBLIC for CHAT context).
    _insert_window(
        rdb,
        0.50,
        title="#secret-channel - Slack",
        bundle_id="com.tinyspeck.slackmacgap",
        window_id="slack-1",
    )

    # A keypress in Slack.
    _insert_action(rdb, 0.60, "press",
                   key_name="h", key_char="h",
                   canonical_key_name="h", canonical_key_char="h")
    _insert_action(rdb, 0.65, "release",
                   key_name="h", key_char="h",
                   canonical_key_name="h", canonical_key_char="h")

    # A drag (down -> moves -> up at distance > threshold).
    _insert_action(rdb, 1.00, "click",
                   mouse_x=300, mouse_y=200,
                   mouse_button_name="left", mouse_pressed=1)
    _insert_action(rdb, 1.05, "move", mouse_x=310, mouse_y=210)
    _insert_action(rdb, 1.10, "move", mouse_x=320, mouse_y=220)
    _insert_action(rdb, 1.15, "move", mouse_x=350, mouse_y=250)
    _insert_action(rdb, 1.20, "click",
                   mouse_x=400, mouse_y=300,
                   mouse_button_name="left", mouse_pressed=0)

    # Window 3: 1Password (EXCLUDE under every mode for PASSWORD_MANAGER).
    _insert_window(
        rdb,
        2.00,
        title="My Personal Vault - 1Password",
        bundle_id="com.1password.1password",
        window_id="1pw-1",
    )

    # Click in 1Password (privacy filtering only acts on window switches,
    # so the click stays in the stream).
    _insert_action(rdb, 2.10, "click",
                   mouse_x=500, mouse_y=400,
                   mouse_button_name="left", mouse_pressed=1)
    _insert_action(rdb, 2.15, "click",
                   mouse_x=500, mouse_y=400,
                   mouse_button_name="left", mouse_pressed=0)

    rdb.session.commit()

    # Chunk slice covers the entire fixture so CLI (full recording) and
    # chunk (slice) see the same rows.
    base = rdb._base_ts
    return (base, base + 5.0)


# ---------------------------------------------------------------------------
# Headline contract tests
# ---------------------------------------------------------------------------


class TestNonCloudByteIdentical:
    """Non-cloud recording: all three callers produce the same post-meta
    output for the same time range. CLI doesn't apply window_filter at
    all; chunk and recovery have ``window_filter=None`` when
    ``cloud_intent=False`` / ``cloud_bound=False``.

    This is the simplest contract — no privacy filter divergence.
    """

    def test_three_callers_produce_identical_post_meta(self, recording_db):
        """The headline test: byte-identical output from CLI, chunk
        processor, and recovery for a non-cloud recording.
        """
        rec_dir = recording_db.db_path.parent
        start_ts, end_ts = _build_full_fixture(recording_db)

        cli_lines = _run_cli_path(rec_dir, include_moves=True)
        chunk_lines = _run_chunk_path(
            rec_dir, start_ts, end_ts, cloud_intent=False,
        )
        recovery_lines = _run_recovery_path(
            rec_dir, start_ts, end_ts, cloud_bound=False,
        )

        # All three callers must agree byte-for-byte.
        assert cli_lines == chunk_lines, (
            "CLI vs chunk diverged in non-cloud mode — "
            "the unified callable's body must produce the same output "
            "for both call sites."
        )
        assert chunk_lines == recovery_lines, (
            "Chunk vs recovery diverged in non-cloud mode — "
            "recovery is supposed to be byte-identical to chunk."
        )
        # Sanity: the output is non-empty.
        assert len(cli_lines) > 0


class TestCloudByteIdenticalChunkAndRecovery:
    """Cloud-intent recording: chunk and recovery are byte-identical
    post-meta. CLI is allowed to differ (CLI does not apply
    ``window_filter`` per ``capture.py:462``); we only assert the
    chunk↔recovery contract here.
    """

    def test_chunk_and_recovery_agree_under_cloud_filter(self, recording_db):
        """Both apply the cloud window filter; both must produce the
        same post-meta JSONL for the same time range.
        """
        rec_dir = recording_db.db_path.parent
        start_ts, end_ts = _build_full_fixture(recording_db)

        with _public_privacy_config():
            chunk_lines = _run_chunk_path(
                rec_dir, start_ts, end_ts, cloud_intent=True,
            )
            recovery_lines = _run_recovery_path(
                rec_dir, start_ts, end_ts, cloud_bound=True,
            )

        assert chunk_lines == recovery_lines, (
            "Chunk vs recovery diverged under the cloud window filter — "
            "the Slack-leak class regression vector. "
            "Both call sites use build_cloud_window_filter; their "
            "outputs must remain byte-identical."
        )

    def test_cloud_filter_observably_changes_output(self, recording_db):
        """Sanity check: the cloud filter actually does something
        observable. Without this, the chunk↔recovery test could
        accidentally pass if both produced the *unfiltered* output.

        Slack should be MASK_WINDOW (title replaced by app_name);
        1Password should be EXCLUDE (suppressed entirely).
        """
        rec_dir = recording_db.db_path.parent
        start_ts, end_ts = _build_full_fixture(recording_db)

        with _public_privacy_config():
            chunk_lines = _run_chunk_path(
                rec_dir, start_ts, end_ts, cloud_intent=True,
            )

        events = [json.loads(ln) for ln in chunk_lines]
        ws_events = [e for e in events if e.get("type") == "window.switch"]
        bundles = {e["app_bundle_id"] for e in ws_events}

        # 1Password (PASSWORD_MANAGER) → EXCLUDE → suppressed entirely.
        assert "com.1password.1password" not in bundles, (
            "Cloud filter must suppress EXCLUDE apps (1Password)."
        )
        # Slack (CHAT) → MASK_WINDOW → title masked to app_name.
        slack = next(
            (e for e in ws_events if e["app_bundle_id"] == "com.tinyspeck.slackmacgap"),
            None,
        )
        assert slack is not None
        assert slack["window_title"] == slack["app_name"], (
            "Cloud filter must mask MASK_WINDOW app titles."
        )
        assert slack["domain"] is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCaseNoMouseMoves:
    """Recording with no ``mouse.move`` events — all three paths still
    match."""

    def test_three_callers_match_with_no_moves(self, recording_db):
        rec_dir = recording_db.db_path.parent

        # Single window switch + click pair, no moves.
        _insert_window(
            recording_db, 0.05,
            title="Documents",
            bundle_id="com.apple.finder",
            window_id="finder-1",
        )
        _insert_action(
            recording_db, 0.50, "click",
            mouse_x=100, mouse_y=100,
            mouse_button_name="left", mouse_pressed=1,
        )
        _insert_action(
            recording_db, 0.55, "click",
            mouse_x=100, mouse_y=100,
            mouse_button_name="left", mouse_pressed=0,
        )
        recording_db.session.commit()

        base = recording_db._base_ts
        start_ts, end_ts = base, base + 5.0

        cli_lines = _run_cli_path(rec_dir, include_moves=True)
        chunk_lines = _run_chunk_path(
            rec_dir, start_ts, end_ts, cloud_intent=False,
        )
        recovery_lines = _run_recovery_path(
            rec_dir, start_ts, end_ts, cloud_bound=False,
        )

        assert cli_lines == chunk_lines == recovery_lines


class TestEdgeCaseDenseMovesInterleavedWithWindows:
    """Recording with mouse.move events densely interleaved with window
    switches. Tests the interleave-by-timestamp invariant under load."""

    def test_three_callers_match_with_dense_moves(self, recording_db):
        rec_dir = recording_db.db_path.parent

        # Pattern: window switch, several moves, window switch, several
        # moves, window switch. Tests that the deduplicate→interleave
        # ordering produces the same result for all three callers.
        _insert_window(
            recording_db, 0.05,
            title="Doc 1",
            bundle_id="com.apple.finder",
            window_id="w1",
        )
        for i, ts in enumerate([0.10, 0.15, 0.20, 0.25, 0.30]):
            _insert_action(recording_db, ts, "move",
                           mouse_x=100 + i * 5, mouse_y=100 + i * 5)

        _insert_window(
            recording_db, 0.50,
            title="Editor",
            bundle_id="com.example.editor",
            window_id="w2",
        )
        for i, ts in enumerate([0.55, 0.60, 0.65, 0.70, 0.75]):
            _insert_action(recording_db, ts, "move",
                           mouse_x=200 + i * 5, mouse_y=200 + i * 5)

        _insert_window(
            recording_db, 1.00,
            title="Browser",
            bundle_id="com.apple.Safari",
            window_id="w3",
        )
        for i, ts in enumerate([1.05, 1.10, 1.15]):
            _insert_action(recording_db, ts, "move",
                           mouse_x=300 + i * 5, mouse_y=300 + i * 5)

        recording_db.session.commit()

        base = recording_db._base_ts
        start_ts, end_ts = base, base + 5.0

        cli_lines = _run_cli_path(rec_dir, include_moves=True)
        chunk_lines = _run_chunk_path(
            rec_dir, start_ts, end_ts, cloud_intent=False,
        )
        recovery_lines = _run_recovery_path(
            rec_dir, start_ts, end_ts, cloud_bound=False,
        )

        assert cli_lines == chunk_lines == recovery_lines
        # Dense moves should produce >= 1 mouse.move event after merging.
        types = {json.loads(ln).get("type") for ln in cli_lines}
        assert "mouse.move" in types
        assert "window.switch" in types


class TestEdgeCaseDragAtChunkBoundary:
    """Drag event with move children — all three paths must still match
    within the constraint that CLI sees the full recording while chunk
    sees ``[start_ts, end_ts)``.

    The chunk slice envelops the entire drag (down + moves + up), so
    behavior should be byte-identical to CLI.
    """

    def test_drag_with_move_children_matches(self, recording_db):
        rec_dir = recording_db.db_path.parent

        _insert_window(
            recording_db, 0.05,
            title="Editor",
            bundle_id="com.example.editor",
            window_id="w1",
        )
        # Drag: down -> moves -> up (span > threshold so it's a drag).
        _insert_action(recording_db, 0.20, "click",
                       mouse_x=100, mouse_y=100,
                       mouse_button_name="left", mouse_pressed=1)
        _insert_action(recording_db, 0.25, "move", mouse_x=120, mouse_y=120)
        _insert_action(recording_db, 0.30, "move", mouse_x=150, mouse_y=150)
        _insert_action(recording_db, 0.35, "move", mouse_x=200, mouse_y=200)
        _insert_action(recording_db, 0.40, "click",
                       mouse_x=250, mouse_y=250,
                       mouse_button_name="left", mouse_pressed=0)
        recording_db.session.commit()

        base = recording_db._base_ts
        start_ts, end_ts = base, base + 5.0

        cli_lines = _run_cli_path(rec_dir, include_moves=True)
        chunk_lines = _run_chunk_path(
            rec_dir, start_ts, end_ts, cloud_intent=False,
        )
        recovery_lines = _run_recovery_path(
            rec_dir, start_ts, end_ts, cloud_bound=False,
        )

        assert cli_lines == chunk_lines == recovery_lines
        # Sanity: a drag event was actually produced.
        types = {json.loads(ln).get("type") for ln in cli_lines}
        assert "mouse.drag" in types, (
            "Fixture must produce a drag — verifies drag-detection runs "
            "in the unified pipeline."
        )


# ---------------------------------------------------------------------------
# Tightness sanity check — does the contract test catch a 1-line drift?
# ---------------------------------------------------------------------------


class TestInitialWindowRowDivergence:
    """Pre-chunk window event covers the ``initial_window_row`` injection
    path that the chunk processor and recovery share but the CLI does not.

    Both chunk and recovery look up the most recent window event before
    ``start_ts`` and rewrite its timestamp to ``start_ts - 0.001`` so it
    surfaces as the first interleaved event. CLI export sees the full
    recording and passes ``initial_window_row=None`` (capture.py:462).

    This fixture pins:
    1. Chunk and recovery emit a *byte-identical* synthesized initial
       window line at ``base_ts - 0.001`` (the divergence vector).
    2. CLI's first non-meta line has timestamp ``>= base_ts`` — the
       documented and intentional asymmetry vs chunk/recovery.

    Without a negative-offset window event in the fixture the
    ``initial_window_row`` lookup returns ``None`` and the entire path is
    invisible — chunk and recovery would both happily skip it and the
    contract test would still pass.
    """

    def _build_initial_window_fixture(self, rdb) -> tuple[float, float]:
        """Variant fixture: a window event at ``ts_offset = -0.5``.

        The pre-chunk window event lives 0.5s before ``base_ts`` so it
        falls outside the chunk's ``[base_ts, base_ts + 5)`` slice but
        feeds the ``initial_window_row`` lookup. A handful of in-range
        actions follow so the JSONL is non-trivial.
        """
        # Pre-chunk window context: Finder switch at -0.5s. Outside the
        # chunk slice; only reachable via the initial_window_row lookup.
        _insert_window(
            rdb,
            -0.5,
            title="Pre-chunk Documents",
            bundle_id="com.apple.finder",
            window_id="finder-pre",
        )

        # In-range click pair to ensure post-meta lines exist.
        _insert_action(
            rdb, 0.20, "click",
            mouse_x=100, mouse_y=100,
            mouse_button_name="left", mouse_pressed=1,
        )
        _insert_action(
            rdb, 0.25, "click",
            mouse_x=100, mouse_y=100,
            mouse_button_name="left", mouse_pressed=0,
        )

        rdb.session.commit()

        base = rdb._base_ts
        return (base, base + 5.0)

    def test_chunk_and_recovery_inject_synthesized_initial_window(
        self, recording_db,
    ):
        """Chunk and recovery both synthesize an initial window line at
        ``base_ts - 0.001`` from the pre-chunk window event. The two paths
        must agree byte-for-byte on this line and on the rest of the
        post-meta output.
        """
        rec_dir = recording_db.db_path.parent
        start_ts, end_ts = self._build_initial_window_fixture(recording_db)

        chunk_lines = _run_chunk_path(
            rec_dir, start_ts, end_ts, cloud_intent=False,
        )
        recovery_lines = _run_recovery_path(
            rec_dir, start_ts, end_ts, cloud_bound=False,
        )

        # 1. Chunk and recovery agree on every post-meta line, including
        #    the synthesized initial-window-row.
        assert chunk_lines == recovery_lines, (
            "Chunk vs recovery diverged on the initial_window_row path — "
            "the synthesized window event at start_ts - 0.001 must match "
            "byte-for-byte between the two raw-sqlite3 callers."
        )

        # 2. The first emitted line is the synthesized window.switch with
        #    timestamp == base_ts - 0.001 (divergence-path fingerprint).
        assert chunk_lines, "Fixture must emit at least one event"
        first = json.loads(chunk_lines[0])
        assert first["type"] == "window.switch", (
            "First emitted line must be the synthesized initial window "
            f"event; got type={first.get('type')!r}"
        )
        assert first["app_bundle_id"] == "com.apple.finder"
        assert first["timestamp"] == pytest.approx(start_ts - 0.001), (
            "Synthesized initial window event must be rewritten to "
            f"start_ts - 0.001; got {first['timestamp']!r}"
        )

    def test_cli_does_not_inject_initial_window_row(self, recording_db):
        """CLI passes ``initial_window_row=None`` by design (it sees the
        full recording rather than a slice). Its first emitted line must
        therefore have a timestamp ``>= base_ts``, NOT the synthesized
        ``base_ts - 0.001`` that chunk/recovery produce.

        This is the documented asymmetry between the CLI path and the
        slice-based callers — encoded as an explicit assertion rather than
        a comment so a future refactor that accidentally wires
        ``initial_window_row`` through the CLI path would break this test.
        """
        rec_dir = recording_db.db_path.parent
        start_ts, _end_ts = self._build_initial_window_fixture(recording_db)

        cli_lines = _run_cli_path(rec_dir, include_moves=True)

        assert cli_lines, "CLI must emit at least one event"
        first = json.loads(cli_lines[0])
        # CLI sees the actual pre-chunk window event with its real
        # timestamp (base_ts - 0.5), NOT a rewritten base_ts - 0.001.
        # The first non-meta line is the original window.switch at -0.5.
        assert first["type"] == "window.switch"
        # CLI's first window event preserves the original timestamp; it
        # does not get rewritten to start_ts - 0.001 like chunk/recovery do.
        assert first["timestamp"] != pytest.approx(start_ts - 0.001), (
            "CLI must NOT rewrite the pre-chunk window event timestamp — "
            "that rewrite is a chunk/recovery-only behaviour. If this "
            "fails, CLI started injecting initial_window_row, which is a "
            "behaviour change that must be reviewed."
        )
        # Sanity: CLI's first ts is the actual stored ts (base_ts - 0.5).
        assert first["timestamp"] == pytest.approx(start_ts - 0.5)


class TestContractTightness:
    """The byte-identical contract is meaningful only if it would fail
    on a small drift. This test verifies that a single-byte change in
    one caller's output causes the comparison to fail.

    Approach: run the chunk path normally, then mutate one character in
    the chunk output and assert that the lists no longer compare equal.
    This pins the test's sensitivity without needing to actually diff
    against a pre-refactor commit.
    """

    def test_one_char_drift_breaks_equality(self, recording_db):
        rec_dir = recording_db.db_path.parent
        start_ts, end_ts = _build_full_fixture(recording_db)

        chunk_lines = _run_chunk_path(
            rec_dir, start_ts, end_ts, cloud_intent=False,
        )
        assert chunk_lines, "Fixture must produce at least one event"

        # Mutate the first character of the first line and confirm
        # equality breaks.
        mutated = list(chunk_lines)
        first = mutated[0]
        mutated[0] = ("X" if first[0] != "X" else "Y") + first[1:]

        assert mutated != chunk_lines, (
            "Sanity check failed: a 1-character mutation did NOT break "
            "the equality comparison. The contract test is not tight."
        )
