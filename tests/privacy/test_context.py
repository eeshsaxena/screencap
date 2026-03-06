"""Tests for privacy v3 Phase 2: context association and classification."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from screencap.privacy.context import (
    BROWSER_BUNDLE_IDS,
    BrowserContext,
    DefaultContextClassifier,
    TemporalContextClassifier,
    WindowContext,
    _BUNDLE_ID_MAP,
    associate_screenshot,
    find_nearest_window,
    load_browser_events,
    load_window_events,
    parse_screenshot_timestamp,
)
from screencap.privacy.policy import ContextClass, FrameMetadata

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Screenshot timestamp parsing
# ---------------------------------------------------------------------------


class TestParseScreenshotTimestamp:
    def test_parses_recorder_filename_format(self):
        assert parse_screenshot_timestamp("1709745600.123456.jpg") == pytest.approx(
            1709745600.123456
        )

    def test_parses_jpeg_extension(self):
        assert parse_screenshot_timestamp("1709745600.123456.jpeg") == pytest.approx(
            1709745600.123456
        )

    def test_parses_db_image_path_with_directory_prefix(self):
        # image_path in DB is stored as "screenshots/{ts}.jpg"
        assert parse_screenshot_timestamp("screenshots/1709745600.123456.jpg") == pytest.approx(
            1709745600.123456
        )

    @pytest.mark.parametrize("filename", [
        "1709745600.123456.png",
        "screenshot.jpg",
    ])
    def test_rejects_non_parseable_filenames(self, filename):
        assert parse_screenshot_timestamp(filename) is None


# ---------------------------------------------------------------------------
# Nearest-event lookup (bisect logic)
# ---------------------------------------------------------------------------


class TestFindNearestWindow:
    def _make_events(self, timestamps: list[float]) -> list[WindowContext]:
        return [
            WindowContext(timestamp=ts, app_bundle_id=f"app{i}", title=f"title{i}")
            for i, ts in enumerate(timestamps)
        ]

    def test_exact_match(self):
        events = self._make_events([1.0, 2.0, 3.0])
        result = find_nearest_window(events, 2.0)
        assert result is not None
        assert result.timestamp == 2.0

    @pytest.mark.parametrize("target,expected_ts", [
        (1.3, 1.0),  # closer to before
        (2.8, 3.0),  # closer to after
    ])
    def test_picks_closest_by_distance(self, target, expected_ts):
        events = self._make_events([1.0, 3.0])
        result = find_nearest_window(events, target)
        assert result is not None
        assert result.timestamp == expected_ts

    def test_rejects_events_beyond_max_delta(self):
        events = self._make_events([1.0])
        assert find_nearest_window(events, 10.0, max_delta=5.0) is None


# ---------------------------------------------------------------------------
# DB loaders (integration with real SQLite)
# ---------------------------------------------------------------------------


def _create_test_db(tmp_path: Path) -> Path:
    """Create a test SQLite DB with window_event and browser_event tables."""
    db_path = tmp_path / "recording.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE window_event (
            id INTEGER PRIMARY KEY,
            recording_id INTEGER,
            recording_timestamp REAL,
            timestamp REAL,
            state TEXT,
            title TEXT,
            "left" INTEGER,
            top INTEGER,
            width INTEGER,
            height INTEGER,
            window_id TEXT,
            app_bundle_id TEXT,
            app_version TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE browser_event (
            id INTEGER PRIMARY KEY,
            recording_id INTEGER,
            recording_timestamp REAL,
            message TEXT,
            timestamp REAL
        )
    """)
    conn.commit()
    conn.close()
    return db_path


class TestLoadWindowEvents:
    def test_loads_and_sorts_by_timestamp(self, tmp_path):
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        # Insert out of order to verify ORDER BY
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id) "
            "VALUES (2.0, 'com.apple.mail', 'Inbox', 'w1')"
        )
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id) "
            "VALUES (1.0, 'com.apple.Safari', 'Google', 'w2')"
        )
        conn.commit()
        conn.close()

        events = load_window_events(db_path)
        assert len(events) == 2
        assert events[0].timestamp == 1.0
        assert events[0].app_bundle_id == "com.apple.Safari"
        assert events[1].timestamp == 2.0

    def test_missing_table_returns_empty(self, tmp_path):
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert load_window_events(db_path) == []

    def test_null_timestamps_excluded(self, tmp_path):
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title) "
            "VALUES (NULL, 'com.foo', 'Bar')"
        )
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title) "
            "VALUES (1.0, 'com.bar', 'Baz')"
        )
        conn.commit()
        conn.close()
        assert len(load_window_events(db_path)) == 1


class TestLoadBrowserEvents:
    def test_extracts_url_and_domain_from_json_message(self, tmp_path):
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        msg = json.dumps({"url": "https://mail.google.com/inbox", "type": "browser.click"})
        conn.execute(
            "INSERT INTO browser_event (timestamp, message) VALUES (1.0, ?)", (msg,)
        )
        conn.commit()
        conn.close()

        events = load_browser_events(db_path)
        assert len(events) == 1
        assert events[0].domain == "mail.google.com"
        assert events[0].url == "https://mail.google.com/inbox"

    def test_skips_events_without_url(self, tmp_path):
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO browser_event (timestamp, message) VALUES (1.0, ?)",
            (json.dumps({"type": "scroll"}),),
        )
        conn.commit()
        conn.close()
        assert load_browser_events(db_path) == []


# ---------------------------------------------------------------------------
# DefaultContextClassifier — classification priority chain
# ---------------------------------------------------------------------------


class TestDefaultContextClassifier:
    def setup_method(self):
        self.classifier = DefaultContextClassifier()

    # Path 1: known app bundle ID
    def test_known_bundle_id_classifies_directly(self):
        meta = FrameMetadata(bundle_id="com.apple.mail", window_title="Inbox")
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "bundle_id"

    # Path 1 > Path 2: bundle ID takes priority over browser+domain
    def test_known_bundle_id_beats_browser_domain(self):
        meta = FrameMetadata(
            bundle_id="com.apple.mail",
            domain="mail.google.com",
        )
        result = self.classifier.classify(meta)
        assert result.confidence == "bundle_id"

    # Path 2a: browser with verified domain
    def test_browser_with_known_domain(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="mail.google.com",
            window_title="Inbox - Gmail",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "domain"

    # Path 2a: subdomain matching (parent-domain fallback)
    def test_browser_subdomain_matches_parent_domain(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="workspace.mail.google.com",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "domain"

    # Path 2b: browser + unknown domain falls to title heuristic
    def test_browser_unknown_domain_falls_to_title(self):
        meta = FrameMetadata(
            bundle_id="com.apple.Safari",
            domain="random-site.com",
            window_title="My Inbox - Some Mail",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "title"

    # Path 2c: browser without domain data → browser_unverified
    def test_browser_without_domain_is_unverified(self):
        meta = FrameMetadata(bundle_id="com.google.Chrome")
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    # Path 3: unknown app with title heuristic
    def test_unknown_app_uses_title_heuristic(self):
        meta = FrameMetadata(
            bundle_id="com.unknown.app",
            window_title="Bank of America - Account",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BANKING
        assert result.confidence == "title"

    # Path 4: no signal → explicit unknown
    def test_no_signal_produces_explicit_unknown(self):
        result = self.classifier.classify(FrameMetadata())
        assert result.context_class == ContextClass.UNKNOWN
        assert result.evidence == "no_matching_signal"

    # Data integrity: bundle ID map and browser IDs must be disjoint
    def test_bundle_id_map_disjoint_from_browser_ids(self):
        overlap = set(_BUNDLE_ID_MAP.keys()) & BROWSER_BUNDLE_IDS
        assert overlap == set(), (
            f"Bundle IDs in both _BUNDLE_ID_MAP and BROWSER_BUNDLE_IDS would "
            f"never reach the browser path: {overlap}"
        )


# ---------------------------------------------------------------------------
# TemporalContextClassifier (state machine)
# ---------------------------------------------------------------------------


class TestTemporalContextClassifier:
    def test_holds_classification_within_window(self):
        tc = TemporalContextClassifier(hold_seconds=3.0)
        tc.classify(FrameMetadata(bundle_id="com.apple.mail", timestamp=10.0))

        result = tc.classify(FrameMetadata(timestamp=12.0))
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "temporal_hold"

    def test_decays_to_unknown_after_hold_expires(self):
        tc = TemporalContextClassifier(hold_seconds=3.0)
        tc.classify(FrameMetadata(bundle_id="com.apple.mail", timestamp=10.0))

        result = tc.classify(FrameMetadata(timestamp=15.0))
        assert result.context_class == ContextClass.UNKNOWN

    def test_new_classification_resets_hold_timer(self):
        tc = TemporalContextClassifier(hold_seconds=3.0)
        tc.classify(FrameMetadata(bundle_id="com.apple.mail", timestamp=10.0))
        tc.classify(FrameMetadata(bundle_id="com.tinyspeck.slackmacgap", timestamp=12.0))

        # t=14 — within hold of chat (12+3), not email
        result = tc.classify(FrameMetadata(timestamp=14.0))
        assert result.context_class == ContextClass.CHAT

    def test_zero_timestamp_does_not_hold(self):
        tc = TemporalContextClassifier(hold_seconds=3.0)
        tc.classify(FrameMetadata(bundle_id="com.apple.mail", timestamp=10.0))

        result = tc.classify(FrameMetadata(timestamp=0.0))
        assert result.context_class == ContextClass.UNKNOWN


# ---------------------------------------------------------------------------
# associate_screenshot — integration
# ---------------------------------------------------------------------------


class TestAssociateScreenshot:
    def test_correlates_window_and_browser_by_timestamp(self):
        windows = [
            WindowContext(timestamp=1.0, app_bundle_id="com.google.Chrome", title="Gmail"),
        ]
        browsers = [
            BrowserContext(timestamp=1.1, url="https://mail.google.com", domain="mail.google.com"),
        ]
        meta = associate_screenshot(1.05, windows, browsers)
        assert meta.bundle_id == "com.google.Chrome"
        assert meta.domain == "mail.google.com"
        assert meta.timestamp == 1.05

    def test_no_events_returns_empty_metadata(self):
        meta = associate_screenshot(1.0, [], [])
        assert meta.bundle_id == ""
        assert meta.domain is None

    def test_non_browser_window_does_not_inherit_stale_browser_domain(self):
        """Switching from Chrome to Finder must not carry the browser domain."""
        windows = [
            WindowContext(timestamp=8.0, app_bundle_id="com.google.Chrome", title="Gmail"),
            WindowContext(timestamp=10.0, app_bundle_id="com.apple.Finder", title="Documents"),
        ]
        browsers = [
            BrowserContext(timestamp=8.0, url="https://chase.com", domain="chase.com"),
        ]
        # Screenshot at t=10 — Finder is active, browser event is within max_delta
        meta = associate_screenshot(10.0, windows, browsers)
        assert meta.bundle_id == "com.apple.Finder"
        assert meta.domain is None  # must NOT be "chase.com"
