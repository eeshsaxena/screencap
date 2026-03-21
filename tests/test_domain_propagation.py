"""Tests for domain propagation through the privacy pipeline (Phases 3-5).

Validates that browser_url → domain flows correctly through:
- Capture-time gating (RecorderPrivacyFilter)
- Export path (build_privacy_filter)
- Scrubber (associate_screenshot, _build_blocked_intervals, DB scrubbing)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from screencap.privacy.actions import PrivacyAction
from screencap.privacy.context import (
    WindowContext,
    associate_screenshot,
    load_window_events,
)
from screencap.privacy.policy import (
    PrivacyConfig,
    PrivacyMode,
)
from screencap.privacy.recorder_enforcement import RecorderPrivacyFilter


def _make_config(**kwargs) -> PrivacyConfig:
    defaults = dict(mode=PrivacyMode.PUBLIC)
    defaults.update(kwargs)
    return PrivacyConfig(**defaults)


# ---------------------------------------------------------------------------
# Phase 3: Capture-time domain propagation
# ---------------------------------------------------------------------------


class TestRecorderDomainPropagation:
    """RecorderPrivacyFilter reads browser_url and classifies by domain."""

    def test_browser_on_safe_domain_allowed(self):
        """Chrome on github.com → CODE_EDITOR_TERMINAL → OCR_FALLBACK (public).
        Non-cloud OCR_FALLBACK passes through."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
            cloud_intent=False,
        )
        f.on_window_event({
            "app_bundle_id": "com.google.Chrome",
            "title": "GitHub",
            "browser_url": "https://github.com/user/repo",
        })
        # github.com → CODE_EDITOR_TERMINAL → OCR_FALLBACK in public
        # Non-cloud: OCR_FALLBACK is not in the block set → allowed
        assert f.is_screen_allowed() is True

    def test_browser_on_banking_domain_excluded(self):
        """Chrome on chase.com → BANKING → EXCLUDE (public)."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )
        f.on_window_event({
            "app_bundle_id": "com.google.Chrome",
            "title": "Chase Online",
            "browser_url": "https://chase.com/dashboard",
        })
        assert f.is_screen_allowed() is False

    def test_browser_on_email_domain_masked(self):
        """Chrome on mail.google.com → EMAIL → MASK_WINDOW (public) → blocked."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )
        f.on_window_event({
            "app_bundle_id": "com.google.Chrome",
            "title": "Inbox - Gmail",
            "browser_url": "https://mail.google.com/mail/u/0/#inbox",
        })
        assert f.is_screen_allowed() is False

    def test_browser_no_url_falls_to_unverified(self):
        """Chrome with no browser_url → BROWSER_UNVERIFIED → MASK_WINDOW (public)."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )
        f.on_window_event({
            "app_bundle_id": "com.google.Chrome",
            "title": "Some Page",
        })
        # No browser_url → domain=None → BROWSER_UNVERIFIED → MASK_WINDOW → blocked
        assert f.is_screen_allowed() is False


# ---------------------------------------------------------------------------
# Phase 4: Export path domain propagation
# ---------------------------------------------------------------------------


def _mock_privacy_config():
    """Patch get_privacy_config to return a clean public-mode config."""
    return patch(
        "screencap.config.get_privacy_config",
        return_value=PrivacyConfig(mode=PrivacyMode.PUBLIC),
    )


class TestExportDomainPropagation:
    """build_privacy_filter reads event.domain for classification."""

    def test_filter_uses_domain_for_classification(self):
        """Export privacy filter reads domain from WindowSwitchEvent."""
        from screencap.exporter import build_privacy_filter
        from screencap.engine.events import WindowSwitchEvent

        with _mock_privacy_config():
            pf = build_privacy_filter(privacy_mode="public", cloud_intent=False)

        # Browser on github.com with domain → CODE_EDITOR_TERMINAL
        event = WindowSwitchEvent(
            timestamp=1.0,
            app_name="Chrome",
            app_bundle_id="com.google.Chrome",
            window_title="GitHub",
            window_id="1",
            x=0, y=0, width=800, height=600,
            domain="github.com",
        )
        result = pf(event)
        # github.com → CODE_EDITOR_TERMINAL → OCR_FALLBACK in public
        # Non-cloud → not suppressed
        assert result is not None
        assert result.domain == "github.com"

    def test_filter_excludes_banking_domain(self):
        """Export privacy filter suppresses events for EXCLUDE domains."""
        from screencap.exporter import build_privacy_filter
        from screencap.engine.events import WindowSwitchEvent

        with _mock_privacy_config():
            pf = build_privacy_filter(privacy_mode="public", cloud_intent=False)

        event = WindowSwitchEvent(
            timestamp=1.0,
            app_name="Chrome",
            app_bundle_id="com.google.Chrome",
            window_title="Chase",
            window_id="1",
            x=0, y=0, width=800, height=600,
            domain="chase.com",
        )
        result = pf(event)
        assert result is None  # EXCLUDE → suppressed

    def test_filter_masks_email_domain(self):
        """MASK_WINDOW domains get title masked and domain set to None."""
        from screencap.exporter import build_privacy_filter
        from screencap.engine.events import WindowSwitchEvent

        with _mock_privacy_config():
            pf = build_privacy_filter(privacy_mode="public", cloud_intent=False)

        event = WindowSwitchEvent(
            timestamp=1.0,
            app_name="Chrome",
            app_bundle_id="com.google.Chrome",
            window_title="Inbox - Gmail",
            window_id="1",
            x=0, y=0, width=800, height=600,
            domain="mail.google.com",
        )
        result = pf(event)
        assert result is not None
        assert result.window_title == "Chrome"  # masked to app_name
        assert result.domain is None  # domain suppressed for MASK_WINDOW

    def test_filter_no_domain_falls_to_unverified(self):
        """Browser without domain → BROWSER_UNVERIFIED → MASK_WINDOW."""
        from screencap.exporter import build_privacy_filter
        from screencap.engine.events import WindowSwitchEvent

        with _mock_privacy_config():
            pf = build_privacy_filter(privacy_mode="public", cloud_intent=False)

        event = WindowSwitchEvent(
            timestamp=1.0,
            app_name="Chrome",
            app_bundle_id="com.google.Chrome",
            window_title="Some Page",
            window_id="1",
            x=0, y=0, width=800, height=600,
            domain=None,
        )
        result = pf(event)
        # BROWSER_UNVERIFIED → MASK_WINDOW → title masked, domain None
        assert result is not None
        assert result.window_title == "Chrome"
        assert result.domain is None


# ---------------------------------------------------------------------------
# Phase 5: Scrubber domain propagation
# ---------------------------------------------------------------------------


def _create_recording_db(db_path: Path, *, with_browser_url: bool = True) -> None:
    """Create a minimal recording.db for testing."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description TEXT)")
    conn.execute("INSERT INTO recording VALUES (1, 'test')")

    if with_browser_url:
        conn.execute("""
            CREATE TABLE window_event (
                id INTEGER PRIMARY KEY,
                timestamp REAL,
                app_bundle_id TEXT,
                title TEXT,
                window_id TEXT,
                state TEXT,
                left INTEGER,
                top INTEGER,
                width INTEGER,
                height INTEGER,
                browser_url TEXT
            )
        """)
    else:
        conn.execute("""
            CREATE TABLE window_event (
                id INTEGER PRIMARY KEY,
                timestamp REAL,
                app_bundle_id TEXT,
                title TEXT,
                window_id TEXT,
                state TEXT,
                left INTEGER,
                top INTEGER,
                width INTEGER,
                height INTEGER
            )
        """)

    conn.execute("""
        CREATE TABLE action_event (
            id INTEGER PRIMARY KEY,
            timestamp REAL,
            name TEXT,
            key_char TEXT,
            canonical_key_char TEXT,
            key_name TEXT,
            canonical_key_name TEXT,
            key_vk TEXT,
            canonical_key_vk TEXT,
            text TEXT,
            element_state TEXT,
            active_segment_description TEXT,
            available_segment_descriptions TEXT,
            mouse_x REAL,
            mouse_y REAL,
            mouse_button_name TEXT,
            mouse_pressed INTEGER,
            mouse_dx REAL,
            mouse_dy REAL,
            mouse_pressure REAL,
            modifier_flags INTEGER,
            scroll_phase INTEGER,
            momentum_phase INTEGER,
            is_continuous INTEGER
        )
    """)
    conn.commit()
    conn.close()


class TestLoadWindowEventsWithDomain:
    """load_window_events reads browser_url and extracts domain."""

    def test_loads_domain_from_browser_url(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _create_recording_db(db_path, with_browser_url=True)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id, browser_url) "
            "VALUES (1.0, 'com.google.Chrome', 'GitHub', '1', 'https://github.com/user/repo')"
        )
        conn.commit()
        conn.close()

        events = load_window_events(db_path)
        assert len(events) == 1
        assert events[0].domain == "github.com"

    def test_null_browser_url_gives_none_domain(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _create_recording_db(db_path, with_browser_url=True)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id, browser_url) "
            "VALUES (1.0, 'com.google.Chrome', 'Page', '1', NULL)"
        )
        conn.commit()
        conn.close()

        events = load_window_events(db_path)
        assert len(events) == 1
        assert events[0].domain is None

    def test_old_db_without_browser_url_column(self, tmp_path):
        """Old recordings without browser_url column → domain=None, no crash."""
        db_path = tmp_path / "recording.db"
        _create_recording_db(db_path, with_browser_url=False)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id) "
            "VALUES (1.0, 'com.google.Chrome', 'Page', '1')"
        )
        conn.commit()
        conn.close()

        events = load_window_events(db_path)
        assert len(events) == 1
        assert events[0].domain is None


class TestAssociateScreenshotDomain:
    """associate_screenshot passes domain through to FrameMetadata."""

    def test_domain_propagated_to_metadata(self):
        events = [
            WindowContext(
                timestamp=1.0,
                app_bundle_id="com.google.Chrome",
                title="GitHub",
                domain="github.com",
            ),
        ]
        meta = associate_screenshot(1.5, events)
        assert meta.domain == "github.com"

    def test_no_matching_window_gives_none_domain(self):
        meta = associate_screenshot(1.5, [])
        assert meta.domain is None


class TestBuildBlockedIntervalsDomain:
    """_build_blocked_intervals uses domain from WindowContext."""

    def test_banking_domain_produces_blocked_interval(self):
        from screencap.privacy.context import DefaultContextClassifier
        from screencap.privacy.policy import DefaultPolicyEvaluator
        from screencap.scrubber import _build_blocked_intervals

        config = _make_config()
        evaluator = DefaultPolicyEvaluator(config)
        classifier = DefaultContextClassifier()

        events = [
            WindowContext(
                timestamp=1.0,
                app_bundle_id="com.google.Chrome",
                title="Chase",
                domain="chase.com",
            ),
            WindowContext(
                timestamp=5.0,
                app_bundle_id="com.microsoft.VSCode",
                title="main.py",
            ),
        ]

        intervals = _build_blocked_intervals(events, evaluator, classifier)
        assert len(intervals) == 1
        assert intervals[0].start == 1.0
        assert intervals[0].end == 5.0
        assert intervals[0].action == PrivacyAction.EXCLUDE

    def test_safe_domain_no_blocked_interval(self):
        from screencap.privacy.context import DefaultContextClassifier
        from screencap.privacy.policy import DefaultPolicyEvaluator
        from screencap.scrubber import _build_blocked_intervals

        config = _make_config()
        evaluator = DefaultPolicyEvaluator(config)
        classifier = DefaultContextClassifier()

        events = [
            WindowContext(
                timestamp=1.0,
                app_bundle_id="com.google.Chrome",
                title="GitHub",
                domain="github.com",
            ),
        ]

        intervals = _build_blocked_intervals(events, evaluator, classifier)
        # github.com → CODE_EDITOR_TERMINAL → OCR_FALLBACK (not in BLOCK_ACTIONS)
        assert len(intervals) == 0


class TestNullDbBrowserUrl:
    """_null_db_rows_for_intervals NULLs browser_url during blocked intervals."""

    def test_browser_url_nulled_in_blocked_interval(self, tmp_path):
        from screencap.scrubber import ScrubResult, _BlockedInterval, _null_db_rows_for_intervals

        db_path = tmp_path / "recording.db"
        _create_recording_db(db_path, with_browser_url=True)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id, browser_url) "
            "VALUES (2.0, 'com.google.Chrome', 'Chase', '1', 'https://chase.com/dashboard')"
        )
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id, browser_url) "
            "VALUES (6.0, 'com.google.Chrome', 'GitHub', '2', 'https://github.com')"
        )
        conn.commit()
        conn.close()

        intervals = [
            _BlockedInterval(
                start=1.0, end=5.0,
                action=PrivacyAction.EXCLUDE,
                reason="test",
            ),
        ]

        result = ScrubResult(output_dir=tmp_path)
        _null_db_rows_for_intervals(tmp_path, intervals, result)

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT timestamp, title, browser_url FROM window_event ORDER BY timestamp"
        ).fetchall()
        conn.close()

        # Row at ts=2.0 is in the blocked interval → title and browser_url NULLed
        assert rows[0][1] is None  # title
        assert rows[0][2] is None  # browser_url

        # Row at ts=6.0 is outside → preserved
        assert rows[1][1] == "GitHub"
        assert rows[1][2] == "https://github.com"

    def test_old_db_without_browser_url_no_crash(self, tmp_path):
        """Old DBs without browser_url column don't crash."""
        from screencap.scrubber import ScrubResult, _BlockedInterval, _null_db_rows_for_intervals

        db_path = tmp_path / "recording.db"
        _create_recording_db(db_path, with_browser_url=False)

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id) "
            "VALUES (2.0, 'com.google.Chrome', 'Page', '1')"
        )
        conn.commit()
        conn.close()

        intervals = [
            _BlockedInterval(
                start=1.0, end=5.0,
                action=PrivacyAction.EXCLUDE,
                reason="test",
            ),
        ]

        result = ScrubResult(output_dir=tmp_path)
        # Should not raise
        _null_db_rows_for_intervals(tmp_path, intervals, result)


class TestNullEventContentWindowSwitch:
    """_null_event_content nulls window_title and domain on window.switch events."""

    def test_window_switch_fields_nulled(self):
        from screencap.scrubber import _null_event_content

        event = {
            "type": "window.switch",
            "timestamp": 1.0,
            "app_bundle_id": "com.google.Chrome",
            "window_title": "Chase Online",
            "domain": "chase.com",
        }
        _null_event_content(event)
        assert event["window_title"] is None
        assert event["domain"] is None
        # Non-content fields preserved
        assert event["app_bundle_id"] == "com.google.Chrome"
        assert event["timestamp"] == 1.0

    def test_non_window_event_unchanged(self):
        from screencap.scrubber import _null_event_content

        event = {
            "type": "key.type",
            "timestamp": 1.0,
            "text": "hello",
        }
        _null_event_content(event)
        # text is in KEYSTROKE_CONTENT_FIELDS → nulled
        assert event["text"] is None
        # No window_title/domain keys added
        assert "window_title" not in event
        assert "domain" not in event


