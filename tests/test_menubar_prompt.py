"""Tests for the first-seen prompt state machine.

These tests cover the pure-Python ``PromptState`` class extracted from
``screencap.menubar``. They do NOT touch AppKit — see the module-level
docstring in ``menubar.py`` for the rationale (NSPanel headless tests
are too brittle to be worth running in pytest).
"""

from __future__ import annotations

import pytest

from screencap.menubar import PromptDecision, PromptState


@pytest.fixture
def state() -> PromptState:
    return PromptState()


def _decision(
    bundle_id: str = "com.example.app",
    domain: str | None = None,
    app_name: str = "Example App",
) -> PromptDecision:
    return PromptDecision(bundle_id=bundle_id, domain=domain, app_name=app_name)


# ---------------------------------------------------------------------------
# should_prompt
# ---------------------------------------------------------------------------


class TestShouldPrompt:
    def test_first_seen_returns_true(self, state):
        assert state.should_prompt(
            "com.tinyspeck.slackmacgap", None, "allow",
            is_password_manager=False, is_browser_header=False,
        ) is True

    def test_dedup_same_key(self, state):
        state.enqueue(_decision("com.tinyspeck.slackmacgap"))
        assert state.should_prompt(
            "com.tinyspeck.slackmacgap", None, "allow",
            is_password_manager=False, is_browser_header=False,
        ) is False

    def test_skip_password_manager(self, state):
        assert state.should_prompt(
            "com.1password.1password", None, "exclude",
            is_password_manager=True, is_browser_header=False,
        ) is False

    def test_skip_browser_header(self, state):
        assert state.should_prompt(
            "com.google.Chrome", None, "allow",
            is_password_manager=False, is_browser_header=True,
        ) is False

    def test_skip_already_excluded(self, state):
        assert state.should_prompt(
            "com.example.app", None, "exclude",
            is_password_manager=False, is_browser_header=False,
        ) is False

    def test_skip_already_masked(self, state):
        # mask_window is in EXCLUDED_ACTION_VALUES (see actions.py)
        assert state.should_prompt(
            "com.example.app", None, "mask_window",
            is_password_manager=False, is_browser_header=False,
        ) is False

    def test_skip_empty_bundle_id(self, state):
        assert state.should_prompt(
            "", None, "allow",
            is_password_manager=False, is_browser_header=False,
        ) is False

    def test_browser_tab_distinct_from_app(self, state):
        """A `(chrome, github.com)` key is independent of bare `chrome`."""
        state.enqueue(_decision("com.google.Chrome", domain=None))
        # Bare app already prompted, but a domain entry is a new key
        assert state.should_prompt(
            "com.google.Chrome", "github.com", "allow",
            is_password_manager=False, is_browser_header=False,
        ) is True


# ---------------------------------------------------------------------------
# enqueue / pop_next
# ---------------------------------------------------------------------------


class TestEnqueueAndPop:
    def test_enqueue_marks_prompted(self, state):
        d = _decision("com.example.app")
        state.enqueue(d)
        assert state.already_prompted(d.key) is True

    def test_pop_next_returns_enqueued(self, state):
        d = _decision("com.example.app")
        state.enqueue(d)
        assert state.pop_next(current_frontmost_key=d.key) == d

    def test_pop_next_empty_returns_none(self, state):
        assert state.pop_next() is None

    def test_pop_next_drops_stale(self, state):
        """If the user has switched away, the queued prompt is dropped."""
        d = _decision("com.example.app")
        state.enqueue(d)
        # Frontmost is now a different key
        result = state.pop_next(current_frontmost_key="com.other.app")
        assert result is None

    def test_pop_next_no_frontmost_key_returns_decision(self, state):
        """When current_frontmost_key=None, no staleness check is done."""
        d = _decision("com.example.app")
        state.enqueue(d)
        assert state.pop_next(current_frontmost_key=None) == d

    def test_pop_next_skips_stale_returns_fresh(self, state):
        stale = _decision("com.first.app")
        fresh = _decision("com.second.app")
        state.enqueue(stale)
        state.enqueue(fresh)
        result = state.pop_next(current_frontmost_key=fresh.key)
        assert result == fresh
        # Queue is now empty (stale was dropped)
        assert state.has_pending() is False


# ---------------------------------------------------------------------------
# Capacity / burst
# ---------------------------------------------------------------------------


class TestCapacity:
    def test_max_pending_caps_queue(self):
        state = PromptState(max_pending=3)
        for i in range(5):
            state.enqueue(_decision(f"com.app.{i}"))
        # Only the last 3 remain in pending
        kept = []
        while state.has_pending():
            d = state.pop_next()
            if d:
                kept.append(d.bundle_id)
        assert kept == ["com.app.2", "com.app.3", "com.app.4"]

    def test_burst_marks_all_prompted_even_after_dropping(self):
        """All bursted keys stay in `_prompted` even if dropped from FIFO."""
        state = PromptState(max_pending=2)
        for i in range(5):
            state.enqueue(_decision(f"com.app.{i}"))
        # Even the dropped entries should NOT re-prompt
        for i in range(5):
            assert state.should_prompt(
                f"com.app.{i}", None, "allow",
                is_password_manager=False, is_browser_header=False,
            ) is False


# ---------------------------------------------------------------------------
# PromptDecision
# ---------------------------------------------------------------------------


class TestPromptDecision:
    def test_key_for_native_app(self):
        d = PromptDecision(
            bundle_id="com.tinyspeck.slackmacgap",
            domain=None,
            app_name="Slack",
        )
        assert d.key == "com.tinyspeck.slackmacgap"

    def test_key_for_browser_tab(self):
        d = PromptDecision(
            bundle_id="com.google.Chrome",
            domain="chase.com",
            app_name="Google Chrome",
        )
        assert d.key == "com.google.Chrome::chase.com"
