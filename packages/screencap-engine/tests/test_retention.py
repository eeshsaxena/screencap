"""Unit tests for ScreenRetentionFilter — pure logic, no mocking."""

from __future__ import annotations

import pytest

from sc_engine.retention import RetentionDecision, ScreenRetentionFilter


class TestDragLifecycle:
    """click-down → moves → key mid-drag → click-up → settle."""

    def test_full_drag_sequence(self):
        f = ScreenRetentionFilter(
            drag_interval=0.1, type_interval=1.0, settle_secs=0.4,
        )
        t = 100.0

        # Click down → SAVE
        d = f.should_save("click", {"pressed": True, "button": "left"}, t)
        assert d is RetentionDecision.SAVE

        # Move immediately (0ms later) → SKIP (within 0.1s drag interval)
        d = f.should_save("move", {}, t)
        assert d is RetentionDecision.SKIP

        # Move at +0.05s → still within interval → SKIP
        d = f.should_save("move", {}, t + 0.05)
        assert d is RetentionDecision.SKIP

        # Move at +0.11s → interval elapsed → BYPASS_DEDUP
        d = f.should_save("move", {}, t + 0.11)
        assert d is RetentionDecision.BYPASS_DEDUP

        # Move at +0.15s → within interval from last save (0.11) → SKIP
        d = f.should_save("move", {}, t + 0.15)
        assert d is RetentionDecision.SKIP

        # Move at +0.22s → interval elapsed → BYPASS_DEDUP
        d = f.should_save("move", {}, t + 0.22)
        assert d is RetentionDecision.BYPASS_DEDUP

        # Key press mid-drag → uses drag interval (0.1s), not type interval
        # Last save was at +0.22, so +0.27 is within drag interval
        d = f.should_save("press", {}, t + 0.27)
        assert d is RetentionDecision.SKIP

        # Key press at +0.33s → interval elapsed → BYPASS_DEDUP
        d = f.should_save("press", {}, t + 0.33)
        assert d is RetentionDecision.BYPASS_DEDUP

        # Click up → SAVE + settle deadline set
        d = f.should_save("click", {"pressed": False, "button": "left"}, t + 0.4)
        assert d is RetentionDecision.SAVE
        assert f.has_pending_settle()

        # Settle fires at +0.4 + 0.4 = +0.8
        assert not f.check_settle(t + 0.75)
        assert f.check_settle(t + 0.81)
        # Second check returns False (fires exactly once)
        assert not f.check_settle(t + 0.8)


class TestScrollBurstAndSettle:
    """Scroll events with cadence checks, then settle after silence."""

    def test_scroll_burst_and_settle(self):
        f = ScreenRetentionFilter(scroll_interval=0.1, settle_secs=0.4)
        t = 200.0

        # First scroll → BYPASS_DEDUP (no prior save)
        d = f.should_save("scroll", {}, t)
        assert d is RetentionDecision.BYPASS_DEDUP
        assert f.has_pending_settle()

        # Scroll at +0.05 → within interval → SKIP, but settle resets
        d = f.should_save("scroll", {}, t + 0.05)
        assert d is RetentionDecision.SKIP

        # Scroll at +0.11 → interval elapsed → BYPASS_DEDUP
        d = f.should_save("scroll", {}, t + 0.11)
        assert d is RetentionDecision.BYPASS_DEDUP

        # Scroll at +0.16 → SKIP (within interval from 0.11)
        d = f.should_save("scroll", {}, t + 0.16)
        assert d is RetentionDecision.SKIP

        # magnify event at +0.22 → also scroll group
        d = f.should_save("magnify", {}, t + 0.22)
        assert d is RetentionDecision.BYPASS_DEDUP

        # No more events — settle should fire at last scroll + 0.4 = 0.22 + 0.4 = 0.62
        assert not f.check_settle(t + 0.55)
        assert f.check_settle(t + 0.63)
        assert not f.has_pending_settle()


class TestClickWithoutDrag:
    """Click-down + click-up with no moves → no settle."""

    def test_click_no_drag(self):
        f = ScreenRetentionFilter(settle_secs=0.4)
        t = 300.0

        d = f.should_save("click", {"pressed": True, "button": "left"}, t)
        assert d is RetentionDecision.SAVE

        d = f.should_save("click", {"pressed": False, "button": "left"}, t + 0.05)
        assert d is RetentionDecision.SAVE

        # No settle because no drag was active
        assert not f.has_pending_settle()


class TestKeyAndIdleMoveIntervals:
    """press/release use type_interval; idle move uses idle_interval."""

    def test_intervals(self):
        f = ScreenRetentionFilter(
            type_interval=1.0, idle_interval=2.0,
        )
        t = 400.0

        # First key press → BYPASS_DEDUP (no prior save)
        d = f.should_save("press", {}, t)
        assert d is RetentionDecision.BYPASS_DEDUP

        # Key release at +0.5s → within 1.0s type interval → SKIP
        d = f.should_save("release", {}, t + 0.5)
        assert d is RetentionDecision.SKIP

        # Key press at +1.0s → interval elapsed → BYPASS_DEDUP
        d = f.should_save("press", {}, t + 1.0)
        assert d is RetentionDecision.BYPASS_DEDUP

        # Idle move at +1.5s → within 2.0s idle interval from last save (+1.0) → SKIP
        d = f.should_save("move", {}, t + 1.5)
        assert d is RetentionDecision.SKIP

        # Idle move at +3.0s → 2.0s elapsed from last save (+1.0) → BYPASS_DEDUP
        d = f.should_save("move", {}, t + 3.0)
        assert d is RetentionDecision.BYPASS_DEDUP


class TestDragThenScrollStateReset:
    """Complete a drag, then start a scroll burst — scroll handled correctly."""

    def test_drag_then_scroll(self):
        f = ScreenRetentionFilter(
            drag_interval=0.1, scroll_interval=0.1, settle_secs=0.4,
        )
        t = 500.0

        # Start drag
        f.should_save("click", {"pressed": True, "button": "left"}, t)
        f.should_save("move", {}, t + 0.1)

        # End drag → settle pending
        f.should_save("click", {"pressed": False, "button": "left"}, t + 0.2)
        assert f.has_pending_settle()

        # Consume the drag settle
        assert f.check_settle(t + 0.65)

        # Now start scroll burst — should work independently
        d = f.should_save("scroll", {}, t + 0.7)
        assert d is RetentionDecision.BYPASS_DEDUP
        assert f.has_pending_settle()

        # Scroll settle
        d = f.should_save("scroll", {}, t + 0.8)
        assert d is RetentionDecision.BYPASS_DEDUP

        assert not f.check_settle(t + 1.1)
        assert f.check_settle(t + 1.2)  # 0.8 + 0.4 = 1.2


class TestUnknownActionBaseline:
    """Unknown action names fall through to BASELINE."""

    def test_unknown(self):
        f = ScreenRetentionFilter()
        d = f.should_save("unknown_action", {}, 700.0)
        assert d is RetentionDecision.BASELINE


class TestCrossTypeIntervalSharing:
    """_last_save_mono is shared across action types — a save from one type
    suppresses a different type within its interval."""

    def test_typing_does_not_suppress_click(self):
        f = ScreenRetentionFilter(type_interval=1.0)
        t = 900.0

        # Key press saves
        f.should_save("press", {}, t)

        # Click 0.1s later — clicks always SAVE regardless of _last_save_mono
        d = f.should_save("click", {"pressed": True, "button": "left"}, t + 0.1)
        assert d is RetentionDecision.SAVE
