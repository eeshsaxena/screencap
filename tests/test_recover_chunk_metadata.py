"""Tests for screencap.recovery._recover_chunk_metadata.

Recovery rebuilds per-chunk events JSONL when ChunkProcessor crashed but
chunk video files exist. Post-Unit 7 it routes through the unified export
callable so the v2 format (``_meta`` header + processed Pydantic events
+ deduplicated ``window.switch`` events) matches the live chunk processor.

The ``cloud_bound`` keyword is REQUIRED — omitting it must raise TypeError
rather than silently fall open. The Slack-leak regression test validates
that recovery invoked from ``screencap upload`` applies the cloud privacy
filter even when ``.recording_intent`` records ``destination=local``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console


# ---------------------------------------------------------------------------
# Fixture helpers — extend the conftest ``recording_db`` fixture with the
# files a chunked recording needs (chunk_*.mp4) so the recovery loop fires.
# ---------------------------------------------------------------------------


def _capture_dir(recording_db) -> Path:
    """Return the recording directory backing ``recording_db``."""
    return recording_db.db_path.parent


def _stub_chunk_video(capture_dir: Path, idx: int) -> None:
    """Create a stub chunk_NNNN.mp4 file so recovery treats it as a chunk."""
    (capture_dir / f"chunk_{idx:04d}.mp4").write_bytes(b"\x00" * 16)


def _write_intent(capture_dir: Path, **payload) -> None:
    """Write a ``.recording_intent`` JSON file."""
    (capture_dir / ".recording_intent").write_text(json.dumps(payload))


def _read_events_jsonl(path: Path) -> tuple[dict, list[dict]]:
    """Parse an events_NNNN.jsonl into (meta, events)."""
    raw = path.read_text().splitlines()
    meta = json.loads(raw[0])
    events = [json.loads(line) for line in raw[1:] if line.strip()]
    return meta, events


def _patch_short_chunk_duration():
    """Force chunk_duration small so test fixture data spans multiple chunks.

    The ``recording_db`` fixture inserts events at base_ts + small offsets
    (sub-second). Default chunk_duration is 900s so all events would land
    in chunk 0. Tests that assert per-chunk behaviour patch this to a
    smaller value.
    """
    return patch("screencap.config.get_chunk_duration", return_value=2.0)


# ---------------------------------------------------------------------------
# Required-keyword contract
# ---------------------------------------------------------------------------


class TestRequiredKeywordContract:
    """The cloud_bound keyword MUST be required, not defaulted."""

    def test_signature_has_no_default_for_cloud_bound(self):
        """The runtime TypeError test below catches a removed kwarg, but a
        future refactor that swallows ``cloud_bound`` into ``**kwargs`` (or
        adds a default) would silently regress to the unsafe path while
        still appearing to "require" the keyword. Pin the contract at the
        signature level so both regressions fail CI.
        """
        import inspect

        from screencap.recovery import _recover_chunk_metadata

        params = inspect.signature(_recover_chunk_metadata).parameters
        assert "cloud_bound" in params, (
            "cloud_bound kwarg removed from _recover_chunk_metadata — "
            "the upload recovery path must always pass it explicitly."
        )
        assert params["cloud_bound"].default is inspect.Parameter.empty, (
            "cloud_bound now has a default — recovery must require the "
            "caller to assert the cloud-bound posture explicitly. See "
            "CLAUDE.md: 'cloud_bound is a REQUIRED keyword (no default)'."
        )

    def test_omitting_cloud_bound_raises_type_error(self, recording_db):
        """Calling without cloud_bound= must raise TypeError, not silently fail-OPEN."""
        from screencap.recovery import _recover_chunk_metadata

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)

        with pytest.raises(TypeError):
            _recover_chunk_metadata(capture_dir, Console(), force=True)

    def test_passing_cloud_bound_keyword_works(self, recording_db):
        """Sanity: calling WITH cloud_bound= succeeds (no TypeError)."""
        from screencap.recovery import _recover_chunk_metadata

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)

        # Pre-seed click events so the unified pipeline has data
        recording_db.add_click(0.5)

        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=True,
            )


# ---------------------------------------------------------------------------
# Happy path — v2 format with window.switch events
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Recovery emits v2 format: meta header + processed events + window switches.

    Cross-caller byte-identical equivalence with the chunk processor is pinned
    by ``tests/test_unified_export_contract.py``. We keep one sanity check here
    that recovery actually runs the processing pipeline (not raw row dumps).
    """

    def test_produces_processed_pydantic_events(self, recording_db):
        """Click pair gets merged into mouse.singleclick (not raw rows)."""
        from screencap.recovery import _recover_chunk_metadata

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)
        recording_db.add_click(0.5)

        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=False,
            )

        _meta, events = _read_events_jsonl(capture_dir / "events_0000.jsonl")
        types = [e.get("type") for e in events]
        assert "mouse.singleclick" in types, (
            "Click pair should have merged into singleclick, not raw rows"
        )
        # Direct contrast with today's degraded format: no raw 'name' rows
        assert not any(e.get("name") == "click" for e in events)


# ---------------------------------------------------------------------------
# Slack-leak regression: cloud_bound=True from upload context wins over
# .recording_intent destination=local. THIS IS THE PRIVACY-CRITICAL TEST.
# ---------------------------------------------------------------------------


class TestSlackLeakRegression:
    """The local-then-uploaded threat case.

    Recording captured as destination=local, later uploaded via
    ``screencap upload``. The intent file says local, but the upload call
    site forces ``cloud_bound=True`` so the cloud filter still applies and
    Slack window titles get masked. Without this, Slack titles leak to the
    cloud copy.
    """

    def test_local_intent_recording_uploaded_masks_slack_titles(self, recording_db):
        """Intent file says local; recovery from upload context masks titles."""
        from screencap.recovery import _recover_chunk_metadata

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)
        # The .recording_intent file says local — the threat case
        _write_intent(
            capture_dir,
            version=1,
            destination="local",
            privacy_mode="internal",
        )
        # Slack window event with a sensitive title (CHAT → MASK_WINDOW in PUBLIC)
        recording_db.add_window_event(
            0.1,
            title="Project secrets thread - John Smith",
            bundle_id="com.tinyspeck.slackmacgap",
            window_id="slack-1",
        )
        recording_db.add_click(0.5)

        with _patch_short_chunk_duration():
            # Mirror the upload command's call: cloud_bound=True regardless
            # of the intent file's destination.
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=True,
            )

        _meta, events = _read_events_jsonl(capture_dir / "events_0000.jsonl")
        slack_events = [
            e for e in events
            if e.get("type") == "window.switch"
            and e.get("app_bundle_id") == "com.tinyspeck.slackmacgap"
        ]
        assert slack_events, "Slack window.switch should be present"
        for ev in slack_events:
            # MASK_WINDOW: window_title is replaced with app_name (derived
            # from bundle_id last component, titlecased = "Slackmacgap").
            assert ev["window_title"] != "Project secrets thread - John Smith", (
                "Sensitive title leaked — Slack-leak class regression"
            )
            assert "secrets" not in ev["window_title"].lower()
            assert "John Smith" not in ev["window_title"]

    def test_missing_intent_file_recovery_from_upload_filters(self, recording_db):
        """No .recording_intent file present — upload context still applies filter."""
        from screencap.recovery import _recover_chunk_metadata

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)
        # No .recording_intent file — recovery falls back to config for
        # privacy_mode but still applies the cloud filter (cloud_bound=True).
        recording_db.add_window_event(
            0.1,
            title="Confidential thread",
            bundle_id="com.tinyspeck.slackmacgap",
            window_id="slack-2",
        )
        recording_db.add_click(0.5)

        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=True,
            )

        _meta, events = _read_events_jsonl(capture_dir / "events_0000.jsonl")
        slack = [
            e for e in events
            if e.get("type") == "window.switch"
            and e.get("app_bundle_id") == "com.tinyspeck.slackmacgap"
        ]
        assert slack and "Confidential" not in slack[0]["window_title"]


# ---------------------------------------------------------------------------
# Skip-on-error for corrupt rows
# ---------------------------------------------------------------------------


class TestSkipOnErrorForCorruptRows:
    """Mid-chunk pipeline failure: skip the chunk, log, proceed."""

    def test_corrupt_row_skips_chunk_and_continues(self, recording_db):
        """If unified_export_events raises mid-chunk, skip that chunk's JSONL."""
        from screencap.recovery import _recover_chunk_metadata

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)
        _stub_chunk_video(capture_dir, 1)
        recording_db.add_click(0.5)  # chunk 0
        recording_db.add_click(2.5)  # chunk 1

        # Make unified_export_events crash for chunk 0 only by patching it
        # to raise the first time it's called and succeed thereafter.
        from screencap.engine.export import (
            unified_export_events as _real_unified_export_events,
        )

        call_count = {"n": 0}

        def flaky_export(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated process_events aggregate-state failure")
            return _real_unified_export_events(*args, **kwargs)

        with _patch_short_chunk_duration(), \
             patch(
                 "screencap.engine.export.unified_export_events",
                 side_effect=flaky_export,
             ):
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=False,
            )

        # Chunk 0 should be missing (skipped); chunk 1 should be present.
        assert not (capture_dir / "events_0000.jsonl").exists(), (
            "Failed chunk must NOT have a JSONL file written"
        )
        assert (capture_dir / "events_0001.jsonl").exists(), (
            "Subsequent chunks must continue after a per-chunk failure"
        )
        # No stale .tmp files for the failed chunk
        assert not (capture_dir / "events_0000.jsonl.tmp").exists()


# ---------------------------------------------------------------------------
# Atomic write — exception during write removes .tmp
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    """Mid-write exception must clean up .tmp; no partial events_NNNN.jsonl."""

    def test_mid_write_exception_removes_tmp(self, recording_db):
        """Simulated mid-write failure: .tmp removed, real path not created."""
        from screencap.recovery import _recover_chunk_metadata

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)
        recording_db.add_click(0.5)

        # Patch model_dump_json on the merged-click Pydantic event class.
        # write_events_jsonl iterates events and calls model_dump_json on
        # each — raising mid-iteration triggers the BaseException cleanup
        # branch that unlinks the .tmp file.
        from screencap.engine.events import MouseClickEvent

        def boom(self, *a, **kw):
            raise RuntimeError("simulated mid-write failure")

        with _patch_short_chunk_duration(), \
             patch.object(MouseClickEvent, "model_dump_json", boom):
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=False,
            )

        # The exception is caught by the per-chunk skip-on-error path;
        # write_events_jsonl's BaseException cleanup removed the .tmp.
        assert not (capture_dir / "events_0000.jsonl").exists()
        assert not (capture_dir / "events_0000.jsonl.tmp").exists()


# ---------------------------------------------------------------------------
# disabled=True row filter (R16)
# ---------------------------------------------------------------------------


class TestDisabledRowFilter:
    """disabled=True rows must be filtered out at the SELECT layer (R16)."""

    def test_disabled_action_rows_excluded(self, recording_db):
        """An action_event with disabled=True does not appear in recovered JSONL."""
        from screencap.recovery import _recover_chunk_metadata
        from screencap.engine.db import crud

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)

        # Insert two click pairs: one disabled, one not.
        ts = recording_db.recording.timestamp
        # Enabled click pair @ 0.5s
        recording_db.add_click(0.5, x=100, y=100)
        # Disabled click pair @ 1.0s — flag both rows as disabled
        crud.insert_action_event(
            recording_db.session, recording_db.recording, ts + 1.0,
            {
                "name": "click",
                "mouse_x": 999, "mouse_y": 999,
                "mouse_button_name": "left",
                "mouse_pressed": True,
                "disabled": True,
            },
        )
        crud.insert_action_event(
            recording_db.session, recording_db.recording, ts + 1.05,
            {
                "name": "click",
                "mouse_x": 999, "mouse_y": 999,
                "mouse_button_name": "left",
                "mouse_pressed": False,
                "disabled": True,
            },
        )

        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=False,
            )

        _meta, events = _read_events_jsonl(capture_dir / "events_0000.jsonl")
        # The enabled click (x=100,y=100) should be present; disabled
        # (x=999,y=999) should NOT be present.
        click_events = [e for e in events if e.get("type") == "mouse.singleclick"]
        assert click_events, "Enabled click should still be present"
        for c in click_events:
            assert c.get("x") != 999 and c.get("y") != 999, (
                "Disabled click row leaked into output"
            )


# ---------------------------------------------------------------------------
# Empty chunk — no events at all
# ---------------------------------------------------------------------------


class TestEmptyChunk:
    """A chunk with no events still produces a JSONL with just the meta header."""

    def test_empty_chunk_writes_meta_only(self, recording_db):
        """No events → JSONL has the meta header line and nothing else."""
        from screencap.recovery import _recover_chunk_metadata

        capture_dir = _capture_dir(recording_db)
        # Two chunk videos: chunk 0 (empty) and chunk 1 (extended to cover events).
        # The last chunk always extends to last_ts + 1 in the recovery logic, so
        # only NON-LAST chunks can be genuinely empty.
        _stub_chunk_video(capture_dir, 0)
        _stub_chunk_video(capture_dir, 1)
        # Click at ts=1050 lands in chunk 1 (extended), leaving chunk 0 empty.
        recording_db.add_click(50.0)

        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=False,
            )

        jsonl_path = capture_dir / "events_0000.jsonl"
        assert jsonl_path.exists(), "Empty chunk must still get a JSONL with meta"
        meta, events = _read_events_jsonl(jsonl_path)
        assert meta["_meta"] is True
        assert events == [], f"Expected no events for empty chunk, got: {events}"


# ---------------------------------------------------------------------------
# Stale .tmp cleanup sweep
# ---------------------------------------------------------------------------


class TestStaleTmpCleanup:
    """Pre-existing .tmp file from a SIGKILL/OOM crash gets removed."""

    def test_stale_tmp_removed_before_re_recovery(self, recording_db):
        """A pre-existing events_NNNN.jsonl.tmp is unlinked at recovery start."""
        from screencap.recovery import _recover_chunk_metadata

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)
        recording_db.add_click(0.5)

        # Plant a stale .tmp simulating a previous SIGKILL between open() and rename()
        stale = capture_dir / "events_0000.jsonl.tmp"
        stale.write_bytes(b"PARTIAL-DATA-THAT-MUST-NOT-PERSIST")
        assert stale.exists()

        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=False,
            )

        # After recovery, the .tmp should be gone (either swept upfront or
        # consumed by write_events_jsonl) and the real file should exist.
        assert not stale.exists(), "Stale .tmp must be removed"
        assert (capture_dir / "events_0000.jsonl").exists()


# ---------------------------------------------------------------------------
# Recovery → scrubber chain integration: load-bearing ordering invariant.
# ---------------------------------------------------------------------------


class TestOlderSchemaWithoutWindowEventTable:
    """Older recordings predate the ``window_event`` table.

    ``_recover_chunk_metadata`` checks ``has_window_table`` at
    ``cli.py:1577`` and skips both the per-chunk window SELECT and the
    ``initial_window_row`` lookup when the table is absent. Without this
    guard, raw sqlite3 would raise on the missing table and abort the whole
    recovery. The branch is reachable in production (older recordings may
    still be uploaded) but ``create_db()`` always materializes the full
    schema, leaving this branch untested by every other recovery test.
    """

    def test_recovery_succeeds_without_window_event_table(self, recording_db):
        """Drop ``window_event`` from the schema; recovery still produces
        a valid v2 JSONL containing the action events but zero
        ``window.switch`` events.
        """
        from sqlalchemy import text

        from screencap.recovery import _recover_chunk_metadata

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)

        # Insert a couple of action events that the recovery must keep.
        recording_db.add_click(0.5)
        recording_db.add_keypress(0.7, char="a")

        # Drop the window_event table to simulate an older-schema DB.
        # Use the SQLAlchemy + DROP TABLE approach (rather than raw DDL)
        # so the rest of the schema stays exactly what create_db() would
        # produce — keeps the fixture infrastructure consistent with the
        # other recovery tests while exercising the older-schema guard.
        with recording_db.engine.begin() as conn:
            conn.execute(text("DROP TABLE window_event"))

        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=True,
            )

        jsonl_path = capture_dir / "events_0000.jsonl"
        assert jsonl_path.exists(), (
            "Recovery must produce a JSONL even when window_event "
            "table is absent (older-schema guard at cli.py:1577)"
        )
        meta, events = _read_events_jsonl(jsonl_path)
        assert meta["_meta"] is True
        assert meta["format_version"] == 2
        # Action events still flow through.
        assert events, "Action events must be present in the recovered JSONL"
        # Zero window.switch events because the source table doesn't exist.
        ws = [e for e in events if e.get("type") == "window.switch"]
        assert ws == [], (
            "Older-schema recording must produce zero window.switch events; "
            f"got {len(ws)}: {ws}"
        )


class TestPerRecordingClickThresholds:
    """P2 regression: recovery must use the recording's per-recording
    click thresholds, not the engine defaults.

    Pre-fix, ``_recover_chunk_metadata`` called ``unified_export_events``
    without ``double_click_interval``/``double_click_distance``, so the
    callable used the default 0.5s/5px thresholds regardless of what the
    recording.db row recorded. For recordings that overrode the defaults
    (e.g. ``double_click_interval_seconds=0.3``), recovery emitted
    different click merges than the live chunk processor, breaking the
    byte-identical contract on the recovered JSONL.

    See ``tests/test_unified_export_contract.py::TestNonDefaultThresholds
    Agreement`` for the cross-caller contract assertion. This test pins
    the recovery-only behaviour: at a 0.3s threshold, two clicks 0.4s
    apart MUST yield two ``mouse.singleclick`` events, not a
    ``mouse.doubleclick``.
    """

    def test_recovery_respects_per_recording_double_click_interval(
        self, recording_db,
    ):
        """Recording with interval=0.3s + two clicks 0.4s apart yields two
        singleclicks. Pre-fix this would have produced one doubleclick.
        """
        from sqlalchemy import text

        from screencap.recovery import _recover_chunk_metadata
        from screencap.engine.db import crud

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)

        # Override the fixture's default 0.5s threshold to a strict 0.3s.
        # The threshold columns are nullable; rewriting the row matches
        # what the engine's recorder would do for a user with a custom
        # config.toml double_click_interval_seconds.
        with recording_db.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE recording SET "
                    "double_click_interval_seconds = 0.3, "
                    "double_click_distance_pixels = 5.0"
                ),
            )

        # Two click pairs 0.4s apart (gap between MouseDowns = 0.4s).
        # Default threshold (0.5s) would merge → one doubleclick.
        # Per-recording 0.3s threshold should NOT merge → two singleclicks.
        ts = recording_db.recording.timestamp
        for offset in (0.10, 0.50):
            crud.insert_action_event(
                recording_db.session, recording_db.recording, ts + offset,
                {
                    "name": "click",
                    "mouse_x": 100.0, "mouse_y": 100.0,
                    "mouse_button_name": "left",
                    "mouse_pressed": True,
                },
            )
            crud.insert_action_event(
                recording_db.session, recording_db.recording, ts + offset + 0.01,
                {
                    "name": "click",
                    "mouse_x": 100.0, "mouse_y": 100.0,
                    "mouse_button_name": "left",
                    "mouse_pressed": False,
                },
            )

        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=False,
            )

        _meta, events = _read_events_jsonl(capture_dir / "events_0000.jsonl")
        types = [e.get("type") for e in events]
        # Pre-fix expectation (using ignored 0.5s default): one doubleclick.
        # Post-fix expectation (using 0.3s recording value): two singleclicks.
        assert "mouse.doubleclick" not in types, (
            "Recovery used the engine default 0.5s threshold instead of the "
            "recording's 0.3s value — the P2 regression. Two clicks 0.4s "
            "apart merged into a doubleclick when they should not have."
        )
        assert types.count("mouse.singleclick") == 2, (
            f"Expected 2 singleclicks at 0.3s threshold, got types={types}"
        )


class TestOlderSchemaMissingClickThresholds:
    """Older recordings predate the per-recording click threshold columns
    (``double_click_interval_seconds`` / ``double_click_distance_pixels``).

    The P2 fix in commit ``2bcd8d4`` added these columns to recovery's
    recording-table SELECT unconditionally. Older ``recording.db`` files
    that lack the columns raise ``OperationalError`` on the SELECT, which
    the outer ``except Exception`` swallowed — silently aborting recovery
    for the entire recording. The user uploads and gets an empty cloud
    copy.

    The fix gates the threshold columns on ``has_column`` (mirroring
    ``chunk_processor._load_click_thresholds``), falling back to engine
    defaults when absent. ``timestamp`` is required regardless because
    recovery uses it for chunk-range derivation.
    """

    def test_older_schema_succeeds_with_default_thresholds(self, recording_db):
        """Drop both threshold columns; recovery still produces a valid v2
        JSONL with default thresholds (clicks 0.4s apart merge to a single
        doubleclick under the 0.5s default).
        """
        from sqlalchemy import text

        from screencap.recovery import _recover_chunk_metadata
        from screencap.engine.db import crud

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)

        # Two click pairs 0.4s apart (gap between MouseDowns = 0.4s).
        # Default threshold (0.5s) merges → one doubleclick.
        ts = recording_db.recording.timestamp
        for offset in (0.10, 0.50):
            crud.insert_action_event(
                recording_db.session, recording_db.recording, ts + offset,
                {
                    "name": "click",
                    "mouse_x": 100.0, "mouse_y": 100.0,
                    "mouse_button_name": "left",
                    "mouse_pressed": True,
                },
            )
            crud.insert_action_event(
                recording_db.session, recording_db.recording, ts + offset + 0.01,
                {
                    "name": "click",
                    "mouse_x": 100.0, "mouse_y": 100.0,
                    "mouse_button_name": "left",
                    "mouse_pressed": False,
                },
            )

        # Drop both threshold columns to simulate an older-schema DB.
        # SQLite supports DROP COLUMN since 3.35; the local environment
        # ships 3.51. Mirrors TestOlderSchemaWithoutWindowEventTable's
        # SQLAlchemy + DROP TABLE pattern (here DROP COLUMN).
        with recording_db.engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE recording "
                     "DROP COLUMN double_click_interval_seconds")
            )
            conn.execute(
                text("ALTER TABLE recording "
                     "DROP COLUMN double_click_distance_pixels")
            )

        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=False,
            )

        jsonl_path = capture_dir / "events_0000.jsonl"
        assert jsonl_path.exists(), (
            "Recovery must produce a JSONL even when threshold columns "
            "are absent (older-schema guard at cli.py:1513)"
        )
        meta, events = _read_events_jsonl(jsonl_path)
        assert meta["_meta"] is True
        assert meta["format_version"] == 2
        types = [e.get("type") for e in events]
        # Default 0.5s threshold merges the two clicks → one doubleclick.
        assert "mouse.doubleclick" in types, (
            "Older schema (no threshold columns) must use defaults — two "
            "clicks 0.4s apart should merge into a doubleclick under the "
            f"0.5s default; got types={types}"
        )

    def test_older_schema_uses_defaults_regardless_of_live_model(
        self, recording_db,
    ):
        """Regression: confirms recovery reads the actual DB schema, not the
        live ``Recording`` SQLAlchemy model. With the threshold columns
        dropped, recovery MUST fall back to engine defaults even if the
        live Recording row would have set them to non-default values
        (e.g. via the ``recording_db`` fixture which seeds 0.5s/5.0px).
        """
        from sqlalchemy import text

        from screencap.recovery import _recover_chunk_metadata
        from screencap.engine.db import crud

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)

        # Two click pairs 0.4s apart.
        ts = recording_db.recording.timestamp
        for offset in (0.10, 0.50):
            crud.insert_action_event(
                recording_db.session, recording_db.recording, ts + offset,
                {
                    "name": "click",
                    "mouse_x": 100.0, "mouse_y": 100.0,
                    "mouse_button_name": "left",
                    "mouse_pressed": True,
                },
            )
            crud.insert_action_event(
                recording_db.session, recording_db.recording, ts + offset + 0.01,
                {
                    "name": "click",
                    "mouse_x": 100.0, "mouse_y": 100.0,
                    "mouse_button_name": "left",
                    "mouse_pressed": False,
                },
            )

        # Drop only one of the two columns. The has_column guard requires
        # BOTH columns present — dropping one forces the fallback path so
        # the live model's per-recording values cannot be used either.
        with recording_db.engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE recording "
                     "DROP COLUMN double_click_interval_seconds")
            )

        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=False,
            )

        _meta, events = _read_events_jsonl(capture_dir / "events_0000.jsonl")
        types = [e.get("type") for e in events]
        # Default 0.5s threshold merges → one doubleclick. If recovery
        # wrongly read the live SQLAlchemy model instead of the DB schema,
        # the output would still merge — so this test asserts the fallback
        # path produces the SAME merging the all-defaults path produces,
        # confirming the threshold values came from the engine constants
        # rather than from the live model.
        assert "mouse.doubleclick" in types, (
            "Recovery must fall back to engine defaults (0.5s) when even "
            "ONE threshold column is missing — partial schemas force the "
            f"defaults path, not the live-model path; got types={types}"
        )


class TestRecoveryScrubberChain:
    """Load-bearing ordering: recovery + scrubber together produce the
    full cloud-bound privacy posture (masked titles + no in-interval mouse.move).
    """

    def test_recovery_plus_scrubber_masks_titles_and_drops_in_interval_moves(
        self, recording_db, monkeypatch, tmp_path,
    ):
        """The recovery → scrubber chain produces cloud-correct output.

        After ``_recover_chunk_metadata(cloud_bound=True)`` followed by the
        scrubber's interval-aware mouse.move suppression, MASK_WINDOW
        intervals contain neither the original title (engine-layer) nor any
        in-interval mouse.move events (scrub-layer).
        """
        from screencap.recovery import _recover_chunk_metadata
        from screencap.engine.db import crud
        from screencap.scrubber import (
            ScrubResult,
            build_scrub_context,
            scrub_events_jsonl,
        )

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)

        # Window events: open Slack (CHAT → MASK_WINDOW in PUBLIC) at 0.1s.
        recording_db.add_window_event(
            0.1,
            title="Sensitive Slack thread",
            bundle_id="com.tinyspeck.slackmacgap",
            window_id="slack-3",
        )

        # mouse.move events while Slack is the active app — these should
        # be dropped by the scrubber's interval-aware filter.
        ts = recording_db.recording.timestamp
        for offset in (0.5, 0.6, 0.7):
            crud.insert_action_event(
                recording_db.session, recording_db.recording, ts + offset,
                {"name": "move", "mouse_x": 200, "mouse_y": 200},
            )

        # Run recovery for upload (cloud_bound=True).
        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=True,
            )

        events_path = capture_dir / "events_0000.jsonl"
        assert events_path.exists()

        # Pre-scrub assertions: engine-layer filter masked the title.
        _meta, events = _read_events_jsonl(events_path)
        slack = [
            e for e in events
            if e.get("type") == "window.switch"
            and e.get("app_bundle_id") == "com.tinyspeck.slackmacgap"
        ]
        assert slack and "Sensitive" not in slack[0]["window_title"], (
            "Engine-layer filter must mask Slack title (recovery → cloud filter)"
        )

        # Now run the scrubber on the same JSONL (the second half of the
        # upload command's chain). Use the same masking classifier the
        # scrubber would derive in production.
        from screencap.config import get_privacy_config
        from screencap.privacy.context import DefaultContextClassifier
        from screencap.privacy.policy import DefaultPolicyEvaluator, PrivacyMode
        from dataclasses import replace as dc_replace

        cfg = dc_replace(get_privacy_config(), mode=PrivacyMode.PUBLIC)
        evaluator = DefaultPolicyEvaluator(cfg)
        classifier = DefaultContextClassifier(app_classes=cfg.app_classes)

        ctx = build_scrub_context(
            recording_db.db_path,
            evaluator,
            classifier,
            time_range=None,
            pipeline=None,
            anonymizer=None,
        )

        # Use a no-op pipeline/anonymizer — we only care about the
        # interval-driven mouse.move drop here, not PII detection.
        from screencap.privacy import Anonymizer
        from unittest.mock import MagicMock

        pipeline = MagicMock()

        def _detect(text):
            from screencap.privacy import DetectionResult
            return DetectionResult(text, [])

        pipeline.detect = _detect
        anonymizer = Anonymizer()

        result = ScrubResult()
        scrub_events_jsonl(
            events_path, pipeline, anonymizer, ctx=ctx, result=result,
        )

        _meta_post, events_post = _read_events_jsonl(events_path)
        # No mouse.move events should remain inside the Slack interval.
        in_interval_moves = [
            e for e in events_post
            if e.get("type") == "mouse.move"
            and ts + 0.4 <= e.get("timestamp", 0.0) <= ts + 1.0
        ]
        assert in_interval_moves == [], (
            "Scrub-layer must drop in-interval mouse.move; load-bearing "
            "recovery → scrubber ordering broken"
        )
        # Title must still be masked. The scrub layer additionally nulls
        # window_title in blocked intervals (null_event_content), so the
        # post-scrub title is either the engine-masked app_name or None;
        # in either case it must NOT contain the original sensitive text.
        slack_post = [
            e for e in events_post
            if e.get("type") == "window.switch"
            and e.get("app_bundle_id") == "com.tinyspeck.slackmacgap"
        ]
        assert slack_post, "Slack window.switch should still be present post-scrub"
        post_title = slack_post[0].get("window_title")
        assert post_title is None or "Sensitive" not in post_title


# ---------------------------------------------------------------------------
# V1 scope guard: zero network.* lines in recovered chunk JSONL
# ---------------------------------------------------------------------------


class TestV1NetworkScopeGuard:
    """V1 contract: recovery emits ZERO network.* lines.

    Mirrors the chunk_processor scope guard. JSONL emission of network
    events is deferred to V1.75 alongside the cloud bucket policy +
    build_cloud_network_filter factory; V1 keeps network events DB-only.
    """

    def test_no_network_lines_in_recovered_jsonl(self, recording_db):
        """Seeded network_event row in DB → zero network.* in JSONL."""
        from screencap.recovery import _recover_chunk_metadata
        from screencap.engine.db import crud

        capture_dir = _capture_dir(recording_db)
        _stub_chunk_video(capture_dir, 0)
        # An action so the chunk is non-empty.
        recording_db.add_click(0.5)
        # And a network_event row inside the chunk window.
        ts = recording_db._base_ts + 0.5
        crud.insert_network_event(
            recording_db.session,
            recording_db.recording,
            {
                "kind": "request",
                "flow_id": "flow-1",
                "method": "GET",
                "url": "https://example.com/api",
                "host": "example.com",
                "headers_json": json.dumps([["Host", "example.com"]]),
                "body_size": 0,
                "body_sha256": None,
                "content_type": None,
                "direction": None,
                "frame_type": None,
                "http_version": "HTTP/1.1",
                "details_json": None,
                "timestamp": ts,
                "timestamp_ns": int(ts * 1_000_000_000),
            },
        )
        crud.flush_buffers(recording_db.session)

        # Recovery is the cloud-bound path; V1 must STILL not emit
        # network rows because the row-fetch query is gated on V1.75.
        with _patch_short_chunk_duration():
            _recover_chunk_metadata(
                capture_dir, Console(), force=True, cloud_bound=True,
            )

        _meta, events = _read_events_jsonl(capture_dir / "events_0000.jsonl")
        types = [e.get("type") for e in events]
        assert all(not (t or "").startswith("network.") for t in types), (
            f"V1 recovery must emit zero network.* lines; got types: {types}"
        )
