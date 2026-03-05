"""Tests for screenshot deduplication gate in process_events()."""

from __future__ import annotations

import multiprocessing
import queue
import threading
import time
from collections import namedtuple
from unittest import mock

import pytest
from PIL import Image

from sc_engine import utils
from sc_engine.config import config
from sc_engine.dedup import dhash
from sc_engine.extensions.synchronized_queue import SynchronizedQueue
from sc_engine.recorder import Event, process_events

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _img(color: tuple[int, int, int] = (100, 100, 100)) -> Image.Image:
    """Create a small solid-color test image."""
    return Image.new("RGB", (50, 50), color)


def _gradient_img() -> Image.Image:
    """Create an image with a strong gradient (visually distinct from solids)."""
    img = Image.new("L", (50, 50))
    px = img.load()
    for x in range(50):
        for y in range(50):
            px[x, y] = (x * 255) // 49
    return img.convert("RGB")


def _screen_event(ts: float, image: Image.Image) -> Event:
    return Event(timestamp=ts, type="screen", data=image)


def _window_event(ts: float, title: str = "App", window_id: int = 1) -> Event:
    return Event(timestamp=ts, type="window", data={"title": title, "window_id": window_id})


def _action_event(ts: float) -> Event:
    return Event(timestamp=ts, type="action", data={"name": "key.down", "key": "a"})


@pytest.fixture(autouse=True)
def _setup_time():
    utils.set_start_time(time.time())
    yield


@pytest.fixture(autouse=True)
def _dedup_config():
    """Enable dedup with known settings for each test, restore after."""
    orig_dedup = config.SCREENSHOT_DEDUP
    orig_interval = config.SCREENSHOT_MIN_INTERVAL
    orig_threshold = config.SCREENSHOT_HASH_THRESHOLD
    orig_video = config.RECORD_VIDEO
    orig_window = config.RECORD_WINDOW_DATA
    orig_full_video = config.RECORD_FULL_VIDEO
    orig_ax = config.RECORD_READ_ACTIVE_ELEMENT_STATE

    object.__setattr__(config, "SCREENSHOT_DEDUP", True)
    object.__setattr__(config, "SCREENSHOT_MIN_INTERVAL", 1.0)
    object.__setattr__(config, "SCREENSHOT_HASH_THRESHOLD", 8)
    object.__setattr__(config, "RECORD_VIDEO", False)
    object.__setattr__(config, "RECORD_WINDOW_DATA", True)
    object.__setattr__(config, "RECORD_FULL_VIDEO", False)
    object.__setattr__(config, "RECORD_READ_ACTIVE_ELEMENT_STATE", False)
    yield
    object.__setattr__(config, "SCREENSHOT_DEDUP", orig_dedup)
    object.__setattr__(config, "SCREENSHOT_MIN_INTERVAL", orig_interval)
    object.__setattr__(config, "SCREENSHOT_HASH_THRESHOLD", orig_threshold)
    object.__setattr__(config, "RECORD_VIDEO", orig_video)
    object.__setattr__(config, "RECORD_WINDOW_DATA", orig_window)
    object.__setattr__(config, "RECORD_FULL_VIDEO", orig_full_video)
    object.__setattr__(config, "RECORD_READ_ACTIVE_ELEMENT_STATE", orig_ax)


def _recording():
    """Create a mock recording object."""
    rec = mock.MagicMock()
    rec.timestamp = time.time()
    return rec


def _run_process_events(events: list[Event]):
    """Feed events through process_events() and return written event types + drop counts.

    Returns (screen_count, action_count, action_events_data, drop_counts_dict).
    action_events_data: list of event.data dicts for each action written.
    """
    event_q = queue.Queue()
    for ev in events:
        event_q.put(ev)

    # Mock write queues — just accept everything
    screen_wq = mock.MagicMock(spec=SynchronizedQueue)
    action_wq = mock.MagicMock(spec=SynchronizedQueue)
    window_wq = mock.MagicMock(spec=SynchronizedQueue)
    browser_wq = mock.MagicMock(spec=SynchronizedQueue)
    video_wq = mock.MagicMock(spec=SynchronizedQueue)
    perf_q = mock.MagicMock(spec=SynchronizedQueue)

    recording = _recording()
    terminate = multiprocessing.Event()
    started = threading.Event()
    num_screen = multiprocessing.Value("i", 0)
    num_action = multiprocessing.Value("i", 0)
    num_window = multiprocessing.Value("i", 0)
    num_browser = multiprocessing.Value("i", 0)
    num_video = multiprocessing.Value("i", 0)

    # Mock process_event to always succeed and capture what's written
    written_types = []
    action_data = []

    original_process_event = None

    def fake_process_event(event, wq, wfn, rec, pq, tp):
        written_types.append(event.type)
        if event.type == "action":
            action_data.append(dict(event.data))
        return True

    # Signal terminate after queue drains
    def set_terminate():
        time.sleep(0.1)
        terminate.set()

    t = threading.Thread(target=set_terminate)
    t.start()

    with mock.patch("sc_engine.recorder.process_event", side_effect=fake_process_event):
        # Capture _drops from inside process_events via _drop_counts
        from sc_engine import recorder as rec_mod
        old_drops = dict(rec_mod._drop_counts)
        rec_mod._drop_counts = {}

        process_events(
            event_q, screen_wq, action_wq, window_wq, browser_wq, video_wq,
            perf_q, recording, terminate, started,
            num_screen, num_action, num_window, num_browser, num_video,
        )
        drops = dict(rec_mod._drop_counts)
        rec_mod._drop_counts = old_drops

    t.join()

    screen_count = written_types.count("screen")
    action_count = written_types.count("action")
    return screen_count, action_count, action_data, drops


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDedupSkipsIdenticalScreenshots:
    """Dedup should skip saves when hash distance <= threshold AND within time floor."""

    def test_identical_images_within_time_floor_skipped(self):
        """Same image, < 1s apart → skip screenshot."""
        img = _img()
        t0 = 1000.0
        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _action_event(t0 + 0.01),  # first → saves screen
            _screen_event(t0 + 0.5, img),
            _action_event(t0 + 0.51),  # same image, < 1s → skip
        ]
        screen_count, action_count, _, drops = _run_process_events(events)
        assert screen_count == 1  # only first frame saved
        assert action_count == 2  # both actions still written
        assert drops.get("screen_time_floor", 0) > 0


class TestDedupAllowsOnHashChange:
    """Dedup should allow save when hash distance > threshold."""

    def test_different_image_after_time_floor_saved(self):
        """Different image, > 1s apart → save screenshot."""
        img1 = _img((100, 100, 100))
        img2 = _gradient_img()
        t0 = 1000.0
        events = [
            _screen_event(t0, img1),
            _window_event(t0 + 0.001),
            _action_event(t0 + 0.01),
            _screen_event(t0 + 2.0, img2),
            _action_event(t0 + 2.01),
        ]
        screen_count, action_count, _, drops = _run_process_events(events)
        assert screen_count == 2
        assert action_count == 2
        assert drops.get("screen_dedup", 0) == 0


class TestDedupAllowsOnTimeFloorExceeded:
    """Dedup should allow save when time floor exceeded even for similar images."""

    def test_same_image_after_time_floor_with_high_threshold(self):
        """Same image, > 1s apart, but threshold=64 (accept everything) → save."""
        object.__setattr__(config, "SCREENSHOT_HASH_THRESHOLD", 64)
        img = _img()
        t0 = 1000.0
        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _action_event(t0 + 0.01),
            _screen_event(t0 + 2.0, img),
            _action_event(t0 + 2.01),
        ]
        screen_count, action_count, _, drops = _run_process_events(events)
        # With threshold=64, identical images (dist=0) are <= threshold → dedup
        assert screen_count == 1
        assert drops.get("screen_dedup", 0) == 1


class TestWindowChangeBypassesDedup:
    """Window title change forces save regardless of hash/time."""

    def test_title_change_forces_save(self):
        img = _img()
        t0 = 1000.0
        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001, title="App A", window_id=1),
            _action_event(t0 + 0.01),
            _screen_event(t0 + 0.5, img),  # same image, < 1s
            _window_event(t0 + 0.501, title="App B", window_id=1),  # title changed
            _action_event(t0 + 0.51),
        ]
        screen_count, action_count, _, drops = _run_process_events(events)
        assert screen_count == 2  # both saved due to window change
        assert drops.get("screen_time_floor", 0) == 0
        assert drops.get("screen_dedup", 0) == 0


class TestSameTitleDifferentWindowId:
    """Same title but different window_id should force save."""

    def test_window_id_change_forces_save(self):
        img = _img()
        t0 = 1000.0
        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001, title="Untitled", window_id=100),
            _action_event(t0 + 0.01),
            _screen_event(t0 + 0.5, img),
            _window_event(t0 + 0.501, title="Untitled", window_id=200),
            _action_event(t0 + 0.51),
        ]
        screen_count, _, _, drops = _run_process_events(events)
        assert screen_count == 2
        assert drops.get("screen_time_floor", 0) == 0
        assert drops.get("screen_dedup", 0) == 0


class TestDedupDisabled:
    """`SCREENSHOT_DEDUP=False` saves every frame."""

    def test_dedup_off_saves_all(self):
        object.__setattr__(config, "SCREENSHOT_DEDUP", False)
        img = _img()
        t0 = 1000.0
        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _action_event(t0 + 0.01),
            _screen_event(t0 + 0.5, img),
            _action_event(t0 + 0.51),
        ]
        screen_count, action_count, _, drops = _run_process_events(events)
        assert screen_count == 2
        assert action_count == 2
        assert drops.get("screen_dedup", 0) == 0
        assert drops.get("screen_time_floor", 0) == 0


class TestDropCounters:
    """Drop counters increment correctly for both dedup types."""

    def test_time_floor_counter(self):
        img = _img()
        t0 = 1000.0
        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _action_event(t0 + 0.01),
            _screen_event(t0 + 0.2, img),
            _action_event(t0 + 0.21),
            _screen_event(t0 + 0.4, img),
            _action_event(t0 + 0.41),
        ]
        _, _, _, drops = _run_process_events(events)
        assert drops.get("screen_time_floor", 0) == 2

    def test_hash_dedup_counter(self):
        """Same image, time floor passed → hash dedup kicks in."""
        img = _img()
        t0 = 1000.0
        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _action_event(t0 + 0.01),
            _screen_event(t0 + 2.0, img),  # > 1s but identical image
            _action_event(t0 + 2.01),
        ]
        _, _, _, drops = _run_process_events(events)
        assert drops.get("screen_dedup", 0) == 1


class TestDedupedActionReferentialIntegrity:
    """Deduped action's screenshot_timestamp points to last saved frame."""

    def test_screenshot_timestamp_points_to_last_saved(self):
        img = _img()
        t0 = 1000.0
        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _action_event(t0 + 0.01),       # saves screen at t0
            _screen_event(t0 + 0.5, img),    # will be deduped
            _action_event(t0 + 0.51),        # should reference t0
        ]
        _, _, action_data, _ = _run_process_events(events)
        assert len(action_data) == 2
        # First action: screenshot_timestamp = t0
        assert action_data[0]["screenshot_timestamp"] == t0
        # Second action: deduped → screenshot_timestamp should still be t0
        assert action_data[1]["screenshot_timestamp"] == t0


class TestFirstFrameAlwaysSaved:
    """First screenshot of a recording is always saved (None sentinel)."""

    def test_first_frame_saved_even_with_window_data_disabled(self):
        object.__setattr__(config, "RECORD_WINDOW_DATA", False)
        img = _img()
        t0 = 1000.0
        events = [
            _screen_event(t0, img),
            _action_event(t0 + 0.01),
        ]
        screen_count, action_count, _, _ = _run_process_events(events)
        assert screen_count == 1
        assert action_count == 1
