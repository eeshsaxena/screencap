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
    associate_screenshot,
    find_nearest_browser,
    find_nearest_window,
    load_browser_events,
    load_window_events,
    parse_screenshot_timestamp,
)
from screencap.privacy.policy import ContextClass, ContextResult, FrameMetadata

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Screenshot timestamp parsing
# ---------------------------------------------------------------------------


class TestParseScreenshotTimestamp:
    def test_standard_filename(self):
        assert parse_screenshot_timestamp("1709745600.123456.jpg") == pytest.approx(
            1709745600.123456
        )

    def test_integer_timestamp(self):
        assert parse_screenshot_timestamp("1709745600.000000.jpg") == pytest.approx(
            1709745600.0
        )

    def test_non_jpg_returns_none(self):
        assert parse_screenshot_timestamp("1709745600.123456.png") is None

    def test_non_numeric_returns_none(self):
        assert parse_screenshot_timestamp("screenshot.jpg") is None

    def test_empty_string_returns_none(self):
        assert parse_screenshot_timestamp("") is None

    def test_jpeg_extension(self):
        assert parse_screenshot_timestamp("1709745600.123456.jpeg") == pytest.approx(
            1709745600.123456
        )


# ---------------------------------------------------------------------------
# Nearest-event lookup
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

    def test_nearest_before(self):
        events = self._make_events([1.0, 3.0])
        result = find_nearest_window(events, 1.3)
        assert result is not None
        assert result.timestamp == 1.0

    def test_nearest_after(self):
        events = self._make_events([1.0, 3.0])
        result = find_nearest_window(events, 2.8)
        assert result is not None
        assert result.timestamp == 3.0

    def test_exceeds_max_delta(self):
        events = self._make_events([1.0])
        result = find_nearest_window(events, 10.0, max_delta=5.0)
        assert result is None

    def test_empty_events(self):
        assert find_nearest_window([], 1.0) is None

    def test_within_max_delta(self):
        events = self._make_events([1.0])
        result = find_nearest_window(events, 3.0, max_delta=5.0)
        assert result is not None
        assert result.timestamp == 1.0


class TestFindNearestBrowser:
    def _make_events(self, timestamps: list[float]) -> list[BrowserContext]:
        return [
            BrowserContext(timestamp=ts, url=f"https://example{i}.com", domain=f"example{i}.com")
            for i, ts in enumerate(timestamps)
        ]

    def test_exact_match(self):
        events = self._make_events([1.0, 2.0, 3.0])
        result = find_nearest_browser(events, 2.0)
        assert result is not None
        assert result.timestamp == 2.0

    def test_exceeds_max_delta(self):
        events = self._make_events([1.0])
        result = find_nearest_browser(events, 100.0, max_delta=5.0)
        assert result is None


# ---------------------------------------------------------------------------
# DB loaders
# ---------------------------------------------------------------------------


def _create_test_db(tmp_path: Path, schema: str = "recording") -> Path:
    """Create a test SQLite DB with window_event and browser_event tables."""
    db_path = tmp_path / "recording.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    if schema == "recording":
        cur.execute("""
            CREATE TABLE recording (
                id INTEGER PRIMARY KEY, timestamp REAL
            )
        """)
        cur.execute("INSERT INTO recording (timestamp) VALUES (1000.0)")

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
    def test_loads_sorted_events(self, tmp_path):
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
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
        assert events[1].app_bundle_id == "com.apple.mail"

    def test_no_table(self, tmp_path):
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
        events = load_window_events(db_path)
        assert len(events) == 1


class TestLoadBrowserEvents:
    def test_loads_url_and_domain(self, tmp_path):
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        msg = json.dumps({"url": "https://mail.google.com/inbox", "type": "browser.click"})
        conn.execute(
            "INSERT INTO browser_event (timestamp, message) VALUES (1.0, ?)",
            (msg,),
        )
        conn.commit()
        conn.close()

        events = load_browser_events(db_path)
        assert len(events) == 1
        assert events[0].domain == "mail.google.com"
        assert events[0].url == "https://mail.google.com/inbox"

    def test_no_url_in_message_skipped(self, tmp_path):
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
# DefaultContextClassifier
# ---------------------------------------------------------------------------


class TestDefaultContextClassifier:
    def setup_method(self):
        self.classifier = DefaultContextClassifier()

    def test_known_app_bundle_id(self):
        meta = FrameMetadata(bundle_id="com.apple.mail", window_title="Inbox")
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "bundle_id"

    def test_password_manager_bundle(self):
        meta = FrameMetadata(bundle_id="com.1password.1password")
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.PASSWORD_MANAGER

    def test_browser_with_verified_domain(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="mail.google.com",
            window_title="Inbox - Gmail",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "domain"

    def test_browser_without_domain_is_unverified(self):
        meta = FrameMetadata(bundle_id="com.google.Chrome")
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED
        assert result.confidence == "bundle_id"

    def test_browser_unknown_domain_with_title_heuristic(self):
        meta = FrameMetadata(
            bundle_id="com.apple.Safari",
            domain="random-site.com",
            window_title="My Inbox - Some Mail",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "title"

    def test_browser_unknown_domain_no_title_is_unverified(self):
        meta = FrameMetadata(
            bundle_id="com.apple.Safari",
            domain="random-site.com",
            window_title="Random Page",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    def test_unknown_app_with_title_heuristic(self):
        meta = FrameMetadata(
            bundle_id="com.unknown.app",
            window_title="Bank of America - Account",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BANKING
        assert result.confidence == "title"

    def test_no_signal_produces_explicit_unknown(self):
        meta = FrameMetadata()
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.UNKNOWN
        assert result.confidence == "none"
        assert result.evidence == "no_matching_signal"

    def test_code_editor_bundle(self):
        meta = FrameMetadata(bundle_id="com.microsoft.VSCode")
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.CODE_EDITOR_TERMINAL

    def test_chat_app_bundle(self):
        meta = FrameMetadata(bundle_id="com.tinyspeck.slackmacgap")
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.CHAT

    def test_browser_with_admin_console_domain(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="console.aws.amazon.com",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.ADMIN_CONSOLE
        assert result.confidence == "domain"

    def test_bundle_id_takes_priority_over_domain(self):
        """Non-browser app with a domain should classify by bundle, not domain."""
        meta = FrameMetadata(
            bundle_id="com.apple.mail",
            domain="mail.google.com",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.EMAIL
        assert result.confidence == "bundle_id"

    def test_all_browser_bundle_ids_recognized(self):
        """Every bundle in BROWSER_BUNDLE_IDS should classify as browser_unverified
        when no domain or title signal is present."""
        for bid in BROWSER_BUNDLE_IDS:
            meta = FrameMetadata(bundle_id=bid)
            result = self.classifier.classify(meta)
            assert result.context_class == ContextClass.BROWSER_UNVERIFIED, (
                f"{bid} should be browser_unverified, got {result.context_class}"
            )


# ---------------------------------------------------------------------------
# TemporalContextClassifier (state machine)
# ---------------------------------------------------------------------------


class TestTemporalContextClassifier:
    def test_hold_previous_class_within_window(self):
        tc = TemporalContextClassifier(hold_seconds=3.0)

        # First frame: classified as email
        result1 = tc.classify(
            FrameMetadata(bundle_id="com.apple.mail", timestamp=10.0)
        )
        assert result1.context_class == ContextClass.EMAIL

        # Second frame 2s later: no signal, within hold window
        result2 = tc.classify(FrameMetadata(timestamp=12.0))
        assert result2.context_class == ContextClass.EMAIL
        assert result2.confidence == "temporal_hold"

    def test_decay_to_unknown_after_hold_expires(self):
        tc = TemporalContextClassifier(hold_seconds=3.0)

        tc.classify(FrameMetadata(bundle_id="com.apple.mail", timestamp=10.0))

        # 5 seconds later — beyond hold window
        result = tc.classify(FrameMetadata(timestamp=15.0))
        assert result.context_class == ContextClass.UNKNOWN

    def test_new_classification_resets_hold(self):
        tc = TemporalContextClassifier(hold_seconds=3.0)

        tc.classify(FrameMetadata(bundle_id="com.apple.mail", timestamp=10.0))
        # Switch to chat at t=12
        result = tc.classify(
            FrameMetadata(bundle_id="com.tinyspeck.slackmacgap", timestamp=12.0)
        )
        assert result.context_class == ContextClass.CHAT

        # t=14 — within hold from chat, not email
        result = tc.classify(FrameMetadata(timestamp=14.0))
        assert result.context_class == ContextClass.CHAT

    def test_deterministic_behavior(self):
        """Same inputs always produce same outputs."""
        for _ in range(3):
            tc = TemporalContextClassifier(hold_seconds=3.0)
            r1 = tc.classify(FrameMetadata(bundle_id="com.apple.mail", timestamp=10.0))
            r2 = tc.classify(FrameMetadata(timestamp=12.0))
            r3 = tc.classify(FrameMetadata(timestamp=20.0))
            assert r1.context_class == ContextClass.EMAIL
            assert r2.context_class == ContextClass.EMAIL
            assert r3.context_class == ContextClass.UNKNOWN

    def test_reset_clears_state(self):
        tc = TemporalContextClassifier(hold_seconds=3.0)
        tc.classify(FrameMetadata(bundle_id="com.apple.mail", timestamp=10.0))
        tc.reset()
        result = tc.classify(FrameMetadata(timestamp=11.0))
        assert result.context_class == ContextClass.UNKNOWN

    def test_zero_timestamp_no_hold(self):
        """Frames with timestamp=0 should not benefit from hold."""
        tc = TemporalContextClassifier(hold_seconds=3.0)
        tc.classify(FrameMetadata(bundle_id="com.apple.mail", timestamp=10.0))
        result = tc.classify(FrameMetadata(timestamp=0.0))
        assert result.context_class == ContextClass.UNKNOWN


# ---------------------------------------------------------------------------
# associate_screenshot integration
# ---------------------------------------------------------------------------


class TestAssociateScreenshot:
    def test_associates_window_and_browser(self):
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
        assert meta.window_title == ""

    def test_window_too_far_away(self):
        windows = [
            WindowContext(timestamp=1.0, app_bundle_id="com.apple.mail", title="Inbox"),
        ]
        meta = associate_screenshot(100.0, windows, [], max_delta=5.0)
        assert meta.bundle_id == ""

    def test_browser_without_window(self):
        browsers = [
            BrowserContext(timestamp=1.0, url="https://example.com", domain="example.com"),
        ]
        meta = associate_screenshot(1.0, [], browsers)
        assert meta.domain == "example.com"
        assert meta.bundle_id == ""
