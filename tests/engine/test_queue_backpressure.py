"""Tests for bounded queue backpressure (SynchronizedQueue maxsize + process_event)."""

import multiprocessing
import queue
import time
from collections import namedtuple
from unittest import mock

import pytest

from screencap.engine.extensions.synchronized_queue import SynchronizedQueue


def _child_check_maxsize(q, result_q):
    """Child process target: verify queue respects maxsize."""
    for i in range(5):
        q.put(i)
    try:
        q.put_nowait("overflow")
        result_q.put("FAIL: put_nowait should have raised Full")
    except queue.Full:
        result_q.put("OK")
    except Exception as e:
        result_q.put(f"FAIL: unexpected exception: {e}")


@pytest.fixture(autouse=True)
def _set_start_time():
    """Ensure get_timestamp() works in all tests."""
    from screencap.engine import utils
    utils.set_start_time(time.time())
    yield


# ── Phase 1: SynchronizedQueue maxsize ──────────────────────────────────────


class TestSynchronizedQueueMaxsize:
    """Validate SynchronizedQueue forwards maxsize."""

    def test_default_maxsize_is_unbounded(self):
        """SynchronizedQueue() with no args should accept many items (unbounded)."""
        q = SynchronizedQueue()
        for i in range(50):
            q.put(i)
        assert q.qsize() == 50
        q.close()
        q.join_thread()

    def test_maxsize_blocks_at_capacity(self):
        """SynchronizedQueue(maxsize=N) should block/raise at capacity."""
        q = SynchronizedQueue(maxsize=3)
        q.put("a")
        q.put("b")
        q.put("c")
        with pytest.raises(queue.Full):
            q.put("d", timeout=0.05)
        q.close()
        q.join_thread()

    def test_put_nowait_raises_full(self):
        """put_nowait() should raise queue.Full when queue is at capacity."""
        q = SynchronizedQueue(maxsize=1)
        q.put("a")
        with pytest.raises(queue.Full):
            q.put_nowait("b")
        q.close()
        q.join_thread()

    def test_get_unblocks_put(self):
        """After a get(), a blocked put() should succeed."""
        q = SynchronizedQueue(maxsize=1)
        q.put("a")
        result = []

        def delayed_put():
            q.put("b", timeout=2.0)
            result.append(True)

        import threading
        t = threading.Thread(target=delayed_put)
        t.start()
        time.sleep(0.05)
        q.get()
        t.join(timeout=2.0)
        assert result == [True]
        q.close()
        q.join_thread()

    def test_qsize_accurate_with_maxsize(self):
        """qsize() should work correctly with bounded queues."""
        q = SynchronizedQueue(maxsize=5)
        assert q.qsize() == 0
        q.put("a")
        assert q.qsize() == 1
        q.put("b")
        assert q.qsize() == 2
        q.get()
        assert q.qsize() == 1
        q.close()
        q.join_thread()

    def test_maxsize_survives_subprocess(self):
        """maxsize should survive being passed to a child process."""
        q = SynchronizedQueue(maxsize=5)
        result_q = SynchronizedQueue()

        p = multiprocessing.Process(target=_child_check_maxsize, args=(q, result_q))
        p.start()
        p.join(timeout=10)

        result = result_q.get(timeout=5)
        assert result == "OK", result

        q.close()
        q.join_thread()
        result_q.close()
        result_q.join_thread()


# ── Phase 3c: process_event returns success/failure ─────────────────────────

Event = namedtuple("Event", ["timestamp", "type", "data"])


class TestProcessEvent:
    """Test that process_event returns True/False on success/failure."""

    def test_returns_true_on_successful_put(self):
        from screencap.engine.recorder import process_event

        write_q = SynchronizedQueue(maxsize=10)
        perf_q = SynchronizedQueue()
        event = Event(1.0, "action", {"name": "click"})

        result = process_event(
            event, write_q, mock.MagicMock(), mock.MagicMock(), perf_q
        )
        assert result is True
        assert write_q.qsize() == 1
        write_q.close()
        write_q.join_thread()
        perf_q.close()
        perf_q.join_thread()

    def test_returns_false_when_queue_full(self):
        from screencap.engine.recorder import process_event

        write_q = SynchronizedQueue(maxsize=1)
        perf_q = SynchronizedQueue()
        write_q.put("filler")

        event = Event(2.0, "action", {"name": "click"})
        result = process_event(
            event, write_q, mock.MagicMock(), mock.MagicMock(), perf_q
        )
        assert result is False
        write_q.close()
        write_q.join_thread()
        perf_q.close()
        perf_q.join_thread()

    def test_shorter_timeout_during_shutdown(self):
        from screencap.engine.recorder import process_event

        write_q = SynchronizedQueue(maxsize=1)
        perf_q = SynchronizedQueue()
        write_q.put("filler")

        terminate = multiprocessing.Event()
        terminate.set()

        event = Event(1.0, "action", {"name": "click"})

        start = time.monotonic()
        result = process_event(
            event, write_q, mock.MagicMock(), mock.MagicMock(), perf_q,
            terminate_processing=terminate,
        )
        elapsed = time.monotonic() - start

        assert result is False
        # Shutdown timeout is 0.5s — should complete well under 1.0s
        assert elapsed < 0.8
        write_q.close()
        write_q.join_thread()
        perf_q.close()
        perf_q.join_thread()


# ── Phase 3a: trigger_action_event backpressure ─────────────────────────────


class TestTriggerActionEvent:
    """Test backpressure in trigger_action_event."""

    def test_move_uses_put_nowait(self):
        """Mouse moves should use put_nowait (silent drop on full)."""
        from screencap.engine.recorder import trigger_action_event

        event_q = queue.Queue(maxsize=1)
        event_q.put("filler")

        # Should not raise or block
        trigger_action_event(event_q, {"name": "move", "mouse_x": 0, "mouse_y": 0})
        assert event_q.qsize() == 1

    def test_click_drops_with_warning_on_full(self):
        """Clicks should drop with a warning when queue is full."""
        from screencap.engine.recorder import trigger_action_event

        event_q = queue.Queue(maxsize=1)
        event_q.put("filler")

        start = time.monotonic()
        trigger_action_event(event_q, {"name": "click", "mouse_x": 0, "mouse_y": 0})
        elapsed = time.monotonic() - start

        assert event_q.qsize() == 1
        # Should complete within the 50ms timeout + some margin
        assert elapsed < 0.2

    def test_click_succeeds_when_queue_has_room(self):
        """Clicks should be queued normally when there's room."""
        from screencap.engine.recorder import trigger_action_event

        event_q = queue.Queue(maxsize=5)

        trigger_action_event(event_q, {"name": "click", "mouse_x": 10, "mouse_y": 20})
        assert event_q.qsize() == 1

        event = event_q.get_nowait()
        assert event.type == "action"
        assert event.data["name"] == "click"


# ── Drop counting ────────────────────────────────────────────────────────────


class TestDropCounting:
    """Test that drop counters are incremented for all event types."""

    def test_action_drop_counted(self):
        """Non-move action drops should be counted in _drop_counts."""
        import screencap.engine.recorder as rec

        rec._drop_counts = {}
        event_q = queue.Queue(maxsize=1)
        event_q.put("filler")

        rec.trigger_action_event(event_q, {"name": "click", "mouse_x": 0, "mouse_y": 0})
        assert rec._drop_counts.get("action", 0) == 1

    def test_move_drop_not_counted(self):
        """Mouse move drops are silent and should not be counted."""
        import screencap.engine.recorder as rec

        rec._drop_counts = {}
        event_q = queue.Queue(maxsize=1)
        event_q.put("filler")

        rec.trigger_action_event(event_q, {"name": "move", "mouse_x": 0, "mouse_y": 0})
        assert rec._drop_counts.get("action", 0) == 0

    def test_process_event_drop_not_in_drop_counts(self):
        """process_event uses local _drops — module _drop_counts stays clean."""
        import screencap.engine.recorder as rec

        rec._drop_counts = {}
        write_q = SynchronizedQueue(maxsize=1)
        perf_q = SynchronizedQueue()
        write_q.put("filler")

        event = Event(1.0, "action", {"name": "click"})
        result = rec.process_event(
            event, write_q, mock.MagicMock(), mock.MagicMock(), perf_q
        )
        assert result is False
        # process_event itself does not touch _drop_counts
        # (process_events is responsible for counting via its local _drops)
        assert rec._drop_counts == {}
        write_q.close()
        write_q.join_thread()
        perf_q.close()
        perf_q.join_thread()
