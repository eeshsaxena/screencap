"""Tests for privacy v3 Phase 2: context association and classification."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from screencap.privacy.context import (
    BROWSER_BUNDLE_IDS,
    DefaultContextClassifier,
    WindowContext,
    BUNDLE_ID_MAP,
    associate_screenshot,
    find_nearest_window,
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
        (1.3, 1.0),  # after first event, before second
        (2.8, 1.0),  # still before second event — latest-at-or-before is first
        (3.5, 3.0),  # after second event — latest-at-or-before is second
    ])
    def test_picks_latest_at_or_before(self, target, expected_ts):
        events = self._make_events([1.0, 3.0])
        result = find_nearest_window(events, target)
        assert result is not None
        assert result.timestamp == expected_ts

    def test_rejects_target_before_all_events(self):
        events = self._make_events([5.0, 10.0])
        assert find_nearest_window(events, 1.0) is None

    def test_rejects_events_beyond_max_delta(self):
        events = self._make_events([1.0])
        assert find_nearest_window(events, 10.0, max_delta=5.0) is None


# ---------------------------------------------------------------------------
# DB loaders (integration with real SQLite)
# ---------------------------------------------------------------------------


def _create_test_db(tmp_path: Path) -> Path:
    """Create a test SQLite DB with window_event table."""
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

    # Data integrity: expanded bundle ID map spot checks
    @pytest.mark.parametrize("bundle_id, expected", [
        ("org.keepassxc.keepassxc", ContextClass.PASSWORD_MANAGER),
        ("org.whispersystems.signal-desktop", ContextClass.CHAT),
        ("io.alacritty", ContextClass.CODE_EDITOR_TERMINAL),
        ("com.mitchellh.ghostty", ContextClass.CODE_EDITOR_TERMINAL),
        ("com.apple.Passwords", ContextClass.PASSWORD_MANAGER),
        ("com.apple.FaceTime", ContextClass.VIDEO_CALL),
        ("org.mozilla.thunderbird", ContextClass.EMAIL),
        ("com.tableplus.TablePlus", ContextClass.ADMIN_CONSOLE),
    ])
    def test_expanded_bundle_id_map(self, bundle_id, expected):
        meta = FrameMetadata(bundle_id=bundle_id)
        result = self.classifier.classify(meta)
        assert result.context_class == expected

    # Data integrity: bundle ID map and browser IDs must be disjoint
    def test_bundle_id_map_disjoint_from_browser_ids(self):
        overlap = set(BUNDLE_ID_MAP.keys()) & BROWSER_BUNDLE_IDS
        assert overlap == set(), (
            f"Bundle IDs in both BUNDLE_ID_MAP and BROWSER_BUNDLE_IDS would "
            f"never reach the browser path: {overlap}"
        )


# ---------------------------------------------------------------------------
# Domain classification via loaded index (UT1 + supplement)
# ---------------------------------------------------------------------------


class TestDomainClassification:
    """Test domain lookup using the loaded domain index."""

    def setup_method(self):
        self.classifier = DefaultContextClassifier()

    @pytest.mark.parametrize("domain, expected", [
        # UT1 banks
        ("chase.com", ContextClass.BANKING),
        ("wellsfargo.com", ContextClass.BANKING),
        # Supplement entries
        ("vault.bitwarden.com", ContextClass.PASSWORD_MANAGER),
        ("github.com", ContextClass.CODE_EDITOR_TERMINAL),
        ("drive.google.com", ContextClass.CLOUD_STORAGE),
        ("app.slack.com", ContextClass.CHAT),
        ("meet.google.com", ContextClass.VIDEO_CALL),
        ("calendar.google.com", ContextClass.CALENDAR),
        ("console.aws.amazon.com", ContextClass.ADMIN_CONSOLE),
        # Supplement email gap-fills
        ("outlook.live.com", ContextClass.EMAIL),
        ("mail.proton.me", ContextClass.EMAIL),
    ])
    def test_known_domains(self, domain, expected):
        meta = FrameMetadata(bundle_id="com.google.Chrome", domain=domain)
        result = self.classifier.classify(meta)
        assert result.context_class == expected
        assert result.confidence == "domain"

    def test_parent_domain_expansion(self):
        """Child domain should inherit parent's classification."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="online.banking.chase.com",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BANKING

    def test_trailing_dot_normalized(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="chase.com.",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BANKING

    def test_case_insensitive_domain(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="Chase.Com",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BANKING

    def test_unknown_domain_falls_through(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="docs.example.com",
        )
        result = self.classifier.classify(meta)
        # Unknown domain, no title → browser_unverified
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED


# ---------------------------------------------------------------------------
# Keyword detection (auth/payment flows)
# ---------------------------------------------------------------------------


class TestKeywordDetection:
    """Test URL path and subdomain keyword matching."""

    def setup_method(self):
        self.classifier = DefaultContextClassifier()

    # Auth keywords in URL path
    @pytest.mark.parametrize("path, expected", [
        ("/login", ContextClass.AUTH_FLOW),
        ("/signin", ContextClass.AUTH_FLOW),
        ("/sign-in", ContextClass.AUTH_FLOW),
        ("/auth", ContextClass.AUTH_FLOW),
        ("/oauth", ContextClass.AUTH_FLOW),
        ("/sso", ContextClass.AUTH_FLOW),
        ("/mfa", ContextClass.AUTH_FLOW),
        ("/2fa", ContextClass.AUTH_FLOW),
        ("/verify", ContextClass.AUTH_FLOW),
        ("/recovery", ContextClass.AUTH_FLOW),
        ("/forgot-password", ContextClass.AUTH_FLOW),
        ("/reset-password", ContextClass.AUTH_FLOW),
    ])
    def test_auth_keywords_in_path(self, path, expected):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url=f"https://example.com{path}",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == expected

    # Payment keywords in URL path
    @pytest.mark.parametrize("path", [
        "/checkout", "/billing", "/payment", "/pay", "/subscribe",
    ])
    def test_payment_keywords_in_path(self, path):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url=f"https://example.com{path}",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.PAYMENT_FLOW

    # Auth keywords in subdomain
    def test_auth_keyword_in_subdomain(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="login.example.com",
            browser_url="https://login.example.com/",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    def test_auth_subdomain_with_co_uk_tld(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="auth.payments.example.co.uk",
            browser_url="https://auth.payments.example.co.uk/",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    # First-segment-only matching
    def test_first_segment_only(self):
        """Only the first path segment triggers keyword matching."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/blog/login",
        )
        result = self.classifier.classify(meta)
        # First segment is "blog", not "login"
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    # No substring matching
    def test_no_substring_matching(self):
        """Token 'blogindex' should NOT match 'login'."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/blogindex",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    # URL decoding
    def test_url_decoded_path(self):
        """URL-encoded path segments should be decoded before matching."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/%6Cogin",  # 'login' encoded
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    # Segment tokenization
    def test_segment_tokenization(self):
        """Hyphenated segments should be tokenized: login-callback → ['login', 'callback']."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/login-callback",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    # Combined domain + keyword (stricter wins)
    def test_domain_plus_keyword_stricter_wins(self):
        """chase.com/login: BANKING + AUTH_FLOW → stricter action wins."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="chase.com",
            browser_url="https://chase.com/login",
        )
        result = self.classifier.classify(meta)
        # AUTH_FLOW is EXCLUDE in public, BANKING is also EXCLUDE in public.
        # In internal mode: AUTH_FLOW is MASK_WINDOW, BANKING is MASK_WINDOW.
        # Either way, stricter wins. The combined confidence should show both.
        assert result.confidence == "domain+keyword"

    def test_github_login_picks_auth_flow(self):
        """github.com/login: CODE_EDITOR_TERMINAL (OCR_FALLBACK in public) vs AUTH_FLOW (EXCLUDE) → AUTH_FLOW wins."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="github.com",
            browser_url="https://github.com/login",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.AUTH_FLOW

    # No keyword match for regular paths
    def test_no_match_for_regular_path(self):
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="example.com",
            browser_url="https://example.com/about",
        )
        result = self.classifier.classify(meta)
        assert result.context_class == ContextClass.BROWSER_UNVERIFIED

    # Without browser_url, only subdomain keywords work
    def test_subdomain_keyword_without_browser_url(self):
        """If no browser_url, subdomain labels still checked via domain field."""
        meta = FrameMetadata(
            bundle_id="com.google.Chrome",
            domain="login.example.com",
        )
        result = self.classifier.classify(meta)
        # Without browser_url, path is empty but subdomain "login" matches
        assert result.context_class == ContextClass.AUTH_FLOW


# ---------------------------------------------------------------------------
# associate_screenshot — integration
# ---------------------------------------------------------------------------


class TestAssociateScreenshot:
    def test_correlates_window_by_timestamp(self):
        windows = [
            WindowContext(timestamp=1.0, app_bundle_id="com.google.Chrome", title="Gmail"),
        ]
        meta = associate_screenshot(1.5, windows)
        assert meta.bundle_id == "com.google.Chrome"
        assert meta.domain is None
        assert meta.timestamp == 1.5

    def test_no_events_returns_empty_metadata(self):
        meta = associate_screenshot(1.0, [])
        assert meta.bundle_id == ""
        assert meta.domain is None

    def test_non_browser_window(self):
        """Switching from Chrome to Finder picks up the correct window."""
        windows = [
            WindowContext(timestamp=8.0, app_bundle_id="com.google.Chrome", title="Gmail"),
            WindowContext(timestamp=10.0, app_bundle_id="com.apple.Finder", title="Documents"),
        ]
        meta = associate_screenshot(10.0, windows)
        assert meta.bundle_id == "com.apple.Finder"
        assert meta.domain is None
