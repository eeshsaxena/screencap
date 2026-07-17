"""Tests for the v0 retroactive scrub worker.

Covers the four scenarios pinned by the v0 scope:
    1. App-level scrub deletes only the target bundle's rows.
    2. Domain-level scrub deletes only the matching browser_url rows.
    3. Recursive action_event delete walks parent_id chains.
    4. Empty target completes successfully with all-zero counts.

Tests seed a tmp ``recording.db`` via :func:`screencap.engine.db.create_db`
so the schema matches production exactly, then drive ``ScrubWorker.handle``
synchronously without spinning up the thread loop.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from screencap.engine.db import create_db
from screencap.enforcement.scrub_worker import ScrubWorker

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_recording_db(tmp_path: Path) -> Path:
    """Create an empty per-capture recording.db with the production schema."""
    db_path = tmp_path / "recording.db"
    create_db(str(db_path))
    return db_path


def _seed_recording(conn: sqlite3.Connection) -> int:
    """Insert one Recording row and return its id."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO recording (timestamp, monitor_width, monitor_height, "
        "pixel_ratio, platform) VALUES (?, ?, ?, ?, ?)",
        (1000.0, 1920, 1080, 2.0, "darwin"),
    )
    return cur.lastrowid


def _insert_window_event(
    conn: sqlite3.Connection,
    *,
    recording_id: int,
    timestamp: float,
    bundle_id: str,
    app_name: str | None = None,
    browser_url: str | None = None,
    title: str = "",
) -> None:
    conn.execute(
        "INSERT INTO window_event "
        "(recording_id, timestamp, recording_timestamp, app_bundle_id, "
        " app_name, browser_url, title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (recording_id, timestamp, timestamp, bundle_id, app_name,
         browser_url, title),
    )


def _insert_action_event(
    conn: sqlite3.Connection,
    *,
    recording_id: int,
    timestamp: float,
    window_event_timestamp: float,
    name: str = "click",
    parent_id: int | None = None,
) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO action_event "
        "(recording_id, timestamp, recording_timestamp, "
        " window_event_timestamp, name, parent_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (recording_id, timestamp, timestamp, window_event_timestamp,
         name, parent_id),
    )
    return cur.lastrowid


def _insert_screenshot(
    conn: sqlite3.Connection,
    *,
    recording_id: int,
    timestamp: float,
    png_data: bytes = b"\x89PNG_FAKE",
) -> None:
    conn.execute(
        "INSERT INTO screenshot "
        "(recording_id, timestamp, recording_timestamp, png_data) "
        "VALUES (?, ?, ?, ?)",
        (recording_id, timestamp, timestamp, png_data),
    )


def _insert_window_geometry(
    conn: sqlite3.Connection,
    *,
    recording_id: int,
    screenshot_timestamp: float,
    window_list_json: str = "[]",
) -> None:
    conn.execute(
        "INSERT INTO window_geometry "
        "(recording_id, screenshot_timestamp, recording_timestamp, "
        " window_list_json) "
        "VALUES (?, ?, ?, ?)",
        (recording_id, screenshot_timestamp, screenshot_timestamp,
         window_list_json),
    )


def _count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql).fetchone()[0]


def _read_log(capture_dir: Path) -> list[dict]:
    """Return all non-meta entries from .menubar_disable_log.jsonl."""
    log_path = capture_dir / ".menubar_disable_log.jsonl"
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("_meta"):
            continue
        entries.append(obj)
    return entries


def _make_worker(tmp_path: Path) -> tuple[ScrubWorker, Path]:
    """Return (worker, db_path). Tests drive ``worker._handle`` directly
    so the queue is unused.
    """
    db_path = _make_recording_db(tmp_path)
    worker = ScrubWorker(
        disable_q=None,
        recording_db_path=db_path,
        capture_dir=tmp_path,
    )
    return worker, db_path


# ---------------------------------------------------------------------------
# Test 1 — App-level scrub
# ---------------------------------------------------------------------------


def test_app_scrub_deletes_only_target_bundle(tmp_path: Path) -> None:
    worker, db_path = _make_worker(tmp_path)
    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)

    # 5 Spotify window events with matching action_events + screenshots,
    # 5 Safari ones too. Spotify should be fully scrubbed.
    spotify_ts = [100.0, 101.0, 102.0, 103.0, 104.0]
    safari_ts = [200.0, 201.0, 202.0, 203.0, 204.0]
    for ts in spotify_ts:
        _insert_window_event(
            conn, recording_id=rec_id, timestamp=ts,
            bundle_id="com.spotify.client", app_name="Spotify",
        )
        _insert_action_event(
            conn, recording_id=rec_id, timestamp=ts + 0.1,
            window_event_timestamp=ts,
        )
        _insert_screenshot(conn, recording_id=rec_id, timestamp=ts)
        _insert_window_geometry(
            conn, recording_id=rec_id, screenshot_timestamp=ts,
        )
    for ts in safari_ts:
        _insert_window_event(
            conn, recording_id=rec_id, timestamp=ts,
            bundle_id="com.apple.Safari", app_name="Safari",
        )
        _insert_action_event(
            conn, recording_id=rec_id, timestamp=ts + 0.1,
            window_event_timestamp=ts,
        )
        _insert_screenshot(conn, recording_id=rec_id, timestamp=ts)
        _insert_window_geometry(
            conn, recording_id=rec_id, screenshot_timestamp=ts,
        )
    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "app",
        "bundle_id": "com.spotify.client",
        "app_name": "Spotify",
        "root_domain": None,
        "ts_unix": 1000.0,
        "source": "menubar",
    })

    assert entry["scrub_status"] == "completed"
    assert entry["scrub_result"]["matched_timestamps"] == 5
    assert entry["scrub_result"]["window_events"] == 5
    assert entry["scrub_result"]["action_events"] == 5
    assert entry["scrub_result"]["screenshots"] == 5
    assert entry["scrub_result"]["window_geometries"] == 5

    conn = sqlite3.connect(str(db_path))
    try:
        # Spotify gone everywhere
        assert _count(
            conn, "window_event", "app_bundle_id = 'com.spotify.client'"
        ) == 0
        assert _count(
            conn, "action_event",
            "window_event_timestamp BETWEEN 100 AND 105",
        ) == 0
        assert _count(
            conn, "screenshot", "timestamp BETWEEN 100 AND 105"
        ) == 0
        assert _count(
            conn, "window_geometry",
            "screenshot_timestamp BETWEEN 100 AND 105",
        ) == 0
        # Safari fully intact
        assert _count(
            conn, "window_event", "app_bundle_id = 'com.apple.Safari'"
        ) == 5
        assert _count(
            conn, "action_event",
            "window_event_timestamp BETWEEN 200 AND 205",
        ) == 5
        assert _count(
            conn, "screenshot", "timestamp BETWEEN 200 AND 205"
        ) == 5
        assert _count(
            conn, "window_geometry",
            "screenshot_timestamp BETWEEN 200 AND 205",
        ) == 5
    finally:
        conn.close()

    log_entries = _read_log(tmp_path)
    assert len(log_entries) == 1
    assert log_entries[0]["target"]["bundle_id"] == "com.spotify.client"


# ---------------------------------------------------------------------------
# Test 2 — Domain-level scrub
# ---------------------------------------------------------------------------


def test_domain_scrub_only_matches_browser_url(tmp_path: Path) -> None:
    worker, db_path = _make_worker(tmp_path)
    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)

    # Two Chrome windows: github.com and example.com. Only github.com
    # should be scrubbed. extract_root_domain handles subdomain collapsing
    # so api.github.com should also match.
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=300.0,
        bundle_id="com.google.Chrome", app_name="Google Chrome",
        browser_url="https://github.com/foo/bar",
    )
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=301.0,
        bundle_id="com.google.Chrome", app_name="Google Chrome",
        browser_url="https://api.github.com/repos",
    )
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=302.0,
        bundle_id="com.google.Chrome", app_name="Google Chrome",
        browser_url="https://example.com/page",
    )
    for ts in (300.0, 301.0, 302.0):
        _insert_action_event(
            conn, recording_id=rec_id, timestamp=ts + 0.1,
            window_event_timestamp=ts,
        )
        _insert_screenshot(conn, recording_id=rec_id, timestamp=ts)
    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "domain",
        "bundle_id": "com.google.Chrome",
        "app_name": "Google Chrome",
        "root_domain": "github.com",
        "ts_unix": 2000.0,
        "source": "menubar",
    })

    assert entry["scrub_status"] == "completed"
    assert entry["scrub_result"]["matched_timestamps"] == 2
    assert entry["scrub_result"]["window_events"] == 2
    assert entry["scrub_result"]["action_events"] == 2
    assert entry["scrub_result"]["screenshots"] == 2

    conn = sqlite3.connect(str(db_path))
    try:
        # github.com (and api.github.com) gone
        assert _count(
            conn, "window_event",
            "browser_url LIKE '%github.com%'",
        ) == 0
        # example.com untouched
        assert _count(
            conn, "window_event",
            "browser_url = 'https://example.com/page'",
        ) == 1
        # action_events for github gone, example.com still there
        assert _count(
            conn, "action_event", "window_event_timestamp = 300.0"
        ) == 0
        assert _count(
            conn, "action_event", "window_event_timestamp = 301.0"
        ) == 0
        assert _count(
            conn, "action_event", "window_event_timestamp = 302.0"
        ) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 3 — Recursive ActionEvent delete (parent_id has no cascade)
# ---------------------------------------------------------------------------


def test_recursive_action_event_delete(tmp_path: Path) -> None:
    worker, db_path = _make_worker(tmp_path)
    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)

    # One Spotify window event. Build a parent action_event whose
    # window_event_timestamp matches the target, then 3 children whose
    # parent_id points at that parent. Children's window_event_timestamp
    # is intentionally NULL so they would NOT be matched by the direct
    # IN clause — only the recursive walk catches them.
    target_ts = 500.0
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=target_ts,
        bundle_id="com.spotify.client", app_name="Spotify",
    )
    parent_id = _insert_action_event(
        conn, recording_id=rec_id, timestamp=target_ts + 0.1,
        window_event_timestamp=target_ts, name="type_aggregate",
    )
    for i in range(3):
        # Children have NULL window_event_timestamp on purpose.
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO action_event "
            "(recording_id, timestamp, recording_timestamp, "
            " name, parent_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec_id, target_ts + 0.2 + i * 0.01, target_ts + 0.2 + i * 0.01,
             "key_press", parent_id),
        )
    conn.commit()
    # Sanity: 4 action_events present before scrub
    assert _count(conn, "action_event") == 4
    conn.close()

    entry = worker._handle({
        "kind": "app",
        "bundle_id": "com.spotify.client",
        "app_name": "Spotify",
        "root_domain": None,
        "ts_unix": 3000.0,
        "source": "menubar",
    })

    assert entry["scrub_status"] == "completed"
    assert entry["scrub_result"]["action_events"] == 4

    conn = sqlite3.connect(str(db_path))
    try:
        assert _count(conn, "action_event") == 0
        assert _count(conn, "window_event") == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 4 — Empty target completes with zero counts
# ---------------------------------------------------------------------------


def test_empty_target_completes_with_zero_counts(tmp_path: Path) -> None:
    worker, db_path = _make_worker(tmp_path)
    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)

    # Seed an unrelated app so the DB isn't empty — just nothing
    # matching the disable target.
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=400.0,
        bundle_id="com.apple.Safari", app_name="Safari",
    )
    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "app",
        "bundle_id": "com.example.NotRunning",
        "app_name": "Nothing",
        "root_domain": None,
        "ts_unix": 4000.0,
        "source": "menubar",
    })

    assert entry["scrub_status"] == "completed"
    assert entry["scrub_result"]["matched_timestamps"] == 0
    assert entry["scrub_result"]["window_events"] == 0
    assert entry["scrub_result"]["action_events"] == 0
    assert entry["scrub_result"]["screenshots"] == 0
    assert entry["scrub_result"]["window_geometries"] == 0
    assert entry["error"] is None

    # Safari should still be present.
    conn = sqlite3.connect(str(db_path))
    try:
        assert _count(
            conn, "window_event", "app_bundle_id = 'com.apple.Safari'"
        ) == 1
    finally:
        conn.close()

    # Log frontmatter + one entry.
    log_path = tmp_path / ".menubar_disable_log.jsonl"
    assert log_path.exists()
    lines = [
        json.loads(line)
        for line in log_path.read_text().splitlines() if line.strip()
    ]
    assert lines[0].get("_meta") is True
    assert lines[0]["format_version"] == 1
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# Test 5 — On-disk JPEG files are unlinked
# ---------------------------------------------------------------------------


def test_scrub_unlinks_on_disk_screenshot_files(tmp_path: Path) -> None:
    worker, db_path = _make_worker(tmp_path)
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir()

    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)

    # 3 Spotify rows with on-disk image files; 2 Safari rows that must
    # NOT be unlinked.
    spotify_files = []
    for i, ts in enumerate([100.0, 101.0, 102.0]):
        rel = f"screenshots/spotify_{i}.jpg"
        abs_path = tmp_path / rel
        abs_path.write_bytes(b"fake-jpeg")
        spotify_files.append(abs_path)
        _insert_window_event(
            conn, recording_id=rec_id, timestamp=ts,
            bundle_id="com.spotify.client", app_name="Spotify",
        )
        conn.execute(
            "INSERT INTO screenshot "
            "(recording_id, timestamp, recording_timestamp, png_data, image_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec_id, ts, ts, b"\x89PNG_FAKE", rel),
        )

    safari_files = []
    for i, ts in enumerate([200.0, 201.0]):
        rel = f"screenshots/safari_{i}.jpg"
        abs_path = tmp_path / rel
        abs_path.write_bytes(b"fake-jpeg")
        safari_files.append(abs_path)
        _insert_window_event(
            conn, recording_id=rec_id, timestamp=ts,
            bundle_id="com.apple.Safari", app_name="Safari",
        )
        conn.execute(
            "INSERT INTO screenshot "
            "(recording_id, timestamp, recording_timestamp, png_data, image_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec_id, ts, ts, b"\x89PNG_FAKE", rel),
        )

    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "app",
        "bundle_id": "com.spotify.client",
        "app_name": "Spotify",
        "root_domain": None,
        "ts_unix": 5000.0,
        "source": "menubar",
    })

    assert entry["scrub_status"] == "completed"
    assert entry["scrub_result"]["screenshots"] == 3
    assert entry["scrub_result"]["screenshot_files"] == 3

    # Spotify .jpg files gone, Safari .jpg files intact
    for f in spotify_files:
        assert not f.exists(), f"expected {f} to be unlinked"
    for f in safari_files:
        assert f.exists(), f"expected {f} to remain"


def test_scrub_handles_missing_on_disk_files_gracefully(tmp_path: Path) -> None:
    """If image_path points to a file that no longer exists on disk, the
    scrub should still complete and report the row count truthfully."""
    worker, db_path = _make_worker(tmp_path)

    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=300.0,
        bundle_id="com.spotify.client", app_name="Spotify",
    )
    # image_path points to a missing file
    conn.execute(
        "INSERT INTO screenshot "
        "(recording_id, timestamp, recording_timestamp, png_data, image_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (rec_id, 300.0, 300.0, b"\x89PNG_FAKE", "screenshots/missing.jpg"),
    )
    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "app",
        "bundle_id": "com.spotify.client",
        "app_name": "Spotify",
        "root_domain": None,
        "ts_unix": 6000.0,
        "source": "menubar",
    })

    assert entry["scrub_status"] == "completed"
    assert entry["scrub_result"]["screenshots"] == 1
    # missing_ok=True → unlink succeeds even though the file wasn't there
    assert entry["scrub_result"]["screenshot_files"] == 1


# ---------------------------------------------------------------------------
# Test 6 — Engine flush handshake (the original bug)
# ---------------------------------------------------------------------------


def test_scrub_triggers_engine_flush_before_select(tmp_path: Path) -> None:
    """Simulates the BATCH_SIZE flush race: rows are 'in writer buffer' (not
    in DB) until flush_requested is set. The fake writer thread acts as the
    engine writer would — it commits its buffered row when it sees the
    Event, then increments the ack counter.
    """
    import multiprocessing
    import threading as _threading_mod
    from screencap.enforcement.scrub_worker import ScrubWorker

    db_path = _make_recording_db(tmp_path)
    rec_id_holder = {}

    conn0 = sqlite3.connect(str(db_path))
    rec_id_holder["id"] = _seed_recording(conn0)
    conn0.commit()
    conn0.close()

    flush_requested = multiprocessing.Event()
    flush_ack_counter = multiprocessing.Value("i", 0)
    flush_lock = _threading_mod.Lock()

    # Fake writer thread: holds a row in memory, commits it only when
    # flush_requested fires (mirrors engine/recorder.py:874-878).
    writer_done = _threading_mod.Event()
    writer_acked = _threading_mod.Event()

    def fake_writer():
        while not writer_done.is_set():
            if flush_requested.is_set():
                wconn = sqlite3.connect(str(db_path))
                try:
                    wconn.execute(
                        "INSERT INTO window_event "
                        "(recording_id, timestamp, recording_timestamp, "
                        " app_bundle_id, app_name) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (rec_id_holder["id"], 999.0, 999.0,
                         "com.spotify.client", "Spotify"),
                    )
                    wconn.commit()
                finally:
                    wconn.close()
                with flush_ack_counter.get_lock():
                    flush_ack_counter.value += 1
                writer_acked.set()
                # Wait until the Event is cleared so we don't double-write
                while flush_requested.is_set() and not writer_done.is_set():
                    time.sleep(0.05)
            time.sleep(0.05)

    writer_thread = _threading_mod.Thread(
        target=fake_writer, daemon=True, name="fake_engine_writer",
    )
    writer_thread.start()

    try:
        worker = ScrubWorker(
            disable_q=None,
            recording_db_path=db_path,
            capture_dir=tmp_path,
            flush_requested=flush_requested,
            flush_ack_counter=flush_ack_counter,
            flush_lock=flush_lock,
        )

        # BEFORE the scrub: the row is NOT in the DB (it's in the fake
        # writer's "buffer").
        check_conn = sqlite3.connect(str(db_path))
        try:
            assert _count(
                check_conn, "window_event",
                "app_bundle_id = 'com.spotify.client'",
            ) == 0
        finally:
            check_conn.close()

        # Run the scrub. This should trigger the flush, which causes the
        # fake writer to commit the row, then the SELECT finds it and
        # deletes it.
        entry = worker._handle({
            "kind": "app",
            "bundle_id": "com.spotify.client",
            "app_name": "Spotify",
            "root_domain": None,
            "ts_unix": 7000.0,
            "source": "menubar",
        })

        assert writer_acked.is_set(), "fake writer never acked the flush"
        assert entry["scrub_status"] == "completed"
        assert entry["scrub_result"]["matched_timestamps"] == 1
        assert entry["scrub_result"]["window_events"] == 1

        # Verify the row is actually gone post-scrub.
        final_conn = sqlite3.connect(str(db_path))
        try:
            assert _count(
                final_conn, "window_event",
                "app_bundle_id = 'com.spotify.client'",
            ) == 0
        finally:
            final_conn.close()
    finally:
        writer_done.set()
        writer_thread.join(timeout=2)


def test_scrub_deletes_screenshots_in_target_active_interval(tmp_path: Path) -> None:
    """Regression for the ghostty-chrome-session leak: screenshots have
    independent timestamps from window events. The scrub must delete
    screenshots whose timestamp falls in the WINDOW where the disabled
    target was the most-recently-active app, not just where they share
    a timestamp with a target window event (which never happens in
    practice)."""
    worker, db_path = _make_worker(tmp_path)
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir()

    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)

    # Timeline (mirrors the user's recording structure):
    #   t=100  ghostty active
    #   t=110  chrome (no URL yet — AX hasn't classified the new tab)
    #   t=115  chrome github.com — user has navigated, URL is now extracted
    #   t=120  chrome github.com — second URL update
    #   t=130  chrome quora.com — user navigated away
    #   t=140  ghostty active
    # Screenshots are sampled every ~1s, completely independent of
    # window events.
    timeline = [
        ("ghostty",   100.0, "com.mitchellh.ghostty", None),
        ("chrome_no_url", 110.0, "com.google.Chrome", None),
        ("chrome_gh1",    115.0, "com.google.Chrome", "https://github.com/"),
        ("chrome_gh2",    120.0, "com.google.Chrome", "https://github.com/proteus"),
        ("chrome_quora",  130.0, "com.google.Chrome", "https://www.quora.com/"),
        ("ghostty2", 140.0, "com.mitchellh.ghostty", None),
    ]
    for label, ts, bid, url in timeline:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO window_event "
            "(recording_id, timestamp, recording_timestamp, "
            " app_bundle_id, browser_url, title) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rec_id, ts, ts, bid, url, label),
        )

    # Screenshots scattered across the whole timeline. The interval
    # logic also applies a 2-second URL-detection-lag PRELUDE, so
    # screenshots within 2s before the first github event (t=115) are
    # ALSO deleted — clamped to the previous same-bundle event at t=110.
    # That means the effective github interval is [113, 130).
    screenshot_layout = [
        # In ghostty pre-chrome (KEEP — different bundle)
        (101.5, "screenshots/keep_ghostty_1.jpg"),
        (105.0, "screenshots/keep_ghostty_2.jpg"),
        # In chrome-no-url BEFORE the prelude window (KEEP)
        (111.5, "screenshots/keep_chrome_pre_url_before_prelude.jpg"),
        (112.5, "screenshots/keep_chrome_pre_url_just_before_prelude.jpg"),
        # In chrome-no-url INSIDE the prelude window [113, 115) — these
        # were captured during a possible URL-detection lag, so the
        # scrub deletes them as a safety margin against the leak the
        # user reported in chrome-ghostty-session-2.
        (113.5, "screenshots/del_chrome_in_prelude.jpg"),
        (114.5, "screenshots/del_chrome_in_prelude_2.jpg"),
        # In github interval [115, 130) — DELETE
        (115.0, "screenshots/del_github_first_frame.jpg"),
        (116.5, "screenshots/del_github_2.jpg"),
        (118.0, "screenshots/del_github_3.jpg"),
        (122.5, "screenshots/del_github_4.jpg"),
        (125.0, "screenshots/del_github_5.jpg"),
        (129.999, "screenshots/del_github_last_frame.jpg"),
        # In chrome quora (KEEP — different domain, after target ended)
        (131.0, "screenshots/keep_quora_1.jpg"),
        (135.0, "screenshots/keep_quora_2.jpg"),
        # In ghostty post-chrome (KEEP)
        (141.0, "screenshots/keep_ghostty_post.jpg"),
    ]
    for ts, rel in screenshot_layout:
        # Create the on-disk file too so we can verify unlinking.
        (tmp_path / rel).write_bytes(b"fake-screenshot")
        conn.execute(
            "INSERT INTO screenshot "
            "(recording_id, timestamp, recording_timestamp, png_data, image_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec_id, ts, ts, b"\x89PNG_FAKE", rel),
        )
        # And a window_geometry row pinned to the same screenshot ts.
        conn.execute(
            "INSERT INTO window_geometry "
            "(recording_id, screenshot_timestamp, recording_timestamp, "
            " window_list_json) "
            "VALUES (?, ?, ?, ?)",
            (rec_id, ts, ts, "[]"),
        )

    conn.commit()
    conn.close()

    # Disable github.com.
    entry = worker._handle({
        "kind": "domain",
        "bundle_id": "com.google.Chrome",
        "app_name": "Google Chrome",
        "root_domain": "github.com",
        "ts_unix": 9000.0,
        "source": "menubar",
    })

    assert entry["scrub_status"] == "completed"
    # 2 github window events (gh1, gh2)
    assert entry["scrub_result"]["matched_timestamps"] == 2
    assert entry["scrub_result"]["window_events"] == 2
    # 8 deleted screenshots: 6 in [115, 130) + 2 prelude frames at
    # 113.5 and 114.5 (caught by the URL-detection-lag prelude)
    assert entry["scrub_result"]["screenshots"] == 8, (
        f"Expected 8 deleted screenshots (6 github + 2 prelude), got "
        f"{entry['scrub_result']['screenshots']}"
    )
    assert entry["scrub_result"]["window_geometries"] == 8
    assert entry["scrub_result"]["screenshot_files"] == 8

    # On-disk: github + prelude files gone, all others intact.
    for ts, rel in screenshot_layout:
        path = tmp_path / rel
        if rel.startswith("screenshots/del_"):
            assert not path.exists(), f"expected {rel} to be unlinked"
        else:
            assert path.exists(), f"expected {rel} to remain"

    # DB: github + prelude screenshots gone, others intact.
    conn = sqlite3.connect(str(db_path))
    try:
        # All in-interval screenshots gone (113.5 → 129.999)
        assert _count(
            conn, "screenshot",
            "timestamp >= 113.0 AND timestamp < 130.0",
        ) == 0
        # 4 keepers before the prelude window (101.5, 105, 111.5, 112.5)
        keep_pre = _count(conn, "screenshot", "timestamp < 113.0")
        # 3 keepers after the github interval (131, 135, 141)
        keep_post = _count(conn, "screenshot", "timestamp >= 130.0")
        assert keep_pre == 4, f"got {keep_pre} pre-prelude keepers"
        assert keep_post == 3, f"got {keep_post} post-interval keepers"
        # Quora window event still present
        assert _count(
            conn, "window_event",
            "browser_url = 'https://www.quora.com/'",
        ) == 1
    finally:
        conn.close()


def test_scrub_prelude_catches_url_detection_lag_frame(tmp_path: Path) -> None:
    """Regression for chrome-ghostty-session-2: when the user clicks a
    link from a search results page to the disabled target site, the
    actual target page can render BEFORE Chrome's AX URL extraction
    catches up and emits a window event with the new URL. The
    screenshot taken in that lag window visually shows the target site
    even though its predecessor window event is the previous (innocent)
    page in the same browser. The interval prelude must catch it.
    """
    worker, db_path = _make_worker(tmp_path)
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir()

    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)

    # Mirrors the user's exact timeline:
    #   t=361.02  chrome google.com/search?q=cleartrip  ← previous page
    #   t=362.0   chrome cleartrip.com                  ← target (URL detected late)
    #   t=369.17  chrome quora.com                      ← user moved on
    cur = conn.cursor()
    for ts, url in [
        (361.02, "https://www.google.com/search?q=cleartrip"),
        (362.0,  "https://www.cleartrip.com/flights/"),
        (369.17, "https://www.quora.com/"),
    ]:
        cur.execute(
            "INSERT INTO window_event "
            "(recording_id, timestamp, recording_timestamp, "
            " app_bundle_id, browser_url, title) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rec_id, ts, ts, "com.google.Chrome", url, "Google Chrome"),
        )

    # The actual cleartrip frame is captured at 361.288 — 270ms after the
    # google search window event but ~712ms BEFORE the cleartrip window
    # event fires. Without the prelude, this frame leaks.
    layout = [
        # Pre-google KEEPER (KEEP)
        (360.0, "screenshots/keep_pre_google.jpg"),
        # Just-after google search KEEPER (KEEP — well outside prelude)
        (361.023, "screenshots/keep_google_results.jpg"),
        # The lagged cleartrip frame (DELETE — caught by prelude)
        (361.288, "screenshots/del_cleartrip_lag_frame.jpg"),
        # Mid-cleartrip frames (DELETE — in interval)
        (363.0, "screenshots/del_cleartrip_2.jpg"),
        (365.0, "screenshots/del_cleartrip_3.jpg"),
        (368.0, "screenshots/del_cleartrip_4.jpg"),
        # Post-cleartrip quora frame (KEEP)
        (370.0, "screenshots/keep_quora.jpg"),
    ]
    for ts, rel in layout:
        (tmp_path / rel).write_bytes(b"fake")
        conn.execute(
            "INSERT INTO screenshot "
            "(recording_id, timestamp, recording_timestamp, png_data, image_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec_id, ts, ts, b"PNG", rel),
        )
    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "domain",
        "bundle_id": "com.google.Chrome",
        "app_name": "Google Chrome",
        "root_domain": "cleartrip.com",
        "ts_unix": 11000.0,
        "source": "menubar",
    })

    assert entry["scrub_status"] == "completed"
    # The lag frame plus 3 in-interval frames = 4 deletions.
    # Effective interval: max(362.0 - 2.0, 361.02) = 361.02 → [361.02, 369.17).
    # Screenshot at 361.023 is BEFORE 361.02 (technically — within rounding
    # it's 0.003 after, which is INSIDE the interval) — let me be more precise.
    # 361.023 >= 361.02 → IN interval → deleted as well.
    # So actually 5 frames deleted: 361.023, 361.288, 363, 365, 368
    assert entry["scrub_result"]["screenshots"] == 5, (
        f"Expected 5 deleted screenshots (lag prelude + cleartrip), got "
        f"{entry['scrub_result']['screenshots']}"
    )
    assert entry["scrub_result"]["screenshot_files"] == 5

    # The CRITICAL frame: the cleartrip lag frame must be gone.
    assert not (tmp_path / "screenshots/del_cleartrip_lag_frame.jpg").exists()
    # Pre-google keeper survives (it's at t=360, before the prelude window)
    assert (tmp_path / "screenshots/keep_pre_google.jpg").exists()
    # Post-cleartrip quora keeper survives
    assert (tmp_path / "screenshots/keep_quora.jpg").exists()


def test_scrub_extends_interval_to_recording_end_when_target_is_last(
    tmp_path: Path,
) -> None:
    """If the target is the LAST window event in the recording, the
    interval extends to +inf. All screenshots from the target's first
    appearance through the end of the recording must be deleted."""
    worker, db_path = _make_worker(tmp_path)
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir()

    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO window_event "
        "(recording_id, timestamp, recording_timestamp, app_bundle_id, browser_url) "
        "VALUES (?, ?, ?, ?, ?)",
        (rec_id, 200.0, 200.0, "com.mitchellh.ghostty", None),
    )
    cur.execute(
        "INSERT INTO window_event "
        "(recording_id, timestamp, recording_timestamp, app_bundle_id, browser_url) "
        "VALUES (?, ?, ?, ?, ?)",
        (rec_id, 250.0, 250.0, "com.google.Chrome", "https://github.com/"),
    )

    layout = [
        (210.0, "screenshots/keep_pre.jpg"),
        (250.5, "screenshots/del_first.jpg"),
        (260.0, "screenshots/del_mid.jpg"),
        (300.0, "screenshots/del_late.jpg"),
        (999.0, "screenshots/del_far_future.jpg"),
    ]
    for ts, rel in layout:
        (tmp_path / rel).write_bytes(b"fake")
        conn.execute(
            "INSERT INTO screenshot "
            "(recording_id, timestamp, recording_timestamp, png_data, image_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec_id, ts, ts, b"PNG", rel),
        )
    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "domain",
        "bundle_id": "com.google.Chrome",
        "app_name": "Google Chrome",
        "root_domain": "github.com",
        "ts_unix": 10000.0,
        "source": "menubar",
    })

    assert entry["scrub_result"]["screenshots"] == 4
    assert entry["scrub_result"]["screenshot_files"] == 4
    assert (tmp_path / "screenshots/keep_pre.jpg").exists()
    for rel in (
        "screenshots/del_first.jpg",
        "screenshots/del_mid.jpg",
        "screenshots/del_late.jpg",
        "screenshots/del_far_future.jpg",
    ):
        assert not (tmp_path / rel).exists()


def test_scrub_cleans_up_orphan_action_events(tmp_path: Path) -> None:
    """Regression for chrome-session: after a previous scrub deletes a
    target window_event, the engine keeps inserting new action_events
    that reference the deleted timestamp (because the engine's in-memory
    prev_window_event still points there). The next scrub call must
    catch these orphans even when no surviving target window_event
    exists to compute an interval from.
    """
    worker, db_path = _make_worker(tmp_path)

    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)

    # Surviving non-target window event
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=100.0,
        bundle_id="com.apple.Safari", app_name="Safari",
    )

    # Orphan action_events: their window_event_timestamp (200.0) doesn't
    # match any window_event row. This is what the engine produces after
    # a target window_event row gets deleted.
    for i in range(15):
        conn.execute(
            "INSERT INTO action_event "
            "(recording_id, timestamp, recording_timestamp, "
            " window_event_timestamp, name) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec_id, 200.5 + i * 0.01, 200.5 + i * 0.01, 200.0, "click"),
        )

    # A legitimate action_event tied to the surviving Safari row
    conn.execute(
        "INSERT INTO action_event "
        "(recording_id, timestamp, recording_timestamp, "
        " window_event_timestamp, name) "
        "VALUES (?, ?, ?, ?, ?)",
        (rec_id, 100.5, 100.5, 100.0, "click"),
    )
    conn.commit()
    conn.close()

    # Run a scrub against a target that has no surviving window events
    # (mirrors the catchall path).
    entry = worker._handle({
        "kind": "domain",
        "bundle_id": "com.google.Chrome",
        "app_name": "Google Chrome",
        "root_domain": "github.com",
        "ts_unix": 12000.0,
        "source": "shutdown_catchall",
    })

    assert entry["scrub_status"] == "completed"
    # No surviving target → matched_timestamps stays 0, but orphan
    # cleanup catches the 15 dangling action_events.
    assert entry["scrub_result"]["matched_timestamps"] == 0
    assert entry["scrub_result"]["orphan_action_events"] == 15

    # Verify: the orphans are gone, the legitimate Safari action remains.
    conn = sqlite3.connect(str(db_path))
    try:
        assert _count(conn, "action_event") == 1
        assert _count(
            conn, "action_event", "window_event_timestamp = 100.0"
        ) == 1
        assert _count(
            conn, "action_event", "window_event_timestamp = 200.0"
        ) == 0
    finally:
        conn.close()


def test_scrub_works_with_no_flush_primitives(tmp_path: Path) -> None:
    """Backward compat: if flush_requested/counter are None (e.g. unchunked
    recording or test fixture), the scrub still runs and just reads the DB
    as-is."""
    worker, db_path = _make_worker(tmp_path)

    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=400.0,
        bundle_id="com.spotify.client", app_name="Spotify",
    )
    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "app",
        "bundle_id": "com.spotify.client",
        "app_name": "Spotify",
        "root_domain": None,
        "ts_unix": 8000.0,
        "source": "menubar",
    })

    assert entry["scrub_status"] == "completed"
    assert entry["scrub_result"]["window_events"] == 1


# ---------------------------------------------------------------------------
# SCR-277 — the purge spans are persisted (crash-consistently) so the R11 strip
# can still block the disabled app's stale flat-file events/transcripts
# ---------------------------------------------------------------------------


def _read_purged_intervals(conn: sqlite3.Connection) -> list[tuple[float, float | None]]:
    """Return (start_ts, end_ts) rows from the ``purged_interval`` table, or []."""
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='purged_interval'"
    ).fetchone()
    if not has_table:
        return []
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT start_ts, end_ts FROM purged_interval ORDER BY start_ts"
        ).fetchall()
    ]


def test_purge_persists_bounded_interval(tmp_path: Path) -> None:
    """A disabled app sandwiched between benign windows persists a bounded span.

    Benign Safari at 100, target Spotify at 200, benign Safari at 300 → the
    target's active span is [200 - prelude, 300). The purge deletes Spotify's
    rows AND persists that span in ``purged_interval`` in the SAME transaction, so
    the R11 re-derivation (blind to the now-deleted span) can still block it.
    """
    worker, db_path = _make_worker(tmp_path)
    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=100.0,
        bundle_id="com.apple.Safari", app_name="Safari",
    )
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=200.0,
        bundle_id="com.spotify.client", app_name="Spotify",
    )
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=300.0,
        bundle_id="com.apple.Safari", app_name="Safari",
    )
    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "app",
        "bundle_id": "com.spotify.client",
        "app_name": "Spotify",
        "root_domain": None,
        "ts_unix": 5000.0,
        "source": "menubar",
    })

    assert entry["scrub_status"] == "completed"
    assert entry["scrub_result"]["window_events"] == 1
    assert entry["scrub_result"]["purged_intervals"] == 1

    conn = sqlite3.connect(str(db_path))
    try:
        # The deleted span and its persisted interval are crash-consistent: the
        # Spotify row is gone AND the interval that covers it is recorded.
        assert _count(conn, "window_event", "app_bundle_id = 'com.spotify.client'") == 0
        rows = _read_purged_intervals(conn)
        assert rows == [(198.0, 300.0)]  # 200 - 2.0s prelude → 300 (next benign)
    finally:
        conn.close()


def test_purge_persists_open_ended_interval_as_null_end(tmp_path: Path) -> None:
    """A target active at recording end persists ``end_ts = NULL`` (→ +inf).

    Only a Spotify window survives at the tail, so its active interval is
    open-ended. It must round-trip through the DB as a NULL ``end_ts`` — the
    reader (``skip_intervals._read_purged_intervals``) maps NULL → +inf, so the
    open-ended purge span blocks every later timestamp.
    """
    worker, db_path = _make_worker(tmp_path)
    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=100.0,
        bundle_id="com.apple.Safari", app_name="Safari",
    )
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=200.0,
        bundle_id="com.spotify.client", app_name="Spotify",
    )
    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "app",
        "bundle_id": "com.spotify.client",
        "app_name": "Spotify",
        "root_domain": None,
        "ts_unix": 6000.0,
        "source": "menubar",
    })

    assert entry["scrub_result"]["purged_intervals"] == 1
    conn = sqlite3.connect(str(db_path))
    try:
        rows = _read_purged_intervals(conn)
        assert len(rows) == 1
        start_ts, end_ts = rows[0]
        assert start_ts == 198.0
        assert end_ts is None  # open-ended → NULL, NOT a serialized inf
    finally:
        conn.close()


def test_empty_target_persists_no_interval(tmp_path: Path) -> None:
    """A no-op disable (nothing matched) writes no ``purged_interval`` rows."""
    worker, db_path = _make_worker(tmp_path)
    conn = sqlite3.connect(str(db_path))
    rec_id = _seed_recording(conn)
    _insert_window_event(
        conn, recording_id=rec_id, timestamp=400.0,
        bundle_id="com.apple.Safari", app_name="Safari",
    )
    conn.commit()
    conn.close()

    entry = worker._handle({
        "kind": "app",
        "bundle_id": "com.example.NotRunning",
        "app_name": "Nothing",
        "root_domain": None,
        "ts_unix": 7000.0,
        "source": "menubar",
    })

    assert entry["scrub_result"]["purged_intervals"] == 0
    conn = sqlite3.connect(str(db_path))
    try:
        assert _read_purged_intervals(conn) == []
    finally:
        conn.close()
