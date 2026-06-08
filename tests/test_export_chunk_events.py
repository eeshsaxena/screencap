"""Behavioral tests for ``screencap.export.export_chunk_events``.

The function unifies the three historical export call sites (CLI,
chunk processor, recovery) behind a single seam. The tests below
exercise its public contract through real fixture recordings; they do
not depend on the legacy callers.
"""

from __future__ import annotations

import sqlite3


def _insert_action(rdb, ts_offset, name, **kwargs):
    """Insert one action_event at base_ts + ts_offset."""
    from screencap.engine.db import crud

    data = dict(kwargs)
    data["name"] = name
    crud.insert_action_event(rdb.session, rdb.recording, rdb._base_ts + ts_offset, data)


def _insert_window(rdb, ts_offset, *, title="Win", bundle_id="com.app", window_id="w1"):
    from screencap.engine.db import crud

    data = {
        "title": title,
        "app_bundle_id": bundle_id,
        "window_id": window_id,
        "left": 0,
        "top": 0,
        "width": 100,
        "height": 100,
    }
    crud.insert_window_event(rdb.session, rdb.recording, rdb._base_ts + ts_offset, data)


class TestFullRecording:
    """No time range, no filter: yields every processed event from the recording."""

    def test_yields_processed_events(self, recording_db):
        from screencap.engine.events import KeyTypeEvent, MouseClickEvent, WindowSwitchEvent
        from screencap.export import export_chunk_events

        _insert_window(recording_db, 0.05, title="Editor", bundle_id="com.apple.editor")
        _insert_action(
            recording_db, 0.10, "click",
            mouse_x=10, mouse_y=10, mouse_button_name="left", mouse_pressed=1,
        )
        _insert_action(
            recording_db, 0.12, "click",
            mouse_x=10, mouse_y=10, mouse_button_name="left", mouse_pressed=0,
        )
        _insert_action(
            recording_db, 0.30, "press",
            key_char="x", key_name="x", canonical_key_char="x", canonical_key_name="x",
        )
        _insert_action(
            recording_db, 0.32, "release",
            key_char="x", key_name="x", canonical_key_char="x", canonical_key_name="x",
        )
        recording_db.session.commit()

        events = list(export_chunk_events(recording_db.db_path.parent))

        # Exactly: one window switch + one click + one key.type
        assert any(isinstance(e, WindowSwitchEvent) for e in events)
        assert any(isinstance(e, MouseClickEvent) for e in events)
        assert any(isinstance(e, KeyTypeEvent) for e in events)
        # Sorted by timestamp.
        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps)


class TestMaterialized:
    """``materialized`` toggles list vs iterator return."""

    def test_default_returns_iterator(self, recording_db):
        from screencap.export import export_chunk_events

        _insert_action(
            recording_db, 0.10, "click",
            mouse_x=1, mouse_y=1, mouse_button_name="left", mouse_pressed=1,
        )
        recording_db.session.commit()

        result = export_chunk_events(recording_db.db_path.parent)
        assert not isinstance(result, list)
        # Iterator contract: consumable once.
        assert iter(result) is result

    def test_materialized_returns_list(self, recording_db):
        from screencap.export import export_chunk_events

        _insert_action(
            recording_db, 0.10, "click",
            mouse_x=1, mouse_y=1, mouse_button_name="left", mouse_pressed=1,
        )
        _insert_action(
            recording_db, 0.12, "click",
            mouse_x=1, mouse_y=1, mouse_button_name="left", mouse_pressed=0,
        )
        recording_db.session.commit()

        result = export_chunk_events(recording_db.db_path.parent, materialized=True)
        assert isinstance(result, list)
        assert len(result) >= 1


class TestTimeRange:
    """``[start_ts, end_ts)`` selects only rows in the half-open interval."""

    def test_excludes_rows_outside_range(self, recording_db):
        from screencap.export import export_chunk_events

        # Three click pairs at offsets 0.1, 1.5, 3.0
        for offset, mx in [(0.1, 10), (1.5, 20), (3.0, 30)]:
            _insert_action(
                recording_db, offset, "click",
                mouse_x=mx, mouse_y=10, mouse_button_name="left", mouse_pressed=1,
            )
            _insert_action(
                recording_db, offset + 0.02, "click",
                mouse_x=mx, mouse_y=10, mouse_button_name="left", mouse_pressed=0,
            )
        recording_db.session.commit()

        base = recording_db._base_ts
        # Slice covers the middle click only.
        events = list(export_chunk_events(
            recording_db.db_path.parent,
            start_ts=base + 1.0,
            end_ts=base + 2.0,
        ))

        from screencap.engine.events import MouseClickEvent
        click_xs = [e.x for e in events if isinstance(e, MouseClickEvent)]
        assert click_xs == [20]

    def test_end_ts_is_exclusive(self, recording_db):
        from screencap.export import export_chunk_events

        # Click pair exactly at end_ts must be excluded.
        _insert_action(
            recording_db, 0.10, "click",
            mouse_x=99, mouse_y=10, mouse_button_name="left", mouse_pressed=1,
        )
        _insert_action(
            recording_db, 0.12, "click",
            mouse_x=99, mouse_y=10, mouse_button_name="left", mouse_pressed=0,
        )
        recording_db.session.commit()

        base = recording_db._base_ts
        events = list(export_chunk_events(
            recording_db.db_path.parent,
            start_ts=base,
            end_ts=base + 0.10,  # exclusive: row at base+0.10 is out.
        ))
        from screencap.engine.events import MouseClickEvent
        # The click pair (down at base+0.10) is excluded by half-open.
        assert all(not isinstance(e, MouseClickEvent) or e.x != 99 for e in events)


class TestInitialWindowContext:
    """When ``start_ts`` is provided, the last window event before
    ``start_ts`` is prepended with timestamp rewritten to
    ``start_ts - 0.001`` so it sorts before any in-chunk event (R3).
    """

    def test_pre_chunk_window_prepended(self, recording_db):
        from screencap.engine.events import WindowSwitchEvent
        from screencap.export import export_chunk_events

        # Window switch BEFORE the chunk slice.
        _insert_window(
            recording_db, 0.05,
            title="EarlyApp", bundle_id="com.early", window_id="early-1",
        )
        # An action inside the chunk so the slice isn't empty.
        _insert_action(
            recording_db, 1.50, "click",
            mouse_x=1, mouse_y=1, mouse_button_name="left", mouse_pressed=1,
        )
        recording_db.session.commit()

        base = recording_db._base_ts
        events = list(export_chunk_events(
            recording_db.db_path.parent,
            start_ts=base + 1.0,
            end_ts=base + 2.0,
        ))

        ws = [e for e in events if isinstance(e, WindowSwitchEvent)]
        assert len(ws) == 1
        assert ws[0].app_bundle_id == "com.early"
        # Timestamp rewritten to start_ts - 0.001.
        assert abs(ws[0].timestamp - (base + 1.0 - 0.001)) < 1e-9

    def test_no_initial_context_for_full_recording(self, recording_db):
        """Without ``start_ts`` (full recording / CLI path), no rewrite happens —
        every window event keeps its real timestamp.
        """
        from screencap.engine.events import WindowSwitchEvent
        from screencap.export import export_chunk_events

        _insert_window(recording_db, 0.05, title="W1", bundle_id="com.a", window_id="a-1")
        recording_db.session.commit()

        base = recording_db._base_ts
        events = list(export_chunk_events(recording_db.db_path.parent))
        ws = [e for e in events if isinstance(e, WindowSwitchEvent)]
        assert len(ws) == 1
        # Original timestamp preserved (no R3 rewrite).
        assert abs(ws[0].timestamp - (base + 0.05)) < 1e-9


class TestWindowFilter:
    """``window_filter`` is forwarded to the unified pipeline and applied
    to every WindowSwitchEvent."""

    def test_filter_drops_event_when_returns_none(self, recording_db):
        from screencap.engine.events import WindowSwitchEvent
        from screencap.export import export_chunk_events

        _insert_window(recording_db, 0.05, title="A", bundle_id="com.a", window_id="a-1")
        _insert_window(recording_db, 0.10, title="B", bundle_id="com.b", window_id="b-1")
        recording_db.session.commit()

        # Drop everything from com.b; pass com.a through.
        def _filter(ws):
            return None if ws.app_bundle_id == "com.b" else ws

        events = list(export_chunk_events(
            recording_db.db_path.parent, window_filter=_filter,
        ))
        ws = [e for e in events if isinstance(e, WindowSwitchEvent)]
        bundles = {e.app_bundle_id for e in ws}
        assert bundles == {"com.a"}

    def test_filter_can_transform_event(self, recording_db):
        from screencap.engine.events import WindowSwitchEvent
        from screencap.export import export_chunk_events

        _insert_window(
            recording_db, 0.05,
            title="secret", bundle_id="com.app", window_id="w-1",
        )
        recording_db.session.commit()

        def _mask(ws):
            return ws.model_copy(update={"title": "MASKED"})

        events = list(export_chunk_events(
            recording_db.db_path.parent, window_filter=_mask,
        ))
        ws = [e for e in events if isinstance(e, WindowSwitchEvent)]
        assert len(ws) == 1
        assert ws[0].title == "MASKED"


class TestAgnosticRunnerExportAppliesNoCloudFilter:
    """U5: the destination-agnostic ``PipelineStageRunner`` applies NO cloud
    window filter in its own code (R4). Driving the runner with a plain
    ``export_chunk_events`` (``window_filter=None``, the local-only seam)
    yields unfiltered window titles — the runner never re-homes the cloud
    filter into the agnostic export. The cloud filter stays in the caller's
    injected step (chunk_processor), audited by
    ``test_privacy_filter_call_graph.py``.
    """

    def test_runner_export_does_not_filter_window_titles(self, recording_db):
        from screencap.engine.events import WindowSwitchEvent
        from screencap.export import export_chunk_events
        from screencap.exporter import build_export_metadata, write_events_jsonl
        from screencap.pipeline_stages import PipelineStageRunner

        # A window title that a cloud filter WOULD redact, plus an in-chunk
        # action so the slice isn't empty.
        _insert_window(
            recording_db, 0.05,
            title="Secret Customer PII", bundle_id="com.app", window_id="w-1",
        )
        _insert_action(
            recording_db, 0.10, "click",
            mouse_x=1, mouse_y=1, mouse_button_name="left", mouse_pressed=1,
        )
        recording_db.session.commit()

        capture_dir = recording_db.db_path.parent
        events_path = capture_dir / "events_0000.jsonl"

        # The injected export step the runner gets is the AGNOSTIC one: a
        # plain export with window_filter=None (NOT build_cloud_window_filter).
        def agnostic_export(idx, s, e):
            events_iter = export_chunk_events(capture_dir, s, e, window_filter=None)
            meta = build_export_metadata(exclude_moves=False)
            write_events_jsonl(events_path, events_iter, meta)
            return events_path

        runner = PipelineStageRunner(
            capture_dir,
            transcribe=lambda idx: None,
            export_events=agnostic_export,
            manifest=lambda idx, s, e: (capture_dir / "m.json"),
            ledger=None,
        )
        artifacts = runner.run_chunk(0, recording_db._base_ts, recording_db._base_ts + 1.0)

        # The window title reached the export VERBATIM — no cloud masking
        # was applied by the runner.
        text = artifacts.events.read_text()
        assert "Secret Customer PII" in text


class TestDisabledRows:
    """Rows with ``disabled=1`` are excluded (R16)."""

    def test_disabled_action_excluded(self, recording_db):
        from screencap.engine.events import MouseClickEvent
        from screencap.export import export_chunk_events

        _insert_action(
            recording_db, 0.10, "click",
            mouse_x=11, mouse_y=10, mouse_button_name="left", mouse_pressed=1,
        )
        _insert_action(
            recording_db, 0.12, "click",
            mouse_x=11, mouse_y=10, mouse_button_name="left", mouse_pressed=0,
        )
        recording_db.session.commit()

        # Mark the click pair disabled directly in SQL.
        with sqlite3.connect(recording_db.db_path) as conn:
            conn.execute(
                "UPDATE action_event SET disabled=1 WHERE mouse_x=?", (11,),
            )
            conn.commit()

        events = list(export_chunk_events(recording_db.db_path.parent))
        clicks = [e for e in events if isinstance(e, MouseClickEvent)]
        assert clicks == []

    def test_disabled_window_excluded(self, recording_db):
        """Forward-looking: when a future migration adds
        ``window_event.disabled``, the export must filter it out.
        Today's schema lacks the column; we add it inline so the test
        pins the desired behavior for when it lands.
        """
        from screencap.engine.events import WindowSwitchEvent
        from screencap.export import export_chunk_events

        _insert_window(recording_db, 0.05, title="A", bundle_id="com.a", window_id="a-1")
        _insert_window(recording_db, 0.10, title="B", bundle_id="com.b", window_id="b-1")
        recording_db.session.commit()

        with sqlite3.connect(recording_db.db_path) as conn:
            conn.execute("ALTER TABLE window_event ADD COLUMN disabled INTEGER")
            conn.execute(
                "UPDATE window_event SET disabled=1 WHERE app_bundle_id=?",
                ("com.b",),
            )
            conn.commit()

        events = list(export_chunk_events(recording_db.db_path.parent))
        bundles = {e.app_bundle_id for e in events if isinstance(e, WindowSwitchEvent)}
        assert bundles == {"com.a"}


class TestClickThresholds:
    """Per-recording double-click thresholds are loaded from the
    ``recording`` table and reach the merge pipeline. We observe this
    via the merge outcome: under a wide interval the two click pairs
    merge into a double-click; under a narrow interval they stay as
    two separate clicks.
    """

    def _two_clicks_at(self, rdb, *, gap: float):
        """Click pair at offset 0.10 + click pair at offset 0.10+gap."""
        for off in (0.10, 0.10 + gap):
            _insert_action(
                rdb, off, "click",
                mouse_x=50, mouse_y=50,
                mouse_button_name="left", mouse_pressed=1,
            )
            _insert_action(
                rdb, off + 0.01, "click",
                mouse_x=50, mouse_y=50,
                mouse_button_name="left", mouse_pressed=0,
            )
        rdb.session.commit()

    def test_wide_interval_merges_to_double_click(self, recording_db):
        from screencap.engine.events import MouseClickEvent, MouseDoubleClickEvent
        from screencap.export import export_chunk_events

        # Recording fixture defaults: interval=0.5s. Two clicks 0.2s
        # apart fall well inside that window.
        self._two_clicks_at(recording_db, gap=0.2)

        events = list(export_chunk_events(recording_db.db_path.parent))
        doubles = [e for e in events if isinstance(e, MouseDoubleClickEvent)]
        singles = [e for e in events if isinstance(e, MouseClickEvent)]
        assert len(doubles) == 1
        assert len(singles) == 0

    def test_narrow_interval_keeps_clicks_separate(self, recording_db):
        from screencap.engine.events import MouseClickEvent, MouseDoubleClickEvent
        from screencap.export import export_chunk_events

        # Tighten the recording's interval to 0.05s. Two clicks 0.2s
        # apart now exceed the merge window, so they stay separate.
        with sqlite3.connect(recording_db.db_path) as conn:
            conn.execute(
                "UPDATE recording SET double_click_interval_seconds=?",
                (0.05,),
            )
            conn.commit()

        self._two_clicks_at(recording_db, gap=0.2)

        events = list(export_chunk_events(recording_db.db_path.parent))
        doubles = [e for e in events if isinstance(e, MouseDoubleClickEvent)]
        singles = [e for e in events if isinstance(e, MouseClickEvent)]
        assert doubles == []
        assert len(singles) == 2

    def test_null_thresholds_fall_back_to_defaults(self, recording_db):
        """If the recording row has NULL thresholds, the pipeline uses
        the engine defaults (0.5s / 5px). Two clicks 0.2s apart should
        merge.
        """
        from screencap.engine.events import MouseDoubleClickEvent
        from screencap.export import export_chunk_events

        with sqlite3.connect(recording_db.db_path) as conn:
            conn.execute(
                "UPDATE recording SET double_click_interval_seconds=NULL, "
                "double_click_distance_pixels=NULL"
            )
            conn.commit()

        self._two_clicks_at(recording_db, gap=0.2)

        events = list(export_chunk_events(recording_db.db_path.parent))
        doubles = [e for e in events if isinstance(e, MouseDoubleClickEvent)]
        assert len(doubles) == 1


class TestSchemaDrift:
    """Older / partial schemas must not raise."""

    def test_missing_window_event_table(self, recording_db):
        """Older recording.db files lack the ``window_event`` table; the
        action stream alone must still flow through."""
        from screencap.engine.events import MouseClickEvent
        from screencap.export import export_chunk_events

        _insert_action(
            recording_db, 0.10, "click",
            mouse_x=7, mouse_y=7, mouse_button_name="left", mouse_pressed=1,
        )
        _insert_action(
            recording_db, 0.12, "click",
            mouse_x=7, mouse_y=7, mouse_button_name="left", mouse_pressed=0,
        )
        recording_db.session.commit()

        with sqlite3.connect(recording_db.db_path) as conn:
            conn.execute("DROP TABLE window_event")
            conn.commit()

        events = list(export_chunk_events(recording_db.db_path.parent))
        clicks = [e for e in events if isinstance(e, MouseClickEvent)]
        assert len(clicks) == 1

    def test_missing_action_disabled_column(self, recording_db, tmp_path):
        """Recordings predating the ``disabled`` column on action_event
        must export without filtering — exporting must not raise."""
        from screencap.engine.events import MouseClickEvent
        from screencap.export import export_chunk_events

        # Build a fresh DB without the disabled column on action_event.
        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir()
        legacy_db = legacy_dir / "recording.db"
        with sqlite3.connect(legacy_db) as conn:
            conn.executescript(
                """
                CREATE TABLE recording (
                    id INTEGER PRIMARY KEY,
                    timestamp REAL,
                    double_click_interval_seconds REAL,
                    double_click_distance_pixels REAL
                );
                CREATE TABLE action_event (
                    id INTEGER PRIMARY KEY,
                    recording_id INTEGER,
                    timestamp REAL,
                    name TEXT,
                    mouse_x REAL,
                    mouse_y REAL,
                    mouse_button_name TEXT,
                    mouse_pressed INTEGER
                );
                INSERT INTO recording (id, timestamp, double_click_interval_seconds,
                    double_click_distance_pixels) VALUES (1, 1000.0, 0.5, 5.0);
                INSERT INTO action_event (recording_id, timestamp, name, mouse_x, mouse_y,
                    mouse_button_name, mouse_pressed)
                    VALUES (1, 1000.10, 'click', 1, 1, 'left', 1),
                           (1, 1000.12, 'click', 1, 1, 'left', 0);
                """
            )
            conn.commit()

        events = list(export_chunk_events(legacy_dir))
        clicks = [e for e in events if isinstance(e, MouseClickEvent)]
        assert len(clicks) == 1

    def test_missing_click_threshold_columns(self, tmp_path):
        """Recordings predating the ``double_click_*`` columns must fall
        back to engine defaults instead of raising OperationalError —
        regression for the recovery P2 bug."""
        from screencap.engine.events import MouseClickEvent
        from screencap.export import export_chunk_events

        legacy_dir = tmp_path / "legacy_thresholds"
        legacy_dir.mkdir()
        legacy_db = legacy_dir / "recording.db"
        with sqlite3.connect(legacy_db) as conn:
            # ``recording`` row with no threshold columns at all.
            conn.executescript(
                """
                CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL);
                CREATE TABLE action_event (
                    id INTEGER PRIMARY KEY, recording_id INTEGER,
                    timestamp REAL, name TEXT,
                    mouse_x REAL, mouse_y REAL,
                    mouse_button_name TEXT, mouse_pressed INTEGER
                );
                INSERT INTO recording (id, timestamp) VALUES (1, 1000.0);
                INSERT INTO action_event
                    (recording_id, timestamp, name, mouse_x, mouse_y,
                     mouse_button_name, mouse_pressed)
                    VALUES (1, 1000.10, 'click', 1, 1, 'left', 1),
                           (1, 1000.12, 'click', 1, 1, 'left', 0);
                """
            )
            conn.commit()

        events = list(export_chunk_events(legacy_dir))
        clicks = [e for e in events if isinstance(e, MouseClickEvent)]
        assert len(clicks) == 1


class TestFailSoftOperationalError:
    """Live chunk export races the writer. A transient SQLite lock on the
    optional reads (recording-level click thresholds, window_event slice,
    initial window context) must NOT abort the chunk — action rows still
    export with engine-default thresholds and no window context.

    The action_event SELECT intentionally bubbles (a chunk with no action
    rows isn't useful), so its OperationalError still propagates out.
    """

    def _patch_execute_to_raise_on(self, monkeypatch, table_substring):
        """Patch ``open_recording_db`` so its connection's ``execute``
        raises ``OperationalError`` whenever the SQL contains
        ``table_substring``. Other queries pass through unchanged.

        Wrapping the connection in a delegating proxy is necessary
        because ``sqlite3.Connection.execute`` is read-only and can't
        be monkeypatched directly.
        """
        import contextlib

        from screencap.recording_db import OperationalError, open_recording_db

        class _Proxy:
            def __init__(self, real, substring):
                self._real = real
                self._substring = substring

            def execute(self, sql, *a, **kw):
                if self._substring in sql:
                    raise OperationalError("database is locked")
                return self._real.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(self._real, name)

        @contextlib.contextmanager
        def patched(db_path, *args, **kwargs):
            with open_recording_db(db_path, *args, **kwargs) as conn:
                yield _Proxy(conn, table_substring)

        monkeypatch.setattr("screencap.export.open_recording_db", patched)

    def test_window_event_lock_degrades_to_no_context(self, recording_db, monkeypatch):
        from screencap.engine.events import MouseClickEvent, WindowSwitchEvent
        from screencap.export import export_chunk_events

        _insert_window(recording_db, 0.05, title="W", bundle_id="com.a")
        _insert_action(
            recording_db, 0.10, "click",
            mouse_x=1, mouse_y=1, mouse_button_name="left", mouse_pressed=1,
        )
        _insert_action(
            recording_db, 0.12, "click",
            mouse_x=1, mouse_y=1, mouse_button_name="left", mouse_pressed=0,
        )
        recording_db.session.commit()

        self._patch_execute_to_raise_on(monkeypatch, "FROM window_event")

        events = list(export_chunk_events(recording_db.db_path.parent))

        assert any(isinstance(e, MouseClickEvent) for e in events)
        assert not any(isinstance(e, WindowSwitchEvent) for e in events)

    def test_recording_table_lock_falls_back_to_default_thresholds(
        self, recording_db, monkeypatch,
    ):
        from screencap.engine.events import MouseDoubleClickEvent
        from screencap.export import export_chunk_events

        # Tighten thresholds in the DB so a successful read would keep
        # the two clicks separate; the lock-induced fallback to defaults
        # (0.5s) merges them. The merge outcome is the observable proof
        # that defaults — not the row's narrow values — were used.
        with sqlite3.connect(recording_db.db_path) as conn:
            conn.execute(
                "UPDATE recording SET double_click_interval_seconds=?", (0.05,),
            )
            conn.commit()

        for off in (0.10, 0.30):
            _insert_action(
                recording_db, off, "click",
                mouse_x=5, mouse_y=5, mouse_button_name="left", mouse_pressed=1,
            )
            _insert_action(
                recording_db, off + 0.01, "click",
                mouse_x=5, mouse_y=5, mouse_button_name="left", mouse_pressed=0,
            )
        recording_db.session.commit()

        self._patch_execute_to_raise_on(monkeypatch, "FROM recording")

        events = list(export_chunk_events(recording_db.db_path.parent))
        doubles = [e for e in events if isinstance(e, MouseDoubleClickEvent)]
        assert len(doubles) == 1
