"""Tests for the menu bar app list feature.

Covers: extract_root_domain(), runtime overrides in RecorderPrivacyFilter,
override persistence via .menubar_overrides.json, and build_privacy_filter()
override loading.
"""

from __future__ import annotations

import json
import multiprocessing
import time

from screencap.privacy.domain_loader import extract_root_domain
from screencap.privacy.policy import PrivacyConfig, PrivacyMode
from screencap.privacy.recorder_enforcement import RecorderPrivacyFilter

# ---------------------------------------------------------------------------
# extract_root_domain()
# ---------------------------------------------------------------------------


class TestExtractRootDomain:
    def test_simple_domain(self):
        assert extract_root_domain("chase.com") == "chase.com"

    def test_www_subdomain(self):
        assert extract_root_domain("www.chase.com") == "chase.com"

    def test_deep_subdomain(self):
        assert extract_root_domain("secure.login.chase.com") == "chase.com"

    def test_co_uk_tld(self):
        assert extract_root_domain("login.bank.co.uk") == "bank.co.uk"

    def test_com_au_tld(self):
        assert extract_root_domain("www.example.com.au") == "example.com.au"

    def test_single_label(self):
        assert extract_root_domain("localhost") == "localhost"

    def test_trailing_dot(self):
        assert extract_root_domain("www.example.com.") == "example.com"

    def test_uppercase_normalized(self):
        assert extract_root_domain("WWW.GitHub.COM") == "github.com"

    def test_two_part_domain(self):
        assert extract_root_domain("github.com") == "github.com"

    def test_org_br_tld(self):
        assert extract_root_domain("app.example.org.br") == "example.org.br"


# ---------------------------------------------------------------------------
# Runtime overrides in RecorderPrivacyFilter
# ---------------------------------------------------------------------------


def _make_config(**kwargs) -> PrivacyConfig:
    defaults = dict(mode=PrivacyMode.PUBLIC)
    defaults.update(kwargs)
    return PrivacyConfig(**defaults)


class TestRuntimeOverrides:
    def test_override_excludes_allowed_app(self):
        """A runtime override can exclude an app that policy would allow."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )
        # Manually set an override
        f._runtime_overrides["com.microsoft.VSCode"] = "exclude"

        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "title": "main.py",
        })

        assert f.is_screen_allowed() is False

    def test_override_allows_excluded_app(self):
        """A runtime override can allow an app that policy would exclude."""
        config = _make_config(
            exclude_apps=frozenset({"com.tinyspeck.slackmacgap"}),
        )
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )
        f._runtime_overrides["com.tinyspeck.slackmacgap"] = "allow"

        f.on_window_event({
            "app_bundle_id": "com.tinyspeck.slackmacgap",
            "title": "Slack",
        })

        assert f.is_screen_allowed() is True

    def test_domain_level_override(self):
        """A runtime override can target a specific browser domain."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )
        # Override chase.com to allow (policy would EXCLUDE for banking)
        f._runtime_overrides["com.google.Chrome::chase.com"] = "allow"

        f.on_window_event({
            "app_bundle_id": "com.google.Chrome",
            "title": "Chase Bank",
            "browser_url": "https://secure.chase.com/accounts",
        })

        assert f.is_screen_allowed() is True

    def test_app_level_fallback_for_domain(self):
        """When no domain-specific override exists, falls back to app-level."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )
        f._runtime_overrides["com.google.Chrome"] = "exclude"

        f.on_window_event({
            "app_bundle_id": "com.google.Chrome",
            "title": "GitHub",
            "browser_url": "https://github.com/somerepo",
        })

        assert f.is_screen_allowed() is False

    def test_domain_override_takes_precedence_over_app(self):
        """Domain-specific override wins over app-level override."""
        config = _make_config()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
        )
        f._runtime_overrides["com.google.Chrome"] = "exclude"
        f._runtime_overrides["com.google.Chrome::github.com"] = "allow"

        f.on_window_event({
            "app_bundle_id": "com.google.Chrome",
            "title": "GitHub",
            "browser_url": "https://github.com/somerepo",
        })

        assert f.is_screen_allowed() is True

    def test_poll_overrides_drains_queue(self):
        """poll_overrides() reads from the queue and updates the dict."""
        config = _make_config()
        q = multiprocessing.Queue()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
            override_q=q,
        )

        q.put({"key": "com.microsoft.VSCode", "action": "exclude"})
        # Give the queue a moment to be readable
        time.sleep(0.05)

        f.poll_overrides()

        assert f._runtime_overrides.get("com.microsoft.VSCode") == "exclude"

    def test_poll_overrides_writes_file(self, tmp_path):
        """poll_overrides() persists overrides to the session file."""
        config = _make_config()
        q = multiprocessing.Queue()
        override_file = tmp_path / ".menubar_overrides.json"
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
            override_q=q, override_file=override_file,
        )

        q.put({"key": "com.microsoft.VSCode", "action": "exclude"})
        time.sleep(0.05)
        f.poll_overrides()

        assert override_file.exists()
        data = json.loads(override_file.read_text())
        assert data["com.microsoft.VSCode"] == "exclude"

    def test_window_feed_queue_receives_events(self):
        """on_window_event() feeds a summary to the window feed queue."""
        config = _make_config()
        feed_q = multiprocessing.Queue()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
            window_feed_q=feed_q,
        )

        f.on_window_event({
            "app_bundle_id": "com.microsoft.VSCode",
            "app_name": "Visual Studio Code",
            "title": "main.py",
        })

        evt = feed_q.get(timeout=1.0)
        assert evt["bundle_id"] == "com.microsoft.VSCode"
        assert evt["app_name"] == "Visual Studio Code"
        assert evt["action"] in ("allow", "text_redact")
        assert evt["domain"] is None

    def test_window_feed_includes_root_domain(self):
        """Window feed collapses subdomains to root domain."""
        config = _make_config()
        feed_q = multiprocessing.Queue()
        f = RecorderPrivacyFilter(
            config, transition_hold_seconds=0.0, secure_input_fn=None,
            window_feed_q=feed_q,
        )

        f.on_window_event({
            "app_bundle_id": "com.google.Chrome",
            "app_name": "Google Chrome",
            "title": "Chase",
            "browser_url": "https://secure.chase.com/accounts",
        })

        evt = feed_q.get(timeout=1.0)
        assert evt["domain"] == "chase.com"


# ---------------------------------------------------------------------------
# build_privacy_filter() with overrides
# ---------------------------------------------------------------------------


class TestBuildPrivacyFilterOverrides:
    def test_loads_overrides_from_file(self, tmp_path):
        """build_privacy_filter() loads .menubar_overrides.json if present."""
        from screencap.exporter import build_privacy_filter

        overrides = {"com.tinyspeck.slackmacgap": "allow"}
        (tmp_path / ".menubar_overrides.json").write_text(json.dumps(overrides))

        pf = build_privacy_filter(
            privacy_mode="public",
            capture_dir=tmp_path,
        )

        # Create a mock event-like object
        class FakeEvent:
            app_bundle_id = "com.tinyspeck.slackmacgap"
            app_name = "Slack"
            window_title = "general - Slack"
            domain = None
            timestamp = time.time()

        result = pf(FakeEvent())
        # Should be allowed (override says "allow"), not excluded
        assert result is not None

    def test_override_exclude_returns_none(self, tmp_path):
        """build_privacy_filter() returns None for override-excluded events."""
        from screencap.exporter import build_privacy_filter

        overrides = {"com.microsoft.VSCode": "exclude"}
        (tmp_path / ".menubar_overrides.json").write_text(json.dumps(overrides))

        pf = build_privacy_filter(
            privacy_mode="internal",
            capture_dir=tmp_path,
        )

        class FakeEvent:
            app_bundle_id = "com.microsoft.VSCode"
            app_name = "Visual Studio Code"
            window_title = "main.py"
            domain = None
            timestamp = time.time()

        result = pf(FakeEvent())
        assert result is None

    def test_no_override_file_is_fine(self, tmp_path):
        """build_privacy_filter() works normally when no overrides file exists."""
        from screencap.exporter import build_privacy_filter

        pf = build_privacy_filter(
            privacy_mode="internal",
            capture_dir=tmp_path,
        )

        class FakeEvent:
            app_bundle_id = "com.microsoft.VSCode"
            app_name = "Visual Studio Code"
            window_title = "main.py"
            domain = None
            timestamp = time.time()

        result = pf(FakeEvent())
        # VSCode in internal mode → ALLOW
        assert result is not None
